"""Durable state: batches, items, the job queue, the order journal and locks.

Everything that could cost money is written here BEFORE the network call and
updated after it. An in-memory queue would lose work on redeploy, so the queue
lives in SQLite on the Railway volume.

SQLite is used through ``asyncio.to_thread`` with WAL and a busy timeout. That is
ample for a single-replica, single-worker, ten-links-per-batch dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .models import ACTIVE_DELIVERY_STATES, DeliveryState, Outcome, RunMode, iso, utcnow

log = logging.getLogger("app.database")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS batches (
    id                TEXT PRIMARY KEY,
    idempotency_key   TEXT NOT NULL UNIQUE,
    mode              TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'queued',
    keyword           TEXT NOT NULL,
    comment_scope     TEXT NOT NULL,
    frozen_config     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    stop_requested_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id                    TEXT PRIMARY KEY,
    batch_id              TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    position              INTEGER NOT NULL,
    input_url             TEXT NOT NULL,
    video_id              TEXT,
    canonical_url         TEXT,
    video_verified        INTEGER NOT NULL DEFAULT 0,
    outcome               TEXT NOT NULL DEFAULT 'pending',
    delivery_state        TEXT NOT NULL DEFAULT 'not_applicable',
    target_comment_id     TEXT,
    target_text           TEXT,
    target_likes          INTEGER,
    owner_username        TEXT,
    owner_user_id         TEXT,
    submitted_link        TEXT,
    top_likes             INTEGER,
    top_comment_id        TEXT,
    scan_status           TEXT,
    scan_complete         INTEGER,
    pages_read            INTEGER DEFAULT 0,
    comments_read         INTEGER DEFAULT 0,
    quantity              INTEGER,
    estimated_cost        TEXT,
    order_id              TEXT,
    error                 TEXT,
    scan_json             TEXT,
    timings_json          TEXT,
    started_at            TEXT,
    finished_at           TEXT,
    UNIQUE (batch_id, position)
);
CREATE INDEX IF NOT EXISTS idx_items_batch ON items(batch_id, position);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    batch_id     TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    item_id      TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    state        TEXT NOT NULL DEFAULT 'queued',
    attempts     INTEGER NOT NULL DEFAULT 0,
    claimed_at   TEXT,
    finished_at  TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(state, created_at, position);

CREATE TABLE IF NOT EXISTS order_attempts (
    id              TEXT PRIMARY KEY,
    local_key       TEXT NOT NULL UNIQUE,
    item_id         TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    batch_id        TEXT NOT NULL,
    panel_url       TEXT NOT NULL,
    service_id      TEXT NOT NULL,
    video_id        TEXT NOT NULL,
    submitted_link  TEXT NOT NULL,
    owner_username  TEXT NOT NULL,
    comment_id      TEXT,
    quantity        INTEGER NOT NULL,
    state           TEXT NOT NULL,
    order_id        TEXT,
    delivery_state  TEXT NOT NULL DEFAULT 'not_applicable',
    charge          TEXT,
    response_json   TEXT,
    resolution      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_status_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempt_target
    ON order_attempts(panel_url, service_id, video_id);
CREATE INDEX IF NOT EXISTS idx_attempt_state ON order_attempts(state, delivery_state);

CREATE TABLE IF NOT EXISTS video_locks (
    lock_key     TEXT PRIMARY KEY,
    video_id     TEXT NOT NULL,
    panel_url    TEXT NOT NULL,
    attempt_id   TEXT,
    item_id      TEXT,
    state        TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    released_at  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT,
    item_id     TEXT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id, id);
"""


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        #: Set whenever a new event row is written, so SSE streams wake up
        #: immediately instead of polling on a fixed interval.
        self.event_written = asyncio.Event()

    # ------------------------------------------------------------------ setup
    def connect_sync(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        probe = self.path.parent / ".write-probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"DATABASE_PATH directory {self.path.parent} is not writable: {exc}. "
                "On Railway, attach a volume mounted at /data and set "
                "DATABASE_PATH=/data/app.db."
            ) from exc
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    async def connect(self) -> None:
        await asyncio.to_thread(self.connect_sync)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ----------------------------------------------------------------- batches
    async def create_batch(
        self,
        *,
        idempotency_key: str,
        mode: RunMode,
        keyword: str,
        comment_scope: str,
        frozen_config: dict[str, Any],
        links: list[str],
    ) -> tuple[str, bool]:
        """Create a batch and its jobs atomically. Returns (batch_id, created)."""

        def work() -> tuple[str, bool]:
            conn = self.conn
            existing = conn.execute(
                "SELECT id FROM batches WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return existing["id"], False
            batch_id = new_id()
            now = iso(utcnow())
            with conn:
                conn.execute(
                    "INSERT INTO batches (id, idempotency_key, mode, state, keyword,"
                    " comment_scope, frozen_config, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        idempotency_key,
                        mode.value,
                        "queued",
                        keyword,
                        comment_scope,
                        json.dumps(frozen_config, sort_keys=True),
                        now,
                    ),
                )
                for position, link in enumerate(links, start=1):
                    item_id = new_id()
                    conn.execute(
                        "INSERT INTO items (id, batch_id, position, input_url, outcome)"
                        " VALUES (?,?,?,?,?)",
                        (item_id, batch_id, position, link, Outcome.PENDING.value),
                    )
                    conn.execute(
                        "INSERT INTO jobs (id, batch_id, item_id, position, state, created_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (new_id(), batch_id, item_id, position, "queued", now),
                    )
            return batch_id, True

        return await self._run(work)

    async def request_stop(self, batch_id: str) -> int:
        def work() -> int:
            conn = self.conn
            now = iso(utcnow())
            with conn:
                conn.execute(
                    "UPDATE batches SET stop_requested_at = ? WHERE id = ? AND"
                    " stop_requested_at IS NULL",
                    (now, batch_id),
                )
                cur = conn.execute(
                    "SELECT item_id FROM jobs WHERE batch_id = ? AND state = 'queued'",
                    (batch_id,),
                )
                item_ids = [row["item_id"] for row in cur.fetchall()]
                conn.execute(
                    "UPDATE jobs SET state = 'cancelled', finished_at = ? WHERE"
                    " batch_id = ? AND state = 'queued'",
                    (now, batch_id),
                )
                for item_id in item_ids:
                    conn.execute(
                        "UPDATE items SET outcome = ?, error = ?, finished_at = ?"
                        " WHERE id = ?",
                        (
                            Outcome.CANCELLED.value,
                            "Stopped before submission. Orders already accepted by the"
                            " panel are not cancelled automatically.",
                            now,
                            item_id,
                        ),
                    )
            return len(item_ids)

        return await self._run(work)

    async def stop_requested(self, batch_id: str) -> bool:
        def work() -> bool:
            row = self.conn.execute(
                "SELECT stop_requested_at FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return bool(row and row["stop_requested_at"])

        return await self._run(work)

    async def finish_batch_if_done(self, batch_id: str) -> None:
        def work() -> None:
            conn = self.conn
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE batch_id = ? AND state IN"
                " ('queued','claimed')",
                (batch_id,),
            ).fetchone()["n"]
            if remaining == 0:
                with conn:
                    conn.execute(
                        "UPDATE batches SET state = 'finished', finished_at = ?"
                        " WHERE id = ? AND finished_at IS NULL",
                        (iso(utcnow()), batch_id),
                    )

        await self._run(work)

    # -------------------------------------------------------------- job queue
    async def claim_next_job(self) -> dict[str, Any] | None:
        """Transactionally claim the oldest queued job, lowest position first."""

        def work() -> dict[str, Any] | None:
            conn = self.conn
            now = iso(utcnow())
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT j.id, j.batch_id, j.item_id, j.position, b.mode, b.keyword,"
                    " b.comment_scope, b.frozen_config, i.input_url"
                    " FROM jobs j"
                    " JOIN batches b ON b.id = j.batch_id"
                    " JOIN items i ON i.id = j.item_id"
                    " WHERE j.state = 'queued' AND b.stop_requested_at IS NULL"
                    " ORDER BY b.created_at ASC, j.position ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE jobs SET state = 'claimed', claimed_at = ?, attempts ="
                    " attempts + 1 WHERE id = ?",
                    (now, row["id"]),
                )
                conn.execute(
                    "UPDATE batches SET state = 'running', started_at ="
                    " COALESCE(started_at, ?) WHERE id = ?",
                    (now, row["batch_id"]),
                )
                conn.execute(
                    "UPDATE items SET outcome = ?, started_at = ? WHERE id = ?",
                    (Outcome.PROCESSING.value, now, row["item_id"]),
                )
            return dict(row)

        return await self._run(work)

    async def finish_job(self, job_id: str, state: str = "done") -> None:
        def work() -> None:
            with self.conn as conn:
                conn.execute(
                    "UPDATE jobs SET state = ?, finished_at = ? WHERE id = ?",
                    (state, iso(utcnow()), job_id),
                )

        await self._run(work)

    async def recover_claimed_jobs(self) -> dict[str, int]:
        """Startup recovery.

        * Jobs left ``claimed`` by a crash are requeued - reading is idempotent.
        * Order attempts left ``submitting`` become ``unknown``: the process may
          have died after the panel accepted the order. They are never retried
          automatically and their video lock stays held.
        """

        def work() -> dict[str, int]:
            conn = self.conn
            now = iso(utcnow())
            with conn:
                unknown = conn.execute(
                    "SELECT id, item_id FROM order_attempts WHERE state = 'submitting'"
                ).fetchall()
                for row in unknown:
                    conn.execute(
                        "UPDATE order_attempts SET state = 'unknown', delivery_state = ?,"
                        " updated_at = ?, resolution = COALESCE(resolution, ?)"
                        " WHERE id = ?",
                        (
                            DeliveryState.UNKNOWN.value,
                            now,
                            "process restarted while submitting; outcome unverified",
                            row["id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE items SET outcome = ?, delivery_state = ?, error = ?,"
                        " finished_at = COALESCE(finished_at, ?) WHERE id = ?",
                        (
                            Outcome.SUBMISSION_UNKNOWN.value,
                            DeliveryState.UNKNOWN.value,
                            "Submission interrupted. Check the panel manually before"
                            " retrying; this link stays locked.",
                            now,
                            row["item_id"],
                        ),
                    )
                requeued = conn.execute(
                    "SELECT id, item_id FROM jobs WHERE state = 'claimed'"
                ).fetchall()
                for row in requeued:
                    has_attempt = conn.execute(
                        "SELECT 1 FROM order_attempts WHERE item_id = ?", (row["item_id"],)
                    ).fetchone()
                    if has_attempt:
                        # Do not re-read or re-submit an item that already got as
                        # far as an order attempt; leave it for manual review.
                        conn.execute(
                            "UPDATE jobs SET state = 'done', finished_at = ? WHERE id = ?",
                            (now, row["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE jobs SET state = 'queued', claimed_at = NULL WHERE id = ?",
                            (row["id"],),
                        )
                        conn.execute(
                            "UPDATE items SET outcome = ? WHERE id = ?",
                            (Outcome.PENDING.value, row["item_id"]),
                        )
            return {"submitting_to_unknown": len(unknown), "requeued_jobs": len(requeued)}

        return await self._run(work)

    async def has_ready_jobs(self) -> bool:
        def work() -> bool:
            row = self.conn.execute(
                "SELECT 1 FROM jobs j JOIN batches b ON b.id = j.batch_id"
                " WHERE j.state = 'queued' AND b.stop_requested_at IS NULL LIMIT 1"
            ).fetchone()
            return row is not None

        return await self._run(work)

    # ------------------------------------------------------------------ items
    async def update_item(self, item_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())

        def work() -> None:
            with self.conn as conn:
                conn.execute(f"UPDATE items SET {columns} WHERE id = ?", (*values, item_id))

        await self._run(work)

    async def get_item(self, item_id: str) -> dict[str, Any] | None:
        def work():
            row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            return dict(row) if row else None

        return await self._run(work)

    # ------------------------------------------------------- order protection
    async def existing_target_state(
        self, *, panel_url: str, service_id: str, video_id: str
    ) -> dict[str, Any] | None:
        """Return the most relevant prior attempt for this panel/service/video."""

        def work():
            rows = self.conn.execute(
                "SELECT * FROM order_attempts WHERE panel_url = ? AND service_id = ?"
                " AND video_id = ? ORDER BY created_at DESC",
                (panel_url, service_id, video_id),
            ).fetchall()
            if not rows:
                return None
            for row in rows:
                if row["state"] in {"submitting", "unknown"}:
                    return dict(row)
                if row["state"] == "accepted" and DeliveryState(
                    row["delivery_state"]
                ) in ACTIVE_DELIVERY_STATES:
                    return dict(row)
            for row in rows:
                if row["state"] == "accepted":
                    return dict(row)
            return dict(rows[0])

        return await self._run(work)

    async def begin_order_intent(
        self,
        *,
        item_id: str,
        batch_id: str,
        panel_url: str,
        service_id: str,
        video_id: str,
        submitted_link: str,
        owner_username: str,
        comment_id: str | None,
        quantity: int,
        lock_key: str,
    ) -> tuple[str | None, str | None]:
        """Take the per-video lock and journal a ``submitting`` attempt.

        Returns ``(attempt_id, None)`` on success or ``(None, reason)`` when a
        new paid attempt must not be made. Both happen inside one transaction so
        a double submission cannot slip between the check and the insert.

        The journal keeps EVERY attempt, so the local key carries an attempt
        sequence. The policy for a repeat attempt on the same
        panel/service/video/owner/item is explicit:

        * a prior attempt still ``submitting`` or ``unknown`` blocks a new one
          (``prior_attempt:unknown``) - an uncertain paid order is never retried;
        * a prior ``accepted`` attempt blocks a new one
          (``prior_attempt:accepted``) until its delivery is resolved;
        * a prior ``failed`` attempt - a documented definitive rejection that
          created nothing remotely - allows a new attempt, journaled under the
          next sequence number, with the old row left intact.

        A key collision that still somehow occurs is reported as
        ``duplicate_attempt_key`` rather than escaping as an unhandled
        ``sqlite3.IntegrityError``.
        """
        key_prefix = f"{panel_url}|{service_id}|{video_id}|{owner_username.casefold()}|{item_id}"

        def work() -> tuple[str | None, str | None]:
            conn = self.conn
            now = iso(utcnow())
            attempt_id = new_id()
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    lock = conn.execute(
                        "SELECT * FROM video_locks WHERE lock_key = ?", (lock_key,)
                    ).fetchone()
                    if lock is not None and lock["released_at"] is None:
                        return None, f"active_lock:{lock['state']}"

                    previous = conn.execute(
                        "SELECT state FROM order_attempts WHERE local_key LIKE ?"
                        " ORDER BY created_at",
                        (key_prefix + "#%",),
                    ).fetchall()
                    for row in previous:
                        if row["state"] in {"submitting", "unknown"}:
                            return None, "prior_attempt:unknown"
                        if row["state"] == "accepted":
                            return None, "prior_attempt:accepted"
                    local_key = f"{key_prefix}#{len(previous) + 1}"

                    conn.execute(
                        "INSERT INTO video_locks (lock_key, video_id, panel_url, attempt_id,"
                        " item_id, state, acquired_at, released_at)"
                        " VALUES (?,?,?,?,?,?,?,NULL)"
                        " ON CONFLICT(lock_key) DO UPDATE SET attempt_id = excluded.attempt_id,"
                        " item_id = excluded.item_id, state = excluded.state,"
                        " acquired_at = excluded.acquired_at, released_at = NULL",
                        (lock_key, video_id, panel_url, attempt_id, item_id, "submitting", now),
                    )
                    conn.execute(
                        "INSERT INTO order_attempts (id, local_key, item_id, batch_id,"
                        " panel_url, service_id, video_id, submitted_link, owner_username,"
                        " comment_id, quantity, state, delivery_state, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            attempt_id,
                            local_key,
                            item_id,
                            batch_id,
                            panel_url,
                            service_id,
                            video_id,
                            submitted_link,
                            owner_username,
                            comment_id,
                            quantity,
                            "submitting",
                            DeliveryState.UNKNOWN.value,
                            now,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                # Never surface a raw database error to the paid-order path.
                log.warning("order intent rejected by the journal: %s", exc)
                return None, "duplicate_attempt_key"
            return attempt_id, None

        return await self._run(work)

    async def settle_order_intent(
        self,
        *,
        attempt_id: str,
        state: str,
        order_id: str | None,
        delivery_state: DeliveryState,
        response: dict[str, Any] | None,
        charge: Decimal | None = None,
        lock_state: str | None = None,
        release_lock: bool = False,
    ) -> None:
        def work() -> None:
            now = iso(utcnow())
            with self.conn as conn:
                conn.execute(
                    "UPDATE order_attempts SET state = ?, order_id = ?, delivery_state = ?,"
                    " response_json = ?, charge = ?, updated_at = ? WHERE id = ?",
                    (
                        state,
                        order_id,
                        delivery_state.value,
                        json.dumps(response, sort_keys=True) if response is not None else None,
                        None if charge is None else str(charge),
                        now,
                        attempt_id,
                    ),
                )
                if release_lock:
                    conn.execute(
                        "UPDATE video_locks SET state = ?, released_at = ? WHERE attempt_id = ?",
                        (lock_state or state, now, attempt_id),
                    )
                else:
                    conn.execute(
                        "UPDATE video_locks SET state = ? WHERE attempt_id = ?",
                        (lock_state or state, attempt_id),
                    )

        await self._run(work)

    async def attempts_for_item(self, item_id: str) -> list[dict[str, Any]]:
        """Every journalled attempt for one item, oldest first.

        The journal is append-only: a repeat attempt after a definitive
        rejection is a NEW row, so history is never overwritten or deleted.
        """

        def work() -> list[dict[str, Any]]:
            rows = self.conn.execute(
                "SELECT * FROM order_attempts WHERE item_id = ? ORDER BY created_at, rowid",
                (item_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._run(work)

    async def attempts_needing_status(self, limit: int) -> list[dict[str, Any]]:
        def work():
            rows = self.conn.execute(
                "SELECT * FROM order_attempts WHERE state = 'accepted' AND order_id IS NOT NULL"
                " AND delivery_state IN ('unknown','pending','in_progress','processing')"
                " ORDER BY COALESCE(last_status_at, created_at) ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._run(work)

    async def record_delivery_status(
        self,
        *,
        attempt_id: str,
        delivery_state: DeliveryState,
        raw: dict[str, Any] | None,
        charge: Decimal | None = None,
    ) -> None:
        def work() -> None:
            now = iso(utcnow())
            with self.conn as conn:
                conn.execute(
                    "UPDATE order_attempts SET delivery_state = ?, response_json = ?,"
                    " charge = COALESCE(?, charge), updated_at = ?, last_status_at = ?"
                    " WHERE id = ?",
                    (
                        delivery_state.value,
                        json.dumps(raw, sort_keys=True) if raw is not None else None,
                        None if charge is None else str(charge),
                        now,
                        now,
                        attempt_id,
                    ),
                )
                conn.execute(
                    "UPDATE items SET delivery_state = ? WHERE id = (SELECT item_id FROM"
                    " order_attempts WHERE id = ?)",
                    (delivery_state.value, attempt_id),
                )
                if delivery_state is DeliveryState.COMPLETED:
                    conn.execute(
                        "UPDATE video_locks SET state = 'completed', released_at = ?"
                        " WHERE attempt_id = ?",
                        (now, attempt_id),
                    )
                elif delivery_state in {
                    DeliveryState.CANCELED,
                    DeliveryState.ERROR,
                    DeliveryState.PARTIAL,
                }:
                    # Needs an explicit human decision under the service rules:
                    # keep the lock held, mark it for review.
                    conn.execute(
                        "UPDATE video_locks SET state = 'needs_review' WHERE attempt_id = ?",
                        (attempt_id,),
                    )

        await self._run(work)

    # ----------------------------------------------------------------- events
    async def add_event(self, kind: str, payload: dict[str, Any], *, batch_id: str | None = None,
                        item_id: str | None = None) -> int:
        def work() -> int:
            with self.conn as conn:
                cur = conn.execute(
                    "INSERT INTO events (batch_id, item_id, kind, payload, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (batch_id, item_id, kind, json.dumps(payload, sort_keys=True), iso(utcnow())),
                )
                return int(cur.lastrowid)

        event_id = await self._run(work)
        self.event_written.set()
        return event_id

    async def events_since(self, last_id: int, limit: int = 200) -> list[dict[str, Any]]:
        def work():
            rows = self.conn.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (last_id, limit)
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "batch_id": row["batch_id"],
                    "item_id": row["item_id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

        return await self._run(work)

    async def latest_event_id(self) -> int:
        def work() -> int:
            row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM events").fetchone()
            return int(row["n"])

        return await self._run(work)

    # ------------------------------------------------------------------ views
    async def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        def work():
            rows = self.conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            out = []
            for row in rows:
                batch = {
                    "id": row["id"],
                    "mode": row["mode"],
                    "state": row["state"],
                    "keyword": row["keyword"],
                    "comment_scope": row["comment_scope"],
                    "created_at": row["created_at"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "stop_requested_at": row["stop_requested_at"],
                    "frozen_config": json.loads(row["frozen_config"]),
                    "items": [],
                }
                items = self.conn.execute(
                    "SELECT * FROM items WHERE batch_id = ? ORDER BY position ASC", (row["id"],)
                ).fetchall()
                batch["items"] = [_item_json(item) for item in items]
                out.append(batch)
            return out

        return await self._run(work)

    async def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        batches = await self.list_batches(limit=200)
        for batch in batches:
            if batch["id"] == batch_id:
                return batch
        return None


def _item_json(row: sqlite3.Row) -> dict[str, Any]:
    """Serialize an item with every id kept as a JSON string."""
    return {
        "id": row["id"],
        "position": row["position"],
        "input_url": row["input_url"],
        "video_id": _s(row["video_id"]),
        "canonical_url": row["canonical_url"],
        "video_verified": bool(row["video_verified"]),
        "outcome": row["outcome"],
        "delivery_state": row["delivery_state"],
        "target_comment_id": _s(row["target_comment_id"]),
        "target_text": row["target_text"],
        "target_likes": row["target_likes"],
        "owner_username": row["owner_username"],
        "owner_user_id": _s(row["owner_user_id"]),
        "submitted_link": row["submitted_link"],
        "top_likes": row["top_likes"],
        "top_comment_id": _s(row["top_comment_id"]),
        "scan_status": row["scan_status"],
        "scan_complete": None if row["scan_complete"] is None else bool(row["scan_complete"]),
        "pages_read": row["pages_read"],
        "comments_read": row["comments_read"],
        "quantity": row["quantity"],
        "estimated_cost": row["estimated_cost"],
        "order_id": _s(row["order_id"]),
        "error": row["error"],
        "scan": json.loads(row["scan_json"]) if row["scan_json"] else None,
        "timings": json.loads(row["timings_json"]) if row["timings_json"] else None,
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _s(value: Any) -> str | None:
    """Force id-like values to strings so JavaScript cannot lose precision."""
    return None if value is None else str(value)


def chunks(values: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
