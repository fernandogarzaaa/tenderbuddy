from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from pydantic import ValidationError

from tenderbuddy.config import Settings, get_settings, reset_settings_cache, split_csv
from tenderbuddy.security import (
    ALGORITHM,
    PasswordPolicyError,
    RateLimiter,
    create_access_token,
    csrf_matches,
    decode_access_token,
    hash_password,
    new_csrf_token,
    validate_password,
    verify_password,
)
from tests.conftest import make_settings

SECRET = "s" * 40


def test_password_hash_roundtrip() -> None:
    h = hash_password("correct horse battery")
    assert h.startswith("$2")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "not-a-hash")


def test_password_policy() -> None:
    validate_password("0123456789")
    with pytest.raises(PasswordPolicyError, match="at least 10"):
        validate_password("short")
    with pytest.raises(PasswordPolicyError, match="72 bytes"):
        validate_password("é" * 40)


def test_token_roundtrip_and_rejections() -> None:
    tok = create_access_token("a@b.c", SECRET, 5)
    assert decode_access_token(tok, SECRET) == "a@b.c"
    assert decode_access_token(tok, "other" * 10) is None
    assert decode_access_token("garbage", SECRET) is None
    expired = create_access_token("a@b.c", SECRET, -1)
    assert decode_access_token(expired, SECRET) is None
    exp = datetime.now(UTC) + timedelta(minutes=5)
    wrong_type = jwt.encode({"sub": "a@b.c", "exp": exp, "type": "refresh"}, SECRET, algorithm=ALGORITHM)
    assert decode_access_token(wrong_type, SECRET) is None
    no_sub = jwt.encode({"exp": exp, "type": "access"}, SECRET, algorithm=ALGORITHM)
    assert decode_access_token(no_sub, SECRET) is None
    empty_sub = jwt.encode({"sub": "", "exp": exp, "type": "access"}, SECRET, algorithm=ALGORITHM)
    assert decode_access_token(empty_sub, SECRET) is None
    payload = jwt.decode(tok, SECRET, algorithms=[ALGORITHM])
    assert payload["exp"] - payload["iat"] == 300


def test_csrf_helpers() -> None:
    a, b = new_csrf_token(), new_csrf_token()
    assert a != b
    assert len(a) >= 40
    assert csrf_matches(a, a)
    assert not csrf_matches(a, b)
    assert not csrf_matches(None, a)
    assert not csrf_matches(a, None)
    assert not csrf_matches("", "")


def test_rate_limiter_window() -> None:
    now = {"t": 100.0}
    rl = RateLimiter(2, 10, clock=lambda: now["t"])
    assert not rl.is_blocked("k")
    rl.hit("k")
    assert not rl.is_blocked("k")
    rl.hit("k")
    assert rl.is_blocked("k")
    assert not rl.is_blocked("other")
    now["t"] = 109.9
    assert rl.is_blocked("k")
    now["t"] = 110.0
    assert not rl.is_blocked("k")
    rl.hit("k")
    rl.hit("k")
    rl.reset("k")
    assert not rl.is_blocked("k")
    rl.reset("never-seen")


# ---------------------------------------------------------------- config

def test_defaults_and_derived(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    assert s.upload_dir == s.DATA_DIR / "uploads"
    assert s.staging_dir == s.DATA_DIR / "staging"
    assert s.llm_mode == "mock"
    assert s.cookie_secure is False
    s2 = make_settings(tmp_path, CSV_STAGING_DIR=tmp_path / "x", COOKIE_SECURE=True)
    assert s2.staging_dir == tmp_path / "x"
    assert s2.cookie_secure is True


def test_csv_lists_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTORS", "ted, sam_gov")
    monkeypatch.setenv("TED_COUNTRIES", "irl,mlt")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.CONNECTORS == ["ted", "sam_gov"]
    assert s.TED_COUNTRIES == ["IRL", "MLT"]
    assert split_csv(None) == []
    assert split_csv(["a", " ", "b"]) == ["a", "b"]
    assert split_csv("x,,y") == ["x", "y"]


def test_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unknown connector"):
        make_settings(tmp_path, CONNECTORS="ted,philgeps")
    with pytest.raises(ValidationError, match="ADMIN_EMAIL"):
        make_settings(tmp_path, ADMIN_EMAIL="nobody")
    assert make_settings(tmp_path, ADMIN_EMAIL=" Boss@Example.COM ").ADMIN_EMAIL == "boss@example.com"


def test_production_guard(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        make_settings(tmp_path, APP_ENV="production", SECRET_KEY="short", ADMIN_PASSWORD="ChangeMe123!",
                      SEED_DEMO_DATA=True)
    msg = str(exc.value)
    assert "SECRET_KEY" in msg
    assert "ADMIN_PASSWORD" in msg
    assert "SEED_DEMO_DATA" in msg
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make_settings(tmp_path, APP_ENV="production", SECRET_KEY="change-me-in-production",
                      ADMIN_PASSWORD="a-real-password-1")
    ok = make_settings(tmp_path, APP_ENV="production", SECRET_KEY="x" * 32, ADMIN_PASSWORD="a-real-password-1")
    assert ok.cookie_secure is True


def test_llm_mode_explicit(tmp_path: Path) -> None:
    assert make_settings(tmp_path, LLM_PROVIDER="openai", LLM_BASE_URL="http://x").llm_mode == "openai"
    assert make_settings(tmp_path, LLM_PROVIDER="mock", LLM_BASE_URL="http://x").llm_mode == "mock"


def test_get_settings_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("APP_NAME", "CachedName")
    try:
        assert get_settings().APP_NAME == "CachedName"
        assert get_settings() is get_settings()
    finally:
        reset_settings_cache()


def test_docker_image_does_not_trust_forwarded_headers_from_everyone():
    """`--forwarded-allow-ips '*'` with a directly published port lets any client
    spoof X-Forwarded-For and dodge the per-IP login rate limit."""
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    cmd = [line for line in dockerfile.splitlines() if line.startswith("CMD")][-1]
    assert "--forwarded-allow-ips '*'" not in cmd
    assert '--forwarded-allow-ips "*"' not in cmd
    assert "${FORWARDED_ALLOW_IPS:-127.0.0.1}" in cmd
