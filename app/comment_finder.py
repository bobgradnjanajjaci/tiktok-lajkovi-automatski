"""Keyword matching and the single-pass comment scan.

Two responsibilities:

1. ``matches_keyword`` - deterministic, normalized, word-boundary matching of the
   configured two-word author name. No fuzzy matching, no transliteration, no
   extra variants, no LLM.
2. ``scan_video`` - one traversal of the selected comment scope that
   simultaneously keeps the FIRST keyword match in source order and the RUNNING
   MAXIMUM like count over every comment in scope (matching or not). The comment
   set is never sorted and never fetched twice.

Completeness is treated as a first-class fact. If the traversal stops for any
reason other than the provider reporting the end of every requested stream, the
result is ``ScanStatus.INCOMPLETE`` and the worker will not order.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from .like_rules import ZERO_QUANTITY_THRESHOLD
from .models import (
    CommentPage,
    CommentScope,
    NormalizedComment,
    ProviderError,
    ScanResult,
    ScanStats,
    ScanStatus,
    utcnow,
)

_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """NFKC-normalize, casefold and collapse whitespace for comparison only.

    The original comment text is never mutated; callers keep it verbatim.
    """
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.casefold()
    normalized = unicodedata.normalize("NFKC", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


def build_keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Compile a whole-name, word-boundary pattern for the configured keyword.

    ``Mael Vorran`` matches ``mael vorran``, ``MAEL   VORRAN``, ``(Mael Vorran!)``
    and ``@Mael Vorran`` but not ``maelvorran``, ``Mael Vorrano`` or ``Mael``
    alone. Whitespace between the words may be any amount or kind.
    """
    tokens = [token for token in normalize_text(keyword).split(" ") if token]
    if not tokens:
        raise ValueError("KEYWORD must contain at least one word")
    body = r"\s+".join(re.escape(token) for token in tokens)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.UNICODE)


def matches_keyword(text: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(normalize_text(text)))


@dataclass(slots=True)
class ScanLimits:
    """Hard budgets. Hitting any of them means incomplete, not "good enough"."""

    deadline_seconds: float = 90.0
    max_pages: int = 60
    max_requests: int = 120
    max_comments: int = 5000
    max_threads_expanded: int = 200
    #: Default False (strict): if fewer distinct replies arrive than the parent
    #: advertises, the scan is incomplete, even when the replies endpoint says
    #: it has ended. True is an explicit opt-in that reinterprets completeness
    #: as "everything the provider made visible during this scan"; it can never
    #: override has_more still being set, an unknown completion marker, invalid
    #: records or an exceeded budget.
    trust_provider_reply_end: bool = False


class BudgetExhausted(Exception):
    """Raised when an outbound attempt would exceed the per-scan budget."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class ScanDeadlineExceeded(Exception):
    """Raised when the overall scan deadline expires, including while waiting."""


class RequestBudget:
    """One shared counter for every outbound attempt made during one scan.

    Adapters call :meth:`charge` BEFORE each attempt - first tries, retries and
    owner lookups alike - so a successful side request can never be added after
    the limit check has already passed. Counting only pages previously let a
    ``max_requests=2`` scan make four calls and still report itself complete.

    It is per scan, deliberately separate from an adapter's process-wide
    ``call_stats``.
    """

    __slots__ = ("limit", "used", "deadline_at", "clock")

    def __init__(self, limit: int, *, deadline_at: float | None = None, clock=time.monotonic):
        self.limit = max(0, int(limit))
        self.used = 0
        self.deadline_at = deadline_at
        self.clock = clock

    def remaining_seconds(self) -> float:
        if self.deadline_at is None:
            return float("inf")
        return self.deadline_at - self.clock()

    def charge(self, count: int = 1) -> None:
        if self.remaining_seconds() <= 0:
            raise ScanDeadlineExceeded("scan deadline reached before the request")
        if self.used + count > self.limit:
            raise BudgetExhausted("max_requests_reached")
        self.used += count


class CommentReader:
    """Structural interface expected of every adapter in ``app/providers``.

    Adapters raise ``ReaderUnconfigured`` when no verified provider contract is
    configured, and ``ProviderError`` for transport/schema failures.
    """

    name: str = "abstract"
    supports_replies: bool = False
    supports_owner_username: bool = False
    supports_owner_lookup: bool = False
    provider_limits: dict[str, object] = {}

    async def iter_top_level_pages(self, video_id: str, video_url: str):
        raise NotImplementedError

    async def iter_reply_pages(self, video_id: str, video_url: str, parent_comment_id: str):
        raise NotImplementedError

    async def resolve_owner_username(self, user_id: str) -> str | None:
        raise NotImplementedError


def _handle_key(comment: NormalizedComment) -> str | None:
    if comment.comment_owner_username:
        return comment.comment_owner_username.casefold()
    return None


class _Accumulator:
    """Holds the state the single pass is responsible for.

    Several things at once, from one traversal:

    * the FIRST keyword match in source order;
    * the RUNNING MAXIMUM like count over every comment in scope;
    * an identity record for EVERY comment - matching or not, before or after
      the target. The panel payload is video + username, so a second comment by
      the same account makes the target ambiguous whatever its text says.
    """

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self.pattern = pattern
        self.seen_ids: set[str] = set()
        self.stats = ScanStats()
        self.target: NormalizedComment | None = None
        self.top_likes: int | None = None
        self.top_comment_id: str | None = None
        self.threshold_comment_id: str | None = None
        self.position = 0
        #: (comment_id, owner_user_id, casefolded_handle) for every unique comment.
        self.records: list[tuple[str, str | None, str | None]] = []
        #: comments whose owner could not be identified at all.
        self.unidentified_owner_ids: list[str] = []
        #: comments whose text could not be read.
        self.unreadable_text_ids: list[str] = []

    def add(self, comment: NormalizedComment) -> bool:
        """Ingest one comment. Returns True when the 10k early exit applies."""
        self.stats.comments_seen += 1
        if comment.comment_id in self.seen_ids:
            # The same comment id returned twice is not a new comment, and must
            # never be counted as a second comment by the same owner.
            self.stats.duplicates_dropped += 1
            return False
        self.seen_ids.add(comment.comment_id)
        self.stats.unique_comments += 1

        self.position += 1
        comment.source_order = self.position

        if comment.like_count_trusted:
            if self.top_likes is None or comment.like_count > self.top_likes:
                self.top_likes = comment.like_count
                self.top_comment_id = comment.comment_id
        else:
            self.stats.invalid_like_counts += 1

        uid = comment.comment_owner_user_id or None
        handle = _handle_key(comment)
        if uid is None and handle is None:
            self.unidentified_owner_ids.append(comment.comment_id)
        self.records.append((comment.comment_id, uid, handle))

        if not comment.text_trusted:
            # An unreadable comment could itself have been the first match, so
            # "first match" cannot be proven while one exists.
            self.stats.unreadable_texts += 1
            self.unreadable_text_ids.append(comment.comment_id)
        elif self.target is None and matches_keyword(comment.text, self.pattern):
            self.target = comment

        if comment.like_count_trusted and comment.like_count >= ZERO_QUANTITY_THRESHOLD:
            self.threshold_comment_id = comment.comment_id
            return True
        return False

    def target_identity(self) -> tuple[list[str], list[str], list[str]]:
        """Resolve which comments belong to the target's account.

        Returns ``(comment_ids, conflicts, notes)``.

        Providers do not always fill both identifiers on every record: one
        comment may carry ``user.uid`` and another only ``user.unique_id``.
        Grouping on whichever field happens to be present made two comments by
        one account look like two accounts, which defeated the ambiguity check.
        So a record joins the target's group when EITHER identifier matches, and
        a partial identity is never treated as evidence of a different account.

        A handle shared by two different owner ids is a conflict: the identity
        cannot be settled, so it is reported instead of guessed.
        """
        if self.target is None:
            return [], [], []

        target_uid = self.target.comment_owner_user_id or None
        target_handle = _handle_key(self.target)

        group: list[str] = []
        conflicts: list[str] = []
        notes: list[str] = []
        renamed: set[str] = set()

        for comment_id, uid, handle in self.records:
            uid_matches = bool(target_uid) and uid == target_uid
            handle_matches = bool(target_handle) and handle == target_handle

            if handle_matches and uid and target_uid and uid != target_uid:
                # Same submitted username, demonstrably different accounts.
                conflicts.append(comment_id)
                continue
            if uid_matches and handle and target_handle and handle != target_handle:
                renamed.add(handle)
            if uid_matches or handle_matches:
                group.append(comment_id)

        if conflicts:
            notes.append(
                f"handle {self.target.comment_owner_username!r} is used by more than "
                "one owner id in this scope, so the account identity is unsettled"
            )
        if renamed:
            notes.append(
                "the target owner id appears under more than one handle in this "
                f"scope ({', '.join(sorted(renamed))})"
            )
        if not group:
            group = [self.target.comment_id]
        return group, conflicts, notes


async def scan_video(
    reader: CommentReader,
    *,
    video_id: str,
    video_url: str,
    keyword: str,
    requested_scope: CommentScope,
    limits: ScanLimits | None = None,
    clock=time.monotonic,
) -> ScanResult:
    """Traverse the selected scope once and return a fully described result."""
    limits = limits or ScanLimits()
    pattern = build_keyword_pattern(keyword)
    acc = _Accumulator(pattern)
    started_at = utcnow()
    start_tick = clock()

    incomplete: list[str] = []
    notes: list[str] = []
    scope_limitations: list[str] = []
    provider_limits: dict[str, object] = dict(reader.provider_limits or {})
    status = ScanStatus.COMPLETE
    threshold_hit = False

    # One shared budget for the whole scan. The adapter charges it before every
    # outbound attempt, so retries and owner lookups are inside the limit too.
    budget = RequestBudget(
        limits.max_requests,
        deadline_at=start_tick + limits.deadline_seconds,
        clock=clock,
    )
    previous_budget = getattr(reader, "request_budget", None)
    try:
        reader.request_budget = budget
    except AttributeError:  # pragma: no cover - exotic reader objects
        pass

    def remaining_seconds() -> float:
        """Wall-clock budget left, used to bound every await."""
        left = (start_tick + limits.deadline_seconds) - clock()
        real_left = (
            (start_tick + limits.deadline_seconds) - time.monotonic()
            if clock is time.monotonic
            else left
        )
        return min(left, real_left)

    actual_scope = requested_scope
    if requested_scope is CommentScope.ALL and not reader.supports_replies:
        actual_scope = CommentScope.TOP_LEVEL
        incomplete.append("provider_cannot_read_replies")
        notes.append(
            f"Reader {reader.name} cannot read replies; scope all was downgraded "
            "to top_level and the result is therefore incomplete."
        )

    traversal = (
        "top_level_source_order"
        if actual_scope is CommentScope.TOP_LEVEL
        else "parent_source_order_then_its_replies_source_order"
    )

    async def bounded(stream):
        """Iterate a provider stream, bounding each await by the time left.

        The deadline used to be checked only after a page had already arrived,
        so a single slow response could overshoot the whole budget. Each
        ``__anext__`` is now wrapped in ``asyncio.wait_for``; on expiry the
        generator is closed so no request task is left running.
        """
        iterator = stream.__aiter__()
        while True:
            left = remaining_seconds()
            if left <= 0:
                await _aclose(iterator)
                raise ScanDeadlineExceeded()
            try:
                page = await asyncio.wait_for(iterator.__anext__(), timeout=left)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                await _aclose(iterator)
                raise ScanDeadlineExceeded() from None
            except BaseException:
                await _aclose(iterator)
                raise
            yield page

    def budget_exceeded() -> str | None:
        if remaining_seconds() <= 0:
            return "scan_deadline_reached"
        if acc.stats.pages_read >= limits.max_pages:
            return "max_pages_reached"
        if budget.used >= limits.max_requests:
            return "max_requests_reached"
        if acc.stats.unique_comments >= limits.max_comments:
            return "max_comments_reached"
        return None

    def absorb(page: CommentPage, label: str) -> None:
        acc.stats.pages_read += 1
        if page.provider_limits:
            provider_limits.update(page.provider_limits)
        if page.invalid_records:
            acc.stats.invalid_like_counts += page.invalid_records
        for note in page.notes:
            notes.append(f"{label}: {note}")

    async def drain(
        stream, *, label: str, parent_id: str | None
    ) -> tuple[bool, str | None, bool]:
        """Consume one paginated stream.

        Returns ``(threshold_hit, stop_reason, declared_end)``. ``declared_end``
        is True only when the provider itself reported that the stream is
        exhausted with a marker this code understands.
        """
        seen_cursors: set[str] = set()
        last_page: CommentPage | None = None
        async for page in bounded(stream):
            absorb(page, label)
            last_page = page

            for comment in page.comments:
                if acc.add(comment):
                    return True, None, False

            if page.completion_unknown:
                # An unrecognised or absent has-more marker proves nothing.
                return False, f"unknown_completion_marker:{label}", False

            if page.cursor_out is not None:
                if page.cursor_out in seen_cursors:
                    return False, f"repeated_cursor:{label}", False
                seen_cursors.add(page.cursor_out)
            elif page.has_more:
                return False, f"missing_cursor_with_has_more:{label}", False

            reason = budget_exceeded()
            if reason:
                return False, f"{reason}:{label}", False

        if last_page is None:
            # An iterator that yields nothing is not proof of an empty section:
            # it is an adapter or provider failure we cannot interpret.
            return False, f"no_pages_returned:{label}", False
        if last_page.has_more:
            # The adapter stopped yielding while the provider still reports more
            # data. A short or empty page is not proof of the end of a stream.
            return False, f"stream_ended_with_has_more:{label}", False
        return False, None, True

    stop_reason: str | None = None
    try:
        top_level = reader.iter_top_level_pages(video_id, video_url)
        if actual_scope is CommentScope.TOP_LEVEL:
            threshold_hit, stop_reason, _ = await drain(
                top_level, label="top_level", parent_id=None
            )
        else:
            # Deterministic traversal: parent in source order, then that
            # parent's replies in source order, then the next parent.
            seen_cursors: set[str] = set()
            last_page: CommentPage | None = None
            async for page in bounded(top_level):
                absorb(page, "top_level")
                last_page = page

                for parent in page.comments:
                    if acc.add(parent):
                        threshold_hit = True
                        break
                    expected_replies = parent.reply_count
                    if expected_replies is None:
                        # Unknown reply count: we must still try, otherwise a
                        # higher-liked reply could be missed silently.
                        notes.append(f"reply_count missing for comment {parent.comment_id}")
                    elif expected_replies <= 0:
                        continue
                    if acc.stats.threads_expanded >= limits.max_threads_expanded:
                        stop_reason = "max_threads_expanded_reached"
                        break
                    acc.stats.threads_expanded += 1
                    before = acc.stats.unique_comments
                    threshold_hit, reply_stop, declared_end = await drain(
                        reader.iter_reply_pages(video_id, video_url, parent.comment_id),
                        label=f"replies:{parent.comment_id}",
                        parent_id=parent.comment_id,
                    )
                    gathered = acc.stats.unique_comments - before
                    if (
                        expected_replies is not None
                        and reply_stop is None
                        and gathered < expected_replies
                    ):
                        detail = (
                            f"comment {parent.comment_id}: {gathered} of "
                            f"{expected_replies} advertised replies were returned"
                        )
                        if limits.trust_provider_reply_end and declared_end:
                            # EXPLICIT OPT-IN ONLY. The provider declared the
                            # thread finished. The shortfall may be hidden,
                            # removed or nested replies, a visibility
                            # difference, or a counter that moved during the
                            # scan. It proves neither that more replies are
                            # retrievable nor that the maximum over the scope
                            # originally requested is known, so the result is
                            # labelled as covering provider-visible data.
                            scope_limitations.append(f"reply_count_mismatch: {detail}")
                            notes.append(
                                "SCAN_TRUST_PROVIDER_REPLY_END=true accepted a reply "
                                f"shortfall ({detail}); completeness here means "
                                "provider-visible data, not the whole comment section"
                            )
                        else:
                            stop_reason = f"truncated_replies:{parent.comment_id}"
                            break
                    if threshold_hit or reply_stop:
                        stop_reason = reply_stop
                        break
                if threshold_hit or stop_reason:
                    break

                if page.completion_unknown:
                    stop_reason = "unknown_completion_marker:top_level"
                    break

                if page.cursor_out is not None:
                    if page.cursor_out in seen_cursors:
                        stop_reason = "repeated_cursor:top_level"
                        break
                    seen_cursors.add(page.cursor_out)
                elif page.has_more:
                    stop_reason = "missing_cursor_with_has_more:top_level"
                    break

                reason = budget_exceeded()
                if reason:
                    stop_reason = f"{reason}:top_level"
                    break

            if (
                not threshold_hit
                and stop_reason is None
                and last_page is not None
                and last_page.has_more
            ):
                stop_reason = "stream_ended_with_has_more:top_level"
            if last_page is None and stop_reason is None and not threshold_hit:
                # Zero pages is not an empty comment section.
                stop_reason = "no_pages_returned:top_level"
    except ScanDeadlineExceeded:
        stop_reason = "scan_deadline_reached:awaiting_provider"
        notes.append(
            "The overall scan deadline expired while waiting for the provider. "
            "Partial findings are kept; the scan is incomplete and nothing was ordered."
        )
    except BudgetExhausted as exc:
        stop_reason = f"{exc.kind}:outbound_attempt"
    except ProviderError as exc:
        stop_reason = f"provider_error:{exc}"
    finally:
        try:
            reader.request_budget = previous_budget
        except AttributeError:  # pragma: no cover
            pass

    if threshold_hit:
        status = ScanStatus.EARLY_EXIT_THRESHOLD
        notes.append(
            "Stopped early: an observed comment already has at least 10,000 likes, "
            "so the quantity formula necessarily yields 0. The scan did NOT complete."
        )
    elif stop_reason:
        status = ScanStatus.INCOMPLETE
        incomplete.append(stop_reason)
    elif incomplete:
        status = ScanStatus.INCOMPLETE

    # Every known invalidity is recorded, not just the first one that happened
    # to make the scan incomplete. Hiding later reasons behind an earlier stop
    # made diagnosis harder and produced confusing assertions.
    def mark_incomplete(reason: str) -> None:
        nonlocal status
        if status is ScanStatus.COMPLETE:
            status = ScanStatus.INCOMPLETE
        incomplete.append(reason)

    if acc.stats.invalid_like_counts:
        mark_incomplete("invalid_like_counts_present")

    if acc.stats.unreadable_texts:
        mark_incomplete("unreadable_comment_text")
        notes.append(
            f"{acc.stats.unreadable_texts} comment(s) had missing, null or "
            "non-string text ("
            + ", ".join(acc.unreadable_text_ids[:5])
            + "). The first keyword match cannot be proven while a comment's text "
            "is unknown, so no order is placed."
        )

    owner_comment_ids, owner_conflicts, owner_notes = acc.target_identity()
    notes.extend(owner_notes)
    if owner_conflicts:
        mark_incomplete("owner_identity_conflict")
        # Conflicting accounts share the submitted username, so the target is
        # also reported as ambiguous rather than guessed.
        owner_comment_ids = _dedupe(owner_comment_ids + owner_conflicts)
    if acc.target is not None and acc.unidentified_owner_ids:
        # Uniqueness of the target's account cannot be proven while some
        # comments have no owner identifier at all.
        notes.append(
            f"{len(acc.unidentified_owner_ids)} comment(s) carried no owner id or "
            "handle, so it cannot be proven that the target account wrote only one "
            "comment here"
        )
        mark_incomplete("owner_identity_unverifiable")

    # Merge adapter-reported counters (owner lookups, profile navigations).
    # requests_made comes from the shared budget, which every outbound attempt
    # charges, so it cannot drift from what actually went over the wire.
    adapter_stats = getattr(reader, "call_stats", None)
    if isinstance(adapter_stats, dict):
        acc.stats.owner_lookups += int(adapter_stats.get("owner_lookups", 0))
        acc.stats.profile_navigations += int(adapter_stats.get("profile_navigations", 0))
        acc.stats.retry_seconds += float(adapter_stats.get("retry_seconds", 0.0))
    acc.stats.requests_made = max(budget.used, acc.stats.pages_read)

    return ScanResult(
        video_id=video_id,
        requested_scope=requested_scope,
        actual_scope=actual_scope,
        status=status,
        target=acc.target,
        observed_top_likes=acc.top_likes,
        observed_top_comment_id=acc.top_comment_id,
        started_at=started_at,
        ended_at=utcnow(),
        stats=acc.stats,
        traversal=traversal,
        incomplete_reasons=_dedupe(incomplete),
        scope_limitations=_dedupe(scope_limitations),
        provider_limits=provider_limits,
        provider_notes=_dedupe(notes),
        owner_duplicate_comment_ids=owner_comment_ids if len(owner_comment_ids) > 1 else [],
        threshold_trigger_comment_id=acc.threshold_comment_id,
    )


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


async def _aclose(iterator) -> None:
    """Close a provider generator so no request task outlives the scan."""
    closer = getattr(iterator, "aclose", None)
    if closer is None:
        return
    with contextlib.suppress(Exception):
        await closer()
