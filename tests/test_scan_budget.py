"""Regression tests for the shared per-scan request budget and the deadline.

Both were reproduced as real defects:

* ``ScanLimits(deadline_seconds=0.01)`` against a response delayed by 150 ms
  returned after ~151 ms, i.e. the clock was only consulted once the page had
  already arrived.
* ``ScanLimits(max_requests=2)`` allowed four transport calls (one page plus
  three owner lookups) and still reported the scan complete, because lookups
  were added to the counter after the page-level check.

These tests assert the number of calls the transport actually received, not
only the reported statistics.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.comment_finder import (
    BudgetExhausted,
    RequestBudget,
    ScanDeadlineExceeded,
    ScanLimits,
    scan_video,
)
from app.models import CommentPage, CommentScope, NormalizedComment, ScanStatus, utcnow

VIDEO_ID = "7300000000000000001"
VIDEO_URL = "https://www.tiktok.com/@creator/video/7300000000000000001"


def make_comment(index: int, text: str) -> NormalizedComment:
    return NormalizedComment(
        video_id=VIDEO_ID,
        video_url=VIDEO_URL,
        comment_id=str(index),
        parent_comment_id=None,
        text=text,
        like_count=5,
        reply_count=0,
        comment_owner_username=f"handle{index}",
        comment_owner_user_id=f"10{index}",
        source_order=0,
        fetched_at=utcnow(),
    )


class CountingReader:
    """Charges the shared budget before each simulated outbound attempt."""

    name = "counting"
    supports_replies = False
    provider_limits: dict = {}
    request_budget = None

    def __init__(self, *, pages: int = 1, delay: float = 0.0, lookups_per_page: int = 0):
        self.pages = pages
        self.delay = delay
        self.lookups_per_page = lookups_per_page
        self.transport_calls = 0
        self.call_stats: dict = {"owner_lookups": 0, "profile_navigations": 0,
                                 "extra_requests": 0, "retry_seconds": 0.0}

    def _attempt(self) -> None:
        if self.request_budget is not None:
            self.request_budget.charge()
        self.transport_calls += 1

    async def iter_top_level_pages(self, video_id, video_url):
        for index in range(self.pages):
            self._attempt()
            if self.delay:
                await asyncio.sleep(self.delay)
            comments = [make_comment(index, "Mael Vorran" if index == 0 else "other")]
            for lookup in range(self.lookups_per_page):
                self._attempt()  # an owner lookup is an outbound attempt too
                self.call_stats["owner_lookups"] += 1
                comments.append(make_comment(100 + lookup, "other"))
            yield CommentPage(
                comments=comments,
                cursor_in=None,
                cursor_out=f"c{index}",
                has_more=index < self.pages - 1,
            )

    async def iter_reply_pages(self, video_id, video_url, parent_comment_id):
        return
        yield  # pragma: no cover


async def run(reader, limits):
    return await scan_video(
        reader,
        video_id=VIDEO_ID,
        video_url=VIDEO_URL,
        keyword="Mael Vorran",
        requested_scope=CommentScope.TOP_LEVEL,
        limits=limits,
    )


# ------------------------------------------------------------------ the budget
def test_the_budget_refuses_an_attempt_over_the_limit():
    budget = RequestBudget(2)
    budget.charge()
    budget.charge()
    with pytest.raises(BudgetExhausted):
        budget.charge()
    assert budget.used == 2


def test_an_expired_budget_refuses_before_counting():
    budget = RequestBudget(10, deadline_at=time.monotonic() - 1.0)
    with pytest.raises(ScanDeadlineExceeded):
        budget.charge()
    assert budget.used == 0


@pytest.mark.asyncio
async def test_pages_stop_at_the_request_budget():
    reader = CountingReader(pages=5)
    result = await run(reader, ScanLimits(max_requests=2))
    assert reader.transport_calls == 2
    assert result.stats.requests_made == 2
    assert result.status is ScanStatus.INCOMPLETE
    assert any("max_requests_reached" in r for r in result.incomplete_reasons)


@pytest.mark.asyncio
async def test_owner_lookups_are_charged_to_the_same_budget():
    """One page plus three lookups must not fit inside a budget of two."""
    reader = CountingReader(pages=1, lookups_per_page=3)
    result = await run(reader, ScanLimits(max_requests=2))
    assert reader.transport_calls == 2  # not 4
    assert result.status is ScanStatus.INCOMPLETE
    assert any("max_requests_reached" in r for r in result.incomplete_reasons)
    assert result.stats.requests_made == 2


@pytest.mark.asyncio
async def test_a_sufficient_budget_completes_normally():
    reader = CountingReader(pages=1, lookups_per_page=3)
    result = await run(reader, ScanLimits(max_requests=10))
    assert reader.transport_calls == 4
    assert result.status is ScanStatus.COMPLETE
    assert result.stats.requests_made == 4


@pytest.mark.asyncio
async def test_the_budget_is_per_scan_not_process_wide():
    reader = CountingReader(pages=1)
    first = await run(reader, ScanLimits(max_requests=5))
    second = await run(reader, ScanLimits(max_requests=5))
    assert first.stats.requests_made == 1
    assert second.stats.requests_made == 1  # not cumulative
    assert reader.transport_calls == 2  # the process-wide counter still adds up
    assert reader.request_budget is None  # restored after each scan


# ---------------------------------------------------------------- the deadline
@pytest.mark.asyncio
async def test_the_deadline_interrupts_an_in_flight_await():
    reader = CountingReader(pages=1, delay=0.30)
    began = time.monotonic()
    result = await run(reader, ScanLimits(deadline_seconds=0.02))
    elapsed = time.monotonic() - began

    assert elapsed < 0.20, "the scan waited for the slow response instead of expiring"
    assert result.status is ScanStatus.INCOMPLETE
    assert any("scan_deadline_reached" in r for r in result.incomplete_reasons)
    assert result.target is None
    assert result.observed_top_likes is None


@pytest.mark.asyncio
async def test_the_deadline_leaves_no_pending_task_behind():
    reader = CountingReader(pages=3, delay=0.30)
    before = len(asyncio.all_tasks())
    await run(reader, ScanLimits(deadline_seconds=0.02))
    await asyncio.sleep(0)
    after = [t for t in asyncio.all_tasks() if not t.done()]
    assert len(after) <= before


@pytest.mark.asyncio
async def test_partial_findings_survive_an_expired_deadline():
    """A page that did arrive is kept, labelled, and never ordered from."""
    reader = CountingReader(pages=3, delay=0.05)
    result = await run(reader, ScanLimits(deadline_seconds=0.08))
    assert result.status is ScanStatus.INCOMPLETE
    assert result.stats.pages_read >= 1
    assert result.observed_top_likes == 5  # shown, but never treated as final
    assert result.complete is False
    assert result.duration_ms >= 0
