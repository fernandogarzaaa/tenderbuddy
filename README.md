# Tenderbuddy

AI tender discovery and bid preparation: find matching government tenders, check eligibility, and prepare bids faster. (Beta)

## What it does

- **Company profile** — turnover, experience, certifications, categories, states; drives the matcher.
- **Tender pool** — 12 seeded Indian government-style tenders (GeM / CPP / eProcure), plus working CSV import.
- **Eligibility matcher** — rule-based 0-100 score per tender with a per-criterion breakdown (turnover 30, experience 20, certifications 25, category 15, state 10).
- **Tender detail + bid workspace** — key dates, EMD, estimated value; RFP/BOQ PDF upload with automatic requirement extraction (regex + heuristics), deadline/amount detection, and risk flags (penalty clauses, corrigendums, short timelines); requirements and document checklists; notes; status pipeline New -> Reviewing -> Preparing -> Submitted -> Won/Lost.
- **Sources** — GeM, CPP Portal, eProcure records with last-sync stamps; "Sync now" imports staged portal-export CSVs from `data/staging/<source>/` (no live portal APIs; this is stated in the UI).
- **Dashboard** — open tenders, matched count, average match score, deadline alerts, Chart.js breakdowns by category and month.

## Quickstart

## Quickstart

```bash
cp .env.example .env
docker compose up --build
# open http://localhost:8004
# login: admin@clusterx.local / ChangeMe123!
```

Local dev (SQLite):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.seed
uvicorn app.main:app --port 8004
```

## Tests

```bash
python -m pytest -q
ruff check app
```

## License

MIT
