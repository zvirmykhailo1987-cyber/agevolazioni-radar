# Agevolazioni Radar v1.0: product spec

Audience: Head of Data & AI at an Italian payroll/HR platform (Jet HR). The demo must read as a finished
internal product that their data team could adopt: knowledge platform + agent in production + proof of quality.

## 1. Knowledge platform
- Corpus: INPS circulars and messages since 2025-01-01 (2026 incentives refer to 2025 acts). Daily, lookback 120 days, versioned.
- Canonical incentive registry (`registry/incentives.yaml`): canonical id, name, aliases (model slugs), group
  (giovani / donne / territorio / stabilizzazione / altro), human-approved. Model output maps to the registry; unknown
  ids land in an "unmapped" queue shown in the UI, never silently merged.
- Retrieval: hybrid. BM25 + local multilingual embeddings (fastembed, free, CPU) fused by reciprocal rank, plus topic
  routing. Choose by holdout eval, not by taste.

## 2. Recupero agent (core value)
- Input: a company workforce in a JSON shape modelled on the public Jet HR API (company: CCNL code, headcount, DURC;
  employees: birth date, gender, region of workplace, hiring date, contract, months without regular job, gross monthly
  salary). Synthetic companies ship with the repo.
- For every hire × incentive: deterministic eligibility (existing engine). The LLM never decides eligibility; it only
  writes the plain-language explanation of each finding and lists the data to collect for "to be checked" items.
- Saving estimate: min(employer contribution × exemption %, monthly cap) × eligible months, using a stated employer
  contribution rate parameter (default shown as an assumption, editable). Never presented as exact.
- Non-cumulability: one incentive per hire unless the sources say otherwise; the agent proposes the highest and marks
  the rest as alternatives "da verificare".
- Human in the loop: a consultant approves/rejects each finding with a note. Every decision is an audit row
  (who, when, what evidence). Reviewer actions require login.
- Output: findings table, totals by incentive, export CSV.
- Agent eval: a synthetic company with a hand-built answer key → precision / recall of findings and saving error.

## 3. Product UI (IT, with EN toggle)
- Panoramica: KB health (freshness, last runs), eval trend, totals.
- Catalogo: incentives with filters (group, status, region), search.
- Incentive detail: rule table with citations, sources timeline, text diff between versions, differences to review
  with resolve action (logged).
- Recupero: pick a company → findings, savings chart, evidence per finding, approve/reject, export.
- Chiedi: grounded Q&A.
- Stato: pipeline runs, LLM calls (model, tokens, latency, estimated cost), eval history.
- Accessible, light/dark, phone + desktop; render QA clean.

## 4. Operations
- LLM call log table; pipeline run log; /health; structured logs.
- Server: daily refresh job (load → dbt → KB → extraction cache), weekly eval run; web reads read-only.
- Secrets only in env; no key in images or repo.

## 5. Quality bar
- ruff, mypy --strict, pytest (offline), coverage ≥ 90%, mutation check 100% caught, CI green.
- Evals: dev + holdout (≥ 20 holdout), 3 repeats, regression gate; agent eval.
- Independent review pass before publishing.

## 6. Docs
- README (what / why / measured), ARCHITECTURE.md (diagram + data flow), ADRs (DuckDB vs Snowflake, hybrid retrieval,
  deterministic eligibility, citation gate, registry), DEMO.md (5-minute walkthrough for an interview).
