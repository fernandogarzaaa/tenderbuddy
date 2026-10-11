from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tenderbuddy import cli
from tenderbuddy.app import scheduled_sync, start_scheduler
from tenderbuddy.config import reset_settings_cache
from tenderbuddy.export import inline, markdown_to_flowables, markdown_to_pdf, split_row
from tenderbuddy.logging_setup import JsonFormatter, TextFormatter, configure_logging, request_id_var
from tests.conftest import make_settings


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'cli.db'}", "DATA_DIR": str(tmp_path / "data"),
        "SECRET_KEY": "k" * 40, "CONNECTORS": "csv", "LOG_LEVEL": "WARNING", "LOG_FORMAT": "text",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()
    yield tmp_path
    reset_settings_cache()


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "tenderbuddy 1.0.0" in capsys.readouterr().out


def test_cli_migrate_user_sync_seed(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 0
    assert "revision 0002_v1" in capsys.readouterr().out
    assert cli.main(["create-user", "ops@example.com", "--password", "ops-password-1", "--role", "admin"]) == 0
    assert "created admin ops@example.com" in capsys.readouterr().out
    assert cli.main(["create-user", "ops@example.com", "--password", "ops-password-1"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["create-user", "weak@example.com", "--password", "short"]) == 1
    (env / "data" / "staging").mkdir(parents=True)
    (env / "data" / "staging" / "x.csv").write_text("title,deadline\nA,2030-01-01\n", encoding="utf-8")
    assert cli.main(["sync"]) == 0
    assert json.loads(capsys.readouterr().out)["csv"]["created"] == 1
    assert cli.main(["sync", "csv"]) == 0
    assert json.loads(capsys.readouterr().out)["csv"]["unchanged"] == 1
    assert cli.main(["sync", "ted"]) == 1
    assert "not in CONNECTORS" in capsys.readouterr().err
    assert cli.main(["seed-demo"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] == 12


def test_cli_create_user_env_password(env: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("TENDERBUDDY_NEW_PASSWORD", "from-env-password")
    assert cli.main(["create-user", "env@example.com"]) == 0
    assert "created member env@example.com" in capsys.readouterr().out


def test_cli_sync_failure_exit_code(env: Path, monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CONNECTORS", "sam_gov")
    reset_settings_cache()
    assert cli.main(["sync"]) == 1
    assert "SAM_API_KEY" in capsys.readouterr().out


def test_cli_check_config_and_bad_config(env: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check-config"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["LLM"] == "mock"
    assert out["CONNECTORS"] == ["csv"]
    assert "SECRET" not in json.dumps(out).upper().replace("SAM_API_KEY_SET", "")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "short")
    reset_settings_cache()
    with pytest.raises(SystemExit) as exc:
        cli.main(["check-config"])
    assert exc.value.code == 2
    assert "configuration error" in capsys.readouterr().err


def test_cli_seed_refused_in_production(env: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-real-admin-password")
    reset_settings_cache()
    assert cli.main(["seed-demo"]) == 1
    assert "refusing" in capsys.readouterr().err


def test_cli_serve_invokes_uvicorn(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.update(args=a, kwargs=kw))
    assert cli.main(["serve", "--port", "9000", "--workers", "2"]) == 0
    assert calls["args"] == ("tenderbuddy.app:create_app",)
    kw = calls["kwargs"]
    assert isinstance(kw, dict)
    assert (kw["factory"], kw["port"], kw["workers"], kw["host"]) == (True, 9000, 2, "0.0.0.0")


# ---------------------------------------------------------------- scheduler

def test_scheduler_disabled_and_enabled(tmp_path: Path) -> None:
    assert start_scheduler(make_settings(tmp_path)) is None
    sched = start_scheduler(make_settings(tmp_path, SYNC_INTERVAL_MINUTES=30))
    try:
        job = sched.get_job("sync_all")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 1800
    finally:
        sched.shutdown(wait=False)


def test_scheduled_sync_swallows_errors(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from tenderbuddy.db import init_engine
    from tenderbuddy.migrate import upgrade

    s = make_settings(tmp_path)
    upgrade(init_engine(s.DATABASE_URL))
    scheduled_sync(s)  # succeeds silently
    bad = make_settings(tmp_path / "nope", DATABASE_URL="sqlite:////nonexistent-dir/x.db")
    init_engine(bad.DATABASE_URL)
    with caplog.at_level(logging.ERROR):
        scheduled_sync(bad)
    assert "scheduled sync failed" in caplog.text


# ---------------------------------------------------------------- logging

def test_json_and_text_formatters() -> None:
    record = logging.LogRecord("tb", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.source = "ted"
    token = request_id_var.set("rid-1")
    try:
        payload = json.loads(JsonFormatter().format(record))
        assert (payload["msg"], payload["level"], payload["source"], payload["request_id"]) == (
            "hello world", "INFO", "ted", "rid-1")
        assert "rid-1" in TextFormatter().format(record)
    finally:
        request_id_var.reset(token)
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        rec2 = logging.LogRecord("tb", logging.ERROR, __file__, 1, "oops", None, sys.exc_info())
    assert "ValueError: bad" in json.loads(JsonFormatter().format(rec2))["exc"]


def test_configure_logging_levels() -> None:
    configure_logging("DEBUG", "json")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert logging.getLogger("httpx").level == logging.WARNING
    configure_logging("ERROR", "text")
    assert isinstance(root.handlers[0].formatter, TextFormatter)
    assert logging.getLogger("httpx").level == logging.ERROR
    configure_logging("WARNING", "text")


# ---------------------------------------------------------------- export

def test_inline_markdown() -> None:
    assert inline("**Bold** and *it* & <x> \\| pipe") == "<b>Bold</b> and <i>it</i> &amp; &lt;x&gt; | pipe"
    assert split_row("| a | b \\| c |") == ["a", "b \\| c"]


def test_markdown_to_pdf_structure() -> None:
    md = ("# Title\n\n## Section\n### Sub\nParagraph **bold**.\n- bullet one\n* bullet two\n\n"
          "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 |\n\n| --- |\n")
    kinds = [type(f).__name__ for f in markdown_to_flowables(md)]
    assert kinds.count("Table") == 1
    assert kinds.count("Paragraph") == 6
    pdf = markdown_to_pdf(md, title="T")
    assert pdf.startswith(b"%PDF")
    assert markdown_to_pdf("").startswith(b"%PDF")
