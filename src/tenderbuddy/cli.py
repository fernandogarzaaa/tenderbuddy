"""``tenderbuddy`` command-line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence

from sqlalchemy import Engine

from tenderbuddy import __version__
from tenderbuddy.config import ConfigError, Settings, get_settings


def load_settings() -> Settings:
    try:
        return get_settings()
    except (ConfigError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def migrated_engine(settings: Settings) -> Engine:
    from tenderbuddy.db import init_engine
    from tenderbuddy.logging_setup import configure_logging
    from tenderbuddy.migrate import upgrade

    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = init_engine(settings.DATABASE_URL)
    upgrade(engine)
    return engine


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    s = load_settings()
    uvicorn.run("tenderbuddy.app:create_app", factory=True, host=args.host or s.HOST, port=args.port or s.PORT,
                workers=args.workers, proxy_headers=True, forwarded_allow_ips=args.forwarded_allow_ips,
                log_config=None, access_log=False)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from tenderbuddy.migrate import current_revision

    engine = migrated_engine(load_settings())
    print(f"database at revision {current_revision(engine)}")
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    from tenderbuddy.db import session_scope
    from tenderbuddy.security import PasswordPolicyError
    from tenderbuddy.users import UserExists, create_user

    migrated_engine(load_settings())
    password = args.password or os.environ.get("TENDERBUDDY_NEW_PASSWORD") or getpass.getpass("Password: ")
    try:
        with session_scope() as db:
            user = create_user(db, args.email, password, name=args.name, role=args.role)
            print(f"created {user.role} {user.email}")
    except (UserExists, PasswordPolicyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from tenderbuddy.db import session_scope
    from tenderbuddy.services import sync_all, sync_source

    s = load_settings()
    if args.source and args.source not in s.CONNECTORS:
        print(f"error: {args.source!r} is not in CONNECTORS ({', '.join(s.CONNECTORS)})", file=sys.stderr)
        return 1
    migrated_engine(s)
    with session_scope() as db:
        results = {args.source: sync_source(db, s, args.source)} if args.source else sync_all(db, s)
    print(json.dumps({k: v.as_dict() for k, v in results.items()}, indent=2))
    return 1 if any(v.failed for v in results.values()) else 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    from tenderbuddy.db import session_scope
    from tenderbuddy.demo import seed_demo

    s = load_settings()
    if s.APP_ENV == "production":
        print("error: refusing to seed demo data in production", file=sys.stderr)
        return 1
    migrated_engine(s)
    with session_scope() as db:
        stats = seed_demo(db)
    print(json.dumps(stats.as_dict()))
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    s = load_settings()
    safe = {
        "APP_ENV": s.APP_ENV,
        "DATABASE": s.DATABASE_URL.split("@")[-1],
        "CONNECTORS": s.CONNECTORS,
        "LLM": s.llm_mode,
        "LLM_MODEL": s.LLM_MODEL if s.llm_mode == "openai" else None,
        "SYNC_INTERVAL_MINUTES": s.SYNC_INTERVAL_MINUTES,
        "COOKIE_SECURE": s.cookie_secure,
        "SAM_API_KEY_SET": bool(s.SAM_API_KEY),
    }
    print(json.dumps(safe, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tenderbuddy", description="AI tender discovery and bid preparation.")
    p.add_argument("--version", action="version", version=f"tenderbuddy {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="run the web server (applies migrations on start)")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--workers", type=int, default=1)
    sp.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    sp.set_defaults(func=cmd_serve)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)

    sp = sub.add_parser("create-user", help="create a user")
    sp.add_argument("email")
    sp.add_argument("--name", default="")
    sp.add_argument("--role", choices=["admin", "member"], default="member")
    sp.add_argument("--password", help="omit to be prompted (or set TENDERBUDDY_NEW_PASSWORD)")
    sp.set_defaults(func=cmd_create_user)

    sp = sub.add_parser("sync", help="fetch tenders from configured sources")
    sp.add_argument("source", nargs="?", help="one source name; default: all enabled")
    sp.set_defaults(func=cmd_sync)

    sub.add_parser("seed-demo", help="load the demo profile and tenders (refused in production)").set_defaults(
        func=cmd_seed_demo)
    sub.add_parser("check-config", help="validate and print the effective configuration").set_defaults(
        func=cmd_check_config)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
