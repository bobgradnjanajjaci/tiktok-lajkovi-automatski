"""Tests for the ScrapeCreators comment reader.

PROVENANCE OF THE SAMPLE DATA BELOW
    The envelope and comment-record shapes are copied from ScrapeCreators'
    published sample responses for ``/v1/tiktok/video/comments`` and
    ``/v1/tiktok/video/comment/replies`` (documentation, read while writing the
    adapter), then trimmed to the fields this application consumes and edited so
    the text, handles and ids suit each scenario. They are DOCUMENTATION-DERIVED
    SYNTHETIC data, not captured live responses, and passing these tests is not
    evidence that a real account works.

Every request here goes to an ``httpx.MockTransport``. No network call is made
and no order can be created from this module.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.comment_finder import ScanLimits, scan_video
from app.config import Settings
from app.models import CommentScope, ProviderError, ScanStatus
from app.providers import build_reader, reader_status
from app.providers.scrapecreators_reader import (
    COMMENTS_PATH,
    PROFILE_PATH,
    REPLIES_PATH,
    ScrapeCreatorsCommentReader,
)

VIDEO_ID = "7463250363559218474"
VIDEO_URL = "https://www.tiktok.com/@amici/video/7463250363559218474"


def settings_for(tmp_path, **overrides) -> Settings:
    values = {
        "ENVIRONMENT": "test",
        "COMMENT_READER": "scrapecreators",
        "READER_BASE_URL": "https://api.scrapecreators.example",
        "READER_API_KEY": "reader-test-key",
        "READER_CONTRACT_FILE": "",
        "DATABASE_PATH": str(tmp_path / "app.db"),
        "ADMIN_PASSWORD": "test-password-1234",
        "SESSION_SECRET": "y" * 48,
    }
    values.update(overrides)
    return Settings(**values)


def comment(
    cid: str,
    text: str,
    likes,
    *,
    handle: str | None = "someone",
    uid: str | None = "6851091024770040837",
    replies: int = 0,
    reply_id: str = "0",
    drop_cid: bool = False,
    drop_likes: bool = False,
):
    """One record in the documented shape, trimmed to the consumed fields."""
    record = {
        "aweme_id": VIDEO_ID,
        "cid": cid,
        "create_time": 1737679448,
        "digg_count": likes,
        "reply_comment_total": replies,
        "reply_id": reply_id,
        "reply_to_reply_id": "0",
        "status": 1,
        "text": text,
        "user": {"nickname": "Display Name", "uid": uid, "unique_id": handle},
    }
    if drop_cid:
        del record["cid"]
    if drop_likes:
        del record["digg_count"]
    if handle is None:
        del record["user"]["unique_id"]
    if uid is None:
        del record["user"]["uid"]
    return record


def envelope(comments, *, cursor=20, has_more=1, total=None, **extra):
    body = {
        "success": True,
        "credits_remaining": 999,
        "credits_charged": 1,
        "comments": comments,
        "cursor": cursor,
        "has_more": has_more,
        "status_code": 0,
        "status_msg": "",
    }
    if total is not None:
        body["total"] = total
    body.update(extra)
    return body


class Recorder:
    """MockTransport that records every request and replies from a script."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.counts: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        index = self.counts.get(path, 0)
        self.counts[path] = index + 1
        script = self.routes.get(path)
        if script is None:
            return httpx.Response(404, json={"success": False, "message": "no route"})
        if callable(script):
            return script(request, index)
        entry = script[min(index, len(script) - 1)]
        if isinstance(entry, httpx.Response):
            return entry
        return httpx.Response(200, json=entry)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def queries_for(self, path: str) -> list[dict[str, str]]:
        return [
            dict(request.url.params)
            for request in self.requests
            if request.url.path == path
        ]


def reader_with(tmp_path, routes, **overrides):
    recorder = Recorder(routes)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    settings = settings_for(tmp_path, **overrides)
    return ScrapeCreatorsCommentReader(settings, client), recorder, settings


async def run_scan(reader, settings, *, scope=CommentScope.ALL, limits=None):
    return await scan_video(
        reader,
        video_id=VIDEO_ID,
        video_url=VIDEO_URL,
        keyword=settings.KEYWORD,
        requested_scope=scope,
        limits=limits
        or ScanLimits(trust_provider_reply_end=settings.SCAN_TRUST_PROVIDER_REPLY_END),
    )


# --------------------------------------------------------------- configuration
def test_reader_is_selected_by_name(tmp_path):
    settings = settings_for(tmp_path)
    reader = build_reader(settings)
    assert reader.name == "scrapecreators"
    assert reader.configured is True


def test_missing_key_blocks_without_falling_back_to_fixtures(tmp_path):
    settings = settings_for(tmp_path, READER_API_KEY="")
    reader = build_reader(settings)
    assert reader.name == "scrapecreators"  # never silently the fixture reader
    assert reader.configured is False
    assert reader.usable_for_dry_run is False
    assert "READER_API_KEY" in (reader.blocker or "")

    status = reader_status(settings)
    assert status["live_capable"] is False
    assert status["links"]["signup"].startswith("https://")


def test_status_reports_real_capabilities(tmp_path):
    status = reader_status(settings_for(tmp_path))
    assert status["supports_replies"] is True
    assert status["supplies_owner_username"] is True
    assert status["configured"] is True
    assert "stability" in status["documented_ordering"]


def test_settings_reject_an_unknown_reader(tmp_path):
    with pytest.raises(Exception):
        settings_for(tmp_path, COMMENT_READER="magic")


# ------------------------------------------------------------- request shaping
@pytest.mark.asyncio
async def test_documented_request_shape_and_auth_header(tmp_path):
    routes = {
        COMMENTS_PATH: [envelope([comment("1", "hello", 5)], has_more=0, cursor=20)],
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)

    request = recorder.requests[0]
    assert request.method == "GET"
    assert request.url.path == COMMENTS_PATH
    assert request.headers["x-api-key"] == "reader-test-key"
    params = dict(request.url.params)
    # url is the documented required parameter; the internal id is not a
    # substitute for it, and no undocumented parameter is invented.
    assert params["url"] == VIDEO_URL
    assert "cursor" not in params  # first page carries no cursor
    for undocumented in ("video_id", "count", "page_size", "limit", "trim"):
        assert undocumented not in params


@pytest.mark.asyncio
async def test_trim_is_only_sent_when_enabled(tmp_path):
    routes = {COMMENTS_PATH: [envelope([comment("1", "x", 1)], has_more=0)]}
    reader, recorder, settings = reader_with(tmp_path, routes, READER_TRIM=True)
    await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert dict(recorder.requests[0].url.params)["trim"] == "true"


@pytest.mark.asyncio
async def test_replies_use_comment_id_and_url(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope([comment("p1", "parent", 2, replies=1)], has_more=0, cursor=20)
        ],
        REPLIES_PATH: [
            envelope(
                [comment("r1", "a reply", 3, reply_id="p1")], has_more=0, cursor=3
            )
        ],
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings)

    reply_params = recorder.queries_for(REPLIES_PATH)[0]
    assert reply_params["comment_id"] == "p1"
    assert reply_params["url"] == VIDEO_URL
    assert result.status is ScanStatus.COMPLETE
    assert result.observed_top_likes == 3


# ------------------------------------------------------------ field normalizing
@pytest.mark.asyncio
async def test_fields_map_to_the_normalized_model(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment(
                        "7463276288959824682",
                        "read Mael Vorran tonight",
                        1015,
                        handle="tmoneyhoney18",
                        uid="6851091024770040837",
                        replies=0,
                    )
                ],
                has_more=0,
                total=269,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)

    target = result.target
    assert target is not None
    assert target.comment_id == "7463276288959824682"
    assert isinstance(target.comment_id, str)
    assert target.like_count == 1015
    assert target.comment_owner_username == "tmoneyhoney18"
    assert target.comment_owner_user_id == "6851091024770040837"
    assert target.parent_comment_id is None  # reply_id "0" means top level
    assert target.owner_username_source == "comment_record"
    assert result.stats.owner_lookups == 0  # handle came from the comment record


@pytest.mark.asyncio
async def test_reply_id_becomes_the_parent_comment_id(tmp_path):
    routes = {
        COMMENTS_PATH: [envelope([comment("p1", "parent", 1, replies=1)], has_more=0)],
        REPLIES_PATH: [
            envelope(
                [comment("r1", "Mael Vorran reply", 1, reply_id="p1")],
                has_more=0,
            )
        ],
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings)
    assert result.target is not None
    assert result.target.parent_comment_id == "p1"


@pytest.mark.asyncio
async def test_record_without_cid_is_dropped_not_invented(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "fine", 5),
                    comment("ignored", "broken record", 900, drop_cid=True),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)

    # The record is not given a synthesized id, and its like count is not used.
    assert result.stats.unique_comments == 1
    assert result.observed_top_likes == 5
    assert result.status is ScanStatus.INCOMPLETE
    assert VIDEO_ID not in [note for note in result.incomplete_reasons]


@pytest.mark.asyncio
async def test_missing_like_count_is_not_zero(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "Mael Vorran", 7),
                    comment("2", "no count", None, drop_likes=True),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.observed_top_likes == 7
    assert result.status is ScanStatus.INCOMPLETE
    assert "invalid_like_counts_present" in result.incomplete_reasons


@pytest.mark.asyncio
async def test_formatted_like_count_is_rejected(tmp_path):
    routes = {COMMENTS_PATH: [envelope([comment("1", "x", "1.2K")], has_more=0)]}
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.observed_top_likes is None
    assert result.status is ScanStatus.INCOMPLETE


# ------------------------------------------------------------------ pagination
@pytest.mark.asyncio
async def test_multiple_pages_are_followed_with_the_echoed_cursor(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope([comment("1", "nothing", 4)], cursor=20, has_more=1),
            envelope([comment("2", "Mael Vorran here", 9)], cursor=40, has_more=1),
            envelope([comment("3", "louder", 900)], cursor=60, has_more=0),
        ]
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)

    queries = recorder.queries_for(COMMENTS_PATH)
    assert "cursor" not in queries[0]
    assert queries[1]["cursor"] == "20"
    assert queries[2]["cursor"] == "40"
    assert result.status is ScanStatus.COMPLETE
    assert result.target.comment_id == "2"  # first match, later page
    assert result.observed_top_likes == 900  # maximum found after the match


@pytest.mark.asyncio
async def test_integer_zero_ends_the_stream(tmp_path):
    routes = {COMMENTS_PATH: [envelope([comment("1", "x", 1)], has_more=0)]}
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert len(recorder.queries_for(COMMENTS_PATH)) == 1
    assert result.status is ScanStatus.COMPLETE


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["false", "0", False, 0])
async def test_falsey_string_markers_are_not_read_as_more_data(tmp_path, marker):
    routes = {COMMENTS_PATH: [envelope([comment("1", "x", 1)], has_more=marker)]}
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert len(recorder.queries_for(COMMENTS_PATH)) == 1
    assert result.status is ScanStatus.COMPLETE


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["true", "1", True, 1])
async def test_truthy_markers_continue_the_stream(tmp_path, marker):
    routes = {
        COMMENTS_PATH: [
            envelope([comment("1", "x", 1)], has_more=marker, cursor=20),
            envelope([comment("2", "y", 2)], has_more=0, cursor=40),
        ]
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert len(recorder.queries_for(COMMENTS_PATH)) == 2
    assert result.status is ScanStatus.COMPLETE


@pytest.mark.asyncio
async def test_unknown_marker_is_incomplete_not_finished(tmp_path):
    routes = {COMMENTS_PATH: [envelope([comment("1", "x", 1)], has_more="maybe")]}
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.INCOMPLETE
    assert any("unknown_completion_marker" in r for r in result.incomplete_reasons)


@pytest.mark.asyncio
async def test_absent_marker_is_incomplete(tmp_path):
    body = envelope([comment("1", "x", 1)], has_more=0)
    del body["has_more"]
    reader, _, settings = reader_with(tmp_path, {COMMENTS_PATH: [body]})
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.INCOMPLETE
    assert any("unknown_completion_marker" in r for r in result.incomplete_reasons)


@pytest.mark.asyncio
async def test_repeated_cursor_stops_and_reports(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope([comment("1", "x", 1)], cursor=20, has_more=1),
            envelope([comment("2", "y", 2)], cursor=20, has_more=1),
        ]
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.INCOMPLETE
    assert len(recorder.queries_for(COMMENTS_PATH)) <= 2


@pytest.mark.asyncio
async def test_has_more_without_a_cursor_is_incomplete(tmp_path):
    body = envelope([comment("1", "x", 1)], has_more=1)
    del body["cursor"]
    reader, _, settings = reader_with(tmp_path, {COMMENTS_PATH: [body]})
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.INCOMPLETE


@pytest.mark.asyncio
async def test_explicit_empty_list_is_a_valid_page(tmp_path):
    routes = {COMMENTS_PATH: [envelope([], has_more=0, cursor=0)]}
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.COMPLETE
    assert result.target is None
    assert result.observed_top_likes is None


@pytest.mark.asyncio
async def test_page_budget_marks_incomplete(tmp_path):
    def always_more(request, index):
        return httpx.Response(
            200,
            json=envelope([comment(f"c{index}", "x", index)], cursor=(index + 1) * 20),
        )

    reader, _, settings = reader_with(tmp_path, {COMMENTS_PATH: always_more})
    result = await run_scan(
        reader, settings, scope=CommentScope.TOP_LEVEL, limits=ScanLimits(max_pages=3)
    )
    assert result.status is ScanStatus.INCOMPLETE
    assert any("max_pages_reached" in r for r in result.incomplete_reasons)


# ---------------------------------------------------------------- reply threads
SHORT_THREAD_ROUTES = {
    COMMENTS_PATH: [envelope([comment("p1", "parent", 1, replies=5)], has_more=0)],
    REPLIES_PATH: [envelope([comment("r1", "only one", 2, reply_id="p1")], has_more=0)],
}


@pytest.mark.asyncio
async def test_reply_shortfall_is_incomplete_by_default(tmp_path):
    """Default is strict: a declared end does not settle a reply-count gap."""
    reader, _, settings = reader_with(tmp_path, dict(SHORT_THREAD_ROUTES))
    assert settings.SCAN_TRUST_PROVIDER_REPLY_END is False
    result = await run_scan(reader, settings)
    assert result.status is ScanStatus.INCOMPLETE
    assert any("truncated_replies" in r for r in result.incomplete_reasons)


@pytest.mark.asyncio
async def test_explicit_opt_in_labels_the_narrowed_completeness(tmp_path):
    reader, _, settings = reader_with(
        tmp_path, dict(SHORT_THREAD_ROUTES), SCAN_TRUST_PROVIDER_REPLY_END=True
    )
    result = await run_scan(reader, settings)
    assert result.status is ScanStatus.COMPLETE
    assert any("reply_count_mismatch" in e for e in result.scope_limitations)
    assert result.as_json()["completeness_basis"].startswith("provider-visible")


@pytest.mark.asyncio
async def test_reply_thread_still_reporting_more_is_always_incomplete(tmp_path):
    routes = {
        COMMENTS_PATH: [envelope([comment("p1", "parent", 1, replies=2)], has_more=0)],
        REPLIES_PATH: [
            # has_more stays 1 but no cursor is offered: cannot continue.
            {
                "success": True,
                "comments": [comment("r1", "one", 1, reply_id="p1")],
                "has_more": 1,
                "status_code": 0,
            }
        ],
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings)
    assert result.status is ScanStatus.INCOMPLETE


@pytest.mark.asyncio
async def test_a_reply_can_hold_the_maximum_under_scope_all(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [comment("p1", "Mael Vorran in the parent", 10, replies=1)], has_more=0
            )
        ],
        REPLIES_PATH: [
            envelope(
                [comment("r1", "unrelated", 4000, handle="other", uid="9", reply_id="p1")],
                has_more=0,
            )
        ],
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings)
    assert result.target.comment_id == "p1"
    assert result.observed_top_likes == 4000
    assert result.status is ScanStatus.COMPLETE


# --------------------------------------------------------------- owner identity
@pytest.mark.asyncio
async def test_missing_handle_triggers_one_cached_profile_lookup(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "Mael Vorran", 3, handle=None, uid="42"),
                    comment("2", "same author again", 4, handle=None, uid="42"),
                ],
                has_more=0,
            )
        ],
        PROFILE_PATH: [{"success": True, "user": {"uniqueId": "resolved.handle"}}],
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)

    assert len(recorder.queries_for(PROFILE_PATH)) == 1  # one per distinct owner id
    assert recorder.queries_for(PROFILE_PATH)[0]["user_id"] == "42"
    assert recorder.queries_for(PROFILE_PATH)[0]["cache_max_age"] == "7d"
    assert result.target.comment_owner_username == "resolved.handle"
    assert result.target.owner_username_source == "profile_lookup_by_user_id"
    assert result.stats.owner_lookups == 1
    assert result.stats.profile_navigations == 0  # no profile page was opened


@pytest.mark.asyncio
async def test_no_lookup_happens_when_handles_are_present(tmp_path):
    routes = {
        COMMENTS_PATH: [envelope([comment("1", "Mael Vorran", 3)], has_more=0)],
        PROFILE_PATH: [{"success": True, "user": {"uniqueId": "should.not.be.called"}}],
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert recorder.queries_for(PROFILE_PATH) == []


@pytest.mark.asyncio
async def test_a_second_comment_by_the_same_owner_without_the_keyword_is_ambiguous(tmp_path):
    """The panel gets video + username, so any second comment matters."""
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "Mael Vorran is great", 3, handle="reader1", uid="11"),
                    comment("2", "just a normal comment", 8, handle="reader1", uid="11"),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.target.comment_id == "1"
    assert result.target_ambiguous is True
    assert result.owner_duplicate_comment_ids == ["1", "2"]


@pytest.mark.asyncio
async def test_an_earlier_comment_by_the_same_owner_also_counts(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "warming up", 2, handle="reader1", uid="11"),
                    comment("2", "Mael Vorran mentioned", 3, handle="reader1", uid="11"),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.target.comment_id == "2"
    assert result.target_ambiguous is True


@pytest.mark.asyncio
async def test_owner_identity_is_matched_by_user_id_not_display_name(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "Mael Vorran", 3, handle="reader1", uid="11"),
                    comment("2", "different account", 9, handle="reader2", uid="22"),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.target_ambiguous is False
    assert result.observed_top_likes == 9


@pytest.mark.asyncio
async def test_a_duplicate_cid_is_not_a_second_comment(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope([comment("1", "Mael Vorran", 3, uid="11")], cursor=20, has_more=1),
            envelope([comment("1", "Mael Vorran", 3, uid="11")], cursor=40, has_more=0),
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.stats.duplicates_dropped == 1
    assert result.target_ambiguous is False


# -------------------------------------------------------------- error handling
@pytest.mark.asyncio
async def test_api_error_served_with_http_200_is_a_failure(tmp_path):
    routes = {
        COMMENTS_PATH: [
            {"success": False, "message": "invalid url", "comments": [], "has_more": 0}
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.INCOMPLETE
    assert any("provider_error" in r for r in result.incomplete_reasons)
    assert result.target is None


@pytest.mark.asyncio
async def test_nonzero_status_code_is_a_failure(tmp_path):
    routes = {
        COMMENTS_PATH: [
            {"comments": [], "has_more": 0, "status_code": 5, "status_msg": "blocked"}
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    with pytest.raises(ProviderError):
        async for _ in reader.iter_top_level_pages(VIDEO_ID, VIDEO_URL):
            pass


@pytest.mark.asyncio
async def test_missing_comments_key_is_a_schema_error_not_an_empty_video(tmp_path):
    routes = {COMMENTS_PATH: [{"success": True, "has_more": 0, "cursor": 0}]}
    reader, _, settings = reader_with(tmp_path, routes)
    with pytest.raises(ProviderError) as info:
        async for _ in reader.iter_top_level_pages(VIDEO_ID, VIDEO_URL):
            pass
    assert "comments" in str(info.value)


@pytest.mark.asyncio
async def test_null_comments_is_a_schema_error(tmp_path):
    routes = {COMMENTS_PATH: [{"success": True, "comments": None, "has_more": 0}]}
    reader, _, settings = reader_with(tmp_path, routes)
    with pytest.raises(ProviderError):
        async for _ in reader.iter_top_level_pages(VIDEO_ID, VIDEO_URL):
            pass


@pytest.mark.asyncio
async def test_html_body_is_reported_as_a_provider_failure(tmp_path):
    routes = {
        COMMENTS_PATH: [
            httpx.Response(200, text="<html><body>blocked</body></html>",
                           headers={"content-type": "text/html"})
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    with pytest.raises(ProviderError) as info:
        async for _ in reader.iter_top_level_pages(VIDEO_ID, VIDEO_URL):
            pass
    assert "HTML" in str(info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_bad_credentials_are_reported_not_worked_around(tmp_path, status):
    routes = {COMMENTS_PATH: [httpx.Response(status, json={"error": "nope"})]}
    reader, recorder, settings = reader_with(tmp_path, routes)
    with pytest.raises(ProviderError) as info:
        async for _ in reader.iter_top_level_pages(VIDEO_ID, VIDEO_URL):
            pass
    assert "READER_API_KEY" in str(info.value)
    assert len(recorder.requests) == 1  # no retry loop on an auth failure


@pytest.mark.asyncio
async def test_out_of_credits_is_explicit_and_not_retried(tmp_path):
    routes = {COMMENTS_PATH: [httpx.Response(402, json={"error": "no credits"})]}
    reader, recorder, settings = reader_with(tmp_path, routes)
    with pytest.raises(ProviderError) as info:
        async for _ in reader.iter_top_level_pages(VIDEO_ID, VIDEO_URL):
            pass
    assert "credits" in str(info.value).lower()
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_not_found_is_not_an_empty_comment_section(tmp_path):
    routes = {COMMENTS_PATH: [httpx.Response(404, json={"error": "not found"})]}
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.INCOMPLETE
    assert result.target is None
    assert result.observed_top_likes is None


@pytest.mark.asyncio
async def test_rate_limit_is_retried_with_retry_after(tmp_path, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.providers.scrapecreators_reader.asyncio.sleep", fake_sleep)
    routes = {
        COMMENTS_PATH: [
            httpx.Response(429, headers={"retry-after": "2"}, json={"error": "slow"}),
            envelope([comment("1", "x", 1)], has_more=0),
        ]
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert slept == [2.0]
    assert result.status is ScanStatus.COMPLETE
    assert result.stats.retry_seconds == 2.0
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
async def test_repeated_server_errors_give_up_after_the_bounded_attempts(tmp_path, monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("app.providers.scrapecreators_reader.asyncio.sleep", fake_sleep)
    routes = {COMMENTS_PATH: lambda request, index: httpx.Response(500, json={})}
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.INCOMPLETE
    assert len(recorder.requests) == 3


@pytest.mark.asyncio
async def test_timeout_is_not_an_empty_section(tmp_path, monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("app.providers.scrapecreators_reader.asyncio.sleep", fake_sleep)

    def raise_timeout(request):
        raise httpx.ReadTimeout("too slow", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(raise_timeout))
    settings = settings_for(tmp_path)
    reader = ScrapeCreatorsCommentReader(settings, client)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.INCOMPLETE
    assert result.target is None


# ------------------------------------------------------------ credit reporting
@pytest.mark.asyncio
async def test_real_credit_usage_is_recorded_not_estimated(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope([comment("1", "x", 1)], cursor=20, has_more=1, credits_charged=1,
                     credits_remaining=500),
            envelope([comment("2", "y", 2)], cursor=40, has_more=0, credits_charged=2,
                     credits_remaining=498),
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert reader.call_stats["credits_charged"] == 3
    assert reader.call_stats["credits_remaining"] == 498
    assert result.provider_limits["credits_charged_this_process"] == 3
    assert result.provider_limits["credits_remaining_last_seen"] == 498


# ------------------------------------------------------------- text integrity
def broken_text_record(kind: str) -> dict:
    """A record whose text cannot be read, in each way a provider can break it."""
    record = comment("1", "placeholder", 5, handle="a", uid="11")
    if kind == "absent":
        del record["text"]
    elif kind == "null":
        record["text"] = None
    elif kind == "number":
        record["text"] = 12
    elif kind == "object":
        record["text"] = {"content": "hidden"}
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["absent", "null", "number", "object"])
async def test_unreadable_text_is_not_silently_an_empty_string(tmp_path, kind):
    """Missing, null or non-string text must not be coerced to "".

    Coercing it made an earlier unreadable comment look like a comment that
    simply did not contain the keyword, so a later match was reported as "the
    first match" on a scan that called itself complete.
    """
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    broken_text_record(kind),
                    comment("2", "Mael Vorran", 3, handle="b", uid="22"),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)

    assert result.status is ScanStatus.INCOMPLETE
    assert "unreadable_comment_text" in result.incomplete_reasons
    assert result.complete is False
    assert result.stats.unreadable_texts == 1
    # The record is kept for diagnostics, not dropped.
    assert result.stats.unique_comments == 2


@pytest.mark.asyncio
async def test_an_explicitly_empty_text_is_trusted(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "", 5, handle="a", uid="11"),
                    comment("2", "Mael Vorran", 3, handle="b", uid="22"),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.status is ScanStatus.COMPLETE
    assert result.target.comment_id == "2"
    assert result.stats.unreadable_texts == 0


# ------------------------------------------------------------ identity mixing
@pytest.mark.asyncio
async def test_a_handle_only_record_is_the_same_account_as_a_uid_record(tmp_path):
    """The panel targets by username, so partial identity is not another account."""
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "Mael Vorran", 50, handle="same.owner", uid="101"),
                    comment("2", "ordinary text", 50, handle="same.owner", uid=None),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.target.comment_id == "1"
    assert result.target_ambiguous is True
    assert result.owner_duplicate_comment_ids == ["1", "2"]


@pytest.mark.asyncio
async def test_two_owner_ids_behind_one_handle_are_reported_as_unsettled(tmp_path):
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "Mael Vorran", 50, handle="shared", uid="101"),
                    comment("2", "ordinary", 50, handle="shared", uid="202"),
                ],
                has_more=0,
            )
        ]
    }
    reader, _, settings = reader_with(tmp_path, routes)
    result = await run_scan(reader, settings, scope=CommentScope.TOP_LEVEL)
    assert result.target_ambiguous is True
    assert "owner_identity_conflict" in result.incomplete_reasons


# --------------------------------------------------------------- scan budget
@pytest.mark.asyncio
async def test_profile_lookups_cannot_exceed_the_scan_request_budget(tmp_path):
    """One page plus three lookups must not fit inside a budget of two."""
    routes = {
        COMMENTS_PATH: [
            envelope(
                [
                    comment("1", "Mael Vorran", 50, handle=None, uid="101"),
                    comment("2", "other", 50, handle=None, uid="102"),
                    comment("3", "other", 50, handle=None, uid="103"),
                ],
                has_more=0,
            )
        ],
        PROFILE_PATH: lambda request, index: httpx.Response(
            200, json={"success": True, "user": {"uniqueId": f"resolved{index}"}}
        ),
    }
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(
        reader,
        settings,
        scope=CommentScope.TOP_LEVEL,
        limits=ScanLimits(max_requests=2),
    )
    assert len(recorder.requests) == 2, recorder.paths()
    assert result.status is ScanStatus.INCOMPLETE
    assert any("max_requests_reached" in r for r in result.incomplete_reasons)
    assert result.stats.requests_made == 2


@pytest.mark.asyncio
async def test_retries_are_charged_to_the_scan_request_budget(tmp_path, monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("app.providers.scrapecreators_reader.asyncio.sleep", fake_sleep)
    routes = {COMMENTS_PATH: lambda request, index: httpx.Response(500, json={})}
    reader, recorder, settings = reader_with(tmp_path, routes)
    result = await run_scan(
        reader, settings, scope=CommentScope.TOP_LEVEL,
        limits=ScanLimits(max_requests=2),
    )
    # The adapter would retry three times; the budget stops it at two.
    assert len(recorder.requests) == 2
    assert result.status is ScanStatus.INCOMPLETE


@pytest.mark.asyncio
async def test_a_slow_provider_cannot_outlast_the_scan_deadline(tmp_path):
    async def slow(request):
        await asyncio.sleep(0.4)
        return httpx.Response(200, json=envelope([comment("1", "x", 1)], has_more=0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    settings = settings_for(tmp_path)
    reader = ScrapeCreatorsCommentReader(settings, client)
    began = time.monotonic()
    result = await run_scan(
        reader, settings, scope=CommentScope.TOP_LEVEL,
        limits=ScanLimits(deadline_seconds=0.05),
    )
    assert time.monotonic() - began < 0.3
    assert result.status is ScanStatus.INCOMPLETE
    assert any("scan_deadline_reached" in r for r in result.incomplete_reasons)
