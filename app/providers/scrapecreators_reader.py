"""ScrapeCreators comment reader - the concrete live integration.

WHAT THIS IS BUILT FROM
=======================
Every request shape, parameter name, response field and error code below comes
from ScrapeCreators' own published documentation (endpoint pages and language
tutorials on scrapecreators.com, read while writing this file). Nothing is
guessed and no TikTok endpoint is contacted directly.

Endpoints used
--------------
1. Top-level comments
   ``GET https://api.scrapecreators.com/v1/tiktok/video/comments``
   Parameters: ``url`` (required, the TikTok video URL), ``cursor`` (optional,
   numeric, taken from the previous response), ``trim`` (optional boolean).
   This adapter sends ``url`` and, from the second page onward, ``cursor``.
   It deliberately does NOT send a page-size parameter, because the endpoint
   does not document one, and it does not send ``trim`` unless
   ``READER_TRIM=true``, because the untrimmed response is what carries the
   fields this application depends on.

2. Replies to one comment
   ``GET https://api.scrapecreators.com/v1/tiktok/video/comment/replies``
   Parameters: ``comment_id`` (required, the ``cid`` from the comments
   endpoint), ``url`` (required, the same video URL), ``cursor`` (optional,
   numeric).

3. Owner handle fallback
   ``GET https://api.scrapecreators.com/v1/tiktok/profile``
   Parameters: ``handle`` or ``user_id``, plus ``cache_max_age`` (documented as
   returning a cached response for 0 credits when one is fresh enough). Used
   only when a comment record somehow lacks ``user.unique_id``; in the
   documented response shape it is always present, so in practice this endpoint
   should be called zero times per video. Commenter profile *pages* are never
   opened.

Response fields consumed (documented sample responses)
------------------------------------------------------
Envelope: ``success``, ``status_code``, ``status_msg``, ``comments`` (list),
``cursor`` (number), ``has_more`` (documented as the integer ``1``), ``total``,
``credits_charged``, ``credits_remaining``.
Comment record: ``cid``, ``text``, ``digg_count``, ``reply_comment_total``,
``reply_id`` (parent ``cid``, or the string ``"0"`` for a top-level comment),
``user.uid``, ``user.unique_id``.

Honest notes about what is and is not established
-------------------------------------------------
* ``has_more`` appears as the integer ``1`` in the documented samples. It is
  parsed strictly (see ``parse_bool_flag``): ``1``/``0``/``true``/``false`` are
  understood, anything else is reported as an UNKNOWN completion marker and the
  scan is marked incomplete rather than quietly treated as finished.
* ``cursor`` is documented as a number and behaves like an offset. It is echoed
  back verbatim, never guessed or incremented locally.
* ScrapeCreators does not publish an ordering-stability guarantee, so this
  adapter reports its ordering as "provider order, stability not documented".
  The traversal rule is still deterministic within one scan.
* The provider states there is no account rate limit but recommends staying
  below high concurrency. This application reads one video at a time anyway.
* Requests cost credits. ``credits_charged`` / ``credits_remaining`` from each
  response are recorded and surfaced, so real consumption is shown rather than
  estimated. Read-only calls still cost credits.
* ``reply_comment_total`` on a parent is TikTok's own counter and can exceed the
  number of replies the replies endpoint returns (hidden, deleted or deeply
  nested replies). See ``SCAN_TRUST_PROVIDER_REPLY_END`` in config.py for how
  that discrepancy is reported.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

from ..like_rules import safe_like_count
from ..models import (
    CommentPage,
    NormalizedComment,
    ProviderError,
    ReaderUnconfigured,
    utcnow,
)
from .base import (
    MISSING,
    CommentReaderBase,
    ReaderCapabilities,
    coerce_id,
    dig,
    dig_strict,
    parse_bool_flag,
    strip_presentation_at,
)

if TYPE_CHECKING:  # settings are injected; no runtime import needed
    from ..config import Settings

log = logging.getLogger("app.reader.scrapecreators")

DEFAULT_BASE_URL = "https://api.scrapecreators.com"
COMMENTS_PATH = "/v1/tiktok/video/comments"
REPLIES_PATH = "/v1/tiktok/video/comment/replies"
PROFILE_PATH = "/v1/tiktok/profile"
DOCS_URL = "https://docs.scrapecreators.com"
SIGNUP_URL = "https://app.scrapecreators.com"

#: Documented transient conditions. 402 (out of credits) and 401/403 (bad key)
#: are deliberately NOT here: retrying them cannot help.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_READ_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 10.0

#: Paths the profile response has been documented to expose the handle at. Tried
#: in order; nothing outside this list is accepted as a handle.
PROFILE_HANDLE_PATHS = ("user.uniqueId", "user.unique_id", "uniqueId", "unique_id")


class ScrapeCreatorsError(ProviderError):
    """A ScrapeCreators call failed or returned something unusable."""


class ScrapeCreatorsCommentReader(CommentReaderBase):
    name = "scrapecreators"

    def __init__(self, settings: "Settings", client: httpx.AsyncClient | None = None) -> None:
        super().__init__()
        self._settings = settings
        self._client = client
        self._owner_cache: dict[str, tuple[float, str | None]] = {}
        self._base = (settings.READER_BASE_URL or DEFAULT_BASE_URL).rstrip("/")
        self.call_stats.update(
            {
                "credits_charged": 0,
                "credits_remaining": None,
                "cached_responses": 0,
                "requests": 0,
            }
        )

    # ----------------------------------------------------------- capabilities
    @property
    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            supports_replies=True,
            supplies_owner_username=True,
            supports_owner_lookup=True,
            supports_exact_counts=True,
            documented_ordering=(
                "provider order as returned by TikTok; ScrapeCreators does not "
                "document a stability guarantee across requests"
            ),
            # The endpoint documents no page-size parameter, so page size is
            # provider-controlled. 0 means "not client-selectable".
            max_page_size=0,
        )

    @property
    def configured(self) -> bool:
        return bool(self._settings.READER_API_KEY and self._base)

    @property
    def blocker(self) -> str | None:
        if self.configured:
            return None
        return (
            "READER_API_KEY is not set. Create a key at "
            f"{SIGNUP_URL} and set READER_API_KEY (Railway Variables or .env). "
            "Reading comments and Live ordering stay disabled until then."
        )

    @property
    def usable_for_dry_run(self) -> bool:
        # Reading is a real API call: without a key there is nothing to read,
        # and the fixture reader is never substituted silently.
        return self.configured

    @property
    def provider_limits(self) -> dict[str, Any]:
        return {
            "provider": "scrapecreators",
            "docs": DOCS_URL,
            "base_url": self._base,
            "documented_ordering": self.capabilities.documented_ordering,
            "page_size": "provider controlled (endpoint documents no page-size parameter)",
            "account_rate_limit": "none published; provider advises low concurrency",
            "billing": "credits per request; cached profile responses are documented as free",
            "credits_charged_this_process": self.call_stats.get("credits_charged"),
            "credits_remaining_last_seen": self.call_stats.get("credits_remaining"),
        }

    # -------------------------------------------------------------- transport
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.READER_TIMEOUT_SECONDS, connect=10.0),
                limits=httpx.Limits(
                    max_connections=self._settings.READER_MAX_CONNECTIONS,
                    max_keepalive_connections=self._settings.READER_MAX_CONNECTIONS,
                ),
                verify=True,
                follow_redirects=False,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        key = self._settings.READER_API_KEY
        if not key:
            raise ReaderUnconfigured(self.blocker or "READER_API_KEY is not set")
        header_name = self._settings.READER_API_KEY_HEADER or "x-api-key"
        return {header_name: key, "accept": "application/json"}

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """One documented GET, with bounded retries on transient failures only."""
        url = self._base + path
        headers = self._headers()
        client = self._http()
        last_error = "unknown error"

        for attempt in range(1, MAX_READ_ATTEMPTS + 1):
            # Charged BEFORE the call, and once per attempt, so a retry or an
            # owner lookup cannot slip past the per-scan request budget.
            self._charge_request()
            self.call_stats["requests"] = int(self.call_stats["requests"]) + 1
            try:
                response = await client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == MAX_READ_ATTEMPTS:
                    raise ScrapeCreatorsError(
                        f"{path} failed after {attempt} attempts: {last_error}. "
                        "A timeout is not an empty comment section.",
                        retryable=True,
                    ) from exc
                await self._backoff(attempt, None)
                continue

            status = response.status_code

            if status in RETRYABLE_STATUS:
                last_error = f"HTTP {status}"
                if attempt == MAX_READ_ATTEMPTS:
                    raise ScrapeCreatorsError(
                        f"{path} returned {last_error} after {attempt} attempts",
                        retryable=True,
                        status=status,
                    )
                await self._backoff(attempt, response.headers.get("retry-after"))
                continue

            if status in (401, 403):
                raise ScrapeCreatorsError(
                    f"ScrapeCreators rejected the credentials (HTTP {status}). Check "
                    "READER_API_KEY. This is an access blocker, not something to "
                    "work around.",
                    retryable=False,
                    status=status,
                )
            if status == 402:
                raise ScrapeCreatorsError(
                    "ScrapeCreators reports no credits left (HTTP 402). Top up the "
                    "account; no comments were read and no order was placed.",
                    retryable=False,
                    status=402,
                )
            if status == 404:
                raise ScrapeCreatorsError(
                    f"ScrapeCreators could not find the resource (HTTP 404) for {path}. "
                    "The video or comment may be private, removed or mistyped. This is "
                    "not a video without comments.",
                    retryable=False,
                    status=404,
                )
            if status >= 400:
                raise ScrapeCreatorsError(
                    f"{path} returned HTTP {status}: {_snippet(response.text)}",
                    retryable=False,
                    status=status,
                )

            return self._decode(response, path)

        raise ScrapeCreatorsError(last_error)  # pragma: no cover - loop always returns

    def _decode(self, response: httpx.Response, path: str) -> dict[str, Any]:
        """Parse and sanity-check the envelope, including errors served as 200."""
        content_type = response.headers.get("content-type", "")
        try:
            payload = response.json()
        except ValueError as exc:
            hint = "HTML" if "html" in content_type.lower() else "non-JSON"
            raise ScrapeCreatorsError(
                f"{path} returned a {hint} body instead of JSON "
                f"({_snippet(response.text)}). Treated as a provider failure, not as "
                "an empty comment section."
            ) from exc

        if not isinstance(payload, dict):
            raise ScrapeCreatorsError(
                f"{path} returned JSON of type {type(payload).__name__}, expected an object"
            )

        # An API-level failure can arrive with HTTP 200.
        success = payload.get("success", MISSING)
        if success is not MISSING and parse_bool_flag(success) is False:
            message = (
                payload.get("message")
                or payload.get("error")
                or payload.get("status_msg")
                or "no message supplied"
            )
            raise ScrapeCreatorsError(f"{path} reported success=false: {message}")

        status_code = payload.get("status_code", MISSING)
        if isinstance(status_code, int) and status_code != 0:
            raise ScrapeCreatorsError(
                f"{path} reported status_code={status_code} "
                f"({payload.get('status_msg') or 'no status_msg'})"
            )

        charged = safe_like_count(payload.get("credits_charged"))
        if charged is not None:
            self.call_stats["credits_charged"] = (
                int(self.call_stats["credits_charged"]) + charged
            )
        remaining = safe_like_count(payload.get("credits_remaining"))
        if remaining is not None:
            self.call_stats["credits_remaining"] = remaining
        if parse_bool_flag(payload.get("cached")) is True:
            self.call_stats["cached_responses"] = int(self.call_stats["cached_responses"]) + 1

        return payload

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = 0.5 * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, MAX_BACKOFF_SECONDS)
        self.call_stats["retry_seconds"] = float(self.call_stats["retry_seconds"]) + delay
        await asyncio.sleep(delay)

    # ----------------------------------------------------------- normalization
    def _normalize(
        self,
        record: Any,
        *,
        video_id: str,
        video_url: str,
        parent_comment_id: str | None,
        source_order: int,
    ) -> NormalizedComment | None:
        """Map one documented comment record. Returns None if unusable.

        A missing ``cid`` makes the record unusable. It is never replaced with
        the video id, a list index, a hash of the text or a random number,
        because the comment id is what deduplication and the same-owner
        ambiguity check depend on.
        """
        if not isinstance(record, dict):
            return None

        comment_id = coerce_id(record.get("cid"))
        if comment_id is None:
            return None

        raw_likes = record.get("digg_count", MISSING)
        likes = None if raw_likes is MISSING else safe_like_count(raw_likes)
        trusted = likes is not None

        reply_count = safe_like_count(record.get("reply_comment_total"))

        # reply_id is the parent cid; the string "0" marks a top-level comment.
        parent_from_record = coerce_id(record.get("reply_id"))
        if parent_from_record in ("0", None):
            parent_from_record = None

        user = record.get("user")
        raw_username = None
        owner_user_id = None
        if isinstance(user, dict):
            raw_value = user.get("unique_id")
            if isinstance(raw_value, str) and raw_value.strip():
                raw_username = raw_value.strip()
            owner_user_id = coerce_id(user.get("uid"))

        username = strip_presentation_at(raw_username) if raw_username else None

        # An explicitly present empty string is a real, readable comment.
        # A missing key, null, or a non-string is NOT: the text is unknown, and
        # an unknown text could itself have been the first keyword match.
        text_value = record.get("text", MISSING)
        text_trusted = isinstance(text_value, str)
        text = text_value if text_trusted else ""

        return NormalizedComment(
            video_id=video_id,
            video_url=video_url,
            comment_id=comment_id,
            parent_comment_id=parent_from_record or parent_comment_id,
            text=text,
            like_count=likes if trusted else 0,
            reply_count=reply_count,
            comment_owner_username=username,
            comment_owner_user_id=owner_user_id,
            source_order=source_order,
            fetched_at=utcnow(),
            owner_username_source="comment_record" if username else "missing",
            raw_owner_username=raw_username,
            like_count_trusted=trusted,
            text_trusted=text_trusted,
        )

    async def _fill_missing_usernames(self, comments: list[NormalizedComment]) -> None:
        """Resolve handles only when the comment record did not carry one.

        One request per distinct owner id, cached for
        ``READER_OWNER_CACHE_TTL_SECONDS``. In the documented response shape
        ``user.unique_id`` is always present, so this normally does nothing.
        """
        pending = {
            comment.comment_owner_user_id
            for comment in comments
            if comment.comment_owner_username is None and comment.comment_owner_user_id
        }
        if not pending:
            return

        ttl = float(self._settings.READER_OWNER_CACHE_TTL_SECONDS)
        now = time.monotonic()
        for user_id in sorted(pending):
            cached = self._owner_cache.get(user_id)
            if cached is not None and (now - cached[0]) < ttl:
                continue
            handle = await self._lookup_handle(user_id)
            self._owner_cache[user_id] = (time.monotonic(), handle)

        for comment in comments:
            if comment.comment_owner_username is not None:
                continue
            cached = self._owner_cache.get(comment.comment_owner_user_id or "")
            if cached and cached[1]:
                comment.comment_owner_username = cached[1]
                comment.owner_username_source = "profile_lookup_by_user_id"
                comment.raw_owner_username = cached[1]

    async def _lookup_handle(self, user_id: str) -> str | None:
        params = {"user_id": user_id}
        cache_age = self._settings.READER_PROFILE_CACHE_MAX_AGE.strip()
        if cache_age:
            params["cache_max_age"] = cache_age
        data = await self._get(PROFILE_PATH, params)
        self.call_stats["owner_lookups"] = int(self.call_stats["owner_lookups"]) + 1
        self.call_stats["extra_requests"] = int(self.call_stats["extra_requests"]) + 1
        for path in PROFILE_HANDLE_PATHS:
            value = dig(data, path)
            if isinstance(value, str) and value.strip():
                return strip_presentation_at(value.strip())
        log.warning("profile lookup for %s returned no handle field", user_id)
        return None

    # -------------------------------------------------------------- pagination
    async def _paginate(
        self,
        path: str,
        *,
        base_params: dict[str, str],
        video_id: str,
        video_url: str,
        parent_comment_id: str | None,
        label: str,
    ) -> AsyncIterator[CommentPage]:
        cursor: str | None = None
        source_order = 0

        while True:
            params = dict(base_params)
            if cursor is not None:
                params["cursor"] = cursor
            if self._settings.READER_TRIM:
                params["trim"] = "true"

            started = time.monotonic()
            payload = await self._get(path, params)
            log.debug("%s page read in %dms", label, int((time.monotonic() - started) * 1000))

            raw_list = dig_strict(payload, "comments")
            if raw_list is MISSING or raw_list is None:
                # Absent or null is a schema/API problem, NOT an empty section.
                raise ScrapeCreatorsError(
                    f"{path} response has no usable 'comments' list "
                    f"({'key absent' if raw_list is MISSING else 'value was null'}). "
                    "Refusing to treat that as a video without comments."
                )
            if not isinstance(raw_list, list):
                raise ScrapeCreatorsError(
                    f"{path} returned 'comments' of type {type(raw_list).__name__}, "
                    "expected a list"
                )

            comments: list[NormalizedComment] = []
            invalid = 0
            notes: list[str] = []
            for record in raw_list:
                normalized = self._normalize(
                    record,
                    video_id=video_id,
                    video_url=video_url,
                    parent_comment_id=parent_comment_id,
                    source_order=source_order,
                )
                if normalized is None:
                    invalid += 1
                    notes.append("record without a usable cid was skipped")
                    continue
                if not normalized.like_count_trusted:
                    invalid += 1
                    notes.append(f"comment {normalized.comment_id} has no valid digg_count")
                if not normalized.text_trusted:
                    notes.append(
                        f"comment {normalized.comment_id} has missing, null or "
                        "non-string text"
                    )
                comments.append(normalized)
                source_order += 1

            await self._fill_missing_usernames(comments)

            raw_has_more = payload.get("has_more", MISSING)
            parsed = None if raw_has_more is MISSING else parse_bool_flag(raw_has_more)
            completion_unknown = parsed is None
            if completion_unknown:
                if raw_has_more is MISSING:
                    notes.append("has_more marker absent from the response")
                else:
                    notes.append(f"has_more marker not recognised: {raw_has_more!r}")
            has_more = bool(parsed)

            next_cursor = coerce_id(payload.get("cursor"))
            total = safe_like_count(payload.get("total"))

            if parse_bool_flag(payload.get("alias_comment_deleted")) is True:
                notes.append("provider reported alias_comment_deleted=true")
            filtered = safe_like_count(payload.get("has_filtered_comments"))
            if filtered:
                notes.append(f"provider reported has_filtered_comments={filtered}")

            yield CommentPage(
                comments=comments,
                cursor_in=cursor,
                cursor_out=next_cursor,
                has_more=has_more,
                total_reported=total,
                parent_comment_id=parent_comment_id,
                provider_limits={
                    "credits_charged_this_process": self.call_stats.get("credits_charged"),
                    "credits_remaining_last_seen": self.call_stats.get("credits_remaining"),
                    "requests_this_process": self.call_stats.get("requests"),
                },
                invalid_records=invalid,
                completion_unknown=completion_unknown,
                notes=notes,
            )

            if completion_unknown or not has_more or not next_cursor:
                return
            if next_cursor == cursor:
                # Identical cursor twice would loop forever; the scanner also
                # detects this and reports it, so simply stop feeding it.
                return
            cursor = next_cursor

    async def iter_top_level_pages(
        self, video_id: str, video_url: str
    ) -> AsyncIterator[CommentPage]:
        if not video_url:
            raise ScrapeCreatorsError(
                "the comments endpoint documents a required 'url' parameter and the "
                "internal video id is not a substitute for it"
            )
        async for page in self._paginate(
            COMMENTS_PATH,
            base_params={"url": video_url},
            video_id=video_id,
            video_url=video_url,
            parent_comment_id=None,
            label="top_level",
        ):
            yield page

    async def iter_reply_pages(
        self, video_id: str, video_url: str, parent_comment_id: str
    ) -> AsyncIterator[CommentPage]:
        async for page in self._paginate(
            REPLIES_PATH,
            base_params={"url": video_url, "comment_id": parent_comment_id},
            video_id=video_id,
            video_url=video_url,
            parent_comment_id=parent_comment_id,
            label=f"replies:{parent_comment_id}",
        ):
            yield page

    async def recheck_comment(self, video_id: str, comment_id: str) -> bool | None:
        """No single-comment endpoint is documented, so existence is unverified.

        Returning None means "not supported" rather than "gone", so the worker
        does not treat the absence of a recheck as a missing comment.
        """
        return None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _snippet(text: str, limit: int = 160) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")
