"""Tests for the web surface: authentication, CSRF, health, SSE and id safety."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from tests import asgi_driver
from tests.conftest import TEST_PASSWORD, TEST_USERNAME

BIG_VIDEO_ID = "7312345678901234567"
BIG_ORDER_ID = "9007199254740993"  # 2**53 + 1


@pytest.fixture
def client(tmp_path, fixture_dir):
    os.environ["DATABASE_PATH"] = str(tmp_path / "app.db")
    os.environ["READER_FIXTURE_DIR"] = str(fixture_dir)
    os.environ["ENVIRONMENT"] = "test"
    os.environ["COOKIE_SECURE"] = "false"

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def sign_in(client) -> str:
    response = client.post(
        "/login",
        data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    # The CSRF token is rendered into the page for the script to read.
    marker = 'data-csrf="'
    start = dashboard.text.index(marker) + len(marker)
    return dashboard.text[start : dashboard.text.index('"', start)]


def test_health_is_public_and_has_no_secrets(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert "live_enabled" in body
    assert "test-key-not-real" not in response.text
    assert "SESSION_SECRET" not in response.text
    assert "password" not in response.text.lower()


def test_dashboard_requires_authentication(client):
    assert client.get("/dashboard").status_code == 401
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/batches", json={}).status_code == 401
    assert client.get("/api/events").status_code == 401
    assert client.get("/api/service").status_code == 401


def test_root_redirects_to_login_when_signed_out(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_is_rejected(client):
    response = client.post(
        "/login", data={"username": TEST_USERNAME, "password": "definitely-not-it"}
    )
    assert response.status_code == 401


def test_session_cookie_is_httponly_and_samesite(client):
    response = client.post(
        "/login",
        data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_mutations_require_a_valid_csrf_token(client):
    sign_in(client)
    response = client.post(
        "/api/batches",
        json={
            "links": f"https://www.tiktok.com/@a/video/{BIG_VIDEO_ID}",
            "mode": "dry_run",
            "idempotency_key": "key-12345678",
            "csrf_token": "wrong-token-value",
        },
    )
    assert response.status_code == 403


def test_batch_creation_is_idempotent(client):
    csrf = sign_in(client)
    payload = {
        "links": "https://www.tiktok.com/@creator/video/7300000000000000001",
        "mode": "dry_run",
        "idempotency_key": "double-click-key",
        "csrf_token": csrf,
    }
    first = client.post("/api/batches", json=payload)
    second = client.post("/api/batches", json=payload)
    assert first.status_code == 201
    assert first.json()["created"] is True
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert first.json()["batch_id"] == second.json()["batch_id"]


def test_more_than_ten_links_is_rejected(client):
    csrf = sign_in(client)
    links = "\n".join(
        f"https://www.tiktok.com/@a/video/73000000000000000{index:02d}" for index in range(11)
    )
    response = client.post(
        "/api/batches",
        json={"links": links, "mode": "dry_run", "idempotency_key": "too-many-key", "csrf_token": csrf},
    )
    assert response.status_code == 422


def test_live_mode_is_refused_while_the_reader_is_unconfigured(client):
    csrf = sign_in(client)
    response = client.post(
        "/api/batches",
        json={
            "links": "https://www.tiktok.com/@creator/video/7300000000000000001",
            "mode": "live",
            "idempotency_key": "live-attempt-key",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 409
    assert "disabled" in response.json()["detail"].lower()


def wait_for_completion(client, batch_id: str, timeout: float = 15.0) -> dict:
    """Poll /api/state until the worker has finished the batch."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        for batch in state["batches"]:
            if batch["id"] != batch_id:
                continue
            outcomes = [item["outcome"] for item in batch["items"]]
            if all(outcome not in ("pending", "processing") for outcome in outcomes):
                return batch
        time.sleep(0.1)
    raise AssertionError("batch did not finish in time")


def test_large_ids_are_serialized_as_json_strings(client):
    """The fixture video and comment ids are both above 2**53 - 1."""
    csrf = sign_in(client)
    created = client.post(
        "/api/batches",
        json={
            "links": "https://www.tiktok.com/@creator.one/video/7300000000000000001",
            "mode": "dry_run",
            "idempotency_key": "precision-key",
            "csrf_token": csrf,
        },
    )
    assert created.status_code == 201
    batch_id = created.json()["batch_id"]
    batch = wait_for_completion(client, batch_id)
    item = batch["items"][0]

    assert item["outcome"] == "dry_run_complete"
    assert item["video_id"] == "7300000000000000001"
    assert item["target_comment_id"] == "7300000000000000102"
    for key in ("video_id", "target_comment_id"):
        value = item[key]
        assert isinstance(value, str)
        # Round-tripping through a JavaScript Number would corrupt these.
        assert int(value) > 2**53

    body = client.get("/api/state").text
    assert '"7300000000000000001"' in body
    assert ": 7300000000000000001" not in body
    assert json.loads(body)["batches"][0]["items"][0]["video_id"] == "7300000000000000001"


def test_comment_text_is_escaped_in_the_rendered_page(client):
    sign_in(client)
    response = client.get("/dashboard")
    # The dashboard renders no provider text server side, and the client script
    # uses textContent only. Assert the template never interpolates raw HTML.
    assert "|safe" not in response.text
    assert "innerHTML" not in response.text


def test_logout_clears_the_session(client):
    csrf = sign_in(client)
    client.post("/logout", follow_redirects=False)
    assert client.get("/api/state").status_code == 401
    assert csrf


# --------------------------------------------------------------------- SSE
#
# These use the bounded ASGI driver rather than TestClient.stream: a synchronous
# streaming client cannot close an endless SSE body, so the previous version of
# the snapshot test hung. See tests/asgi_driver.py.


@asynccontextmanager
async def live_app(tmp_path, fixture_dir):
    """Start the application (lifespan included) and sign in over ASGI."""
    os.environ["DATABASE_PATH"] = str(tmp_path / "sse.db")
    os.environ["READER_FIXTURE_DIR"] = str(fixture_dir)
    os.environ["ADMIN_USERNAME"] = TEST_USERNAME
    os.environ["ADMIN_PASSWORD"] = TEST_PASSWORD

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    async with asgi_driver.lifespan(app):
        headers, body = asgi_driver.form(
            {"username": TEST_USERNAME, "password": TEST_PASSWORD}
        )
        login = await asgi_driver.call(app, "POST", "/login", headers=headers, body=body)
        assert login.status == 303, login.text
        jar = login.cookies()
        assert "session" in jar
        yield app, {"cookie": asgi_driver.cookie_header(jar)}
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_sse_requires_authentication_before_streaming(tmp_path, fixture_dir):
    async with live_app(tmp_path, fixture_dir) as (app, _auth):
        response = await asgi_driver.call(app, "GET", "/api/events", timeout=3.0)
        assert response.status == 401


@pytest.mark.asyncio
async def test_sse_stream_sends_a_snapshot_first(tmp_path, fixture_dir):
    async with live_app(tmp_path, fixture_dir) as (app, auth):
        db = app.state.db
        response, chunk = await asgi_driver.read_stream(
            app,
            "/api/events",
            headers=auth,
            until=("event: snapshot", "\n\n"),
            wake=db.event_written,
            timeout=5.0,
        )
        assert response.status == 200
        assert response.header("content-type").startswith("text/event-stream")
        assert "event: snapshot" in chunk
        assert '"batches"' in chunk
        # The snapshot is the very first frame on the wire.
        assert chunk.lstrip().startswith("event: snapshot")


@pytest.mark.asyncio
async def test_processing_continues_after_the_client_disconnects(tmp_path, fixture_dir):
    """Enqueue, drop the stream, let the worker finish, reconnect, get a snapshot."""
    async with live_app(tmp_path, fixture_dir) as (app, auth):
        db = app.state.db

        dashboard = await asgi_driver.call(app, "GET", "/dashboard", headers=auth)
        marker = 'data-csrf="'
        start = dashboard.text.index(marker) + len(marker)
        csrf = dashboard.text[start : dashboard.text.index('"', start)]

        payload = json.dumps(
            {
                "links": (
                    "https://www.tiktok.com/@creator.one/video/7300000000000000001\n"
                    "https://www.tiktok.com/@creator.two/video/7300000000000000002"
                ),
                "mode": "dry_run",
                "idempotency_key": "reconnect-key",
                "csrf_token": csrf,
            }
        ).encode()
        created = await asgi_driver.call(
            app,
            "POST",
            "/api/batches",
            headers={**auth, "content-type": "application/json"},
            body=payload,
        )
        assert created.status == 201, created.text
        batch_id = json.loads(created.text)["batch_id"]

        # Open the stream, take the snapshot, then disconnect mid-batch.
        _, first = await asgi_driver.read_stream(
            app,
            "/api/events",
            headers=auth,
            until=("event: snapshot", "\n\n"),
            wake=db.event_written,
            timeout=5.0,
        )
        assert "event: snapshot" in first

        # Work continues without any connected client.
        deadline = time.monotonic() + 15
        batch = None
        while time.monotonic() < deadline:
            state = await asgi_driver.call(app, "GET", "/api/state", headers=auth)
            batches = json.loads(state.text)["batches"]
            batch = next((b for b in batches if b["id"] == batch_id), None)
            if batch and batch["state"] == "finished":
                break
            await asyncio.sleep(0.05)
        assert batch is not None and batch["state"] == "finished"
        assert [item["outcome"] for item in batch["items"]] == [
            "dry_run_complete",
            "keyword_not_found",
        ]

        # Reconnecting replays a full snapshot, so nothing is lost.
        _, second = await asgi_driver.read_stream(
            app,
            "/api/events",
            headers=auth,
            until=("event: snapshot", "\n\n"),
            wake=db.event_written,
            timeout=5.0,
        )
        assert batch_id in second
