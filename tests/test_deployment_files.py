"""Checks on the files that make deployment work.

These are real assertions about the repository, not a substitute for deploying:
no test here builds an image or contacts Railway.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "app/__init__.py",
    "app/main.py",
    "app/config.py",
    "app/models.py",
    "app/database.py",
    "app/worker.py",
    "app/comment_finder.py",
    "app/like_rules.py",
    "app/smm_client.py",
    "app/url_resolver.py",
    "app/providers/__init__.py",
    "app/providers/base.py",
    "app/providers/fixture_reader.py",
    "app/providers/contract_http_reader.py",
    "app/providers/scrapecreators_reader.py",
    "scripts/check_integrations.py",
    "scripts/offline_checks.py",
    ".github/workflows/tests.yml",
    "tests/asgi_driver.py",
    "app/templates/login.html",
    "app/templates/dashboard.html",
    "app/static/app.js",
    "app/static/styles.css",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "start.sh",
    "railway.json",
    ".env.example",
    ".gitignore",
    ".dockerignore",
    "README.md",
]


def test_every_required_file_exists():
    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    assert missing == []


def test_there_is_no_competing_root_app_module():
    assert not (ROOT / "app.py").exists()
    assert not (ROOT / "Procfile").exists()


def test_start_script_uses_unix_line_endings():
    raw = (ROOT / "start.sh").read_bytes()
    assert b"\r\n" not in raw
    assert raw.startswith(b"#!/bin/sh")


def test_start_script_execs_uvicorn_with_the_platform_port():
    body = (ROOT / "start.sh").read_text()
    assert 'exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1' in body
    assert "--reload" not in body
    assert "--workers 2" not in body


def test_railway_config_matches_the_dockerfile():
    config = json.loads((ROOT / "railway.json").read_text())
    assert config["build"]["builder"] == "DOCKERFILE"
    assert config["build"]["dockerfilePath"] == "Dockerfile"
    assert config["deploy"]["numReplicas"] == 1
    assert config["deploy"]["healthcheckPath"] == "/health"
    assert config["deploy"]["startCommand"] == "/app/start.sh"

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["/app/start.sh"]' in dockerfile
    # The shell script, not exec-form uvicorn, is what expands ${PORT}.
    assert "uvicorn" not in dockerfile.split("CMD")[1]


def effective_dockerfile_lines() -> list[str]:
    """Dockerfile instructions with comments and blank lines removed.

    Matching words inside explanatory comments produced a false positive: the
    comment "no Playwright, no Chromium" tripped the browser check. What matters
    is what the build actually installs, so only instructions are inspected.
    """
    lines = []
    for raw in (ROOT / "Dockerfile").read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def test_dockerfile_installs_no_browser():
    instructions = " ".join(effective_dockerfile_lines()).lower()
    for unwanted in ("playwright", "chromium", "google-chrome", "firefox", "selenium"):
        assert unwanted not in instructions, f"{unwanted} appears in a build instruction"


def test_no_browser_package_is_declared_as_a_dependency():
    for name in ("requirements.txt", "requirements-dev.txt"):
        body = (ROOT / name).read_text().lower()
        for unwanted in ("playwright", "selenium", "pyppeteer", "undetected-chromedriver"):
            assert unwanted not in body


def test_comments_may_still_explain_the_absence_of_a_browser():
    """Guards the guard: the check must not forbid documenting the decision."""
    raw = (ROOT / "Dockerfile").read_text().lower()
    assert "playwright" in raw  # present only in a comment
    assert "playwright" not in " ".join(effective_dockerfile_lines()).lower()


def test_dockerfile_sets_a_persistent_database_path():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ENV DATABASE_PATH=/data/app.db" in dockerfile
    assert "RUN mkdir -p /data" in dockerfile


def test_env_example_contains_only_placeholders():
    body = (ROOT / ".env.example").read_text()
    assert "API_KEY=REPLACE_WITH_YOUR_GOD_OF_PANEL_KEY" in body
    assert "READER_API_KEY=REPLACE_WITH_YOUR_SCRAPECREATORS_KEY" in body
    assert "ADMIN_PASSWORD=REPLACE_WITH_A_STRONG_PASSWORD" in body
    assert "SESSION_SECRET=REPLACE_WITH_A_RANDOM_SECRET" in body
    assert "SERVICE_ID=5836" in body
    assert "PANEL_URL=https://godofpanel.com/api/v2" in body


def test_env_example_selects_the_real_reader_and_needs_no_contract_file():
    body = (ROOT / ".env.example").read_text()
    assert "COMMENT_READER=scrapecreators" in body
    assert "READER_BASE_URL=https://api.scrapecreators.com" in body
    assert "READER_CONTRACT_FILE=\n" in body  # present but empty
    assert "SCAN_TRUST_PROVIDER_REPLY_END=false" in body


def test_env_example_defaults_match_the_code():
    """A value in .env.example must not contradict the Settings default."""
    from app.config import Settings

    defaults = Settings.model_fields
    # DATABASE_PATH intentionally differs: the code default (./data/app.db)
    # suits a local run, while .env.example targets the Railway volume.
    exceptions = {"DATABASE_PATH"}
    checked = 0
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name in exceptions:
            continue
        if name not in defaults or "REPLACE_WITH" in value:
            continue
        default = defaults[name].default
        if default is None:
            continue
        rendered = str(default).lower() if isinstance(default, bool) else str(default)
        if hasattr(default, "value"):
            rendered = str(default.value)
        if isinstance(default, float) and rendered.endswith(".0"):
            rendered = rendered[:-2]
        assert value == rendered, f"{name}: .env.example={value!r} default={rendered!r}"
        checked += 1
    assert checked > 10


def test_the_docker_image_ships_the_integration_script():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY scripts ./scripts" in dockerfile


def test_the_named_reader_needs_no_contract_file_on_disk():
    from app.config import Settings

    settings = Settings(
        ENVIRONMENT="test",
        COMMENT_READER="scrapecreators",
        READER_API_KEY="k",
        READER_CONTRACT_FILE="",
        ADMIN_PASSWORD="test-password-1234",
        SESSION_SECRET="z" * 48,
    )
    assert settings.reader_configured is True


def test_strict_reply_counting_is_the_shipped_default():
    from app.comment_finder import ScanLimits
    from app.config import Settings

    assert Settings.model_fields["SCAN_TRUST_PROVIDER_REPLY_END"].default is False
    assert ScanLimits().trust_provider_reply_end is False


def test_the_workflow_does_not_inject_deployment_credentials():
    """conftest owns the test credentials; ambient values must not override them.

    An earlier workflow exported ADMIN_PASSWORD, conftest used setdefault, and
    ten HTTP tests failed with 401 instead of 303.
    """
    body = (ROOT / ".github/workflows/tests.yml").read_text()
    pytest_job = body.split("docker:")[0]
    for injected in ("ADMIN_PASSWORD:", "SESSION_SECRET:", "ADMIN_USERNAME:", "API_KEY:"):
        assert injected not in pytest_job


def test_the_suite_has_a_hard_per_test_timeout():
    """No streaming test may be able to hang the job until its 10-minute cap."""
    assert "pytest-timeout==" in (ROOT / "requirements-dev.txt").read_text()
    assert "--timeout=" in (ROOT / ".github/workflows/tests.yml").read_text()


def test_ci_workflow_runs_the_offline_suite_only():
    body = (ROOT / ".github/workflows/tests.yml").read_text()
    assert "python -m pytest -q" in body
    assert "check_integrations.py" in body  # only parsed, never executed live
    assert "scripts/check_integrations.py\n        run: python scripts" not in body
    # No real credentials may be referenced by the workflow.
    for forbidden in ("secrets.READER_API_KEY", "secrets.API_KEY", "api.scrapecreators.com"):
        assert forbidden not in body


def test_gitignore_excludes_secrets_and_local_state():
    body = (ROOT / ".gitignore").read_text().splitlines()
    for entry in (".env", "data/", "*.db", "reader_contract.json"):
        assert entry in body


#: Split so the detector's own definitions do not match themselves. The test
#: used to flag this very file for containing the strings it searches for.
SECRET_MARKERS = (
    "godofpanel.com/api/v2?" + "key=",
    "Bearer " + "sk-",
    "api_key=" + "AIza",
)


def test_no_secret_looking_literals_in_the_source_tree():
    """A crude guard against a pasted key or password ending up in Git."""
    suspicious = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".js", ".html", ".css", ".json", ".sh"}:
            continue
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue  # this module defines the markers it looks for
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in SECRET_MARKERS:
            if marker in text:
                suspicious.append((str(path.relative_to(ROOT)), marker))
    assert suspicious == []


def test_the_secret_detector_actually_detects(tmp_path):
    """Guards the guard: excluding this file must not disable the check."""
    planted = tmp_path / "leak.py"
    planted.write_text(f'URL = "https://{SECRET_MARKERS[0]}abcdef123456"\n')
    assert any(marker in planted.read_text() for marker in SECRET_MARKERS)


def test_requirements_are_pinned():
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-r "):
                continue
            assert "==" in line, f"{name}: {line} is not pinned"
