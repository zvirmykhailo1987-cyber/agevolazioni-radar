# Agevolazioni Radar

[![ci](https://github.com/zvirmykhailo1987-cyber/agevolazioni-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/zvirmykhailo1987-cyber/agevolazioni-radar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Italian hiring incentives, read from the source and applied to a company's hires.** A daily pipeline loads INPS
circulars and messages, versions their text and turns each incentive into a rule in which every value carries the
paragraph and the exact words it was read from. *Recupero* checks every hire of a company against every incentive,
estimates the contribution saving and hands each proposal to a *consulente del lavoro*, who approves or rejects it.
Agents use the same knowledge base through an MCP server.

**Live demo:** https://incentivi-demo.159.195.219.73.sslip.io — public INPS data, two synthetic example companies.
Consultant sign-in: `consulente` / `consulente-demo-2026` (each visitor's decisions stay in their own session).
Five-minute walkthrough: [docs/DEMO.md](docs/DEMO.md).

> Technical demo, not advice. In Italy the decision on payroll incentives belongs to a *consulente del lavoro*
> (L. 12/1979). This system pre-screens, cites and estimates; it never decides. Not affiliated with INPS.

![Incentive page: every value with its quote, paragraph and act](docs/screenshot-incentive.png)

## Why it is built this way

Incentive rules change through a chain of acts: a law, then a circular, then operating messages, sometimes a
corrected circular. Secondary sources often get the details wrong. Example from this corpus: circular 56 says the
2026 ZES bonus needs a worker who has **reached** 35, an employer with **at most 10** employees, and covers **ten**
regions, Marche and Umbria included. A system that proposes money to an employer has to stand on the primary text,
keep its versions, say "not stated" instead of guessing, and leave the decision to the professional.

## What it does

| | |
|---|---|
| **Load and version** | `dlt` source over the public JSON endpoints behind inps.it: 651 acts since January 2025, 49 of them about employer contribution incentives. Each run re-reads the last 120 days, because INPS corrects acts after publication; a text row is appended only when its hash is new. `dbt` on DuckDB builds a version table (SCD2 on the text hash) and the knowledge-base input; source freshness warns after 2 days. Dates in Europe/Rome time (UTC puts every act one day early). |
| **Anchored paragraphs** | INPS HTML becomes numbered paragraphs (`§4.p2`, 4,419 in total), so every value and every answer can point at one place in one act. |
| **Extraction with a citation gate** | Claude fills a strict schema through structured outputs, "null if not stated". A value is kept only if its quote (4+ whole words) is in the cited paragraph **and** states the value: digits or Italian number words, full dates, each region by name. Conditions the schema cannot hold ("madre di almeno tre figli") are extracted as text, tagged as about the worker or the employer, and never assumed true. The gate is re-applied to stored model output on every read. |
| **Registry and consolidation** | A human-maintained registry (18 incentives, aliases, required criteria) maps acts to incentives; an unmapped incentive is reported, never merged by guess. The earliest act is primary; a later act fills gaps; a *different* value is shown for review and applied only when a consultant decides, difference by difference. |
| **Eligibility (code, not the model)** | Per criterion: pass / fail / to check, with the quote behind each line. Missing hire data, a value the gate dropped, a required criterion the acts did not yield, an open-ended hiring window or an unresolved difference all mean "to check" — never "passed". A few exact INPS phrases ("Personale non dirigenziale") are settled from profile data; a sentence that mixes topics or carries an exception stays with the consultant. |
| **Recupero (the agent)** | Every hire × incentive. Saving = min(salary × employer rate × exemption %, monthly cap) × duration, with the ZES cap where the region qualifies and the employer rate shown as an assumption. One proposal per hire (incentives treated as not cumulable), alternatives listed, employer conditions as an attestation checklist, CSV export. |
| **Human in the loop** | Consultant sign-in (scrypt, session token stored as a hash, CSRF on every write, 8-hour expiry), approve/reject with a note, an append-only audit log; rejected proposals leave the totals. |
| **Q&A and MCP** | BM25 over the paragraphs, indexed with each act's subject. Every quote must be in its paragraph and every figure in the answer must appear in the retrieved text, otherwise the answer becomes a refusal. MCP tools (read-only): `search_passages`, `list_incentives`, `get_incentive`, `check_eligibility`, `diff_versions`, `kb_status`. |
| **Operations** | Airflow DAG and a server cron (weekdays, Rome time): load, dbt build, paragraphs, extraction of new or changed acts, eval alert; the refresh runs on a copy and swaps it in atomically. LLM call log (tokens, latency, errors), status page with eval history and loads, CSP and rate limits on live questions. |

Architecture and failure modes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Decisions a reviewer will question
(DuckDB vs Snowflake, why dense retrieval is off, why the model never decides): [docs/DECISIONS.md](docs/DECISIONS.md).
Product scope: [docs/PRODUCT-SPEC.md](docs/PRODUCT-SPEC.md).

## Measured (20 Nov 2026)

**Grounded Q&A** — [golden set](evals/golden_qa.json): 24 *dev* questions used while tuning, 8 *holdout* questions
written afterwards and never used for tuning, 7 questions the sources do not answer (2 of them on-topic near
misses). 3 runs each, scored on whole words; min and max equal the mean unless shown.

| | dev (24) | holdout (8) |
|---|---|---|
| Answer states the expected facts | 92% | 75% |
| A cited paragraph itself states those facts | 90% (88–92) | 75% |
| Refuses when the sources are silent | 5/5 | 2/2 |
| Quotes rejected by the gate | 0 | 0 |
| Gold paragraph among the top 10 retrieved (no LLM) | 18/24 | 5/8 |

Every answered question stated the expected facts; every miss was a refusal, not a wrong answer. The misses are
retrieval misses. Dense retrieval (MiniLM embeddings fused with BM25) was measured and did worse on this corpus, so it
ships switched off ([ADR-2](docs/DECISIONS.md)).

**Extraction:** 26/27 hand-labelled fields across circulars 55, 56 and 57, including fields that must stay empty. The
miss: circular 56 also admits *somministrazione*; the model returned only *tempo indeterminato*.

**Recupero** — [answer key](evals/golden_recupero.json) for the two example companies (8 expected findings), written
by hand from the circulars:

| | |
|---|---|
| Expected findings surfaced (recall) | 8/8 |
| Surfaced findings that are expected (precision) | 8/8 |
| Wrong "probabile" (would cost the employer money) | 0 |
| Verdict "to check" where the key says "probabile" | 5/8 |

The engine is conservative on purpose: worker conditions it cannot settle from data, unresolved differences between
acts and required criteria missing from the extraction all become "to check". The answer key and the companies are
the author's; an independent key from a payroll consultant is the next step.

**Code:** 135 tests, offline (fake INPS over `httpx.MockTransport`, fake LLM, real dlt + dbt in a temporary DuckDB),
96% line+branch coverage (gate 90% in CI), `mypy --strict`, ruff, bandit, pip-audit. Mutation check: each guarantee is removed on purpose
and a test must fail ([docs/mutation-results.json](docs/mutation-results.json)) — citation gate, conservative
resolvers, required criteria, fail-closed eligibility, Rome dates, version dedup, CSRF, session expiry, visitor
isolation in the demo, CSV formula escaping, rate limits, the CSP header.

**Cost:** extracting all 49 acts took 0.72M input and 0.11M output tokens (prompt v6), once. Afterwards only new or
corrected acts are extracted: results are cached by text hash, prompt version and model, and the server caps a daily
refresh at 10 extractions.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -c requirements.lock -e ".[dev]"
cp .env.example .env.local            # ANTHROPIC_API_KEY=... (extraction, evals and live Q&A only)

radar-load --since 2025-01-01         # INPS -> DuckDB (data/radar.duckdb)
dbt build --project-dir dbt --profiles-dir dbt
radar-kb paragraphs && radar-kb extract
radar-evals --repeats 3 --baseline evals/results/baseline.json   # non-zero exit on any regression
radar-web                             # http://localhost:8040
radar-mcp                             # MCP over stdio (Claude Desktop, Claude Code)
pytest                                # no network, no API key
```

`docker compose up` runs the web app with the knowledge base mounted read-only.

## Known limits

- Retrieval misses 3 of 8 holdout questions; the system then refuses rather than answers.
- INPS acts only, since 2025; laws in Gazzetta Ufficiale and CCNL texts are not loaded.
- Savings assume the maximum duration and an employer contribution rate; actual start date, part-time and
  terminations are not modelled.
- Cumulability between incentives is not modelled; the consultant chooses between the proposal and its alternatives.
- The registry, the answer keys and the example companies are written by the author.

## License

[MIT](LICENSE) © 2026 Mykhailo Zvir
