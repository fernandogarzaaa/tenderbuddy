"""Programmatic Alembic runner (``tenderbuddy migrate``)."""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

log = logging.getLogger(__name__)

BETA_REVISION = "0001_beta"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "tenderbuddy:migrations")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
    assert head is not None
    return head


def is_legacy_beta(engine: Engine) -> bool:
    """Beta databases were built with create_all: tables exist but no alembic_version."""
    tables = set(inspect(engine).get_table_names())
    return "alembic_version" not in tables and {"tenders", "users", "source_records"} <= tables


def upgrade(engine: Engine) -> str:
    cfg = alembic_config(engine.url.render_as_string(hide_password=False))
    legacy = is_legacy_beta(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if legacy:
            log.info("legacy beta database detected; stamping baseline", extra={"revision": BETA_REVISION})
            command.stamp(cfg, BETA_REVISION)
        command.upgrade(cfg, "head")
    rev = current_revision(engine) or ""
    log.info("database migrated", extra={"revision": rev})
    return rev


def is_up_to_date(engine: Engine) -> bool:
    return current_revision(engine) == head_revision()
