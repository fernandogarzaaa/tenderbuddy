# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-11

First stable release. A rewrite of the beta as an installable package (`src/tenderbuddy`).

### Added
- Live tender connectors: EU TED and UK Contracts Finder (keyless), US SAM.gov (free API key),
  with retry/backoff, timeouts and per-source sync status; CSV connector for GeM, CPP / eProcure,
  PhilGEPS and any portal export (UI/API upload or staging folder).
- `tenderbuddy` CLI: `serve`, `migrate`, `create-user`, `sync`, `seed-demo`, `check-config`.
- Optional in-process scheduled sync (`SYNC_INTERVAL_MINUTES`).
- Versioned JSON API under `/api/v1` with bearer tokens and OpenAPI docs at `/docs`.
- Multi-user accounts with admin/member roles and user management.
- Optional LLM features (tender summary, requirement extraction, bid draft) via any
  OpenAI-compatible endpoint, with an offline rule/template fallback.
- Bid draft export to Markdown and PDF.
- Alembic migrations (upgrades beta databases), PostgreSQL support (`.[postgres]` extra).
- `/healthz` and `/readyz` endpoints, structured JSON logging with request ids.
- Docker image (non-root, health check), docker compose stack with PostgreSQL, dev container.
- CI: ruff, mypy --strict, tests on Python 3.11-3.13, PostgreSQL suite, Docker build + smoke test;
  tag-triggered release workflow publishing wheel and sdist.
- Hashed `requirements.lock`; Dependabot for pip, Docker and GitHub Actions.

### Changed
- Eligibility matcher is now a pure, currency-aware function with a per-criterion breakdown,
  partial credit, certificate prefix matching and CPV/keyword category matching.
- RFP analysis extracts labelled dates, money amounts (INR/EUR/GBP/USD/PHP) and risk flags.
- Configuration via environment variables / `.env` (pydantic-settings); every setting documented
  in `.env.example`.

### Security
- Production mode refuses to start with the default secret key or admin password, or with demo data.
- bcrypt password hashing with a password policy, JWT via PyJWT (python-jose removed),
  HttpOnly cookies, CSRF tokens on all forms, login rate limiting, security headers (CSP,
  X-Frame-Options, HSTS when secure), upload size limits and path checks on downloads.
