# Tenderbuddy

AI tender discovery and bid preparation: pull public tenders from live government
sources, score each one against your company profile, and prepare the bid in one
workspace. Works fully offline; an LLM is optional.

- **Live sources**: EU TED and UK Contracts Finder (no key), US SAM.gov (free key),
  plus CSV import for portals without a public API (GeM, CPP / eProcure, PhilGEPS, ...).
- **Eligibility matcher**: deterministic 0-100 score with a per-criterion breakdown.
- **Bid workspace**: RFP/BOQ PDF upload with requirement, date, amount and risk extraction;
  requirement checklist, notes, status pipeline, bid drafts exported as Markdown or PDF.
- **Web UI + JSON API** (FastAPI, OpenAPI docs at `/docs`), multi-user with admin/member roles.

## Quickstart

### Docker (app + PostgreSQL)

```bash
cp .env.example .env
# edit .env: APP_ENV=production, SECRET_KEY (32+ random chars), ADMIN_EMAIL,
# ADMIN_PASSWORD (not the default) and POSTGRES_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(48))"   # a SECRET_KEY
docker compose up -d --build
# open http://localhost:8004 and sign in with ADMIN_EMAIL / ADMIN_PASSWORD
```

Migrations run automatically when the container starts.

### Local (SQLite)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .            # add ".[postgres]" for PostgreSQL, ".[dev]" for tests
cp .env.example .env        # development defaults are fine locally
tenderbuddy seed-demo       # optional: demo profile + tenders (refused in production)
tenderbuddy serve           # http://localhost:8004
```

The first admin is created from `ADMIN_EMAIL` / `ADMIN_PASSWORD` when the users table is empty.
Pinned, hashed dependencies for reproducible installs are in `requirements.lock`
(`pip install --require-hashes -r requirements.lock && pip install --no-deps .`).

### Codespaces demo

Open the repo in GitHub Codespaces; the dev container installs the package, seeds demo
data and starts the server on the forwarded port 8004. Sign in with the
`ADMIN_EMAIL` / `ADMIN_PASSWORD` defaults from `.env.example`. Demo only, not for production.

## CLI

```
tenderbuddy serve [--host H] [--port P] [--workers N]   run the web server
tenderbuddy migrate                                     apply database migrations
tenderbuddy create-user EMAIL [--role admin|member]     create a user (prompts for the password)
tenderbuddy sync [SOURCE]                               fetch tenders from all enabled sources, or one
tenderbuddy seed-demo                                   load demo data (refused when APP_ENV=production)
tenderbuddy check-config                                validate and print the effective config (no secrets)
```

`tenderbuddy sync` exits non-zero if any source failed, so it is safe to run from cron.

## Configuration

All settings are environment variables (or a `.env` file). `.env.example` documents every one.

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | `production` refuses to start with the default `SECRET_KEY`/`ADMIN_PASSWORD` or with demo seeding on |
| `DATABASE_URL` | `sqlite:///./data/tenderbuddy.db` | PostgreSQL: `postgresql+psycopg://user:pass@host:5432/db` |
| `DATA_DIR` | `./data` | uploads and CSV staging live here |
| `SECRET_KEY` | insecure sentinel | signs session/API tokens; 32+ random chars in production |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | `admin@example.com` / sentinel | first admin only |
| `COOKIE_SECURE` | `true` in production | set `true` whenever served over HTTPS |
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` | `10` / `300` | failed logins per window |
| `MAX_UPLOAD_MB` | `20` | PDF / CSV upload limit |
| `CONNECTORS` | `ted,contracts_finder,csv` | any of `ted`, `contracts_finder`, `sam_gov`, `csv` |
| `SYNC_INTERVAL_MINUTES` | `0` | in-process scheduled sync; `0` = off (use cron + `tenderbuddy sync`) |
| `SYNC_LOOKBACK_DAYS` / `SYNC_MAX_RECORDS` | `7` / `200` | per sync, per source |
| `HTTP_TIMEOUT_SECONDS` / `HTTP_MAX_RETRIES` | `30` / `3` | connector HTTP behaviour (retries with backoff) |
| `TED_COUNTRIES` / `TED_CPV_PREFIXES` | empty | optional TED filters, e.g. `IRL,MLT` / `72,48` |
| `SAM_API_KEY` / `SAM_PTYPES` | empty / `o,k` | SAM.gov key and notice types |
| `CSV_STAGING_DIR` | `DATA_DIR/staging` | files in `<dir>/<source>/*.csv` |
| `LLM_PROVIDER` | `auto` | `auto` uses the API when `LLM_BASE_URL` is set, else offline `mock` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | empty / empty / `llama-3.1-8b-instant` | any OpenAI-compatible endpoint (Groq, OpenRouter, Ollama, ...) |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `text` for local reading |

## Tender sources (connectors)

| Source | Name | Access | Notes |
|---|---|---|---|
| EU **TED** (Tenders Electronic Daily) | `ted` | live, keyless | TED v3 search API; filter by buyer country and CPV prefix |
| UK **Contracts Finder** | `contracts_finder` | live, keyless | OCDS search API |
| US **SAM.gov** opportunities | `sam_gov` | live, free personal key | SAM.gov -> Account Details -> Public API Key, set `SAM_API_KEY` |
| **CSV** (GeM, CPP / eProcure, PhilGEPS, any portal export) | `csv` | file import | upload in the UI/API, or drop files in `<staging>/<source>/*.csv` and sync |

GeM, CPP Portal / eProcure and PhilGEPS have no public bulk API, so Tenderbuddy imports their
exports instead of scraping them. CSV columns (case-insensitive headers, `*` required):

```
title*, deadline*, ref_no, external_id, authority, category, cpv_codes, country, region,
currency, estimated_value, bid_security, min_turnover, min_experience_years, certs_required,
published_date, description, url, source
```

Lists are `|` or `;` separated. Beta-era columns (`est_value_lakh`, `emd_lakh`,
`turnover_req_cr`, `exp_req_years`, `certs_req`, `state`) are still accepted and converted to INR.

Records are upserted by source + external id, so repeated syncs update rather than duplicate.
To add a source, subclass `tenderbuddy.connectors.base.Connector`, implement `fetch`, and register
it in `tenderbuddy.connectors.REGISTRY` and `config.KNOWN_CONNECTORS`.

## Eligibility matching

`tenderbuddy.matcher.match_score(profile, tender)` is a pure function (no database) returning a
score and breakdown. Weights: turnover 30, experience 20, certifications 25, category 15, location 10.
Missing requirements count as met; turnover/experience earn proportional partial credit; a turnover
requirement in another currency is `unknown` (half credit); certificates match on normalised prefixes
("ISO 9001" ~ "ISO 9001:2015"); category matches by name, CPV prefix or keyword; location by region,
country or a wildcard such as "worldwide".

## AI features

Summaries, requirement extraction and bid drafts use any OpenAI-compatible API when `LLM_BASE_URL`
is set. Without one (or when the call fails) Tenderbuddy falls back to deterministic rules and
templates, and the UI shows which mode produced each result. LLM output adds to rule-based
extraction; it never replaces it.

## Architecture

```
src/tenderbuddy/
  app.py            FastAPI app factory, middleware (request id, security headers, CSRF cookie), health
  cli.py            `tenderbuddy` command
  config.py         pydantic-settings Settings + production guard
  db.py, models.py  SQLAlchemy 2 engine/session and ORM models
  migrate.py, migrations/   Alembic migrations (run on start and via `tenderbuddy migrate`)
  connectors/       TED, Contracts Finder, SAM.gov, CSV -> TenderRecord
  services.py       sync + upsert, profile, scoring, listing
  matcher.py        pure eligibility scoring
  rfp.py            PDF text extraction + regex/heuristic RFP analysis
  bidding.py        uploads, requirements, notes, status pipeline
  ai/               LLM client (OpenAI-compatible + offline mock) and AI features
  export.py         bid draft Markdown/PDF export (reportlab)
  security.py       bcrypt passwords, JWT tokens, CSRF, login rate limiting
  users.py          users and roles
  web/              HTML pages (Jinja2), JSON API (/api/v1), auth
  templates/, static/
```

- Browser sessions use an HttpOnly JWT cookie plus a double-submit CSRF token on every form.
- API clients get a bearer token from `POST /api/v1/auth/token`; see `/docs` for all endpoints.
- `GET /healthz` (liveness) and `GET /readyz` (database reachable) for orchestrators.

## Deploy

- **Docker Compose** (`docker-compose.yml`): app + PostgreSQL 16 with persistent volumes; the image
  runs as a non-root user with a health check. Put it behind a TLS-terminating reverse proxy
  (Caddy, nginx, Traefik) and keep `COOKIE_SECURE=true`. The image trusts `X-Forwarded-*`
  headers only from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`); set it to your proxy's address
  so client IPs (login rate limit, logs) and `https` URLs are right.
- **Single container** (SQLite): `docker build -t tenderbuddy . && docker run -p 8004:8004 -v tb:/data
  -e SECRET_KEY=... -e ADMIN_PASSWORD=... tenderbuddy`.
- **Any PaaS that runs a Dockerfile** works the same way: set the environment variables above and
  attach a volume at `/data` (or use PostgreSQL).
- **Syncing**: either set `SYNC_INTERVAL_MINUTES` (single-worker deployments) or schedule
  `tenderbuddy sync` with cron / a scheduled job.
- **Backups**: back up the database and the `/data/uploads` directory.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
mypy                      # strict
pytest                    # offline suite; live API tests are skipped by default
pytest -m live            # hits TED / Contracts Finder (and SAM.gov if SAM_API_KEY is set)
TEST_POSTGRES_URL=postgresql+psycopg://user:pass@localhost:5432/db pytest tests/test_postgres.py
```

CI runs lint + mypy, tests on Python 3.11-3.13, the PostgreSQL suite and a Docker build + smoke test.
Pushing a `vX.Y.Z` tag builds the wheel and sdist and publishes a GitHub release from `CHANGELOG.md`.

## License

MIT. See [LICENSE](LICENSE). Security reports: see [SECURITY.md](SECURITY.md).
