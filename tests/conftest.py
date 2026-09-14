"""Test configuration.

The test environment is set BEFORE ``app.config`` is imported anywhere, and it
is set with assignment, not ``setdefault``. That matters: CI and developer
shells export deployment-shaped variables (``ADMIN_PASSWORD``, ``API_KEY``,
``DATABASE_PATH`` ...), and with ``setdefault`` those ambient values silently
won the login helpers in the HTTP tests used a different password and ten tests
failed with 401 instead of 303.

Every value here is fake. Nothing in the suite may reach a real panel, a real
comment provider or a real database: all transports are mocks and all databases
are temporary files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: The single source of truth for test credentials. Import these instead of
#: retyping literals, so a change here cannot desynchronise a login helper.
TEST_USERNAME = "test-admin"
TEST_PASSWORD = "test-password-1234"
TEST_SESSION_SECRET = "test-session-secret-that-is-long-enough-0123456789"

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "comments"

#: Assignment, not setdefault: the suite must not inherit ambient credentials.
TEST_ENVIRONMENT = {
    "ENVIRONMENT": "test",
    "API_KEY": "test-key-not-real",
    "PANEL_URL": "https://godofpanel.example/api/v2",
    "SERVICE_ID": "5836",
    "KEYWORD": "Mael Vorran",
    "COMMENT_SCOPE": "all",
    "RUN_MODE": "dry_run",
    "COMMENT_READER": "fixture",
    "READER_BASE_URL": "https://api.scrapecreators.example",
    "READER_API_KEY": "",
    "READER_CONTRACT_FILE": "",
    "READER_FIXTURE_DIR": str(FIXTURE_DIR),
    "SCAN_TRUST_PROVIDER_REPLY_END": "false",
    "ADMIN_USERNAME": TEST_USERNAME,
    "ADMIN_PASSWORD": TEST_PASSWORD,
    "SESSION_SECRET": TEST_SESSION_SECRET,
    "COOKIE_SECURE": "false",
}

os.environ.update(TEST_ENVIRONMENT)

# A stray .env in the working tree must not leak into the suite either.
os.environ.setdefault("DATABASE_PATH", ":memory-not-used:")

import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run the test in an event loop")


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR
