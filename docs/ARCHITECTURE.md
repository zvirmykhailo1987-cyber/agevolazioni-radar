# Architecture

```
                         ┌──────────────── daily, weekdays 06:00 Europe/Rome (Airflow DAG or cron) ────────────────┐
 inps.it public JSON ──► dlt source ──► DuckDB raw ──► dbt staging ──► dbt marts ──► KB paragraphs ──► extraction cache
   (listing + detail)    lookback 120d   acts            stg_acts       doc_versions   anchored §4.p2     (hash, prompt,
                         append-only     documents       stg_documents  (SCD2)                             model) ─► Claude
                         text versions   (one row per                   incentive_                                 structured
                                          new hash)                     documents                                  output
                                                                                                                      │
                                                    citation gate (code): quote 4+ words in the paragraph ◄──────────┘
                                                    and the quote states the value
                                                                │
                              registry (human aliases) ──► consolidation across acts (fill gaps, flag differences)
                                                                │
              ┌─────────────────────────────┬──────────────────┼─────────────────────┬───────────────────────────┐
              ▼                             ▼                  ▼                     ▼                           ▼
     eligibility engine (code)      Recupero scan       hybrid retrieval      MCP server (read-only)     evals + regression
     pass / fail / unknown          hires × incentives  BM25 + embeddings     search, get, check,        dev / holdout / agent,
     fail-closed                    savings, proposal   RRF, topic routing    diff, status               3 repeats
              │                             │                  │
              └──────────────┬──────────────┴──────────────────┘
                             ▼
                FastAPI + Jinja (read-only KB)  ──writes only──►  SQLite app state: users, sessions,
                CSP, rate limits, CSRF                             review audit log, LLM call log
```

## Two stores, on purpose
- **DuckDB** holds the knowledge base. It is rebuilt by the pipeline and opened read-only by the app and the MCP
  server. DuckDB allows one writer per file, so the serving processes never write to it.
- **SQLite** holds what people do: sign-ins, review decisions (append-only), LLM call log. It is small, transactional
  and survives a full rebuild of the knowledge base.

## Where the model is used, and where it is not
| Step | Model | Why |
|---|---|---|
| Reading a circular into the rule schema | Claude, structured output | language understanding over long legal Italian |
| Deciding if a quote supports a value | no, code | must be repeatable and auditable |
| Merging acts | no, registry + code | a wrong merge must need a person |
| Eligibility and savings | no, code | Legge 12/1979: a professional decides; the system must be explainable line by line |
| Answering a question | Claude, structured output | phrasing; every figure and quote is checked by code before display |

## Failure modes the design closes
- **Hallucinated value**: dropped by the citation gate; shown as "scartato".
- **Quote that exists but does not say the value**: dropped (value-in-quote check).
- **Invented figure in an answer next to a real quote**: the answer becomes a refusal.
- **A condition the schema cannot hold** (e.g. "mother of three"): extracted as `other_conditions`, always "to check".
- **INPS corrects a circular**: new text hash, new version, paragraph diff, extraction re-runs for that version only.
- **Two acts disagree**: no automatic overwrite; the difference is shown and makes the pre-check "to check".
- **Model renames an incentive**: no merge unless a person added the alias to the registry.
