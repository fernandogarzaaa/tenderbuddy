"""Pydantic request/response schemas (validation for both the JSON API and HTML forms)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tenderbuddy.models import STATUS_PIPELINE


def to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    out: list[str] = []
    for v in value:
        s = str(v).strip()
        if s and s not in out:
            out.append(s[:200])
    return out[:100]


def to_currency(value: Any) -> str:
    s = str(value or "").strip().upper()
    if s and (len(s) != 3 or not s.isalpha()):
        raise ValueError("currency must be a 3-letter ISO code, e.g. EUR")
    return s


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int


class ProfileIn(BaseModel):
    name: str = Field(default="", max_length=255)
    currency: str = "EUR"
    annual_turnover: float = Field(default=0.0, ge=0)
    years_experience: int = Field(default=0, ge=0, le=500)
    certifications: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    cpv_prefixes: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    capability_statement: str = Field(default="", max_length=10000)

    @field_validator("certifications", "categories", "keywords", "regions", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return to_list(v)

    @field_validator("currency", mode="before")
    @classmethod
    def _cur(cls, v: Any) -> str:
        return to_currency(v) or "EUR"

    @field_validator("cpv_prefixes", mode="before")
    @classmethod
    def _cpv(cls, v: Any) -> list[str]:
        items = to_list(v)
        bad = [i for i in items if not i.isdigit() or len(i) > 8]
        if bad:
            raise ValueError("CPV prefixes must be 1-8 digits: " + ", ".join(bad))
        return items


class ProfileOut(ProfileIn):
    model_config = ConfigDict(from_attributes=True)
    updated_at: datetime | None = None


class TenderIn(BaseModel):
    title: str = Field(min_length=3, max_length=1000)
    deadline: datetime
    ref_no: str = Field(default="", max_length=255)
    authority: str = Field(default="", max_length=500)
    category: str = Field(default="", max_length=255)
    cpv_codes: list[str] = Field(default_factory=list)
    country: str = Field(default="", max_length=64)
    region: str = Field(default="", max_length=255)
    currency: str = ""
    estimated_value: float | None = Field(default=None, ge=0)
    bid_security: float | None = Field(default=None, ge=0)
    min_turnover: float | None = Field(default=None, ge=0)
    min_experience_years: int | None = Field(default=None, ge=0, le=500)
    certs_required: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    description: str = Field(default="", max_length=20000)
    url: str = Field(default="", max_length=1000)

    @field_validator("cpv_codes", "certs_required", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return to_list(v)

    @field_validator("currency", mode="before")
    @classmethod
    def _cur(cls, v: Any) -> str:
        return to_currency(v)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class StatusIn(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in STATUS_PIPELINE:
            raise ValueError("status must be one of " + ", ".join(STATUS_PIPELINE))
        return v


class RequirementIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["requirement", "document"] = "requirement"


class RequirementPatch(BaseModel):
    is_met: bool


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class AnalyzeIn(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(default="", max_length=255)
    password: str = Field(min_length=10, max_length=72)
    role: Literal["admin", "member"] = "member"

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        local, _, domain = v.partition("@")
        if not local or not domain or "." not in domain:
            raise ValueError("invalid email address")
        return v
