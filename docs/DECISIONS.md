# Decisions

Short records of the choices a reviewer is most likely to question.

## ADR-1 DuckDB instead of Snowflake
**Context.** The target stack is dlt + dbt + Snowflake. This demo must run on a laptop and a small VPS at no cost.
**Decision.** DuckDB through `dlt[duckdb]` and `dbt-duckdb`. Models use plain SQL with `qualify` and window functions,
which Snowflake supports as well; moving is a destination and a profile change.
**Cost.** No warehouse features (time travel, roles). Versioning is done in the models (SCD2 on the text hash).

## ADR-2 Lexical retrieval with topic routing; dense retrieval measured and left off
**Context.** Italian legal text with exact terms ("ZES", "35 anni", "tempo indeterminato") and many near-duplicate
paragraphs across acts (the 2024 and 2026 "Bonus Giovani" circulars read almost alike).
**Decision.** BM25 over paragraphs prefixed with the act's distinctive subject ("Bonus Giovani 2026. Prime
indicazioni operative"). Dense retrieval (local multilingual MiniLM via fastembed, fused by reciprocal rank) is
implemented and tested but off by default.
**Evidence.** Offline recall of the hand-picked gold paragraph in the top 10 (no LLM involved):
BM25 18/24 dev and 5/8 holdout; dense alone 8/24 and 1/8; equal-weight fusion 11/24 and 4/8; down-weighted
fusion never beat BM25 (weights 0.15, 0.3, 0.5 → 17, 16, 14 dev). The small multilingual model does not separate
near-identical legal paragraphs; a larger or domain-tuned model is the next experiment, not a default.

## ADR-3 The model never decides eligibility
**Context.** Italian law reserves payroll and contribution decisions to *consulenti del lavoro* (L. 12/1979); an AI
used by a professional must stay instrumental (L. 132/2025, art. 13).
**Decision.** Eligibility and savings are plain code over the extracted rule. Missing data, an ungrounded condition
or an unresolved difference between acts means "to check", never "no" and never "passed". Every finding carries the
quote and the paragraph that justify each criterion, and a consultant approves or rejects it in the app.

## ADR-4 Citation gate in code
**Decision.** A value is kept only if (a) its quote has at least four words and occurs on word boundaries in the
cited paragraph, and (b) the quote states the value: the number as digits or as an Italian number word, the date with
day, month and year, each listed region. The gate re-runs on stored model output at read time, so tightening it costs
no API calls.

## ADR-5 Human-maintained incentive registry
**Context.** The model names the same incentive differently across acts.
**Decision.** Merge only through aliases a person added to `registry/incentives.yaml`; unknown names are reported as
"unmapped" on the status page. A missed merge is visible and cheap; a wrong merge would silently mix two incentives.

## ADR-6 Not cumulable by default
**Decision.** For each hire the scan proposes the incentive with the highest estimate and lists the others as
alternatives. Cumulability rules are not modelled; the consultant decides.
