"""Tests for the contract-driven HTTP reader.

These exercise the adapter against a MockTransport shaped like the contract.
They are fixture tests, NOT verification of any real provider: there is no live
provider configured in this repository. See README.md.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.comment_finder import scan_video
from app.config import Settings
from app.models import CommentScope, ProviderError, ScanStatus
from app.providers.base import ReaderContract
from app.providers.contract_http_reader import ContractHttpCommentReader

BASE = "https://provider.example"

CONTRACT = {
    "verified_by_operator": True,
    "provider_name": "example-comments-api",
    "documented_ordering": "provider default order, documented as stable",
    "auth": {"style": "header", "name": "x-api-key"},
    "top_level": {
        "method": "GET",
        "path": "/v1/comments",
        "query": {"video_id": "{video_id}", "count": "{page_size}", "cursor": "{cursor}"},
        "list_path": "data.comments",
        "cursor_path": "data.cursor",
        "has_more_path": "data.has_more",
    },
    "replies": {
        "method": "GET",
        "path": "/v1/replies",
        "query": {
            "video_id": "{video_id}",
            "comment_id": "{parent_id}",
            "count": "{page_size}",
            "cursor": "{cursor}",
        },
        "list_path": "data.comments",
        "cursor_path": "data.cursor",
        "has_more_path": "data.has_more",
    },
    "owner_lookup": {
        "method": "GET",
        "path": "/v1/user",
        "query": {"user_id": "{user_id}"},
        "username_path": "data.unique_id",
    },
    "fields": {
        "comment_id": "cid",
        "text": "text",
        "like_count": "digg_count",
        "reply_count": "reply_total",
        "parent_comment_id": "reply_to",
        "owner_username": "user.unique_id",
        "owner_user_id": "user.uid",
    },
    "capabilities": {
        "supports_replies": True,
        "supplies_owner_username": True,
        "supports_owner_lookup": True,
        "supports_exact_counts": True,
        "max_page_size": 50,
    },
    "limits": {"requests_per_minute": 60},
}


def write_contract(tmp_path, **overrides):
    data = json.loads(json.dumps(CONTRACT))
    data.update(overrides)
    path = tmp_path / "reader_contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def settings_for(tmp_path, contract_path, **overrides):
    values = {
        "ENVIRONMENT": "test",
        "COMMENT_READER": "http",
        "READER_BASE_URL": BASE,
        "READER_API_KEY": "reader-test-key",
        "READER_CONTRACT_FILE": str(contract_path) if contract_path else "",
        "DATABASE_PATH": str(tmp_path / "app.db"),
        "ADMIN_PASSWORD": "test-password-1234",
        "SESSION_SECRET": "x" * 48,
    }
    values.update(overrides)
    return Settings(**values)


def comment(cid, text, likes, *, username=None, uid=None, replies=0):
    record = {"cid": cid, "text": text, "digg_count": likes, "reply_total": replies, "user": {}}
    if username is not None:
        record["user"]["unique_id"] = username
    if uid is not None:
        record["user"]["uid"] = uid
    return record


# ------------------------------------------------------------- configuration
def test_reader_is_unconfigured_without_a_contract(tmp_path):
    reader = ContractHttpCommentReader(settings_for(tmp_path, None))
    assert reader.configured is False
    assert "READER_CONTRACT_FILE" in reader.blocker


def test_reader_refuses_an_unverified_contract(tmp_path):
    path = write_contract(tmp_path, verified_by_operator=False)
    reader = ContractHttpCommentReader(settings_for(tmp_path, path))
    assert reader.configured is False
    assert "verified_by_operator" in reader.blocker


def test_a_display_name_only_provider_is_rejected_outright(tmp_path):
    data = json.loads(json.dumps(CONTRACT))
    data["fields"] = {"comment_id": "cid", "text": "text", "like_count": "digg_count"}
    data.pop("owner_lookup")
    path = tmp_path / "bad_contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProviderError) as exc:
        ReaderContract.load(path)
    assert "owner" in str(exc.value).lower()


def test_a_valid_contract_is_configured(tmp_path):
    reader = ContractHttpCommentReader(settings_for(tmp_path, write_contract(tmp_path)))
    assert reader.configured is True
    assert reader.blocker is None
    assert reader.capabilities.supports_replies is True
    assert reader.provider_limits["provider"] == "example-comments-api"


# --------------------------------------------------------------- pagination
@pytest.mark.asyncio
async def test_pagination_and_scan_work_against_the_contract(tmp_path):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        params = request.url.params
        if request.url.path == "/v1/comments":
            if not params.get("cursor"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "comments": [
                                comment("c1", "nothing here", 5, username="one", uid="u1"),
                                comment("c2", "Mael Vorran mentioned", 8, username="two", uid="u2", replies=1),
                            ],
                            "cursor": "p2",
                            "has_more": True,
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "comments": [comment("c3", "unrelated leader", 4000, username="three", uid="u3")],
                        "cursor": None,
                        "has_more": False,
                    }
                },
            )
        if request.url.path == "/v1/replies":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "comments": [comment("c2r1", "a reply", 11, username="four", uid="u4")],
                        "cursor": None,
                        "has_more": False,
                    }
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = ContractHttpCommentReader(settings_for(tmp_path, write_contract(tmp_path)), http)
    async with http:
        result = await scan_video(
            reader,
            video_id="7300000000000000001",
            video_url="https://www.tiktok.com/@creator/video/7300000000000000001",
            keyword="Mael Vorran",
            requested_scope=CommentScope.ALL,
        )

    assert result.status is ScanStatus.COMPLETE
    assert result.target.comment_id == "c2"
    assert result.observed_top_likes == 4000
    assert result.stats.unique_comments == 4
    assert all(request.headers["x-api-key"] == "reader-test-key" for request in calls)
    # Largest documented page size is requested, not a default of 10.
    assert calls[0].url.params["count"] == "50"


@pytest.mark.asyncio
async def test_present_usernames_cause_zero_lookup_requests(tmp_path):
    paths: list[str] = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": {
                    "comments": [comment("c1", "Mael Vorran", 3, username="already.here", uid="u1")],
                    "cursor": None,
                    "has_more": False,
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = ContractHttpCommentReader(settings_for(tmp_path, write_contract(tmp_path)), http)
    async with http:
        result = await scan_video(
            reader,
            video_id="7300000000000000001",
            video_url="https://www.tiktok.com/@creator/video/7300000000000000001",
            keyword="Mael Vorran",
            requested_scope=CommentScope.TOP_LEVEL,
        )

    assert "/v1/user" not in paths
    assert reader.call_stats["owner_lookups"] == 0
    assert result.stats.profile_navigations == 0
    assert result.target.comment_owner_username == "already.here"
    assert result.target.owner_username_source == "comment_record"


@pytest.mark.asyncio
async def test_owner_lookup_is_used_only_when_needed_and_cached(tmp_path):
    lookups: list[str] = []

    def handler(request):
        if request.url.path == "/v1/user":
            lookups.append(request.url.params["user_id"])
            return httpx.Response(200, json={"data": {"unique_id": "@resolved.handle"}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "comments": [
                        comment("c1", "Mael Vorran", 3, uid="u1"),
                        comment("c2", "also mentions Mael Vorran", 4, uid="u1"),
                    ],
                    "cursor": None,
                    "has_more": False,
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = ContractHttpCommentReader(settings_for(tmp_path, write_contract(tmp_path)), http)
    async with http:
        result = await scan_video(
            reader,
            video_id="7300000000000000001",
            video_url="https://www.tiktok.com/@creator/video/7300000000000000001",
            keyword="Mael Vorran",
            requested_scope=CommentScope.TOP_LEVEL,
        )

    assert lookups == ["u1"]  # one lookup for one distinct owner id, cached
    assert result.target.comment_owner_username == "resolved.handle"
    assert result.target.owner_username_source == "owner_id_lookup"
    # Both comments belong to the same resolved handle, so the target is ambiguous.
    assert result.target_ambiguous is True


@pytest.mark.asyncio
async def test_retry_after_is_honoured_for_safe_reads_only(tmp_path, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.providers.contract_http_reader.asyncio.sleep", fake_sleep)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(
            200,
            json={"data": {"comments": [comment("c1", "x", 1, username="u")], "cursor": None, "has_more": False}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = ContractHttpCommentReader(settings_for(tmp_path, write_contract(tmp_path)), http)
    async with http:
        pages = [page async for page in reader.iter_top_level_pages("7300000000000000001", "u")]

    assert attempts["n"] == 2
    assert slept == [2.0]
    assert len(pages) == 1
    assert reader.call_stats["retry_seconds"] == 2.0


@pytest.mark.asyncio
async def test_credential_rejection_is_reported_not_worked_around(tmp_path):
    def handler(request):
        return httpx.Response(403, text="forbidden")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = ContractHttpCommentReader(settings_for(tmp_path, write_contract(tmp_path)), http)
    async with http:
        result = await scan_video(
            reader,
            video_id="7300000000000000001",
            video_url="https://www.tiktok.com/@creator/video/7300000000000000001",
            keyword="Mael Vorran",
            requested_scope=CommentScope.TOP_LEVEL,
        )
    assert result.status is ScanStatus.INCOMPLETE
    assert any("provider_error" in reason for reason in result.incomplete_reasons)
