"""End-to-end batch tests with a fixture reader and a mocked panel.

No real network call is made anywhere in this file, and no paid order can be
issued: the panel transport is a MockTransport that records every request.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from app import url_resolver
from app.config import Settings
from app.database import Database
from app.models import Outcome, RunMode
from app.smm_client import SmmClient
from app.worker import Worker

PANEL = "https://godofpanel.example/api/v2"
VIDEO_1 = "7300000000000000001"
VIDEO_2 = "7300000000000000002"
VIDEO_3 = "7300000000000000003"


def link(video_id: str) -> str:
    return f"https://www.tiktok.com/@creator/video/{video_id}"


SERVICES_PAYLOAD = [
    {
        "service": "5836",
        "name": "TikTok Comment Likes",
        "type": "15",
        "category": "TikTok",
        "rate": "0.90",
        "min": "10",
        "max": "1000000",
        "refill": False,
    }
]


class PanelRecorder:
    def __init__(self, order_response=None):
        self.requests: list[dict[str, str]] = []
        self._order_response = order_response or (lambda n: httpx.Response(200, json={"order": f"90071992547409{n:02d}"}))
        self._orders = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        self.requests.append(form)
        action = form.get("action")
        if action == "services":
            return httpx.Response(200, json=SERVICES_PAYLOAD)
        if action == "balance":
            return httpx.Response(200, json={"balance": "125.40", "currency": "USD"})
        if action == "add":
            self._orders += 1
            return self._order_response(self._orders)
        if action == "status":
            return httpx.Response(200, json={"status": "Pending", "charge": "0.45", "remains": "500"})
        return httpx.Response(200, json={"error": f"unexpected action {action}"})

    @property
    def order_requests(self) -> list[dict[str, str]]:
        return [form for form in self.requests if form.get("action") == "add"]


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    async def ok(_host):
        return None

    monkeypatch.setattr(url_resolver, "_assert_public_host", ok)


def build_settings(tmp_path, fixture_dir, **overrides) -> Settings:
    values = {
        "ENVIRONMENT": "test",
        "API_KEY": "test-key-not-real",
        "PANEL_URL": PANEL,
        "SERVICE_ID": "5836",
        "KEYWORD": "Mael Vorran",
        "COMMENT_SCOPE": "all",
        "COMMENT_READER": "fixture",
        "READER_FIXTURE_DIR": str(fixture_dir),
        "DATABASE_PATH": str(tmp_path / "app.db"),
        "ADMIN_PASSWORD": "test-password-1234",
        "SESSION_SECRET": "x" * 48,
        "STATUS_POLL_INTERVAL_SECONDS": 3600,
    }
    values.update(overrides)
    return Settings(**values)


class Harness:
    def __init__(self, settings, db, worker, recorder, http):
        self.settings = settings
        self.db = db
        self.worker = worker
        self.recorder = recorder
        self.http = http

    async def drain(self, timeout: float = 10.0):
        """Run the worker until the queue is empty."""
        loop_deadline = asyncio.get_running_loop().time() + timeout
        while await self.db.has_ready_jobs():
            job = await self.db.claim_next_job()
            if job is None:
                break
            await self.worker._process_job(job)
            if asyncio.get_running_loop().time() > loop_deadline:
                raise AssertionError("worker did not drain in time")


async def make_harness(tmp_path, fixture_dir, *, recorder=None, **overrides):
    settings = build_settings(tmp_path, fixture_dir, **overrides)
    db = Database(settings.database_file)
    await db.connect()
    recorder = recorder or PanelRecorder()
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    smm = SmmClient(http, panel_url=PANEL, api_key=settings.API_KEY, service_id="5836")
    worker = Worker(settings=settings, db=db, http_client=http, smm=smm)
    return Harness(settings, db, worker, recorder, http)


async def enqueue(harness, links, mode, key="batch-1"):
    batch_id, created = await harness.db.create_batch(
        idempotency_key=key,
        mode=mode,
        keyword=harness.settings.KEYWORD,
        comment_scope=harness.settings.COMMENT_SCOPE.value,
        frozen_config=harness.settings.redacted(),
        links=links,
    )
    assert created
    return batch_id


# ---------------------------------------------------------------- dry run
@pytest.mark.asyncio
async def test_dry_run_makes_zero_paid_calls(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    batch_id = await enqueue(harness, [link(VIDEO_1)], RunMode.DRY_RUN)
    await harness.drain()

    batch = await harness.db.get_batch(batch_id)
    item = batch["items"][0]
    assert item["outcome"] == Outcome.DRY_RUN_COMPLETE.value
    assert item["order_id"] is None
    assert harness.recorder.order_requests == []

    # The reply with 301 likes is the true maximum, so the quantity is 391.
    assert item["top_likes"] == 301
    assert item["quantity"] == 391
    # The first matching comment is selected, not the most liked match.
    assert item["target_comment_id"] == "7300000000000000102"
    assert item["target_likes"] == 7
    assert item["owner_username"] == "beta.fan"  # presentation @ removed
    await harness.db.close()
    await harness.http.aclose()


@pytest.mark.asyncio
async def test_keyword_not_found_is_reported_after_a_complete_scan(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    batch_id = await enqueue(harness, [link(VIDEO_2)], RunMode.DRY_RUN)
    await harness.drain()
    item = (await harness.db.get_batch(batch_id))["items"][0]
    assert item["outcome"] == Outcome.KEYWORD_NOT_FOUND.value
    assert item["scan_complete"] is True
    await harness.db.close()
    await harness.http.aclose()


@pytest.mark.asyncio
async def test_sequential_processing_in_input_order(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    links = [link(VIDEO_1), link(VIDEO_2), link(VIDEO_3)]
    batch_id = await enqueue(harness, links, RunMode.DRY_RUN)
    await harness.drain()
    batch = await harness.db.get_batch(batch_id)
    assert [item["position"] for item in batch["items"]] == [1, 2, 3]
    starts = [item["started_at"] for item in batch["items"]]
    assert starts == sorted(starts)
    assert all(item["finished_at"] for item in batch["items"])
    await harness.db.close()
    await harness.http.aclose()


@pytest.mark.asyncio
async def test_one_bad_link_does_not_stop_the_batch(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    batch_id = await enqueue(
        harness, ["https://example.com/not-tiktok", link(VIDEO_1)], RunMode.DRY_RUN
    )
    await harness.drain()
    items = (await harness.db.get_batch(batch_id))["items"]
    assert items[0]["outcome"] == Outcome.URL_INVALID.value
    assert items[1]["outcome"] == Outcome.DRY_RUN_COMPLETE.value
    await harness.db.close()
    await harness.http.aclose()


# ------------------------------------------------------------------- live
@pytest.mark.asyncio
async def test_live_run_submits_the_exact_payload_once(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    harness.worker.reader.__class__.configured = property(lambda self: True)
    try:
        batch_id = await enqueue(harness, [link(VIDEO_1)], RunMode.LIVE)
        await harness.drain()
        item = (await harness.db.get_batch(batch_id))["items"][0]

        assert item["outcome"] == Outcome.SUBMITTED.value
        assert item["order_id"] is not None
        assert isinstance(item["order_id"], str)

        orders = harness.recorder.order_requests
        assert len(orders) == 1
        assert orders[0] == {
            "key": "test-key-not-real",
            "action": "add",
            "service": "5836",
            "link": "https://www.tiktok.com/@creator/video/7300000000000000001",
            "quantity": "391",
            "username": "beta.fan",
        }
        # Never a comment permalink, never an invented comment id field.
        assert "comment" not in orders[0]
        assert item["delivery_state"] == "unknown"  # an order id is not delivery
    finally:
        del harness.worker.reader.__class__.configured
        await harness.db.close()
        await harness.http.aclose()


@pytest.mark.asyncio
async def test_duplicate_video_alias_and_second_batch_cannot_double_spend(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    harness.worker.reader.__class__.configured = property(lambda self: True)
    try:
        await enqueue(harness, [link(VIDEO_1)], RunMode.LIVE, key="b1")
        await harness.drain()
        assert len(harness.recorder.order_requests) == 1

        # Same video, second batch. The panel must not be called again.
        batch_two = await enqueue(harness, [link(VIDEO_1)], RunMode.LIVE, key="b2")
        await harness.drain()
        assert len(harness.recorder.order_requests) == 1
        item = (await harness.db.get_batch(batch_two))["items"][0]
        assert item["outcome"] == Outcome.ACTIVE_ORDER_EXISTS.value
    finally:
        del harness.worker.reader.__class__.configured
        await harness.db.close()
        await harness.http.aclose()


@pytest.mark.asyncio
async def test_ambiguous_same_owner_target_is_not_ordered(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    harness.worker.reader.__class__.configured = property(lambda self: True)
    try:
        batch_id = await enqueue(harness, [link(VIDEO_3)], RunMode.LIVE)
        await harness.drain()
        item = (await harness.db.get_batch(batch_id))["items"][0]
        assert item["outcome"] == Outcome.TARGET_AMBIGUOUS.value
        assert harness.recorder.order_requests == []
    finally:
        del harness.worker.reader.__class__.configured
        await harness.db.close()
        await harness.http.aclose()


@pytest.mark.asyncio
async def test_order_timeout_becomes_unknown_and_no_second_request(tmp_path, fixture_dir):
    class TimeoutRecorder(PanelRecorder):
        def handler(self, request):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.requests.append(form)
            if form.get("action") == "add":
                raise httpx.ReadTimeout("timed out", request=request)
            if form.get("action") == "services":
                return httpx.Response(200, json=SERVICES_PAYLOAD)
            return httpx.Response(200, json={})

    harness = await make_harness(tmp_path, fixture_dir, recorder=TimeoutRecorder())
    harness.worker.reader.__class__.configured = property(lambda self: True)
    try:
        batch_id = await enqueue(harness, [link(VIDEO_1)], RunMode.LIVE, key="t1")
        await harness.drain()
        item = (await harness.db.get_batch(batch_id))["items"][0]
        assert item["outcome"] == Outcome.SUBMISSION_UNKNOWN.value
        assert len(harness.recorder.order_requests) == 1

        # A later batch for the same video is blocked, not retried.
        batch_two = await enqueue(harness, [link(VIDEO_1)], RunMode.LIVE, key="t2")
        await harness.drain()
        assert len(harness.recorder.order_requests) == 1
        item_two = (await harness.db.get_batch(batch_two))["items"][0]
        assert item_two["outcome"] == Outcome.ACTIVE_ORDER_EXISTS.value
    finally:
        del harness.worker.reader.__class__.configured
        await harness.db.close()
        await harness.http.aclose()


@pytest.mark.asyncio
async def test_quantity_out_of_range_is_not_clamped(tmp_path, fixture_dir):
    class NarrowRecorder(PanelRecorder):
        def handler(self, request):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.requests.append(form)
            if form.get("action") == "services":
                return httpx.Response(
                    200,
                    json=[dict(SERVICES_PAYLOAD[0], min="1000", max="2000")],
                )
            return httpx.Response(200, json={"order": "1"})

    harness = await make_harness(tmp_path, fixture_dir, recorder=NarrowRecorder())
    harness.worker.reader.__class__.configured = property(lambda self: True)
    try:
        batch_id = await enqueue(harness, [link(VIDEO_1)], RunMode.LIVE)
        await harness.drain()
        item = (await harness.db.get_batch(batch_id))["items"][0]
        assert item["outcome"] == Outcome.QUANTITY_OUT_OF_RANGE.value
        assert item["quantity"] == 391  # recorded, never clamped to 1000
        assert harness.recorder.order_requests == []
    finally:
        del harness.worker.reader.__class__.configured
        await harness.db.close()
        await harness.http.aclose()


@pytest.mark.asyncio
async def test_incompatible_service_blocks_ordering(tmp_path, fixture_dir):
    class WrongTypeRecorder(PanelRecorder):
        def handler(self, request):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.requests.append(form)
            if form.get("action") == "services":
                return httpx.Response(200, json=[dict(SERVICES_PAYLOAD[0], type="Default")])
            return httpx.Response(200, json={"order": "1"})

    harness = await make_harness(tmp_path, fixture_dir, recorder=WrongTypeRecorder())
    harness.worker.reader.__class__.configured = property(lambda self: True)
    try:
        batch_id = await enqueue(harness, [link(VIDEO_1)], RunMode.LIVE)
        await harness.drain()
        item = (await harness.db.get_batch(batch_id))["items"][0]
        assert item["outcome"] == Outcome.SERVICE_CONFIGURATION_REQUIRED.value
        assert harness.recorder.order_requests == []
    finally:
        del harness.worker.reader.__class__.configured
        await harness.db.close()
        await harness.http.aclose()


@pytest.mark.asyncio
async def test_live_is_refused_while_the_reader_is_unconfigured(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    batch_id = await enqueue(harness, [link(VIDEO_1)], RunMode.LIVE)
    await harness.drain()
    item = (await harness.db.get_batch(batch_id))["items"][0]
    assert item["outcome"] == Outcome.READER_UNCONFIGURED.value
    assert harness.recorder.order_requests == []
    await harness.db.close()
    await harness.http.aclose()


@pytest.mark.asyncio
async def test_switching_modes_does_not_convert_a_dry_run_into_a_purchase(tmp_path, fixture_dir):
    harness = await make_harness(tmp_path, fixture_dir)
    await enqueue(harness, [link(VIDEO_1)], RunMode.DRY_RUN, key="dry")
    await harness.drain()
    assert harness.recorder.order_requests == []
    # The dry-run batch keeps its own frozen mode; nothing is reprocessed.
    batches = await harness.db.list_batches()
    assert batches[0]["mode"] == "dry_run"
    assert await harness.db.has_ready_jobs() is False
    await harness.db.close()
    await harness.http.aclose()


@pytest.mark.asyncio
async def test_worker_loop_drains_without_a_polling_delay(tmp_path, fixture_dir):
    """The worker is woken by an event and drains every ready job immediately."""
    harness = await make_harness(tmp_path, fixture_dir)
    await harness.worker.start()
    try:
        await enqueue(harness, [link(VIDEO_1), link(VIDEO_2)], RunMode.DRY_RUN)
        harness.worker.notify()
        for _ in range(100):
            if not await harness.db.has_ready_jobs():
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        batches = await harness.db.list_batches()
        outcomes = [item["outcome"] for item in batches[0]["items"]]
        assert Outcome.PENDING.value not in outcomes
        assert Outcome.PROCESSING.value not in outcomes
    finally:
        await harness.worker.stop()
        await harness.db.close()
        await harness.http.aclose()
