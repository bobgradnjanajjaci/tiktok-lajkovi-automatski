import pytest

from app.database import Database
from app.models import DeliveryState, Outcome, RunMode

PANEL = "https://godofpanel.com/api/v2"
BIG_VIDEO_ID = "7312345678901234567"
BIG_COMMENT_ID = "7398765432109876543"
BIG_ORDER_ID = "9007199254740993"  # 2**53 + 1


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


async def make_batch(db, *, key="key-1", mode=RunMode.DRY_RUN, links=None):
    return await db.create_batch(
        idempotency_key=key,
        mode=mode,
        keyword="Mael Vorran",
        comment_scope="all",
        frozen_config={"keyword": "Mael Vorran", "comment_scope": "all"},
        links=links or [f"https://www.tiktok.com/@a/video/{BIG_VIDEO_ID}"],
    )


@pytest.mark.asyncio
async def test_idempotency_key_prevents_duplicate_batches(db):
    first_id, created_first = await make_batch(db, key="same")
    second_id, created_second = await make_batch(db, key="same")
    assert created_first is True
    assert created_second is False
    assert first_id == second_id
    batches = await db.list_batches()
    assert len(batches) == 1


@pytest.mark.asyncio
async def test_jobs_are_claimed_in_input_order(db):
    links = [f"https://www.tiktok.com/@a/video/730000000000000000{i}" for i in range(1, 4)]
    await make_batch(db, links=links)
    positions = []
    for _ in range(3):
        job = await db.claim_next_job()
        positions.append(job["position"])
        await db.finish_job(job["id"])
    assert positions == [1, 2, 3]
    assert await db.claim_next_job() is None


@pytest.mark.asyncio
async def test_a_job_is_claimed_only_once(db):
    await make_batch(db)
    first = await db.claim_next_job()
    second = await db.claim_next_job()
    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_video_lock_blocks_a_second_order_for_the_same_video(db):
    batch_id, _ = await make_batch(db, mode=RunMode.LIVE)
    job = await db.claim_next_job()
    attempt_id, reason = await db.begin_order_intent(
        item_id=job["item_id"],
        batch_id=batch_id,
        panel_url=PANEL,
        service_id="5836",
        video_id=BIG_VIDEO_ID,
        submitted_link=f"https://www.tiktok.com/@a/video/{BIG_VIDEO_ID}",
        owner_username="first.owner",
        comment_id=BIG_COMMENT_ID,
        quantity=500,
        lock_key=f"{PANEL}|video:{BIG_VIDEO_ID}",
    )
    assert attempt_id and reason is None

    # A different username and a different item cannot evade the lock, because
    # the key is the panel plus the resolved video id.
    blocked_id, blocked_reason = await db.begin_order_intent(
        item_id=job["item_id"],
        batch_id=batch_id,
        panel_url=PANEL,
        service_id="5836",
        video_id=BIG_VIDEO_ID,
        submitted_link=f"https://vm.tiktok.com/ZMalias/",
        owner_username="second.owner",
        comment_id="7398765432109876544",
        quantity=500,
        lock_key=f"{PANEL}|video:{BIG_VIDEO_ID}",
    )
    assert blocked_id is None
    assert blocked_reason.startswith("active_lock")


@pytest.mark.asyncio
async def test_accepted_order_keeps_the_lock_until_the_provider_reports_completion(db):
    batch_id, _ = await make_batch(db, mode=RunMode.LIVE)
    job = await db.claim_next_job()
    lock_key = f"{PANEL}|video:{BIG_VIDEO_ID}"
    attempt_id, _ = await db.begin_order_intent(
        item_id=job["item_id"], batch_id=batch_id, panel_url=PANEL, service_id="5836",
        video_id=BIG_VIDEO_ID, submitted_link="https://www.tiktok.com/@a/video/x",
        owner_username="owner", comment_id=BIG_COMMENT_ID, quantity=500, lock_key=lock_key,
    )
    await db.settle_order_intent(
        attempt_id=attempt_id, state="accepted", order_id=BIG_ORDER_ID,
        delivery_state=DeliveryState.UNKNOWN, response={"order": BIG_ORDER_ID},
    )

    # Still locked while delivery is unknown / pending / in progress.
    blocked, reason = await db.begin_order_intent(
        item_id=job["item_id"], batch_id=batch_id, panel_url=PANEL, service_id="5836",
        video_id=BIG_VIDEO_ID, submitted_link="https://www.tiktok.com/@a/video/x",
        owner_username="owner", comment_id=BIG_COMMENT_ID, quantity=500, lock_key=lock_key,
    )
    assert blocked is None

    prior = await db.existing_target_state(panel_url=PANEL, service_id="5836", video_id=BIG_VIDEO_ID)
    assert prior["state"] == "accepted"
    assert prior["order_id"] == BIG_ORDER_ID
    assert DeliveryState(prior["delivery_state"]) is DeliveryState.UNKNOWN

    await db.record_delivery_status(
        attempt_id=attempt_id, delivery_state=DeliveryState.COMPLETED, raw={"status": "Completed"}
    )
    prior = await db.existing_target_state(panel_url=PANEL, service_id="5836", video_id=BIG_VIDEO_ID)
    assert DeliveryState(prior["delivery_state"]) is DeliveryState.COMPLETED


@pytest.mark.asyncio
async def test_failed_order_releases_the_lock(db):
    batch_id, _ = await make_batch(db, mode=RunMode.LIVE)
    job = await db.claim_next_job()
    lock_key = f"{PANEL}|video:{BIG_VIDEO_ID}"
    attempt_id, _ = await db.begin_order_intent(
        item_id=job["item_id"], batch_id=batch_id, panel_url=PANEL, service_id="5836",
        video_id=BIG_VIDEO_ID, submitted_link="https://www.tiktok.com/@a/video/x",
        owner_username="owner", comment_id=BIG_COMMENT_ID, quantity=500, lock_key=lock_key,
    )
    await db.settle_order_intent(
        attempt_id=attempt_id, state="failed", order_id=None,
        delivery_state=DeliveryState.NOT_APPLICABLE, response={"error": "nope"},
        release_lock=True,
    )
    again, reason = await db.begin_order_intent(
        item_id=job["item_id"], batch_id=batch_id, panel_url=PANEL, service_id="5836",
        video_id=BIG_VIDEO_ID, submitted_link="https://www.tiktok.com/@a/video/x",
        owner_username="owner", comment_id=BIG_COMMENT_ID, quantity=500, lock_key=lock_key,
    )
    # A definitive rejection created nothing remotely, so a new attempt is
    # allowed. It used to raise sqlite3.IntegrityError, because the local key
    # was deterministic and collided with the journalled failed attempt.
    assert again is not None, reason
    assert again != attempt_id

    # Both attempts are still in the journal; history is not overwritten.
    rows = await db.attempts_for_item(job["item_id"])
    assert [row["state"] for row in rows] == ["failed", "submitting"]
    assert len({row["local_key"] for row in rows}) == 2


@pytest.mark.asyncio
async def test_an_unknown_attempt_blocks_a_new_one_with_a_controlled_reason(db):
    """An order that may have been accepted is never retried automatically."""
    batch_id, _ = await make_batch(db, mode=RunMode.LIVE)
    job = await db.claim_next_job()
    lock_key = f"{PANEL}|video:{BIG_VIDEO_ID}"
    common = dict(
        item_id=job["item_id"], batch_id=batch_id, panel_url=PANEL, service_id="5836",
        video_id=BIG_VIDEO_ID, submitted_link="https://www.tiktok.com/@a/video/x",
        owner_username="owner", comment_id=BIG_COMMENT_ID, quantity=500,
        lock_key=lock_key,
    )
    attempt_id, _ = await db.begin_order_intent(**common)
    await db.settle_order_intent(
        attempt_id=attempt_id, state="unknown", order_id=None,
        delivery_state=DeliveryState.UNKNOWN, response=None,
        lock_state="unknown", release_lock=True,  # even if the lock were freed
    )
    again, reason = await db.begin_order_intent(**common)
    assert again is None
    assert reason == "prior_attempt:unknown"


@pytest.mark.asyncio
async def test_an_accepted_attempt_blocks_a_new_one(db):
    batch_id, _ = await make_batch(db, mode=RunMode.LIVE)
    job = await db.claim_next_job()
    lock_key = f"{PANEL}|video:{BIG_VIDEO_ID}"
    common = dict(
        item_id=job["item_id"], batch_id=batch_id, panel_url=PANEL, service_id="5836",
        video_id=BIG_VIDEO_ID, submitted_link="https://www.tiktok.com/@a/video/x",
        owner_username="owner", comment_id=BIG_COMMENT_ID, quantity=500,
        lock_key=lock_key,
    )
    attempt_id, _ = await db.begin_order_intent(**common)
    await db.settle_order_intent(
        attempt_id=attempt_id, state="accepted", order_id=BIG_ORDER_ID,
        delivery_state=DeliveryState.PENDING, response={"order": BIG_ORDER_ID},
        release_lock=True,
    )
    again, reason = await db.begin_order_intent(**common)
    assert again is None
    assert reason == "prior_attempt:accepted"


@pytest.mark.asyncio
async def test_repeat_attempts_keep_incrementing_the_journal_key(db):
    """Two definitive rejections in a row must both be journalled."""
    batch_id, _ = await make_batch(db, mode=RunMode.LIVE)
    job = await db.claim_next_job()
    lock_key = f"{PANEL}|video:{BIG_VIDEO_ID}"
    common = dict(
        item_id=job["item_id"], batch_id=batch_id, panel_url=PANEL, service_id="5836",
        video_id=BIG_VIDEO_ID, submitted_link="https://www.tiktok.com/@a/video/x",
        owner_username="owner", comment_id=BIG_COMMENT_ID, quantity=500,
        lock_key=lock_key,
    )
    keys = []
    for _ in range(3):
        attempt_id, reason = await db.begin_order_intent(**common)
        assert attempt_id is not None, reason
        await db.settle_order_intent(
            attempt_id=attempt_id, state="failed", order_id=None,
            delivery_state=DeliveryState.NOT_APPLICABLE, response={"error": "nope"},
            release_lock=True,
        )
        keys.append(attempt_id)
    rows = await db.attempts_for_item(job["item_id"])
    assert len(rows) == 3
    assert len({row["local_key"] for row in rows}) == 3
    assert len(set(keys)) == 3


@pytest.mark.asyncio
async def test_restart_turns_submitting_into_unknown_and_does_not_retry(db):
    batch_id, _ = await make_batch(db, mode=RunMode.LIVE)
    job = await db.claim_next_job()
    await db.begin_order_intent(
        item_id=job["item_id"], batch_id=batch_id, panel_url=PANEL, service_id="5836",
        video_id=BIG_VIDEO_ID, submitted_link="https://www.tiktok.com/@a/video/x",
        owner_username="owner", comment_id=BIG_COMMENT_ID, quantity=500,
        lock_key=f"{PANEL}|video:{BIG_VIDEO_ID}",
    )
    # Simulate a crash: the job is still claimed and the attempt still submitting.
    recovery = await db.recover_claimed_jobs()
    assert recovery["submitting_to_unknown"] == 1

    item = await db.get_item(job["item_id"])
    assert item["outcome"] == Outcome.SUBMISSION_UNKNOWN.value

    # The item is NOT requeued, so no second paid request can happen.
    assert await db.claim_next_job() is None

    prior = await db.existing_target_state(panel_url=PANEL, service_id="5836", video_id=BIG_VIDEO_ID)
    assert prior["state"] == "unknown"


@pytest.mark.asyncio
async def test_restart_requeues_an_unsubmitted_claimed_job(db):
    await make_batch(db)
    job = await db.claim_next_job()
    recovery = await db.recover_claimed_jobs()
    assert recovery["requeued_jobs"] == 1
    again = await db.claim_next_job()
    assert again is not None
    assert again["item_id"] == job["item_id"]


@pytest.mark.asyncio
async def test_stop_cancels_only_queued_items(db):
    links = [f"https://www.tiktok.com/@a/video/730000000000000000{i}" for i in range(1, 4)]
    batch_id, _ = await make_batch(db, links=links)
    first = await db.claim_next_job()
    cancelled = await db.request_stop(batch_id)
    assert cancelled == 2
    assert await db.claim_next_job() is None
    batch = await db.get_batch(batch_id)
    outcomes = [item["outcome"] for item in batch["items"]]
    assert outcomes[1:] == [Outcome.CANCELLED.value, Outcome.CANCELLED.value]
    assert first is not None


@pytest.mark.asyncio
async def test_ids_survive_serialization_as_strings(db):
    batch_id, _ = await make_batch(db)
    job = await db.claim_next_job()
    await db.update_item(
        job["item_id"],
        video_id=BIG_VIDEO_ID,
        target_comment_id=BIG_COMMENT_ID,
        order_id=BIG_ORDER_ID,
        owner_user_id="6100000000000000001",
    )
    batch = await db.get_batch(batch_id)
    item = batch["items"][0]
    assert item["video_id"] == BIG_VIDEO_ID
    assert item["target_comment_id"] == BIG_COMMENT_ID
    assert item["order_id"] == BIG_ORDER_ID
    for key in ("video_id", "target_comment_id", "order_id", "owner_user_id"):
        assert isinstance(item[key], str)
        # Round-tripping through float would corrupt these values.
        assert int(item[key]) != int(float(item[key])) or len(item[key]) < 16


@pytest.mark.asyncio
async def test_dry_run_does_not_create_an_order_lock(db):
    batch_id, _ = await make_batch(db, mode=RunMode.DRY_RUN)
    job = await db.claim_next_job()
    await db.update_item(job["item_id"], outcome=Outcome.DRY_RUN_COMPLETE.value, quantity=500)
    prior = await db.existing_target_state(panel_url=PANEL, service_id="5836", video_id=BIG_VIDEO_ID)
    assert prior is None


@pytest.mark.asyncio
async def test_events_are_ordered_and_resumable(db):
    batch_id, _ = await make_batch(db)
    first = await db.add_event("item", {"n": 1}, batch_id=batch_id)
    second = await db.add_event("item", {"n": 2}, batch_id=batch_id)
    assert second > first
    events = await db.events_since(first)
    assert [event["payload"]["n"] for event in events] == [2]
    assert await db.latest_event_id() == second
