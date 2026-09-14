"""Exercise shutdown/cancellation races without crashing a real SQLite handle."""

import asyncio
import threading

import pytest

from app.database import Database


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_query", "next_action"),
    [(False, "close"), (True, "close"), (True, "query")],
)
async def test_active_database_work_finishes_before_next_operation(
    tmp_path, cancel_query, next_action
):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    history = []

    class RecordingConnection:
        # Closing a real SQLite connection concurrently can segfault Python.
        # This probe records the same ordering violation as a normal assertion.
        def close(self):
            history.append("close")

    db = Database(tmp_path / "unused.db")
    db._conn = RecordingConnection()

    def blocking_query():
        history.append("query_started")
        started.set()
        try:
            if not release.wait(5):
                raise AssertionError("test did not release the database operation")
            history.append("query_finished")
        finally:
            finished.set()

    first = asyncio.create_task(db._run(blocking_query))
    following = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if cancel_query:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

        following = asyncio.create_task(
            db.close()
            if next_action == "close"
            else db._run(lambda: history.append("next_query"))
        )
        # The first native operation is deliberately blocked. Closing the
        # connection or starting another operation must remain blocked too.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(following), timeout=0.1)
        assert history == ["query_started"]
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.gather(first, return_exceptions=True)
        if following is not None:
            await asyncio.wait_for(following, timeout=2)
        await db.close()

    expected = ["query_started", "query_finished"]
    if next_action == "query":
        expected.append("next_query")
    assert history == expected + ["close"]
