"""Validated settings.

Precedence: real process environment (Railway Variables) overrides the local
``.env`` file, which overrides the defaults below. Secrets never live in code.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import CommentScope, RunMode

PLACEHOLDER_MARKERS = ("REPLACE_WITH", "CHANGEME", "changeme")

#: Adapters implemented in app/providers. "fixture" is demo/test only.
KNOWN_READERS = frozenset({"scrapecreators", "http", "fixture"})


def _is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- SMM panel (God of Panel) -------------------------------------------
    API_KEY: str = ""
    PANEL_URL: str = "https://godofpanel.com/api/v2"
    SERVICE_ID: str = "5836"
    SERVICE_METADATA_TTL_SECONDS: int = 900

    # --- Selection rules ----------------------------------------------------
    KEYWORD: str = "Mael Vorran"
    COMMENT_SCOPE: CommentScope = CommentScope.ALL
    RUN_MODE: RunMode = RunMode.DRY_RUN

    # --- Comment reader adapter --------------------------------------------
    # "scrapecreators" -> the concrete live integration (default). Needs only
    #                     READER_API_KEY; endpoints are implemented in code.
    # "http"           -> generic adapter for a different provider, driven by an
    #                     operator-written READER_CONTRACT_FILE.
    # "fixture"        -> local JSON samples. Demo/test only, never a live
    #                     source and never an automatic fallback.
    COMMENT_READER: str = "scrapecreators"
    READER_BASE_URL: str = "https://api.scrapecreators.com"
    READER_API_KEY: str = ""
    READER_API_KEY_HEADER: str = "x-api-key"
    # Only the generic "http" adapter needs a contract file.
    READER_CONTRACT_FILE: str = ""
    READER_FIXTURE_DIR: str = "tests/fixtures/comments"
    # Ignored by the scrapecreators adapter: that endpoint documents no
    # page-size parameter, so page size is provider-controlled.
    READER_PAGE_SIZE: int = 50
    READER_TIMEOUT_SECONDS: float = 15.0
    READER_MAX_CONNECTIONS: int = 10
    # Ask for a trimmed response. Off by default: the full response carries the
    # fields this application depends on.
    READER_TRIM: bool = False
    # How long a resolved owner handle may be reused (seconds).
    READER_OWNER_CACHE_TTL_SECONDS: int = 3600
    # Documented cache window for the profile endpoint; a cache hit is free.
    READER_PROFILE_CACHE_MAX_AGE: str = "7d"

    # --- Scan budgets -------------------------------------------------------
    # Raised from the first draft (45s / 40 pages / 60 requests) because the
    # ScrapeCreators comments endpoint returns roughly 20 comments per page and
    # every reply thread costs at least one more request, so scope=all on an
    # ordinary video exhausted the old request budget and reported incomplete.
    # These are still hard budgets: reaching one means incomplete, never
    # "complete enough".
    SCAN_DEADLINE_SECONDS: float = 90.0
    SCAN_MAX_PAGES: int = 60
    SCAN_MAX_REQUESTS: int = 120
    SCAN_MAX_COMMENTS: int = 5000
    SCAN_MAX_THREADS: int = 200
    # A parent advertises reply_comment_total, but the replies endpoint may
    # return fewer distinct replies. That gap can mean hidden/removed replies,
    # nested replies the endpoint does not expose, visibility differences, or a
    # count that changed mid-scan. It does NOT prove more replies are
    # retrievable - and it equally does not prove the maximum across the scope
    # you asked for is known.
    #
    # False (default, strict): any shortfall makes the scan incomplete and no
    #   order is placed.
    # True (explicit opt-in): the provider's end-of-stream declaration is taken
    #   as the completeness rule and the result is labelled as covering
    #   PROVIDER-VISIBLE data only. It never overrides has_more still being set,
    #   an unknown completion marker, invalid records or an exceeded budget.
    SCAN_TRUST_PROVIDER_REPLY_END: bool = False

    # --- Auth / web ---------------------------------------------------------
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = ""
    SESSION_SECRET: str = ""
    SESSION_TTL_SECONDS: int = 86400
    COOKIE_SECURE: bool = True

    # --- Storage ------------------------------------------------------------
    DATABASE_PATH: str = "./data/app.db"

    # --- Delivery status polling -------------------------------------------
    STATUS_POLL_INTERVAL_SECONDS: float = 60.0
    STATUS_POLL_BATCH_SIZE: int = 20

    ENVIRONMENT: str = "production"

    MAX_LINKS_PER_BATCH: int = 10

    @field_validator("PANEL_URL", "READER_BASE_URL")
    @classmethod
    def _https_only(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith("https://"):
            raise ValueError("must be an https:// URL")
        return value.rstrip("/") if value else value

    @field_validator("KEYWORD")
    @classmethod
    def _keyword_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("KEYWORD must not be empty")
        return value.strip()

    @field_validator("SERVICE_ID")
    @classmethod
    def _service_id_str(cls, value: str) -> str:
        value = str(value).strip()
        if not value.isdigit():
            raise ValueError("SERVICE_ID must be a numeric id, kept as a string")
        return value

    @field_validator("COMMENT_READER")
    @classmethod
    def _known_reader(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in KNOWN_READERS:
            raise ValueError(
                "COMMENT_READER must be one of: " + ", ".join(sorted(KNOWN_READERS))
            )
        return value

    @model_validator(mode="after")
    def _production_secrets(self) -> "Settings":
        if self.ENVIRONMENT == "production":
            problems = []
            if _is_placeholder(self.ADMIN_PASSWORD) or len(self.ADMIN_PASSWORD) < 12:
                problems.append("ADMIN_PASSWORD must be set to a real value of 12+ characters")
            if _is_placeholder(self.SESSION_SECRET) or len(self.SESSION_SECRET) < 32:
                problems.append("SESSION_SECRET must be a real random value of 32+ characters")
            if problems:
                raise ValueError("; ".join(problems))
        else:
            if not self.SESSION_SECRET:
                object.__setattr__(self, "SESSION_SECRET", secrets.token_urlsafe(48))
        return self

    # --- Derived helpers ----------------------------------------------------
    @property
    def panel_configured(self) -> bool:
        return not _is_placeholder(self.API_KEY)

    @property
    def database_file(self) -> Path:
        return Path(self.DATABASE_PATH).expanduser()

    @property
    def reader_configured(self) -> bool:
        """True only when the selected adapter can really read TikTok."""
        if self.COMMENT_READER == "fixture":
            return False  # fixtures are never a live source
        if self.COMMENT_READER == "scrapecreators":
            return bool(self.READER_API_KEY and self.READER_BASE_URL)
        return bool(self.READER_BASE_URL and self.READER_CONTRACT_FILE)

    @property
    def live_allowed(self) -> bool:
        return self.panel_configured and self.reader_configured

    def redacted(self) -> dict[str, object]:
        """Config snapshot safe to render, log and freeze onto a batch."""
        return {
            "panel_url": self.PANEL_URL,
            "service_id": self.SERVICE_ID,
            "keyword": self.KEYWORD,
            "comment_scope": self.COMMENT_SCOPE.value,
            "default_run_mode": self.RUN_MODE.value,
            "comment_reader": self.COMMENT_READER,
            "reader_base_url": self.READER_BASE_URL or None,
            "reader_configured": self.reader_configured,
            "reader_api_key_present": bool(self.READER_API_KEY),
            "trust_provider_reply_end": self.SCAN_TRUST_PROVIDER_REPLY_END,
            "panel_configured": self.panel_configured,
            "api_key_present": self.panel_configured,
            "scan_deadline_seconds": self.SCAN_DEADLINE_SECONDS,
            "scan_max_pages": self.SCAN_MAX_PAGES,
            "scan_max_requests": self.SCAN_MAX_REQUESTS,
            "scan_max_comments": self.SCAN_MAX_COMMENTS,
            "scan_max_threads": self.SCAN_MAX_THREADS,
            "max_links_per_batch": self.MAX_LINKS_PER_BATCH,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
