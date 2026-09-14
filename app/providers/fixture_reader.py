"""Fixture-backed comment reader.

Reads normalized comment pages from JSON on disk. It exists so that the whole
pipeline - scanning, quantity calculation, the durable queue, the dashboard, the
SSE stream and the panel payload builder - can be exercised end to end without a
live TikTok data source.

It reports ``configured = False`` on purpose. Latency measured against it is
disk latency, NOT a statement about how fast TikTok can be read.

Fixture file layout (one file per video, named ``<video_id>.json``):

.. code-block:: json

    {
      "video_id": "7300000000000000001",
      "video_url": "https://www.tiktok.com/@creator/video/7300000000000000001",
      "pages": [
        {"has_more": true, "cursor_out": "c1", "comments": [
          {"comment_id": "1", "text": "first", "like_count": 12,
           "reply_count": 0, "owner_username": "someone", "owner_user_id": "u1"}
        ]},
        {"has_more": false, "cursor_out": null, "comments": []}
      ],
      "replies": {
        "1": [
          {"has_more": false, "cursor_out": null, "comments": [
            {"comment_id": "1-1", "text": "Mael Vorran is great",
             "like_count": 3, "owner_username": "reader", "owner_user_id": "u9"}
          ]}
        ]
      }
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..like_rules import safe_like_count
from ..models import CommentPage, NormalizedComment, ProviderError, utcnow
from .base import CommentReaderBase, ReaderCapabilities, strip_presentation_at

if TYPE_CHECKING:  # keeps the fixture reader importable without pydantic
    from ..config import Settings


class FixtureCommentReader(CommentReaderBase):
    name = "fixture"

    def __init__(self, settings: "Settings | None" = None, *, data: dict[str, Any] | None = None,
                 directory: str | Path | None = None) -> None:
        super().__init__()
        self._settings = settings
        self._inline = data
        self._provenance: str | None = (data or {}).get("provenance") if data else None
        self._directory = Path(
            directory
            if directory is not None
            else (settings.READER_FIXTURE_DIR if settings else "tests/fixtures/comments")
        )

    # --- capabilities -------------------------------------------------------
    @property
    def capabilities(self) -> ReaderCapabilities:
        return ReaderCapabilities(
            supports_replies=True,
            supplies_owner_username=True,
            supports_owner_lookup=False,
            supports_exact_counts=True,
            documented_ordering="fixture file order",
            max_page_size=50,
        )

    @property
    def configured(self) -> bool:
        return False

    @property
    def usable_for_dry_run(self) -> bool:
        return True

    @property
    def blocker(self) -> str | None:
        return (
            "The fixture reader is a local test double, not a live TikTok source. "
            "Live ordering stays disabled while it is selected."
        )

    @property
    def provider_limits(self) -> dict[str, Any]:
        return {
            "source": "local fixture files",
            "live": False,
            "provenance": self._provenance or "unmarked",
        }

    # --- data ---------------------------------------------------------------
    def _load(self, video_id: str) -> dict[str, Any]:
        if self._inline is not None:
            return self._inline
        file = self._directory / f"{video_id}.json"
        if not file.is_file():
            raise ProviderError(
                f"no fixture for video {video_id} at {file}", retryable=False
            )
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"fixture {file} is unreadable: {exc}") from exc

    def _build_page(
        self,
        raw_page: dict[str, Any],
        *,
        video_id: str,
        video_url: str,
        parent_comment_id: str | None,
        start_order: int,
    ) -> CommentPage:
        comments: list[NormalizedComment] = []
        invalid = 0
        for offset, record in enumerate(raw_page.get("comments", [])):
            likes = safe_like_count(record.get("like_count"))
            trusted = likes is not None
            if not trusted:
                invalid += 1
            username = record.get("owner_username")
            comments.append(
                NormalizedComment(
                    video_id=video_id,
                    video_url=video_url,
                    comment_id=str(record["comment_id"]),
                    parent_comment_id=(
                        str(record["parent_comment_id"])
                        if record.get("parent_comment_id")
                        else parent_comment_id
                    ),
                    text=record.get("text") if isinstance(record.get("text"), str) else "",
                    text_trusted=isinstance(record.get("text"), str),
                    like_count=likes if trusted else 0,
                    reply_count=record.get("reply_count"),
                    comment_owner_username=(
                        strip_presentation_at(str(username)) if username else None
                    ),
                    comment_owner_user_id=(
                        str(record["owner_user_id"]) if record.get("owner_user_id") else None
                    ),
                    source_order=start_order + offset,
                    fetched_at=utcnow(),
                    owner_username_source="comment_record" if username else "missing",
                    raw_owner_username=str(username) if username else None,
                    like_count_trusted=trusted,
                )
            )
        return CommentPage(
            comments=comments,
            cursor_in=raw_page.get("cursor_in"),
            cursor_out=raw_page.get("cursor_out"),
            has_more=bool(raw_page.get("has_more", False)),
            total_reported=raw_page.get("total"),
            parent_comment_id=parent_comment_id,
            provider_limits={
                "source": "fixture",
                "provenance": raw_page.get("provenance") or self._provenance or "unmarked",
            },
            invalid_records=invalid,
            notes=list(raw_page.get("notes") or []),
        )

    async def iter_top_level_pages(
        self, video_id: str, video_url: str
    ) -> AsyncIterator[CommentPage]:
        data = self._load(video_id)
        self._provenance = data.get("provenance") or self._provenance
        url = data.get("video_url", video_url)
        order = 0
        for raw_page in data.get("pages", []):
            # Fixtures make no network call, but they still charge the shared
            # budget so budget behaviour is testable without a live provider.
            self._charge_request()
            page = self._build_page(
                raw_page,
                video_id=video_id,
                video_url=url,
                parent_comment_id=None,
                start_order=order,
            )
            order += len(page.comments)
            yield page

    async def iter_reply_pages(
        self, video_id: str, video_url: str, parent_comment_id: str
    ) -> AsyncIterator[CommentPage]:
        data = self._load(video_id)
        url = data.get("video_url", video_url)
        order = 0
        for raw_page in (data.get("replies") or {}).get(parent_comment_id, []):
            self._charge_request()
            page = self._build_page(
                raw_page,
                video_id=video_id,
                video_url=url,
                parent_comment_id=parent_comment_id,
                start_order=order,
            )
            order += len(page.comments)
            yield page

    async def recheck_comment(self, video_id: str, comment_id: str) -> bool | None:
        try:
            data = self._load(video_id)
        except ProviderError:
            return None
        for raw_page in data.get("pages", []):
            for record in raw_page.get("comments", []):
                if str(record["comment_id"]) == comment_id:
                    return True
        for pages in (data.get("replies") or {}).values():
            for raw_page in pages:
                for record in raw_page.get("comments", []):
                    if str(record["comment_id"]) == comment_id:
                        return True
        return False
