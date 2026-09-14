"""God of Panel (SMM panel) client.

Transport rules, all deliberate:
  * ``POST`` with URL-encoded form data, exactly as the panel's own client
    example does;
  * TLS verification stays on;
  * redirects are NOT followed, so the API key can never be replayed to another
    host;
  * order creation is NEVER retried automatically, not even after a 5xx. Any
    ambiguity is reported as ``unknown`` for manual resolution;
  * the API key never appears in logs or in any stored payload.

The order payload for comment-likes service 5836 is exactly:
``key, action=add, service, link, quantity, username`` - the link is the VIDEO
URL and the username is the selected comment owner's handle.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .models import (
    DeliveryState,
    OrderSubmission,
    OrderTarget,
    ServiceMetadata,
    utcnow,
)

log = logging.getLogger("app.smm")

REDACTED = "***redacted***"

#: Provider status strings mapped onto our delivery vocabulary.
STATUS_MAP = {
    "pending": DeliveryState.PENDING,
    "in progress": DeliveryState.IN_PROGRESS,
    "inprogress": DeliveryState.IN_PROGRESS,
    "processing": DeliveryState.PROCESSING,
    "partial": DeliveryState.PARTIAL,
    "completed": DeliveryState.COMPLETED,
    "complete": DeliveryState.COMPLETED,
    "canceled": DeliveryState.CANCELED,
    "cancelled": DeliveryState.CANCELED,
    "error": DeliveryState.ERROR,
    "fail": DeliveryState.ERROR,
    "failed": DeliveryState.ERROR,
}


class PanelError(RuntimeError):
    pass


class ServiceIncompatible(PanelError):
    """Service 5836 is missing, or its live API type is not a comment-like type."""


def sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    if "key" in clean:
        clean["key"] = REDACTED
    return clean


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _application_error(data: Any) -> str | None:
    """God of Panel returns errors with HTTP 200, so always inspect the body."""
    if isinstance(data, dict):
        for key in ("error", "errors", "message", "msg"):
            value = data.get(key)
            if value:
                return str(value)
    return None


class SmmClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        panel_url: str,
        api_key: str,
        service_id: str,
        metadata_ttl: int = 900,
    ) -> None:
        self._client = client
        self.panel_url = panel_url
        self._api_key = api_key
        self.service_id = str(service_id)
        self._metadata_ttl = metadata_ttl
        self._service_cache: ServiceMetadata | None = None
        self._service_cache_tick: float = 0.0
        self._service_cache_error: str | None = None

    # ------------------------------------------------------------- transport
    async def _post(self, payload: dict[str, Any], *, timeout: float | None = None) -> tuple[int, Any, str]:
        response = await self._client.post(
            self.panel_url,
            data=payload,
            follow_redirects=False,
            timeout=timeout,
        )
        text = response.text
        if response.status_code in (301, 302, 303, 307, 308):
            raise PanelError(
                "panel responded with a redirect; refusing to forward credentials"
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        return response.status_code, data, text

    def _base_payload(self, action: str) -> dict[str, Any]:
        return {"key": self._api_key, "action": action}

    # -------------------------------------------------------------- metadata
    def cached_service(self) -> ServiceMetadata | None:
        if self._service_cache is None:
            return None
        if time.monotonic() - self._service_cache_tick > self._metadata_ttl:
            return None
        return self._service_cache

    @property
    def last_metadata_error(self) -> str | None:
        return self._service_cache_error

    async def get_service_metadata(self, *, force: bool = False) -> ServiceMetadata:
        """Read ``action=services`` and validate the configured service id.

        The result is cached for a short TTL so a ten-link batch does not repeat
        an identical metadata call for every video.
        """
        if not force:
            cached = self.cached_service()
            if cached is not None:
                return cached

        status, data, text = await self._post(self._base_payload("services"))
        error = _application_error(data)
        if error:
            self._service_cache_error = error
            raise PanelError(f"services call failed: {error}")
        if not isinstance(data, list):
            self._service_cache_error = f"unexpected services payload (HTTP {status})"
            raise PanelError(
                f"services returned {type(data).__name__}, expected a list "
                f"(HTTP {status}, first 120 chars: {text[:120]!r})"
            )

        for entry in data:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("service", "")).strip() != self.service_id:
                continue
            min_q = _int(entry.get("min"))
            max_q = _int(entry.get("max"))
            if min_q is None or max_q is None:
                raise ServiceIncompatible(
                    f"service {self.service_id} does not publish usable min/max limits"
                )
            metadata = ServiceMetadata(
                service_id=self.service_id,
                name=str(entry.get("name", "")),
                service_type=str(entry.get("type", "")).strip(),
                category=(str(entry["category"]) if entry.get("category") else None),
                rate=_decimal(entry.get("rate")),
                min_quantity=min_q,
                max_quantity=max_q,
                refill=bool(entry["refill"]) if "refill" in entry else None,
                fetched_at=utcnow(),
            )
            self._service_cache = metadata
            self._service_cache_tick = time.monotonic()
            self._service_cache_error = None
            return metadata

        self._service_cache_error = f"service {self.service_id} not found in services list"
        raise ServiceIncompatible(self._service_cache_error)

    @staticmethod
    def service_supports_comment_likes(metadata: ServiceMetadata) -> bool:
        """Accept only a service whose live API type takes a username field.

        God of Panel documents the comment-like order shape as type 15
        (key/action/service/link/quantity/username). Some panels report the type
        numerically and some by name, so both are accepted; anything else is an
        incompatibility because our payload would not be understood.
        """
        service_type = metadata.service_type.strip().casefold()
        if service_type in {"15", "comment likes", "comment_likes", "commentlikes"}:
            return True
        return False

    async def get_balance(self) -> tuple[Decimal | None, str | None, str | None]:
        try:
            status, data, text = await self._post(self._base_payload("balance"))
        except (httpx.HTTPError, PanelError) as exc:
            return None, None, str(exc)
        error = _application_error(data)
        if error:
            return None, None, error
        if not isinstance(data, dict):
            return None, None, f"unexpected balance payload (HTTP {status})"
        return _decimal(data.get("balance")), data.get("currency"), None

    # ----------------------------------------------------------------- orders
    def build_payload(self, target: OrderTarget, quantity: int) -> dict[str, Any]:
        """The exact documented comment-like payload. No extra fields, ever."""
        return {
            "key": self._api_key,
            "action": "add",
            "service": self.service_id,
            "link": target.video_url,
            "quantity": quantity,
            "username": target.comment_owner_username,
        }

    async def submit_order(self, target: OrderTarget, quantity: int) -> OrderSubmission:
        """Create one order. Never retried; ambiguity becomes ``unknown``."""
        if quantity <= 0:
            raise ValueError("refusing to submit an order of zero or fewer likes")
        payload = self.build_payload(target, quantity)
        log.info("submitting order payload=%s", sanitize(payload))
        started = time.monotonic()

        try:
            status, data, text = await self._post(payload)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            # The request may or may not have reached the panel. Do not retry.
            return OrderSubmission(
                accepted=False,
                unknown=True,
                order_id=None,
                error=f"transport failure after sending the order: {exc!s}",
                raw_response=None,
                http_status=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except PanelError as exc:
            return OrderSubmission(
                accepted=False,
                unknown=True,
                order_id=None,
                error=str(exc),
                raw_response=None,
                http_status=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        raw = data if isinstance(data, dict) else {"_non_json_body": text[:500]}

        error = _application_error(data)
        if error:
            return OrderSubmission(
                accepted=False,
                unknown=False,
                order_id=None,
                error=error,
                raw_response=raw,
                http_status=status,
                elapsed_ms=elapsed_ms,
            )

        if isinstance(data, dict) and data.get("order") not in (None, ""):
            return OrderSubmission(
                accepted=True,
                unknown=False,
                order_id=str(data["order"]),
                error=None,
                raw_response=raw,
                http_status=status,
                elapsed_ms=elapsed_ms,
            )

        # HTTP looked fine but the body carries neither an order id nor a
        # recognised error. The order may still have been created.
        return OrderSubmission(
            accepted=False,
            unknown=True,
            order_id=None,
            error=(
                f"unrecognised response to an order request (HTTP {status}); the order "
                "may or may not have been created"
            ),
            raw_response=raw,
            http_status=status,
            elapsed_ms=elapsed_ms,
        )

    async def get_order_status(self, order_id: str) -> tuple[DeliveryState, dict[str, Any] | None, Decimal | None, str | None]:
        payload = self._base_payload("status")
        payload["order"] = str(order_id)
        try:
            status, data, text = await self._post(payload)
        except (httpx.HTTPError, PanelError) as exc:
            return DeliveryState.UNKNOWN, None, None, str(exc)
        error = _application_error(data)
        if error:
            return DeliveryState.UNKNOWN, {"error": error}, None, error
        if not isinstance(data, dict):
            return DeliveryState.UNKNOWN, None, None, f"unexpected status payload (HTTP {status})"
        raw_status = str(data.get("status", "")).strip().casefold()
        state = STATUS_MAP.get(raw_status, DeliveryState.UNKNOWN)
        return state, data, _decimal(data.get("charge")), None


def build_http_client(*, timeout: float = 30.0, max_connections: int = 10) -> httpx.AsyncClient:
    """One shared, pooled, TLS-verifying client for the whole process."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        verify=True,
        follow_redirects=False,
        headers={"accept": "application/json"},
    )
