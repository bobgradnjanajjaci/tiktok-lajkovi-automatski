#!/usr/bin/env python3
"""Dependency-free verification runner.

This exists because the build container this repository was assembled in has no
network access, so `pip install -r requirements-dev.txt` fails and pytest cannot
run there. It exercises everything that works with the standard library alone:
the quantity formula, keyword matching, the scanner (traversal, completeness,
ambiguity), and the ScrapeCreators adapter's parsing and pagination with its
HTTP layer replaced by a scripted fake.

It is NOT a replacement for `python -m pytest -q`, which covers the web layer,
database, panel client and end-to-end worker as well. Run pytest wherever you
have network access; the bundled GitHub Actions workflow does exactly that.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Minimal stand-in so modules that import httpx at module scope can be loaded.
if "httpx" not in sys.modules:  # pragma: no cover - environment shim
    try:
        import httpx  # noqa: F401
    except ModuleNotFoundError:
        fake = types.ModuleType("httpx")
        for name in (
            "AsyncClient", "Timeout", "Limits", "TimeoutException", "NetworkError",
            "HTTPError", "RemoteProtocolError", "URL", "Response", "Request",
            "MockTransport", "ReadTimeout",
        ):
            setattr(fake, name, type(name, (Exception,), {}))
        sys.modules["httpx"] = fake

from app.comment_finder import (  # noqa: E402
    ScanLimits,
    build_keyword_pattern,
    matches_keyword,
    normalize_text,
    scan_video,
)
from app.like_rules import (  # noqa: E402
    calculate_quantity,
    safe_like_count,
    validate_like_count,
)
from app.models import CommentScope, ScanStatus  # noqa: E402
from app.providers.base import coerce_id, dig_strict, MISSING, parse_bool_flag  # noqa: E402
from app.providers.fixture_reader import FixtureCommentReader  # noqa: E402
from app.providers.scrapecreators_reader import ScrapeCreatorsCommentReader  # noqa: E402

FAILURES: list[str] = []
PASSED = 0


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILURES.append(name)
        print("FAIL", name)


# ---------------------------------------------------------------- quantity
BOUNDARIES = [
    (0, 150), (99, 150), (100, 150), (101, 500), (249, 500), (280, 500),
    (299, 500), (300, 500), (301, 391), (999, 1298), (1000, 2000),
    (2999, 5998), (3000, 4000), (7999, 8999), (8000, 8000), (9999, 9999),
    (10000, 0), (15000, 0),
]


def quantity_checks() -> None:
    for top, expected in BOUNDARIES:
        check(f"quantity({top})=={expected}", calculate_quantity(top) == expected)
    check("300 steps down to 301", calculate_quantity(300) > calculate_quantity(301))
    for bad in [None, True, False, -1, "-5", "", "12.0", "1,200", "1.2K", "abc", 3.7, [], {}]:
        check(f"invalid like count {bad!r}", safe_like_count(bad) is None)
    check("valid numeric string", validate_like_count("4213") == 4213)
    check("zero is valid when reported", validate_like_count(0) == 0)


# ---------------------------------------------------------------- matching
def matching_checks() -> None:
    pattern = build_keyword_pattern("Mael Vorran")
    for text in [
        "Mael Vorran", "mael vorran", "MAEL VORRAN", "  mael   vorran ",
        "read Mael Vorran!", "(Mael Vorran)", "@Mael Vorran", "Mael\u00a0Vorran",
        "\uff2d\uff41\uff45\uff4c \uff36\uff4f\uff52\uff52\uff41\uff4e", "Mael\tVorran",
    ]:
        check(f"match {text!r}", matches_keyword(text, pattern))
    for text in [
        "Mael", "Vorran", "Maelvorran", "Mael Vorrano", "xMael Vorran",
        "Mael-Vorran", "Michael Vorran", "Vorran Mael", "Mael. Vorran", "",
    ]:
        check(f"no match {text!r}", not matches_keyword(text, pattern))
    check("normalize", normalize_text("  Mael   VORRAN  ") == "mael vorran")


# ------------------------------------------------------------ parse helpers
def helper_checks() -> None:
    for value in (True, 1, 1.0, "1", "true", "TRUE", "yes"):
        check(f"flag true {value!r}", parse_bool_flag(value) is True)
    for value in (False, 0, 0.0, "0", "false", "False", "no"):
        check(f"flag false {value!r}", parse_bool_flag(value) is False)
    for value in (None, "", "maybe", 7, {}, [], "2"):
        check(f"flag unknown {value!r}", parse_bool_flag(value) is None)
    check("dig_strict missing", dig_strict({}, "a") is MISSING)
    check("dig_strict null", dig_strict({"a": None}, "a") is None)
    check("dig_strict nested", dig_strict({"a": {"b": 2}}, "a.b") == 2)
    check("coerce big id", coerce_id(7300000000000000102) == "7300000000000000102")
    check("coerce string id", coerce_id("  7300  ") == "7300")
    check("coerce bool is not an id", coerce_id(True) is None)


# ------------------------------------------------------------------ scanner
def c(cid, text, likes, owner="user", replies=0, uid=None):
    record = {
        "comment_id": cid, "text": text, "like_count": likes,
        "reply_count": replies, "owner_username": owner,
    }
    if uid:
        record["owner_user_id"] = uid
    return record


def vid(pages, replies=None):
    return {
        "provenance": "synthetic test data",
        "video_id": "7300000000000000001",
        "video_url": "https://www.tiktok.com/@creator/video/7300000000000000001",
        "pages": pages,
        "replies": replies or {},
    }


async def run(data, scope=CommentScope.ALL, limits=None):
    return await scan_video(
        FixtureCommentReader(data=data),
        video_id="7300000000000000001",
        video_url="https://www.tiktok.com/@creator/video/7300000000000000001",
        keyword="Mael Vorran",
        requested_scope=scope,
        limits=limits,
    )


async def scanner_checks() -> None:
    result = await run(vid([
        {"has_more": True, "cursor_out": "c1", "comments": [c("1", "nothing", 3)]},
        {"has_more": False, "cursor_out": None, "comments": [c("2", "Mael Vorran!", 5)]},
    ]))
    check("match on a later page", result.status is ScanStatus.COMPLETE and result.target.comment_id == "2")

    result = await run(vid([
        {"has_more": True, "cursor_out": "c1", "comments": [c("1", "Mael Vorran", 10)]},
        {"has_more": False, "cursor_out": None, "comments": [c("2", "huge", 950)]},
    ]))
    check("maximum after the match", result.target.comment_id == "1" and result.observed_top_likes == 950)

    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "early Mael Vorran", 2), c("2", "popular Mael Vorran", 900)]}]))
    check("first match, not the most liked", result.target.comment_id == "1" and result.observed_top_likes == 900)

    data = vid(
        [{"has_more": False, "cursor_out": None, "comments": [c("1", "Mael Vorran", 10, replies=1)]}],
        {"1": [{"has_more": False, "cursor_out": None, "comments": [c("1-1", "liked reply", 4000)]}]},
    )
    result = await run(data)
    check("reply holds the maximum under scope all", result.observed_top_likes == 4000 and result.complete)
    result = await run(data, CommentScope.TOP_LEVEL)
    check("top_level excludes replies", result.observed_top_likes == 10)

    data = vid(
        [{"has_more": False, "cursor_out": None, "comments": [
            c("p1", "parent one", 1, replies=1), c("p2", "parent two", 2, replies=1)]}],
        {
            "p1": [{"has_more": False, "cursor_out": None, "comments": [c("r1", "Mael Vorran reply", 1)]}],
            "p2": [{"has_more": False, "cursor_out": None, "comments": [c("r2", "Mael Vorran later", 1)]}],
        },
    )
    result = await run(data)
    check("traversal parent then its replies", result.target.comment_id == "r1" and result.target.source_order == 2)

    result = await run(vid([{"has_more": True, "cursor_out": "c1", "comments": [c("1", "Mael Vorran", 9)]}]))
    check("short page with has_more", result.status is ScanStatus.INCOMPLETE and result.observed_top_likes == 9)

    result = await run(vid([
        {"has_more": True, "cursor_out": "same", "comments": [c("1", "x", 1)]},
        {"has_more": True, "cursor_out": "same", "comments": [c("2", "y", 2)]},
    ]))
    check("repeated cursor", any(r.startswith("repeated_cursor") for r in result.incomplete_reasons))

    result = await run(vid([{"has_more": True, "cursor_out": None, "comments": [c("1", "x", 1)]}]))
    check("has_more without a cursor", "missing_cursor_with_has_more:top_level" in result.incomplete_reasons)

    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "Mael Vorran", 5),
        {"comment_id": "2", "text": "broken", "like_count": None, "owner_username": "u"}]}]))
    check("unknown count is not zero", result.status is ScanStatus.INCOMPLETE and result.observed_top_likes == 5)

    # Empty iterator.
    result = await run(vid([]))
    check("no pages is not a completed empty scan",
          result.status is ScanStatus.INCOMPLETE
          and any("no_pages_returned" in r for r in result.incomplete_reasons))

    # Reply-count shortfall: strict by default, labelled when opted in.
    short = vid(
        [{"has_more": False, "cursor_out": None, "comments": [c("p1", "parent", 1, replies=5)]}],
        {"p1": [{"has_more": False, "cursor_out": None, "comments": [c("r1", "one of five", 1)]}]},
    )
    strict = await run(short)
    check("reply shortfall is incomplete by default",
          strict.status is ScanStatus.INCOMPLETE
          and any(r.startswith("truncated_replies") for r in strict.incomplete_reasons)
          and strict.scope_limitations == [])
    check("ScanLimits default is strict", ScanLimits().trust_provider_reply_end is False)
    lenient = await run(short, limits=ScanLimits(trust_provider_reply_end=True))
    check("opt-in completes but is labelled",
          lenient.status is ScanStatus.COMPLETE
          and any("reply_count_mismatch" in e for e in lenient.scope_limitations)
          and lenient.as_json()["completeness_basis"].startswith("provider-visible"))

    still_more = vid(
        [{"has_more": False, "cursor_out": None, "comments": [c("p1", "parent", 1, replies=2)]}],
        {"p1": [{"has_more": True, "cursor_out": None, "comments": [c("r1", "one", 1)]}]},
    )
    result = await run(still_more, limits=ScanLimits(trust_provider_reply_end=True))
    check("opt-in cannot excuse has_more", result.status is ScanStatus.INCOMPLETE and result.scope_limitations == [])

    budget = vid([{"has_more": True, "cursor_out": f"c{i}", "comments": [c(str(i), "x", i)]} for i in range(1, 6)])
    result = await run(budget, limits=ScanLimits(max_pages=2, trust_provider_reply_end=True))
    check("opt-in cannot excuse a budget", result.status is ScanStatus.INCOMPLETE)

    result = await run(vid([
        {"has_more": True, "cursor_out": "c1", "comments": [c("1", "no keyword", 10000)]},
        {"has_more": False, "cursor_out": None, "comments": [c("2", "Mael Vorran", 1)]},
    ]))
    check("10k threshold early exit",
          result.status is ScanStatus.EARLY_EXIT_THRESHOLD and result.target is None
          and result.stats.pages_read == 1 and not result.complete)

    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [c("1", "nope", 1)]}]))
    check("keyword_not_found only when complete", result.complete and result.target is None)

    # Ambiguity regardless of keyword, keyed on the stable owner id.
    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "Mael Vorran mentioned", 2, owner="fan", uid="u9"),
        c("2", "no keyword here", 6, owner="fan", uid="u9")]}]))
    check("second comment by the same owner is ambiguous",
          result.target.comment_id == "1" and result.target_ambiguous
          and result.owner_duplicate_comment_ids == ["1", "2"])

    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "warming up", 2, owner="fan", uid="u9"),
        c("2", "Mael Vorran mentioned", 3, owner="fan", uid="u9")]}]))
    check("earlier comment by the same owner counts", result.target_ambiguous)

    # Two owner ids behind one handle: the panel would target both by that same
    # username, so this is reported, not silently resolved either way.
    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "Mael Vorran", 2, owner="same.looking", uid="u1"),
        c("2", "unrelated", 9, owner="same.looking", uid="u2")]}]))
    check("conflicting owner ids behind one handle are reported",
          result.target_ambiguous
          and "owner_identity_conflict" in result.incomplete_reasons)

    # Mixed identifier availability: one record has the uid, the other only the
    # handle. They are the same account and must reconcile.
    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "Mael Vorran", 2, owner="same.owner", uid="101"),
        c("2", "ordinary text", 5, owner="same.owner")]}]))
    check("handle-only record reconciles with a uid record",
          result.target_ambiguous and result.owner_duplicate_comment_ids == ["1", "2"])

    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "ordinary text", 5, owner="same.owner"),
        c("2", "Mael Vorran", 2, owner="same.owner", uid="101")]}]))
    check("reconciliation also works before the target", result.target_ambiguous)

    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "Mael Vorran", 2, owner="Same.Owner", uid="101"),
        c("2", "ordinary text", 5, owner="same.owner")]}]))
    check("handle case differences reconcile", result.target_ambiguous)

    # Unreadable text anywhere blocks the "first match" claim.
    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        {"comment_id": "1", "like_count": 5, "owner_username": "a", "owner_user_id": "u1"},
        c("2", "Mael Vorran", 3, owner="b", uid="u2")]}]))
    check("missing text makes the scan incomplete",
          result.status is ScanStatus.INCOMPLETE
          and "unreadable_comment_text" in result.incomplete_reasons)
    result2 = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        {"comment_id": "1", "text": None, "like_count": 5, "owner_username": "a", "owner_user_id": "u1"},
        c("2", "Mael Vorran", 3, owner="b", uid="u2")]}]))
    check("null text makes the scan incomplete",
          "unreadable_comment_text" in result2.incomplete_reasons)
    result3 = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        {"comment_id": "1", "text": 12, "like_count": 5, "owner_username": "a", "owner_user_id": "u1"},
        c("2", "Mael Vorran", 3, owner="b", uid="u2")]}]))
    check("non-string text makes the scan incomplete",
          "unreadable_comment_text" in result3.incomplete_reasons)
    result4 = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "", 5, owner="a", uid="u1"),
        c("2", "Mael Vorran", 3, owner="b", uid="u2")]}]))
    check("an explicitly empty text is still a complete scan",
          result4.status is ScanStatus.COMPLETE and result4.target.comment_id == "2")

    # All known invalidity reasons are reported, not just the first.
    result = await run(vid([{"has_more": True, "cursor_out": "c1", "comments": [
        {"comment_id": "1", "like_count": None, "owner_username": "a", "owner_user_id": "u1"}]}]))
    check("every invalidity reason is recorded",
          {"invalid_like_counts_present", "unreadable_comment_text"}
          <= set(result.incomplete_reasons))

    result = await run(vid([
        {"has_more": True, "cursor_out": "c1", "comments": [c("1", "Mael Vorran", 2, owner="fan", uid="u9")]},
        {"has_more": False, "cursor_out": None, "comments": [c("1", "Mael Vorran", 2, owner="fan", uid="u9")]},
    ]))
    check("duplicate cid is not a second comment",
          result.stats.duplicates_dropped == 1 and not result.target_ambiguous)

    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "Mael Vorran", 500, owner="fan", uid="u9"), c("2", "quieter", 10, owner="o", uid="u8")]}]))
    check("already-leading target is still processed",
          result.observed_top_likes == 500 and calculate_quantity(500) == 650)

    result = await run(vid([{"has_more": False, "cursor_out": None, "comments": [
        c("1", "Mael Vorran", 1, owner="@handle.x", uid="u1")]}]))
    check("presentation @ stripped, original kept",
          result.target.comment_owner_username == "handle.x"
          and result.target.raw_owner_username == "@handle.x")
    check("no owner lookups when the handle is present",
          result.stats.owner_lookups == 0 and result.stats.profile_navigations == 0)

    # Shipped fixtures on disk.
    reader = FixtureCommentReader(directory=str(ROOT / "tests/fixtures/comments"))
    result = await scan_video(
        reader, video_id="7300000000000000001",
        video_url="https://www.tiktok.com/@creator.one/video/7300000000000000001",
        keyword="Mael Vorran", requested_scope=CommentScope.ALL,
    )
    check("fixture 1 maximum", result.observed_top_likes == 301)
    check("fixture 1 target", result.target.comment_id == "7300000000000000102"
          and result.target.comment_owner_username == "beta.fan")
    check("fixture 1 quantity", calculate_quantity(result.observed_top_likes) == 391)
    result = await scan_video(
        reader, video_id="7300000000000000002", video_url="x",
        keyword="Mael Vorran", requested_scope=CommentScope.ALL)
    check("fixture 2 has no match", result.complete and result.target is None)
    result = await scan_video(
        reader, video_id="7300000000000000003", video_url="x",
        keyword="Mael Vorran", requested_scope=CommentScope.ALL)
    check("fixture 3 is ambiguous", result.target_ambiguous)


# -------------------------------------------- ScrapeCreators adapter parsing
class FakeSettings:
    """Just the attributes the adapter reads."""

    READER_BASE_URL = "https://api.scrapecreators.example"
    READER_API_KEY = "offline-test-key"
    READER_API_KEY_HEADER = "x-api-key"
    READER_TIMEOUT_SECONDS = 15.0
    READER_MAX_CONNECTIONS = 10
    READER_TRIM = False
    READER_OWNER_CACHE_TTL_SECONDS = 3600
    READER_PROFILE_CACHE_MAX_AGE = "7d"
    KEYWORD = "Mael Vorran"
    SCAN_TRUST_PROVIDER_REPLY_END = False


def record(cid, text, likes, handle="someone", uid="6851091024770040837", replies=0, reply_id="0"):
    """Documented ScrapeCreators comment shape, trimmed to consumed fields."""
    user = {"nickname": "Display Name"}
    if uid is not None:
        user["uid"] = uid
    if handle is not None:
        user["unique_id"] = handle
    body = {
        "aweme_id": "7463250363559218474", "cid": cid, "digg_count": likes,
        "reply_comment_total": replies, "reply_id": reply_id, "text": text, "user": user,
    }
    if cid is None:
        del body["cid"]
    if likes is None:
        del body["digg_count"]
    return body


def scripted(reader, pages, profile=None):
    """Replace the HTTP layer with a scripted response list."""
    calls: list[tuple[str, dict]] = []

    async def fake_get(path, params):
        calls.append((path, dict(params)))
        if path.endswith("/profile"):
            return profile or {"success": True, "user": {"uniqueId": "resolved.handle"}}
        index = min(len([c for c in calls if not c[0].endswith("/profile")]) - 1, len(pages) - 1)
        payload = pages[index]
        if isinstance(payload, Exception):
            raise payload
        return payload

    reader._get = fake_get  # noqa: SLF001
    return calls


def envelope(comments, cursor=20, has_more=1, **extra):
    body = {
        "success": True, "credits_remaining": 999, "credits_charged": 1,
        "comments": comments, "cursor": cursor, "has_more": has_more,
        "status_code": 0, "status_msg": "",
    }
    body.update(extra)
    return body


VIDEO_URL = "https://www.tiktok.com/@amici/video/7463250363559218474"


async def adapter_checks() -> None:
    from app.models import ProviderError  # noqa: F401  (used throughout)

    reader = ScrapeCreatorsCommentReader(FakeSettings())
    calls = scripted(reader, [envelope([record("1", "Mael Vorran", 1015, handle="tmoneyhoney18")], has_more=0)])
    pages = [page async for page in reader.iter_top_level_pages("7463250363559218474", VIDEO_URL)]
    check("one page, one request", len(pages) == 1 and len(calls) == 1)
    check("documented url parameter", calls[0][1] == {"url": VIDEO_URL})
    check("no undocumented parameters",
          not {"video_id", "count", "page_size", "limit", "trim"} & set(calls[0][1]))
    comment = pages[0].comments[0]
    check("cid maps to comment_id", comment.comment_id == "1" and isinstance(comment.comment_id, str))
    check("digg_count maps to like_count", comment.like_count == 1015 and comment.like_count_trusted)
    check("unique_id maps to the handle", comment.comment_owner_username == "tmoneyhoney18")
    check("uid maps to the owner id", comment.comment_owner_user_id == "6851091024770040837")
    check("reply_id 0 means top level", comment.parent_comment_id is None)
    check("handle came from the comment record", comment.owner_username_source == "comment_record")
    # _decode is exercised directly, since the scripted fake bypasses transport.
    class FakeResponse:
        def __init__(self, payload, text="", content_type="application/json"):
            self._payload = payload
            self.text = text
            self.status_code = 200
            self.headers = {"content-type": content_type}

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    decoder = ScrapeCreatorsCommentReader(FakeSettings())
    decoder._decode(FakeResponse(envelope([], has_more=0, credits_charged=2,
                                          credits_remaining=498)), "/p")
    check("credits recorded, not estimated",
          decoder.call_stats["credits_charged"] == 2
          and decoder.call_stats["credits_remaining"] == 498)

    for payload, label in (
        ({"success": False, "message": "invalid url"}, "success=false at HTTP 200"),
        ({"status_code": 5, "status_msg": "blocked"}, "non-zero status_code"),
    ):
        try:
            ScrapeCreatorsCommentReader(FakeSettings())._decode(FakeResponse(payload), "/p")
            check(f"{label} is an error", False)
        except ProviderError:
            check(f"{label} is an error", True)

    try:
        ScrapeCreatorsCommentReader(FakeSettings())._decode(
            FakeResponse(None, text="<html>blocked</html>", content_type="text/html"), "/p")
        check("HTML body is an error", False)
    except ProviderError as exc:
        check("HTML body is an error", "HTML" in str(exc))

    try:
        ScrapeCreatorsCommentReader(FakeSettings())._decode(FakeResponse([1, 2]), "/p")
        check("non-object JSON is an error", False)
    except ProviderError:
        check("non-object JSON is an error", True)

    check("cached responses counted",
          (lambda r: (r._decode(FakeResponse({"success": True, "cached": True}), "/p"),
                      r.call_stats["cached_responses"])[1] == 1)(
              ScrapeCreatorsCommentReader(FakeSettings())))

    # Pagination follows the echoed cursor.
    reader = ScrapeCreatorsCommentReader(FakeSettings())
    calls = scripted(reader, [
        envelope([record("1", "a", 1)], cursor=20, has_more=1),
        envelope([record("2", "b", 2)], cursor=40, has_more=1),
        envelope([record("3", "c", 3)], cursor=60, has_more=0),
    ])
    pages = [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
    check("three pages followed", len(pages) == 3)
    check("first page sends no cursor", "cursor" not in calls[0][1])
    check("cursor echoed verbatim", calls[1][1]["cursor"] == "20" and calls[2][1]["cursor"] == "40")

    # Strict marker parsing.
    for marker in (0, "0", "false", False):
        reader = ScrapeCreatorsCommentReader(FakeSettings())
        calls = scripted(reader, [envelope([record("1", "x", 1)], has_more=marker)])
        pages = [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
        check(f"falsey marker {marker!r} ends the stream",
              len(calls) == 1 and not pages[0].has_more and not pages[0].completion_unknown)

    for marker in ("maybe", None, 7):
        reader = ScrapeCreatorsCommentReader(FakeSettings())
        body = envelope([record("1", "x", 1)], has_more=marker)
        if marker is None:
            del body["has_more"]
        scripted(reader, [body])
        pages = [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
        check(f"unknown marker {marker!r} is flagged", pages[0].completion_unknown is True)

    # Missing / null comment list is a schema error, empty list is not.
    for payload, label in (
        ({"success": True, "has_more": 0, "cursor": 0}, "absent"),
        ({"success": True, "comments": None, "has_more": 0}, "null"),
    ):
        reader = ScrapeCreatorsCommentReader(FakeSettings())
        scripted(reader, [payload])
        try:
            [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
            check(f"{label} comments list raises", False)
        except ProviderError:
            check(f"{label} comments list raises", True)

    reader = ScrapeCreatorsCommentReader(FakeSettings())
    scripted(reader, [envelope([], has_more=0)])
    pages = [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
    check("explicit empty list is a valid page", len(pages) == 1 and pages[0].comments == [])

    # A record without a cid is dropped, never invented.
    reader = ScrapeCreatorsCommentReader(FakeSettings())
    scripted(reader, [envelope([record("1", "fine", 5), record(None, "broken", 900)], has_more=0)])
    pages = [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
    ids = [c.comment_id for c in pages[0].comments]
    check("record without cid is dropped", ids == ["1"] and pages[0].invalid_records == 1)

    # A missing like count is untrusted, not zero.
    reader = ScrapeCreatorsCommentReader(FakeSettings())
    scripted(reader, [envelope([record("1", "x", None)], has_more=0)])
    pages = [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
    check("missing digg_count is untrusted", pages[0].comments[0].like_count_trusted is False)

    # Formatted counts are refused.
    reader = ScrapeCreatorsCommentReader(FakeSettings())
    scripted(reader, [envelope([record("1", "x", "1.2K")], has_more=0)])
    pages = [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
    check("1.2K is not an exact count", pages[0].comments[0].like_count_trusted is False)

    # Replies use the documented parameters and map reply_id to the parent.
    reader = ScrapeCreatorsCommentReader(FakeSettings())
    calls = scripted(reader, [envelope([record("r1", "a reply", 3, reply_id="p1")], has_more=0)])
    pages = [p async for p in reader.iter_reply_pages("v", VIDEO_URL, "p1")]
    check("replies send comment_id and url",
          calls[0][1] == {"url": VIDEO_URL, "comment_id": "p1"})
    check("reply_id becomes the parent id", pages[0].comments[0].parent_comment_id == "p1")

    # One cached profile lookup per distinct owner id, and only when needed.
    reader = ScrapeCreatorsCommentReader(FakeSettings())
    calls = scripted(reader, [envelope([
        record("1", "Mael Vorran", 3, handle=None, uid="42"),
        record("2", "again", 4, handle=None, uid="42"),
    ], has_more=0)])
    pages = [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
    profile_calls = [c for c in calls if c[0].endswith("/profile")]
    check("one lookup per distinct owner id", len(profile_calls) == 1)
    check("lookup uses the documented cache window",
          profile_calls[0][1] == {"user_id": "42", "cache_max_age": "7d"})
    check("handle resolved from the profile",
          pages[0].comments[0].comment_owner_username == "resolved.handle"
          and pages[0].comments[0].owner_username_source == "profile_lookup_by_user_id")
    check("lookup counted once", reader.call_stats["owner_lookups"] == 1)

    reader = ScrapeCreatorsCommentReader(FakeSettings())
    calls = scripted(reader, [envelope([record("1", "Mael Vorran", 3)], has_more=0)])
    [p async for p in reader.iter_top_level_pages("v", VIDEO_URL)]
    check("no lookup when the handle is present",
          not [c for c in calls if c[0].endswith("/profile")])

    # url is required; the internal id is not a substitute.
    reader = ScrapeCreatorsCommentReader(FakeSettings())
    scripted(reader, [envelope([], has_more=0)])
    try:
        [p async for p in reader.iter_top_level_pages("v", "")]
        check("empty video url is refused", False)
    except ProviderError:
        check("empty video url is refused", True)

    # Configuration gates.
    class NoKey(FakeSettings):
        READER_API_KEY = ""

    reader = ScrapeCreatorsCommentReader(NoKey())
    check("no key means unconfigured", reader.configured is False)
    check("no key is not dry-run usable", reader.usable_for_dry_run is False)
    check("blocker names the variable", "READER_API_KEY" in (reader.blocker or ""))

    reader = ScrapeCreatorsCommentReader(FakeSettings())
    caps = reader.capabilities
    check("capabilities declare replies and handles",
          caps.supports_replies and caps.supplies_owner_username and caps.supports_exact_counts)
    check("ordering stability is not claimed", "not document" in caps.documented_ordering)


async def budget_checks() -> None:
    """The deadline and the request budget must bind DURING the awaits."""
    import time as _time
    from app.comment_finder import BudgetExhausted, RequestBudget, ScanDeadlineExceeded
    from app.models import CommentPage, NormalizedComment, utcnow

    class SlowReader:
        """Yields one page after a delay, to test deadline enforcement."""

        name = "slow"
        supports_replies = False
        provider_limits: dict = {}
        call_stats: dict = {}
        request_budget = None

        def __init__(self, delay: float, pages: int = 1):
            self.delay = delay
            self.pages = pages
            self.calls = 0

        async def iter_top_level_pages(self, video_id, video_url):
            for index in range(self.pages):
                if self.request_budget is not None:
                    self.request_budget.charge()
                self.calls += 1
                await asyncio.sleep(self.delay)
                yield CommentPage(
                    comments=[
                        NormalizedComment(
                            video_id=video_id, video_url=video_url,
                            comment_id=str(index), parent_comment_id=None,
                            text="Mael Vorran", like_count=5, reply_count=0,
                            comment_owner_username="someone",
                            comment_owner_user_id=f"u{index}",
                            source_order=0, fetched_at=utcnow(),
                        )
                    ],
                    cursor_in=None,
                    cursor_out=f"c{index}",
                    has_more=index < self.pages - 1,
                )

        async def iter_reply_pages(self, video_id, video_url, parent_comment_id):
            return
            yield  # pragma: no cover

    reader = SlowReader(delay=0.20)
    began = _time.monotonic()
    result = await scan_video(
        reader, video_id="v", video_url="https://www.tiktok.com/@a/video/1",
        keyword="Mael Vorran", requested_scope=CommentScope.TOP_LEVEL,
        limits=ScanLimits(deadline_seconds=0.02),
    )
    elapsed = _time.monotonic() - began
    check("the deadline interrupts the await instead of waiting it out", elapsed < 0.15)
    check("an expired deadline is incomplete",
          result.status is ScanStatus.INCOMPLETE
          and any("scan_deadline_reached" in r for r in result.incomplete_reasons))
    check("partial diagnostics survive the deadline", result.duration_ms >= 0)

    # The budget binds before every attempt, including side requests.
    budget = RequestBudget(2)
    budget.charge()
    budget.charge()
    try:
        budget.charge()
        check("the budget refuses the attempt over the limit", False)
    except BudgetExhausted:
        check("the budget refuses the attempt over the limit", True)

    expired = RequestBudget(10, deadline_at=_time.monotonic() - 1.0)
    try:
        expired.charge()
        check("an expired budget refuses new attempts", False)
    except ScanDeadlineExceeded:
        check("an expired budget refuses new attempts", True)

    reader = SlowReader(delay=0.0, pages=5)
    result = await scan_video(
        reader, video_id="v", video_url="https://www.tiktok.com/@a/video/1",
        keyword="Mael Vorran", requested_scope=CommentScope.TOP_LEVEL,
        limits=ScanLimits(max_requests=2),
    )
    check("the transport is not called past the request budget", reader.calls == 2)
    check("an exhausted request budget is incomplete",
          result.status is ScanStatus.INCOMPLETE
          and any("max_requests_reached" in r for r in result.incomplete_reasons))
    check("requests_made matches the attempts actually charged",
          result.stats.requests_made == 2)

    # Owner lookups are inside the same budget: three lookups plus one page
    # cannot happen under a budget of two.
    class LookupReader(SlowReader):
        name = "lookup"

        async def iter_top_level_pages(self, video_id, video_url):
            if self.request_budget is not None:
                self.request_budget.charge()
            self.calls += 1
            comments = []
            for index in range(3):
                if self.request_budget is not None:
                    self.request_budget.charge()  # the owner lookup
                self.calls += 1
                comments.append(
                    NormalizedComment(
                        video_id=video_id, video_url=video_url,
                        comment_id=str(index), parent_comment_id=None,
                        text="Mael Vorran" if index == 0 else "other",
                        like_count=5, reply_count=0,
                        comment_owner_username=f"h{index}",
                        comment_owner_user_id=f"10{index}",
                        source_order=0, fetched_at=utcnow(),
                    )
                )
            yield CommentPage(
                comments=comments, cursor_in=None, cursor_out=None, has_more=False
            )

    reader = LookupReader(delay=0.0)
    result = await scan_video(
        reader, video_id="v", video_url="https://www.tiktok.com/@a/video/1",
        keyword="Mael Vorran", requested_scope=CommentScope.TOP_LEVEL,
        limits=ScanLimits(max_requests=2),
    )
    check("lookups are charged to the same budget", reader.calls == 2)
    check("a budget broken by lookups is incomplete",
          result.status is ScanStatus.INCOMPLETE
          and any("max_requests_reached" in r for r in result.incomplete_reasons))


async def main() -> int:
    quantity_checks()
    matching_checks()
    helper_checks()
    await scanner_checks()
    await adapter_checks()
    await budget_checks()

    print()
    print(f"passed: {PASSED}")
    print(f"failed: {len(FAILURES)}")
    for name in FAILURES:
        print("  -", name)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
