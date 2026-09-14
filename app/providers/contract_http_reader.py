"""HTTP comment reader driven by an operator-supplied provider contract.

WHY IT IS SHAPED THIS WAY
=========================
This application needs comment text, exact comment ids, exact like counts,
replies, pagination and - critically - each commenter's real @handle. There is
no first-party TikTok API that provides that for a commercial project:

  * the Research API requires approved research access with eligibility criteria
    that include independence from commercial interests, so it is not available
    for this use;
  * the Display/Content Posting APIs cover the authenticated user's own content
    and do not expose other people's comment authors' handles.

So the reader has to be a third-party provider that YOU choose and hold an
account with. Rather than guess at that provider's endpoints and field names -
which would be fabrication - this adapter is complete but contract-driven: it
reads the request shape, pagination paths and field mapping from a JSON contract
you write from your provider's own documentation.

It refuses to run until the contract exists AND carries
``"verified_by_operator": true``, so an unverified provider can never be mistaken
for a working integration. See README.md, "The one thing still blocking Live
mode", for exactly what to send me or fill in.

The transport itself is real and finished: pooled keep-alive connections, the
provider's largest documented page size, cursor-based pagination, Retry-After
handling, bounded retries on safe reads only, and one cached owner lookup per
distinct owner id.
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
    ReaderContract,
    dig,
    dig_strict,
    parse_bool_flag,
    strip_presentation_at,
)

if TYPE_CHECKING:  # settings are injected, so no import is needed at runtime
    from ..config import Settings

log = logging.getLogger("app.reader")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_READ_ATTEMPTS = 3


class ContractHttpCommentReader(CommentReaderBase):
    name = "contract_http"

    def __init__(self, settings: "Settings", client: httpx.AsyncClient | None = None) -> None:
        super().__init__()
        self._settings = settings
        self._client = client
        self._contract: ReaderContract | None = None
        self._load_error: str | None = None
        self._owner_cache: dict[str, str | None] = {}

        if not settings.READER_BASE_URL:
            self._load_error = "READER_BASE_URL is not set"
        elif not settings.READER_CONTRACT_FILE:
            self._load_error = "READER_CONTRACT_FILE is not set"
        else:
            try:
                contract = ReaderContract.load(settings.READER_CONTRACT_FILE)
            except ProviderError as exc:
                self._load_error = str(exc)
            else:
                if not contract.verified_by_operator:
                    self._load_error = (
                        "reader contract is present but not marked "
                        '"verified_by_operator": true'
                    )
                else:
                    self._contract = contract

    # --- capabilities -------------------------------------------------------
    @property
    def capabilities(self) -> ReaderCapabilities:
        if self._contract is None:
            return ReaderCapabilities(
                supports_replies=False,
                supplies_owner_username=False,
                supports_owner_lookup=False,
                supports_exact_counts=False,
                documented_ordering="unknown",
                max_page_size=0,
            )
        return self._contract.as_capabilities()

    @property
    def configured(self) -> bool:
        return self._contract is not None

    @property
    def blocker(self) -> str | None:
        if self._contract is not None:
            return None
        return (
            f"Comment reader not configured: {self._load_error}. Live ordering is "
            "disabled until a verified provider contract is in place."
        )

    @property
    def provider_limits(self) -> dict[str, Any]:
        if self._contract is None:
            return {"configured": False, "reason": self._load_error}
        return {
            "provider": self._contract.provider_name,
            "documented_ordering": self._contract.documented_ordering,
            **self._contract.limits,
        }

    def _require(self) -> ReaderContract:
        if self._contract is None:
            raise ReaderUnconfigured(self.blocker or "reader not configured")
        return self._contract

    # --- transport ----------------------------------------------------------
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

    def _auth_headers(self) -> dict[str, str]:
        contract = self._require()
        style = str(contract.auth.get("style", "header")).lower()
        key = self._settings.READER_API_KEY
        if not key:
            raise ReaderUnconfigured("READER_API_KEY is not set")
        if style == "header":
            name = str(contract.auth.get("name") or self._settings.READER_API_KEY_HEADER)
            return {name: key}
        if style == "bearer":
            return {"authorization": f"Bearer {key}"}
        return {}

    def _auth_query(self) -> dict[str, str]:
        contract = self._require()
        if str(contract.auth.get("style", "header")).lower() == "query":
            return {str(contract.auth.get("name", "api_key")): self._settings.READER_API_KEY}
        return {}

    def _render_query(self, template: dict[str, Any], values: dict[str, str]) -> dict[str, str]:
        query: dict[str, str] = {}
        for name, raw in template.items():
            rendered = str(raw)
            for placeholder, value in values.items():
                rendered = rendered.replace("{" + placeholder + "}", value)
            if "{" in rendered and "}" in rendered:
                continue  # unresolved optional placeholder (for example {cursor})
            if rendered == "":
                continue
            query[name] = rendered
        query.update(self._auth_query())
        return query

    async def _request(self, spec: dict[str, Any], values: dict[str, str]) -> Any:
        contract = self._require()
        url = self._settings.READER_BASE_URL + str(spec["path"])
        method = str(spec.get("method", "GET")).upper()
        query = self._render_query(dict(spec.get("query") or {}), values)
        headers = self._auth_headers()
        client = self._http()

        last_error: str | None = None
        for attempt in range(1, MAX_READ_ATTEMPTS + 1):
            # Charged before the call, once per attempt: see RequestBudget.
            self._charge_request()
            try:
                response = await client.request(method, url, params=query, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == MAX_READ_ATTEMPTS:
                    raise ProviderError(
                        f"reader request failed after {attempt} attempts: {last_error}",
                        retryable=True,
                    ) from exc
                await self._sleep_backoff(attempt, None)
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}"
                if attempt == MAX_READ_ATTEMPTS:
                    raise ProviderError(
                        f"reader returned {last_error} after {attempt} attempts",
                        retryable=True,
                        status=response.status_code,
                    )
                await self._sleep_backoff(attempt, response.headers.get("retry-after"))
                continue

            if response.status_code in (401, 403):
                raise ProviderError(
                    f"reader rejected the credentials (HTTP {response.status_code}). "
                    "This is an access blocker, not something to work around.",
                    retryable=False,
                    status=response.status_code,
                )
            if response.status_code >= 400:
                raise ProviderError(
                    f"reader returned HTTP {response.status_code}: {response.text[:200]!r}",
                    retryable=False,
                    status=response.status_code,
                )
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderError(f"reader returned a non-JSON body: {exc}") from exc

        raise ProviderError(last_error or "reader request failed")  # pragma: no cover

    async def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = 0.5 * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, 10.0)
        self.call_stats["retry_seconds"] = float(self.call_stats["retry_seconds"]) + delay
        await asyncio.sleep(delay)

    # --- normalization ------------------------------------------------------
    def _normalize(
        self,
        record: Any,
        *,
        video_id: str,
        video_url: str,
        parent_comment_id: str | None,
        source_order: int,
    ) -> NormalizedComment | None:
        contract = self._require()
        fields = contract.fields
        comment_id = dig(record, fields["comment_id"])
        if comment_id in (None, ""):
            return None
        likes = safe_like_count(dig(record, fields["like_count"]))
        trusted = likes is not None

        raw_username = None
        if fields.get("owner_username"):
            raw_username = dig(record, fields["owner_username"])
        owner_user_id = None
        if fields.get("owner_user_id"):
            value = dig(record, fields["owner_user_id"])
            owner_user_id = None if value in (None, "") else str(value)

        parent_from_record = None
        if fields.get("parent_comment_id"):
            value = dig(record, fields["parent_comment_id"])
            if value not in (None, "", "0"):
                parent_from_record = str(value)

        reply_count = None
        if fields.get("reply_count"):
            reply_count = safe_like_count(dig(record, fields["reply_count"]))

        username = (
            strip_presentation_at(str(raw_username)) if raw_username not in (None, "") else None
        )
        return NormalizedComment(
            video_id=video_id,
            video_url=video_url,
            comment_id=str(comment_id),
            parent_comment_id=parent_from_record or parent_comment_id,
            text=_text_of(record, fields["text"]),
            text_trusted=isinstance(dig(record, fields["text"]), str),
            like_count=likes if trusted else 0,
            reply_count=reply_count,
            comment_owner_username=username,
            comment_owner_user_id=owner_user_id,
            source_order=source_order,
            fetched_at=utcnow(),
            owner_username_source="comment_record" if username else "missing",
            raw_owner_username=str(raw_username) if raw_username not in (None, "") else None,
            like_count_trusted=trusted,
        )

    async def _fill_missing_usernames(self, comments: list[NormalizedComment]) -> None:
        """One cached lookup per distinct owner id; never a profile page visit."""
        contract = self._require()
        if not contract.owner_lookup:
            return
        pending = {
            comment.comment_owner_user_id
            for comment in comments
            if comment.comment_owner_username is None and comment.comment_owner_user_id
        }
        for user_id in pending:
            if user_id in self._owner_cache:
                continue
            data = await self._request(contract.owner_lookup, {"user_id": user_id})
            self.call_stats["owner_lookups"] = int(self.call_stats["owner_lookups"]) + 1
            self.call_stats["extra_requests"] = int(self.call_stats["extra_requests"]) + 1
            handle = dig(data, str(contract.owner_lookup.get("username_path", "")))
            self._owner_cache[user_id] = (
                strip_presentation_at(str(handle)) if handle not in (None, "") else None
            )
        for comment in comments:
            if comment.comment_owner_username is None and comment.comment_owner_user_id:
                resolved = self._owner_cache.get(comment.comment_owner_user_id)
                if resolved:
                    comment.comment_owner_username = resolved
                    comment.owner_username_source = "owner_id_lookup"
                    comment.raw_owner_username = resolved

    async def _paginate(
        self,
        spec: dict[str, Any],
        *,
        video_id: str,
        video_url: str,
        parent_comment_id: str | None,
        extra_values: dict[str, str],
    ) -> AsyncIterator[CommentPage]:
        contract = self._require()
        page_size = min(
            self._settings.READER_PAGE_SIZE, contract.as_capabilities().max_page_size or 50
        )
        cursor: str | None = None
        source_order = 0
        list_path = str(spec.get("list_path", ""))
        cursor_path = str(spec.get("cursor_path", ""))
        has_more_path = str(spec.get("has_more_path", ""))
        total_path = str(spec.get("total_path", ""))

        while True:
            values = {
                "video_id": video_id,
                "video_url": video_url,
                "page_size": str(page_size),
                "cursor": cursor or "",
                **extra_values,
            }
            started = time.monotonic()
            data = await self._request(spec, values)
            log.debug("reader page in %dms", int((time.monotonic() - started) * 1000))

            raw_list = dig_strict(data, list_path)
            if raw_list is MISSING or raw_list is None:
                # Absent or null is a schema/API error. Only an explicit empty
                # list is a legitimately empty page.
                raise ProviderError(
                    f"reader list_path {list_path!r} is "
                    f"{'absent' if raw_list is MISSING else 'null'}; that is a schema "
                    "or API error, not an empty comment section"
                )
            if not isinstance(raw_list, list):
                raise ProviderError(f"reader list_path {list_path!r} is not a list")

            comments: list[NormalizedComment] = []
            invalid = 0
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
                    continue
                if not normalized.like_count_trusted:
                    invalid += 1
                comments.append(normalized)
                source_order += 1

            await self._fill_missing_usernames(comments)

            next_cursor_value = dig(data, cursor_path) if cursor_path else None
            next_cursor = None if next_cursor_value in (None, "") else str(next_cursor_value)

            # Strict parsing: the strings "false" and "0" are truthy in Python,
            # so bool() on a raw marker would read them as "more data". An
            # unrecognised marker is reported as unknown, never as completion.
            completion_unknown = False
            if has_more_path:
                parsed = parse_bool_flag(dig_strict(data, has_more_path))
                if parsed is None:
                    completion_unknown = True
                    has_more = False
                else:
                    has_more = parsed
            else:
                # No marker configured at all: the presence of a next cursor is
                # the only signal available, and its absence is not proof.
                has_more = bool(next_cursor)
                completion_unknown = not next_cursor
            total_value = dig(data, total_path) if total_path else None

            yield CommentPage(
                comments=comments,
                cursor_in=cursor,
                cursor_out=next_cursor,
                has_more=has_more,
                total_reported=safe_like_count(total_value) if total_value is not None else None,
                parent_comment_id=parent_comment_id,
                provider_limits=dict(contract.limits),
                invalid_records=invalid,
                completion_unknown=completion_unknown,
            )

            if completion_unknown or not has_more or not next_cursor:
                return
            if next_cursor == cursor:
                return
            cursor = next_cursor

    async def iter_top_level_pages(
        self, video_id: str, video_url: str
    ) -> AsyncIterator[CommentPage]:
        contract = self._require()
        async for page in self._paginate(
            contract.top_level,
            video_id=video_id,
            video_url=video_url,
            parent_comment_id=None,
            extra_values={},
        ):
            yield page

    async def iter_reply_pages(
        self, video_id: str, video_url: str, parent_comment_id: str
    ) -> AsyncIterator[CommentPage]:
        contract = self._require()
        if not contract.replies:
            raise ProviderError("reader contract declares no replies endpoint")
        async for page in self._paginate(
            contract.replies,
            video_id=video_id,
            video_url=video_url,
            parent_comment_id=parent_comment_id,
            extra_values={"parent_id": parent_comment_id},
        ):
            yield page

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _text_of(record: Any, path: str) -> str:
    """Comment text, or an empty placeholder when the provider omitted it.

    ``text_trusted`` on the model records which of the two happened, so the
    scanner can refuse to claim it found the FIRST match when an earlier
    comment's text was unreadable.
    """
    value = dig(record, path)
    return value if isinstance(value, str) else ""
