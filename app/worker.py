"""The sequential worker.

Lifecycle: started by the FastAPI lifespan, not by a request. It sleeps on an
asyncio.Event, is woken the instant a batch is enqueued, and then drains every
ready job back to back with no polling interval between them. Closing the
browser, refreshing the dashboard or losing the SSE connection has no effect on
it, and a restart resumes from SQLite without a new browser action.

One video is analysed and submitted at a time, strictly in input order. Delivery
tracking runs in a separate bounded loop so it can never stall the video worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Any

import httpx

from .comment_finder import ScanLimits, scan_video
from .config import Settings
from .database import Database
from .like_rules import ZERO_QUANTITY_THRESHOLD, calculate_quantity
from .models import (
    CommentScope,
    DeliveryState,
    Outcome,
    OrderTarget,
    RunMode,
    ScanResult,
    ScanStatus,
    Timings,
    iso,
    utcnow,
)
from .providers import build_reader
from .smm_client import PanelError, ServiceIncompatible, SmmClient
from .url_resolver import UrlError, normalize_lock_key, resolve_video

log = logging.getLogger("app.worker")


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        http_client: httpx.AsyncClient,
        smm: SmmClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self.http = http_client
        self.smm = smm
        self.reader = build_reader(settings, http_client=None)
        self.wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.current_item_id: str | None = None
        self.last_error: str | None = None

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        recovery = await self.db.recover_claimed_jobs()
        if any(recovery.values()):
            log.warning("startup recovery: %s", recovery)
            await self.db.add_event("recovery", recovery)
        self._task = asyncio.create_task(self._run_loop(), name="video-worker")
        self._status_task = asyncio.create_task(self._status_loop(), name="delivery-status")
        # Resume anything already persisted, without waiting for a browser.
        self.wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self.wake.set()
        for task in (self._task, self._status_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        await self.reader.aclose()

    def notify(self) -> None:
        """Wake the worker immediately. No fixed polling interval anywhere."""
        self.wake.set()

    # --------------------------------------------------------------- loops
    async def _run_loop(self) -> None:
        while not self._stopping:
            try:
                await self.wake.wait()
                self.wake.clear()
                while not self._stopping:
                    job = await self.db.claim_next_job()
                    if job is None:
                        break
                    await self._process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad item must not kill the loop
                self.last_error = repr(exc)
                log.exception("worker loop error")
                await asyncio.sleep(1.0)

    async def _status_loop(self) -> None:
        """Bounded delivery-status polling, fully separate from video work."""
        while not self._stopping:
            try:
                await asyncio.sleep(self.settings.STATUS_POLL_INTERVAL_SECONDS)
                if not self.settings.panel_configured:
                    continue
                attempts = await self.db.attempts_needing_status(
                    self.settings.STATUS_POLL_BATCH_SIZE
                )
                for attempt in attempts:
                    state, raw, charge, error = await self.smm.get_order_status(
                        attempt["order_id"]
                    )
                    if error:
                        log.info("status read failed for order %s: %s", attempt["order_id"], error)
                    await self.db.record_delivery_status(
                        attempt_id=attempt["id"],
                        delivery_state=state,
                        raw=raw,
                        charge=charge,
                    )
                    await self.db.add_event(
                        "delivery",
                        {"order_id": str(attempt["order_id"]), "delivery_state": state.value},
                        item_id=attempt["item_id"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("delivery status loop error")

    # ------------------------------------------------------------ one item
    async def _process_job(self, job: dict[str, Any]) -> None:
        item_id = job["item_id"]
        batch_id = job["batch_id"]
        mode = RunMode(job["mode"])
        frozen = json.loads(job["frozen_config"])
        scope = CommentScope(job["comment_scope"])
        keyword = job["keyword"]
        self.current_item_id = item_id
        total_start = time.monotonic()

        if await self.db.stop_requested(batch_id):
            await self._finish(job, Outcome.CANCELLED, error="Batch stopped before processing.")
            return

        timings: dict[str, int] = {}
        identity = None
        try:
            tick = time.monotonic()
            identity = await resolve_video(self.http, job["input_url"])
            timings["url_resolve_ms"] = int((time.monotonic() - tick) * 1000)
        except UrlError as exc:
            await self._finish(job, Outcome.URL_INVALID, error=str(exc), timings=timings)
            return
        except httpx.HTTPError as exc:
            await self._finish(
                job,
                Outcome.PROVIDER_ERROR,
                error=f"could not resolve the link: {exc}",
                timings=timings,
            )
            return

        await self.db.update_item(
            item_id,
            video_id=identity.video_id,
            canonical_url=identity.canonical_url,
            video_verified=1 if identity.verified else 0,
        )
        await self._emit(batch_id, item_id, "resolved", {"video_id": identity.video_id})

        if not self.reader.configured and (
            mode is RunMode.LIVE or not self.reader.usable_for_dry_run
        ):
            await self._finish(
                job,
                Outcome.READER_UNCONFIGURED,
                error=self.reader.blocker or "comment reader is not configured",
                timings=timings,
            )
            return

        # --- one scan, one pass ------------------------------------------
        tick = time.monotonic()
        try:
            scan = await scan_video(
                self.reader,
                video_id=identity.video_id,
                video_url=identity.canonical_url,
                keyword=keyword,
                requested_scope=scope,
                limits=ScanLimits(
                    deadline_seconds=float(frozen.get("scan_deadline_seconds", 45.0)),
                    max_pages=int(frozen.get("scan_max_pages", 40)),
                    max_requests=int(frozen.get("scan_max_requests", 60)),
                    max_comments=int(frozen.get("scan_max_comments", 5000)),
                    max_threads_expanded=int(frozen.get("scan_max_threads", 200)),
                    trust_provider_reply_end=bool(
                        frozen.get("trust_provider_reply_end", False)
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            timings["comment_read_ms"] = int((time.monotonic() - tick) * 1000)
            await self._finish(
                job, Outcome.PROVIDER_ERROR, error=f"comment read failed: {exc}", timings=timings
            )
            return
        timings["comment_read_ms"] = int((time.monotonic() - tick) * 1000)

        await self.db.update_item(
            item_id,
            top_likes=scan.observed_top_likes,
            top_comment_id=scan.observed_top_comment_id,
            scan_status=scan.status.value,
            scan_complete=1 if scan.complete else 0,
            pages_read=scan.stats.pages_read,
            comments_read=scan.stats.unique_comments,
            scan_json=json.dumps(scan.as_json(), sort_keys=True),
            target_comment_id=scan.target.comment_id if scan.target else None,
            target_text=scan.target.text if scan.target else None,
            target_likes=scan.target.like_count if scan.target else None,
            owner_username=scan.target.comment_owner_username if scan.target else None,
            owner_user_id=scan.target.comment_owner_user_id if scan.target else None,
        )

        outcome, error, quantity = await self._decide(
            scan=scan, identity=identity, mode=mode, timings=timings
        )

        if outcome is not None:
            await self._finish(job, outcome, error=error, timings=timings, quantity=quantity)
            return

        assert scan.target is not None and quantity is not None
        target = OrderTarget(
            video_id=identity.video_id,
            video_url=identity.canonical_url,
            comment_owner_username=scan.target.comment_owner_username or "",
            comment_id=scan.target.comment_id,
            comment_owner_user_id=scan.target.comment_owner_user_id,
            raw_owner_username=scan.target.raw_owner_username,
        )
        await self._submit(job, target, quantity, timings, total_start)

    async def _decide(
        self,
        *,
        scan: ScanResult,
        identity,
        mode: RunMode,
        timings: dict[str, int],
    ) -> tuple[Outcome | None, str | None, int | None]:
        """Gate ordering. Returns (terminal_outcome, error, quantity)."""
        if scan.status is ScanStatus.EARLY_EXIT_THRESHOLD:
            return (
                Outcome.SKIPPED_THRESHOLD,
                (
                    f"An observed comment already has at least {ZERO_QUANTITY_THRESHOLD:,} "
                    "likes, so the formula yields 0. Scan stopped early and did not "
                    "complete."
                ),
                0,
            )

        if not scan.complete:
            return (
                Outcome.SCAN_INCOMPLETE,
                "Scan did not complete ("
                + ", ".join(scan.incomplete_reasons)
                + "). Observed maximum is "
                + (str(scan.observed_top_likes) if scan.observed_top_likes is not None else "n/a")
                + " likes, which is the maximum SEEN, not the maximum in the section. "
                "No order placed.",
                None,
            )

        if scan.target is None:
            return (
                Outcome.KEYWORD_NOT_FOUND,
                f"Completed the {scan.actual_scope.value} scope and found no comment "
                "containing the keyword.",
                None,
            )

        if scan.observed_top_likes is None:
            return (
                Outcome.SCAN_INCOMPLETE,
                "No trustworthy like count was observed, so the maximum cannot be "
                "established. Missing data is not zero.",
                None,
            )

        tick = time.monotonic()
        quantity = calculate_quantity(scan.observed_top_likes)
        timings["calculation_ms"] = int((time.monotonic() - tick) * 1000)

        if quantity <= 0:
            return (
                Outcome.SKIPPED_THRESHOLD,
                f"Top comment has {scan.observed_top_likes:,} likes; the formula yields 0.",
                0,
            )

        if scan.target_ambiguous:
            return (
                Outcome.TARGET_AMBIGUOUS,
                (
                    "@"
                    + (scan.target.comment_owner_username or "?")
                    + " wrote "
                    + str(len(scan.owner_duplicate_comment_ids))
                    + " matching comments under this video. The panel payload carries only "
                    "video + username, so it cannot express which comment should receive "
                    "the likes. Skipped automatic ordering."
                ),
                quantity,
            )

        if mode is RunMode.DRY_RUN:
            return (
                Outcome.DRY_RUN_COMPLETE,
                None,
                quantity,
            )

        if not scan.target.comment_owner_username:
            return (
                Outcome.TARGET_UNVERIFIED,
                "The provider did not supply the comment owner's real @handle, and no "
                "authoritative lookup was available. A display name cannot be used. "
                "Live ordering blocked.",
                quantity,
            )
        if not identity.verified:
            return (
                Outcome.TARGET_UNVERIFIED,
                f"Canonical video URL could not be verified ({identity.note}). "
                "Live ordering blocked.",
                quantity,
            )

        # --- service metadata (cached for a short TTL) --------------------
        try:
            metadata = await self.smm.get_service_metadata()
        except ServiceIncompatible as exc:
            return Outcome.SERVICE_CONFIGURATION_REQUIRED, str(exc), quantity
        except (PanelError, httpx.HTTPError) as exc:
            return (
                Outcome.SERVICE_CONFIGURATION_REQUIRED,
                f"could not read live service metadata: {exc}",
                quantity,
            )

        if not self.smm.service_supports_comment_likes(metadata):
            return (
                Outcome.SERVICE_CONFIGURATION_REQUIRED,
                f"Service {metadata.service_id} reports API type {metadata.service_type!r}, "
                "which does not accept the username field this order needs.",
                quantity,
            )

        if quantity < metadata.min_quantity or quantity > metadata.max_quantity:
            return (
                Outcome.QUANTITY_OUT_OF_RANGE,
                f"Calculated quantity {quantity} is outside the live service limits "
                f"{metadata.min_quantity}-{metadata.max_quantity}. Not clamped, not split, "
                "not rounded.",
                quantity,
            )

        # --- duplicate spending protection -------------------------------
        prior = await self.db.existing_target_state(
            panel_url=self.settings.PANEL_URL,
            service_id=self.settings.SERVICE_ID,
            video_id=identity.video_id,
        )
        if prior:
            state = prior["state"]
            delivery = DeliveryState(prior["delivery_state"])
            if state in {"submitting", "unknown"}:
                return (
                    Outcome.ACTIVE_ORDER_EXISTS,
                    f"A previous attempt for this video has an unresolved outcome "
                    f"(order id {prior['order_id'] or 'unknown'}). Resolve it in the panel "
                    "before ordering again.",
                    quantity,
                )
            if state == "accepted" and delivery is DeliveryState.COMPLETED:
                return (
                    Outcome.ALREADY_ORDERED,
                    f"Order {prior['order_id']} for this video already completed. "
                    "No automatic top-up or reorder.",
                    quantity,
                )
            if state == "accepted":
                return (
                    Outcome.ACTIVE_ORDER_EXISTS,
                    f"Order {prior['order_id']} for this video is still "
                    f"{delivery.value}. The service forbids a second order on the same "
                    "link until the previous one completes.",
                    quantity,
                )

        # --- optional pre-order identity recheck --------------------------
        recheck = await self.reader.recheck_comment(identity.video_id, scan.target.comment_id)
        if recheck is False:
            return (
                Outcome.TARGET_UNVERIFIED,
                "The selected comment could no longer be found immediately before "
                "ordering. Nothing was submitted.",
                quantity,
            )

        return None, None, quantity

    # ---------------------------------------------------------- submission
    async def _submit(
        self,
        job: dict[str, Any],
        target: OrderTarget,
        quantity: int,
        timings: dict[str, int],
        total_start: float,
    ) -> None:
        item_id = job["item_id"]
        batch_id = job["batch_id"]
        lock_key = normalize_lock_key(self.settings.PANEL_URL, target.video_id)

        attempt_id, reason = await self.db.begin_order_intent(
            item_id=item_id,
            batch_id=batch_id,
            panel_url=self.settings.PANEL_URL,
            service_id=self.settings.SERVICE_ID,
            video_id=target.video_id,
            submitted_link=target.video_url,
            owner_username=target.comment_owner_username,
            comment_id=target.comment_id,
            quantity=quantity,
            lock_key=lock_key,
        )
        if attempt_id is None:
            outcome = Outcome.ACTIVE_ORDER_EXISTS
            message = (
                "Another order for this video link is in flight "
                f"({reason}). Changing the username or using a different short link "
                "does not release that lock."
            )
            if reason == "prior_attempt:accepted":
                outcome = Outcome.ALREADY_ORDERED
                message = (
                    "This video and comment owner already have an accepted order. "
                    "Resolve its delivery before ordering again; nothing was sent."
                )
            elif reason == "duplicate_attempt_key":
                message = (
                    "The order journal refused a duplicate attempt key. Nothing was "
                    "sent to the panel. Check the existing attempts for this video."
                )
            await self._finish(
                job,
                outcome,
                error=message,
                timings=timings,
                quantity=quantity,
            )
            return

        metadata = self.smm.cached_service()
        estimated = None
        if metadata and metadata.rate is not None:
            estimated = (metadata.rate * Decimal(quantity) / Decimal(1000)).quantize(
                Decimal("0.0001")
            )

        tick = time.monotonic()
        result = await self.smm.submit_order(target, quantity)
        timings["submission_ms"] = int((time.monotonic() - tick) * 1000)

        if result.accepted and result.order_id:
            await self.db.settle_order_intent(
                attempt_id=attempt_id,
                state="accepted",
                order_id=result.order_id,
                delivery_state=DeliveryState.UNKNOWN,
                response=result.raw_response,
                lock_state="accepted",
            )
            await self.db.update_item(
                item_id,
                order_id=result.order_id,
                submitted_link=target.video_url,
                quantity=quantity,
                estimated_cost=None if estimated is None else str(estimated),
                delivery_state=DeliveryState.UNKNOWN.value,
            )
            await self._finish(
                job,
                Outcome.SUBMITTED,
                error=None,
                timings=timings,
                quantity=quantity,
                total_start=total_start,
            )
            return

        if result.unknown:
            await self.db.settle_order_intent(
                attempt_id=attempt_id,
                state="unknown",
                order_id=None,
                delivery_state=DeliveryState.UNKNOWN,
                response=result.raw_response,
                lock_state="unknown",
            )
            await self.db.update_item(
                item_id,
                submitted_link=target.video_url,
                quantity=quantity,
                delivery_state=DeliveryState.UNKNOWN.value,
            )
            await self._finish(
                job,
                Outcome.SUBMISSION_UNKNOWN,
                error=(
                    f"{result.error} No automatic retry was made. Check the panel's order "
                    "list before doing anything else with this link; it stays locked."
                ),
                timings=timings,
                quantity=quantity,
                total_start=total_start,
            )
            return

        # Clean application-level rejection: nothing was created, release lock.
        await self.db.settle_order_intent(
            attempt_id=attempt_id,
            state="failed",
            order_id=None,
            delivery_state=DeliveryState.NOT_APPLICABLE,
            response=result.raw_response,
            lock_state="failed",
            release_lock=True,
        )
        await self.db.update_item(item_id, submitted_link=target.video_url, quantity=quantity)
        await self._finish(
            job,
            Outcome.FAILED,
            error=f"Panel rejected the order: {result.error}",
            timings=timings,
            quantity=quantity,
            total_start=total_start,
        )

    # -------------------------------------------------------------- helpers
    async def _finish(
        self,
        job: dict[str, Any],
        outcome: Outcome,
        *,
        error: str | None = None,
        timings: dict[str, int] | None = None,
        quantity: int | None = None,
        total_start: float | None = None,
    ) -> None:
        item_id = job["item_id"]
        batch_id = job["batch_id"]
        timings = dict(timings or {})
        if total_start is not None:
            timings["total_ms"] = int((time.monotonic() - total_start) * 1000)
        else:
            timings.setdefault(
                "total_ms",
                sum(
                    timings.get(key, 0)
                    for key in ("url_resolve_ms", "comment_read_ms", "submission_ms")
                ),
            )
        fields: dict[str, Any] = {
            "outcome": outcome.value,
            "error": error,
            "finished_at": iso(utcnow()),
            "timings_json": json.dumps(Timings(**{**Timings().as_json(), **timings}).as_json()),
        }
        if quantity is not None:
            fields["quantity"] = quantity
        await self.db.update_item(item_id, **fields)
        await self.db.finish_job(job["id"])
        await self.db.finish_batch_if_done(batch_id)
        self.current_item_id = None
        item = await self.db.get_item(item_id)
        await self._emit(batch_id, item_id, "item", {"item": item, "outcome": outcome.value})

    async def _emit(self, batch_id: str, item_id: str | None, kind: str, payload: dict) -> None:
        await self.db.add_event(kind, payload, batch_id=batch_id, item_id=item_id)
