"""FastAPI entry point: lifecycle, authentication, routes and the SSE stream.

The worker is owned by the application lifespan, never by a request. Enqueueing
a batch returns as soon as the rows are committed; processing continues even if
the browser disconnects.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field, field_validator

from .config import Settings, get_settings
from .database import Database
from .like_rules import QUANTITY_TIERS
from .models import RunMode
from .providers import reader_status
from .smm_client import SmmClient, build_http_client
from .worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app.main")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.autoescape = True

COOKIE_NAME = "session"


class BatchRequest(BaseModel):
    links: str = Field(min_length=1)
    mode: RunMode = RunMode.DRY_RUN
    idempotency_key: str = Field(min_length=8, max_length=128)
    csrf_token: str = Field(min_length=8)

    @field_validator("links")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not [line for line in value.splitlines() if line.strip()]:
            raise ValueError("at least one link is required")
        return value


def parse_links(raw: str, limit: int) -> list[str]:
    links = [line.strip() for line in raw.splitlines() if line.strip()]
    if not links:
        raise HTTPException(status_code=422, detail="Paste at least one link.")
    if len(links) > limit:
        raise HTTPException(
            status_code=422, detail=f"Maximum {limit} links per batch; received {len(links)}."
        )
    seen: set[str] = set()
    unique: list[str] = []
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        unique.append(link)
    return unique


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    app.state.settings = settings

    db = Database(settings.database_file)
    await db.connect()
    app.state.db = db

    http_client = build_http_client(max_connections=settings.READER_MAX_CONNECTIONS)
    app.state.http = http_client

    smm = SmmClient(
        http_client,
        panel_url=settings.PANEL_URL,
        api_key=settings.API_KEY,
        service_id=settings.SERVICE_ID,
        metadata_ttl=settings.SERVICE_METADATA_TTL_SECONDS,
    )
    app.state.smm = smm

    worker = Worker(settings=settings, db=db, http_client=http_client, smm=smm)
    app.state.worker = worker
    await worker.start()
    log.info(
        "started: reader=%s configured=%s panel_configured=%s db=%s",
        settings.COMMENT_READER,
        worker.reader.configured,
        settings.panel_configured,
        settings.database_file,
    )
    try:
        yield
    finally:
        await worker.stop()
        await http_client.aclose()
        await db.close()


app = FastAPI(title="Comment likes dashboard", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------------- session
def serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SESSION_SECRET, salt="dashboard-session")


def read_session(request: Request) -> dict[str, Any] | None:
    settings: Settings = request.app.state.settings
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        return serializer(settings).loads(raw, max_age=settings.SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def require_session(request: Request) -> dict[str, Any]:
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return session


def require_csrf(session: dict[str, Any], token: str | None) -> None:
    expected = session.get("csrf")
    if not expected or not token or not hmac.compare_digest(str(expected), str(token)):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")


def set_session_cookie(response: Response, settings: Settings, session: dict[str, Any]) -> None:
    response.set_cookie(
        COOKIE_NAME,
        serializer(settings).dumps(session),
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


# ----------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if read_session(request) is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "title": "Sign in"}
    )


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    settings: Settings = request.app.state.settings
    ok = hmac.compare_digest(username, settings.ADMIN_USERNAME) and hmac.compare_digest(
        password, settings.ADMIN_PASSWORD
    )
    if not ok:
        await asyncio.sleep(0.4)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Wrong username or password.", "title": "Sign in"},
            status_code=401,
        )
    session = {"user": settings.ADMIN_USERNAME, "csrf": secrets.token_urlsafe(32)}
    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, settings, session)
    return response


@app.post("/logout")
async def logout(request: Request, session: dict = Depends(require_session)):
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session: dict = Depends(require_session)):
    settings: Settings = request.app.state.settings
    worker: Worker = request.app.state.worker
    reader = reader_status(settings)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "title": "Comment likes",
            "csrf_token": session["csrf"],
            "keyword": settings.KEYWORD,
            "scope": settings.COMMENT_SCOPE.value,
            "default_mode": settings.RUN_MODE.value,
            "max_links": settings.MAX_LINKS_PER_BATCH,
            "service_id": settings.SERVICE_ID,
            "panel_url": settings.PANEL_URL,
            "reader": reader,
            "reply_end_trusted": settings.SCAN_TRUST_PROVIDER_REPLY_END,
            "live_allowed": settings.panel_configured and reader["live_capable"],
            "tiers": QUANTITY_TIERS,
            "worker_error": worker.last_error,
        },
    )


# ------------------------------------------------------------------------- API
@app.get("/api/state")
async def api_state(request: Request, session: dict = Depends(require_session)):
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    worker: Worker = request.app.state.worker
    batches = await db.list_batches(limit=20)
    return JSONResponse(
        {
            "last_event_id": await db.latest_event_id(),
            "batches": batches,
            "config": settings.redacted(),
            "reader": reader_status(settings),
            "current_item_id": worker.current_item_id,
        }
    )


@app.post("/api/batches")
async def api_create_batch(request: Request, session: dict = Depends(require_session)):
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    worker: Worker = request.app.state.worker

    try:
        payload = BatchRequest.model_validate(await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    require_csrf(session, payload.csrf_token)
    links = parse_links(payload.links, settings.MAX_LINKS_PER_BATCH)

    if payload.mode is RunMode.LIVE:
        reader = reader_status(settings)
        if not settings.panel_configured:
            raise HTTPException(
                status_code=409,
                detail="Live mode needs API_KEY set to your real God of Panel key.",
            )
        if not reader["live_capable"]:
            raise HTTPException(
                status_code=409,
                detail=f"Live mode is disabled: {reader['blocker']}",
            )

    frozen = settings.redacted()
    frozen.update(
        {
            "scan_deadline_seconds": settings.SCAN_DEADLINE_SECONDS,
            "scan_max_pages": settings.SCAN_MAX_PAGES,
            "scan_max_requests": settings.SCAN_MAX_REQUESTS,
            "scan_max_comments": settings.SCAN_MAX_COMMENTS,
            "scan_max_threads": settings.SCAN_MAX_THREADS,
            "trust_provider_reply_end": settings.SCAN_TRUST_PROVIDER_REPLY_END,
            "comment_reader": settings.COMMENT_READER,
        }
    )

    batch_id, created = await db.create_batch(
        idempotency_key=payload.idempotency_key,
        mode=payload.mode,
        keyword=settings.KEYWORD,
        comment_scope=settings.COMMENT_SCOPE.value,
        frozen_config=frozen,
        links=links,
    )
    if created:
        await db.add_event(
            "batch", {"batch_id": batch_id, "links": len(links), "mode": payload.mode.value},
            batch_id=batch_id,
        )
        worker.notify()

    return JSONResponse(
        {"batch_id": batch_id, "created": created, "links": len(links)},
        status_code=201 if created else 200,
    )


@app.post("/api/batches/{batch_id}/stop")
async def api_stop_batch(
    batch_id: str, request: Request, session: dict = Depends(require_session)
):
    db: Database = request.app.state.db
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    require_csrf(session, body.get("csrf_token"))
    cancelled = await db.request_stop(batch_id)
    await db.add_event("stop", {"batch_id": batch_id, "cancelled": cancelled}, batch_id=batch_id)
    return {
        "batch_id": batch_id,
        "cancelled_items": cancelled,
        "note": (
            "Queued links were cancelled. An order the panel has already accepted is "
            "not cancelled automatically."
        ),
    }


@app.get("/api/service")
async def api_service(request: Request, session: dict = Depends(require_session)):
    """On-demand panel preflight. Kept out of /health on purpose."""
    settings: Settings = request.app.state.settings
    smm: SmmClient = request.app.state.smm
    if not settings.panel_configured:
        return {"configured": False, "error": "API_KEY is not set."}
    result: dict[str, Any] = {"configured": True}
    try:
        metadata = await smm.get_service_metadata(force=True)
        result["service"] = metadata.as_json()
        result["compatible"] = smm.service_supports_comment_likes(metadata)
    except Exception as exc:  # noqa: BLE001
        result["service"] = None
        result["compatible"] = False
        result["error"] = str(exc)
    balance, currency, error = await smm.get_balance()
    result["balance"] = None if balance is None else str(balance)
    result["currency"] = currency
    if error:
        result["balance_error"] = error
    return result


@app.get("/api/events")
async def api_events(request: Request, session: dict = Depends(require_session)):
    """Authenticated SSE. The client sends Last-Event-ID and gets a snapshot."""
    db: Database = request.app.state.db
    header_id = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        last_id = int(header_id) if header_id else 0
    except ValueError:
        last_id = 0

    async def stream():
        cursor = last_id
        snapshot = {
            "last_event_id": await db.latest_event_id(),
            "batches": await db.list_batches(limit=20),
        }
        yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
        while True:
            if await request.is_disconnected():
                return
            events = await db.events_since(cursor)
            for event in events:
                cursor = event["id"]
                yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n"
            if not events:
                db.event_written.clear()
                try:
                    await asyncio.wait_for(db.event_written.wait(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-store",
            "x-accel-buffering": "no",
            "connection": "keep-alive",
        },
    )


@app.get("/health")
async def health(request: Request):
    """Unauthenticated, secret-free, and independent of any external provider."""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    try:
        ready = await db.has_ready_jobs()
        database_ok = True
    except Exception:  # noqa: BLE001
        ready = False
        database_ok = False
    reader = reader_status(settings)
    return {
        "status": "ok" if database_ok else "degraded",
        "database": "ok" if database_ok else "error",
        "database_path": str(settings.database_file),
        "queue_has_ready_jobs": ready,
        "panel_configured": settings.panel_configured,
        "reader_adapter": reader["adapter"],
        "reader_configured": reader["configured"],
        "live_enabled": settings.panel_configured and reader["live_capable"],
    }
