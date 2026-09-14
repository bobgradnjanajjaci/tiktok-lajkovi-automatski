"""Typed models shared by the reader adapters, scanner, worker and web layer.

Deliberately implemented with stdlib dataclasses/enums so the correctness-critical
modules (comment_finder, like_rules) can be imported and tested without any third
party package installed.

ID discipline: every video id, comment id, user id and provider order id is a
``str`` everywhere - in the adapters, in SQLite, in JSON responses and in the
browser. TikTok ids exceed JavaScript's Number.MAX_SAFE_INTEGER (2**53 - 1), so
they are never converted to int.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


class CommentScope(str, Enum):
    ALL = "all"
    TOP_LEVEL = "top_level"


class RunMode(str, Enum):
    DRY_RUN = "dry_run"
    LIVE = "live"


class Outcome(str, Enum):
    """Processing outcome of one input link. Distinct from delivery state."""

    PENDING = "pending"
    PROCESSING = "processing"
    KEYWORD_NOT_FOUND = "keyword_not_found"
    SCAN_INCOMPLETE = "scan_incomplete"
    SKIPPED_THRESHOLD = "skipped_threshold"
    ALREADY_ORDERED = "already_ordered"
    ACTIVE_ORDER_EXISTS = "active_order_exists"
    TARGET_AMBIGUOUS = "target_ambiguous"
    TARGET_UNVERIFIED = "target_unverified"
    QUANTITY_OUT_OF_RANGE = "quantity_out_of_range"
    SERVICE_CONFIGURATION_REQUIRED = "service_configuration_required"
    READER_UNCONFIGURED = "reader_unconfigured"
    URL_INVALID = "url_invalid"
    PROVIDER_ERROR = "provider_error"
    DRY_RUN_COMPLETE = "dry_run_complete"
    SUBMITTED = "submitted"
    SUBMISSION_UNKNOWN = "submission_unknown"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryState(str, Enum):
    """Provider-reported state of an accepted order. Never inferred locally."""

    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PROCESSING = "processing"
    PARTIAL = "partial"
    COMPLETED = "completed"
    CANCELED = "canceled"
    ERROR = "error"


#: Delivery states that keep the per-video in-flight lock held.
ACTIVE_DELIVERY_STATES = frozenset(
    {
        DeliveryState.UNKNOWN,
        DeliveryState.PENDING,
        DeliveryState.IN_PROGRESS,
        DeliveryState.PROCESSING,
    }
)


class ScanStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    EARLY_EXIT_THRESHOLD = "early_exit_threshold"


@dataclass(frozen=True, slots=True)
class VideoIdentity:
    """Result of resolving whatever the operator pasted into a real video."""

    input_url: str
    video_id: str
    canonical_url: str
    author_handle: str | None
    verified: bool
    redirects: int = 0
    note: str | None = None


@dataclass(slots=True)
class NormalizedComment:
    """One comment as required by the scanner, regardless of provider shape."""

    video_id: str
    video_url: str
    comment_id: str
    parent_comment_id: str | None
    text: str
    like_count: int
    reply_count: int | None
    comment_owner_username: str | None
    comment_owner_user_id: str | None
    source_order: int
    fetched_at: datetime
    owner_username_source: str = "unknown"
    raw_owner_username: str | None = None
    like_count_trusted: bool = True
    #: False when the provider omitted the text field, sent null, or sent a
    #: non-string. An explicitly present empty string is trusted; an unknown
    #: text is NOT, because a comment whose text cannot be read may itself have
    #: been the first keyword match.
    text_trusted: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "video_url": self.video_url,
            "comment_id": self.comment_id,
            "parent_comment_id": self.parent_comment_id,
            "text": self.text,
            "like_count": self.like_count,
            "reply_count": self.reply_count,
            "comment_owner_username": self.comment_owner_username,
            "comment_owner_user_id": self.comment_owner_user_id,
            "source_order": self.source_order,
            "fetched_at": iso(self.fetched_at),
            "owner_username_source": self.owner_username_source,
            "like_count_trusted": self.like_count_trusted,
            "text_trusted": self.text_trusted,
        }


@dataclass(slots=True)
class CommentPage:
    """One provider page plus the pagination facts needed for completeness.

    ``completion_unknown`` is the important one: it is set when the provider's
    has-more marker was absent or in a shape this adapter does not recognise.
    An unrecognised marker is NOT the same as "no more data", so the scanner
    turns it into an incomplete result instead of a silent end of stream.
    """

    comments: list[NormalizedComment]
    cursor_in: str | None
    cursor_out: str | None
    has_more: bool
    total_reported: int | None = None
    parent_comment_id: str | None = None
    provider_limits: dict[str, Any] = field(default_factory=dict)
    invalid_records: int = 0
    completion_unknown: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScanStats:
    pages_read: int = 0
    requests_made: int = 0
    comments_seen: int = 0
    unique_comments: int = 0
    duplicates_dropped: int = 0
    invalid_like_counts: int = 0
    unreadable_texts: int = 0
    owner_lookups: int = 0
    profile_navigations: int = 0
    retry_seconds: float = 0.0
    threads_expanded: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "pages_read": self.pages_read,
            "requests_made": self.requests_made,
            "comments_seen": self.comments_seen,
            "unique_comments": self.unique_comments,
            "duplicates_dropped": self.duplicates_dropped,
            "invalid_like_counts": self.invalid_like_counts,
            "unreadable_texts": self.unreadable_texts,
            "owner_lookups": self.owner_lookups,
            "profile_navigations": self.profile_navigations,
            "retry_seconds": round(self.retry_seconds, 3),
            "threads_expanded": self.threads_expanded,
        }


@dataclass(slots=True)
class ScanResult:
    """Single-pass scan output: first keyword match plus the observed maximum."""

    video_id: str
    requested_scope: CommentScope
    actual_scope: CommentScope
    status: ScanStatus
    target: NormalizedComment | None
    observed_top_likes: int | None
    observed_top_comment_id: str | None
    started_at: datetime
    ended_at: datetime
    stats: ScanStats
    traversal: str
    incomplete_reasons: list[str] = field(default_factory=list)
    provider_limits: dict[str, Any] = field(default_factory=dict)
    provider_notes: list[str] = field(default_factory=list)
    owner_duplicate_comment_ids: list[str] = field(default_factory=list)
    threshold_trigger_comment_id: str | None = None
    #: Facts that narrow what "complete" means for this result. Shown in the
    #: dashboard next to the scan status, never only in the logs.
    scope_limitations: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.status is ScanStatus.COMPLETE

    @property
    def target_ambiguous(self) -> bool:
        return len(self.owner_duplicate_comment_ids) > 1

    @property
    def duration_ms(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds() * 1000)

    def as_json(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "requested_scope": self.requested_scope.value,
            "actual_scope": self.actual_scope.value,
            "status": self.status.value,
            "complete": self.complete,
            "target": self.target.as_json() if self.target else None,
            "observed_top_likes": self.observed_top_likes,
            "observed_top_comment_id": self.observed_top_comment_id,
            "traversal": self.traversal,
            "incomplete_reasons": list(self.incomplete_reasons),
            "provider_limits": dict(self.provider_limits),
            "provider_notes": list(self.provider_notes),
            "owner_duplicate_comment_ids": list(self.owner_duplicate_comment_ids),
            "scope_limitations": list(self.scope_limitations),
            "completeness_basis": (
                "provider-visible data (reply-count mismatches accepted)"
                if self.scope_limitations
                else "every requested stream reported its end"
            ),
            "threshold_trigger_comment_id": self.threshold_trigger_comment_id,
            "started_at": iso(self.started_at),
            "ended_at": iso(self.ended_at),
            "duration_ms": self.duration_ms,
            "stats": self.stats.as_json(),
        }


@dataclass(frozen=True, slots=True)
class OrderTarget:
    """Exactly what God of Panel service 5836 accepts: video URL + username."""

    video_id: str
    video_url: str
    comment_owner_username: str
    comment_id: str
    comment_owner_user_id: str | None
    raw_owner_username: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceMetadata:
    service_id: str
    name: str
    service_type: str
    category: str | None
    rate: Decimal | None
    min_quantity: int
    max_quantity: int
    refill: bool | None
    fetched_at: datetime

    def as_json(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "type": self.service_type,
            "category": self.category,
            "rate": None if self.rate is None else str(self.rate),
            "min": self.min_quantity,
            "max": self.max_quantity,
            "refill": self.refill,
            "fetched_at": iso(self.fetched_at),
        }


@dataclass(frozen=True, slots=True)
class OrderSubmission:
    """Result of one paid submission attempt."""

    accepted: bool
    unknown: bool
    order_id: str | None
    error: str | None
    raw_response: dict[str, Any] | None
    http_status: int | None
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class Timings:
    url_resolve_ms: int = 0
    comment_read_ms: int = 0
    owner_lookup_ms: int = 0
    calculation_ms: int = 0
    submission_ms: int = 0
    total_ms: int = 0

    def as_json(self) -> dict[str, int]:
        return {
            "url_resolve_ms": self.url_resolve_ms,
            "comment_read_ms": self.comment_read_ms,
            "owner_lookup_ms": self.owner_lookup_ms,
            "calculation_ms": self.calculation_ms,
            "submission_ms": self.submission_ms,
            "total_ms": self.total_ms,
        }


class ProviderError(RuntimeError):
    """Reader adapter failed in a way the worker should record, not crash on."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ReaderUnconfigured(ProviderError):
    """The live comment reader has no verified provider contract configured."""

    def __init__(self, message: str = "Comment reader is not configured"):
        super().__init__(message, retryable=False)
