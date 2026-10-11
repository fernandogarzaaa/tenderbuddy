"""User management: bootstrap admin, create users, authenticate."""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tenderbuddy import models
from tenderbuddy.config import Settings
from tenderbuddy.security import hash_password, validate_password, verify_password

log = logging.getLogger(__name__)


class UserExists(ValueError):
    pass


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Hash checked for unknown emails so they cost the same time as a wrong password."""
    return hash_password("dummy-password-for-timing")


def create_user(db: Session, email: str, password: str, *, name: str = "", role: str = "member") -> models.User:
    email = email.strip().lower()
    validate_password(password)
    if role not in ("admin", "member"):
        raise ValueError("role must be admin or member")
    if db.scalar(select(models.User).where(models.User.email == email)) is not None:
        raise UserExists(f"user {email} already exists")
    user = models.User(email=email, name=name, role=role, hashed_password=hash_password(password))
    db.add(user)
    db.commit()
    log.info("user created", extra={"email": email, "role": role})
    return user


def authenticate(db: Session, email: str, password: str) -> models.User | None:
    user = db.scalar(select(models.User).where(models.User.email == email.strip().lower()))
    if user is None:
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, user.hashed_password) or not user.is_active:
        return None
    return user


def bootstrap_admin(db: Session, settings: Settings) -> models.User | None:
    """Create the first admin from ADMIN_EMAIL/ADMIN_PASSWORD when the user table is empty."""
    if db.scalar(select(func.count()).select_from(models.User)):
        return None
    user = models.User(email=settings.ADMIN_EMAIL, name="Admin", role="admin",
                       hashed_password=hash_password(settings.ADMIN_PASSWORD))
    db.add(user)
    db.commit()
    log.info("bootstrap admin created", extra={"email": settings.ADMIN_EMAIL})
    return user
