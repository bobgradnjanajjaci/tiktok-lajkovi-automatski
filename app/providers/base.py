"""Shared base class, capability declaration and the provider contract schema.

THE READER CONTRACT
===================
A comment reader is usable by this application only if it can supply, for every
comment in the selected scope:

  1. a stable comment id (string);
  2. the comment text, verbatim;
  3. an exact like count (not "1.2K", not an estimate);
  4. the comment owner's real @handle, or a stable owner id that a single
     supported lookup can turn into the real @handle;
  5. the parent comment id for replies, plus a reply count on parents;
  6. documented pagination: a cursor and a has-more flag, for top-level comments
     and (separately, if the provider paginates them separately) for replies;
  7. a documented ordering that the provider will describe as stable.

A reader that only exposes display names cannot supply the username that God of
Panel service 5836 requires, and is therefore unusable for Live ordering.

``ReaderContract`` is the machine-readable form of that agreement. It is written
by the operator from their provider's own documentation, never guessed here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from ..models import CommentPage, ProviderError


@dataclass(frozen=True, slots=True)
class ReaderCapabilities:
    supports_replies: bool
    supplies_owner_username: bool
    supports_owner_lookup: bool
    supports_exact_counts: bool
    documented_ordering: str
    max_page_size: int


@dataclass(slots=True)
class ReaderContract:
    """Provider-specific request/response description, loaded from JSON.

    Example (``reader_contract.json``) - every value must come from the
    provider's published documentation:

    .. code-block:: json

        {
          "verified_by_operator": true,
          "provider_name": "example-comments-api",
          "documented_ordering": "provider default order, described as stable",
          "auth": {"style": "header", "name": "x-api-key"},
          "top_level": {
            "method": "GET",
            "path": "/v1/tiktok/comments",
            "query": {"video_id": "{video_id}", "count": "{page_size}",
                      "cursor": "{cursor}"},
            "list_path": "data.comments",
            "cursor_path": "data.cursor",
            "has_more_path": "data.has_more",
            "total_path": "data.total"
          },
          "replies": {
            "method": "GET",
            "path": "/v1/tiktok/comment-replies",
            "query": {"video_id": "{video_id}", "comment_id": "{parent_id}",
                      "count": "{page_size}", "cursor": "{cursor}"},
            "list_path": "data.comments",
            "cursor_path": "data.cursor",
            "has_more_path": "data.has_more"
          },
          "owner_lookup": {
            "method": "GET",
            "path": "/v1/tiktok/user",
            "query": {"user_id": "{user_id}"},
            "username_path": "data.unique_id"
          },
          "fields": {
            "comment_id": "cid",
            "text": "text",
            "like_count": "digg_count",
            "reply_count": "reply_comment_total",
            "parent_comment_id": "reply_id",
            "owner_username": "user.unique_id",
            "owner_user_id": "user.uid"
          },
          "capabilities": {
            "supports_replies": true,
            "supplies_owner_username": true,
            "supports_owner_lookup": true,
            "supports_exact_counts": true,
            "max_page_size": 50
          },
          "limits": {"requests_per_minute": 60, "max_comments_per_video": null}
        }
    """

    verified_by_operator: bool
    provider_name: str
    documented_ordering: str
    auth: dict[str, Any]
    top_level: dict[str, Any]
    replies: dict[str, Any] | None
    owner_lookup: dict[str, Any] | None
    fields: dict[str, str]
    capabilities: dict[str, Any]
    limits: dict[str, Any] = field(default_factory=dict)

    REQUIRED_FIELDS = ("comment_id", "text", "like_count")

    @classmethod
    def load(cls, path: str | Path) -> "ReaderContract":
        file = Path(path)
        if not file.is_file():
            raise ProviderError(f"reader contract file not found: {file}")
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"reader contract file is not readable JSON: {exc}") from exc
        missing = [
            key
            for key in ("provider_name", "top_level", "fields", "capabilities")
            if key not in data
        ]
        if missing:
            raise ProviderError(f"reader contract is missing keys: {', '.join(missing)}")
        contract = cls(
            verified_by_operator=bool(data.get("verified_by_operator", False)),
            provider_name=str(data["provider_name"]),
            documented_ordering=str(data.get("documented_ordering", "undocumented")),
            auth=dict(data.get("auth") or {}),
            top_level=dict(data["top_level"]),
            replies=dict(data["replies"]) if data.get("replies") else None,
            owner_lookup=dict(data["owner_lookup"]) if data.get("owner_lookup") else None,
            fields=dict(data["fields"]),
            capabilities=dict(data["capabilities"]),
            limits=dict(data.get("limits") or {}),
        )
        for key in cls.REQUIRED_FIELDS:
            if key not in contract.fields:
                raise ProviderError(f"reader contract field mapping is missing '{key}'")
        if not (
            contract.fields.get("owner_username")
            or (contract.fields.get("owner_user_id") and contract.owner_lookup)
        ):
            raise ProviderError(
                "reader contract supplies no owner username and no owner lookup; such a "
                "provider cannot satisfy God of Panel service 5836"
            )
        return contract

    def as_capabilities(self) -> ReaderCapabilities:
        caps = self.capabilities
        return ReaderCapabilities(
            supports_replies=bool(caps.get("supports_replies", False)) and self.replies is not None,
            supplies_owner_username=bool(caps.get("supplies_owner_username", False))
            or bool(self.owner_lookup),
            supports_owner_lookup=bool(caps.get("supports_owner_lookup", False))
            and self.owner_lookup is not None,
            supports_exact_counts=bool(caps.get("supports_exact_counts", False)),
            documented_ordering=self.documented_ordering,
            max_page_size=int(caps.get("max_page_size", 50)),
        )


class CommentReaderBase:
    """Structural base for every adapter. The scanner depends only on this."""

    name: str = "base"

    #: Set by ``scan_video`` for the duration of one scan. Adapters charge it
    #: before every outbound attempt - first tries, retries and owner lookups -
    #: so the per-scan request limit cannot be exceeded by side requests added
    #: after a page-level check has already passed.
    request_budget = None

    def _charge_request(self, count: int = 1) -> None:
        budget = getattr(self, "request_budget", None)
        if budget is not None:
            budget.charge(count)

    def __init__(self) -> None:
        self.call_stats: dict[str, Any] = {
            "owner_lookups": 0,
            "profile_navigations": 0,
            "extra_requests": 0,
            "retry_seconds": 0.0,
        }

    # --- capability surface -------------------------------------------------
    @property
    def capabilities(self) -> ReaderCapabilities:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def configured(self) -> bool:  # pragma: no cover - overridden
        return False

    @property
    def blocker(self) -> str | None:  # pragma: no cover - overridden
        return "not implemented"

    @property
    def usable_for_dry_run(self) -> bool:
        """True when the adapter can produce results without being a live source.

        Only the fixture reader sets this while ``configured`` is False.
        """
        return self.configured

    @property
    def provider_limits(self) -> dict[str, Any]:
        return {}

    @property
    def supports_replies(self) -> bool:
        return self.capabilities.supports_replies

    # --- data surface -------------------------------------------------------
    async def iter_top_level_pages(
        self, video_id: str, video_url: str
    ) -> AsyncIterator[CommentPage]:  # pragma: no cover - overridden
        raise NotImplementedError
        yield  # type: ignore[unreachable]

    async def iter_reply_pages(
        self, video_id: str, video_url: str, parent_comment_id: str
    ) -> AsyncIterator[CommentPage]:  # pragma: no cover - overridden
        raise NotImplementedError
        yield  # type: ignore[unreachable]

    async def recheck_comment(self, video_id: str, comment_id: str) -> bool | None:
        """Optional pre-order existence recheck. ``None`` = not supported."""
        return None

    async def aclose(self) -> None:
        return None


def dig(data: Any, path: str) -> Any:
    """Read ``a.b.c`` out of nested dicts/lists. Missing paths return None."""
    if not path:
        return None
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def strip_presentation_at(handle: str) -> str:
    """Remove exactly one presentation-only leading @, preserving the rest."""
    return handle[1:] if handle.startswith("@") else handle


MISSING = object()
"""Sentinel telling ``dig_strict`` apart from a legitimately null value."""


def dig_strict(data: Any, path: str) -> Any:
    """Like ``dig``, but returns ``MISSING`` when a key is absent.

    ``dig`` cannot distinguish ``{"comments": null}`` from ``{}``; for the
    comment list that difference decides between "API/schema error" and "this
    page really is empty", so the strict variant is used there.
    """
    if not path:
        return MISSING
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return MISSING
        else:
            return MISSING
    return current


#: Textual forms this codebase is willing to read as booleans. Anything else is
#: reported as unknown rather than guessed, because Python's truthiness would
#: otherwise read the strings "false" and "0" as True.
_TRUE_TOKENS = {"1", "true", "t", "yes", "y"}
_FALSE_TOKENS = {"0", "false", "f", "no", "n"}


def parse_bool_flag(value: Any) -> bool | None:
    """Strictly parse a has-more style marker.

    Returns True/False for recognised shapes and ``None`` for anything else -
    including ``None`` itself, empty strings, dicts and unexpected numbers.
    ``None`` means "the provider did not tell us", which callers must treat as
    incomplete rather than as the end of a stream.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, float):
        if value in (0.0, 1.0):
            return bool(value)
        return None
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return None


def coerce_id(value: Any) -> str | None:
    """Provider ids become strings, or None when genuinely absent.

    Numeric ids are stringified without going through float, so a 19-digit
    TikTok id keeps every digit.
    """
    if value is None or value is MISSING:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, int):
        return str(value)
    return None
