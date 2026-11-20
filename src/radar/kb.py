# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""The knowledge base: paragraphs, cached extractions and rules in DuckDB.

Built from ``marts.incentive_documents`` (dbt). Extraction results are cached by
(content_hash, prompt_version, model), so re-running the pipeline costs nothing
unless a document's text or the prompt actually changed.
"""

from __future__ import annotations

import difflib
import itertools
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from radar.extract import PROMPT_VERSION, Extractor, extract_rules
from radar.pipeline import DB_PATH
from radar.rules import IncentiveRule, coverage, verify
from radar.text import Paragraph, paragraphs

DDL = """
create schema if not exists kb;
create table if not exists kb.paragraphs (
    doc_id bigint, content_hash varchar, anchor varchar, section varchar, heading varchar, text varchar,
    primary key (doc_id, content_hash, anchor));
create table if not exists kb.extractions (
    doc_id bigint, content_hash varchar, prompt_version varchar, model varchar, raw_json varchar,
    rules_json varchar, input_tokens integer, output_tokens integer, created_at timestamp,
    primary key (doc_id, content_hash, prompt_version, model));
"""


@contextmanager
def connect(db_path: Path = DB_PATH, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    try:
        con = duckdb.connect(str(db_path), read_only=read_only)
    except duckdb.ConnectionException:
        # DuckDB allows one configuration per file per process: if this process already
        # holds a read-write handle (e.g. the pipeline or dbt ran in-process), reuse that mode.
        con = duckdb.connect(str(db_path))
    try:
        if not read_only:
            con.execute(DDL)
        yield con
    finally:
        con.close()


def current_documents(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    cur = con.execute(
        "select doc_id, content_hash, version, doc_type, number, published_on, subject, url, body_html "
        "from marts.incentive_documents order by published_on, doc_id"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def build_paragraphs(con: duckdb.DuckDBPyConnection) -> int:
    n = 0
    for doc in current_documents(con):
        exists = con.execute(
            "select count(*) from kb.paragraphs where doc_id=? and content_hash=?",
            [doc["doc_id"], doc["content_hash"]],
        ).fetchone()
        if exists and exists[0]:
            continue
        rows = [
            (doc["doc_id"], doc["content_hash"], p.anchor, p.section, p.heading, p.text)
            for p in paragraphs(doc["body_html"])
        ]
        con.executemany("insert into kb.paragraphs values (?,?,?,?,?,?)", rows)
        n += len(rows)
    return n


def doc_paragraphs(con: duckdb.DuckDBPyConnection, doc_id: int, content_hash: str) -> list[Paragraph]:
    rows = con.execute(
        "select anchor, section, heading, text from kb.paragraphs where doc_id=? and content_hash=? "
        "order by try_cast(section as double) nulls first, section, "
        "cast(split_part(anchor, '.p', 2) as integer)",
        [doc_id, content_hash],
    ).fetchall()
    return [Paragraph(a, s, h, t) for a, s, h, t in rows]


def title(doc: dict[str, Any]) -> str:
    return f"INPS {doc['doc_type']} n. {doc['number']} del {doc['published_on']}: {doc['subject']}"


def extract_all(
    con: duckdb.DuckDBPyConnection,
    llm: Extractor,
    model: str,
    limit: int | None = None,
    only: set[str] | None = None,
) -> dict[str, int]:
    """Extract every current document not yet in the cache.

    ``limit`` caps the number of *paid* calls per run (cached documents do not count), newest acts
    first, so a daily cap never starves recent acts behind old cached ones. One failed call does
    not stop the run: it is counted and retried on the next run (nothing is cached for it).
    """
    stats = {"cached": 0, "extracted": 0, "failed": 0, "deferred": 0, "input_tokens": 0, "output_tokens": 0}
    docs = [d for d in current_documents(con) if not only or str(d["number"]) in only]
    for doc in reversed(docs):  # newest first
        hit = con.execute(
            "select 1 from kb.extractions where doc_id=? and content_hash=? and prompt_version=? and model=?",
            [doc["doc_id"], doc["content_hash"], PROMPT_VERSION, model],
        ).fetchone()
        if hit:
            stats["cached"] += 1
            continue
        if limit is not None and stats["extracted"] + stats["failed"] >= limit:
            stats["deferred"] += 1
            continue
        paras = doc_paragraphs(con, doc["doc_id"], doc["content_hash"])
        try:
            res = extract_rules(paras, title(doc), llm)
        except Exception as exc:  # network, rate limit, malformed output: retry tomorrow, keep going
            stats["failed"] += 1
            print(f"extraction failed for {doc['doc_type']} {doc['number']}: {type(exc).__name__}: {exc}"[:300])
            continue
        con.execute(
            "insert into kb.extractions values (?,?,?,?,?,?,?,?,?)",
            [
                doc["doc_id"],
                doc["content_hash"],
                PROMPT_VERSION,
                model,
                json.dumps(res.raw, ensure_ascii=False),
                json.dumps([r.model_dump() for r in res.rules], ensure_ascii=False),
                res.usage.get("input_tokens", 0),
                res.usage.get("output_tokens", 0),
                datetime.now(UTC),
            ],
        )
        stats["extracted"] += 1
        stats["input_tokens"] += res.usage.get("input_tokens", 0)
        stats["output_tokens"] += res.usage.get("output_tokens", 0)
    return stats


_RULES_SELECT = (
    "select e.doc_id, e.content_hash, d.number, d.doc_type, d.published_on, d.url, e.raw_json, e.model "
    "from kb.extractions e "
    "join marts.incentive_documents d on d.doc_id = e.doc_id and d.content_hash = e.content_hash "
    "where e.prompt_version = ? "
)
_RULES_ORDER = (
    "qualify row_number() over (partition by e.doc_id order by e.created_at desc) = 1 order by d.published_on, d.number"
)
_RULES_ANY_MODEL = _RULES_SELECT + _RULES_ORDER
_RULES_FOR_MODEL = _RULES_SELECT + "and e.model = ? " + _RULES_ORDER


def rules(con: duckdb.DuckDBPyConnection, model: str | None = None) -> list[dict[str, Any]]:
    """Latest extracted rules for the current version of each document.

    The citation gate is re-applied here to the model's raw output, so a stricter gate
    takes effect on stored extractions without paying for the API again.
    """
    # two constant queries, no string formatting: every value is a bound parameter
    q = _RULES_FOR_MODEL if model else _RULES_ANY_MODEL
    params: list[Any] = [PROMPT_VERSION] + ([model] if model else [])
    out = []
    for doc_id, chash, number, doc_type, published_on, url, raw_json, mdl in con.execute(q, params).fetchall():
        paras = doc_paragraphs(con, doc_id, chash)
        for r in json.loads(raw_json).get("incentives", []):
            rule = verify(IncentiveRule.model_validate(r), paras)
            out.append(
                {
                    "doc_id": doc_id,
                    "number": number,
                    "doc_type": doc_type,
                    "published_on": str(published_on),
                    "url": url,
                    "model": mdl,
                    "rule": rule,
                    "coverage": coverage(rule),
                }
            )
    return out


def diff_versions(con: duckdb.DuckDBPyConnection, doc_id: int) -> list[dict[str, Any]]:
    """Paragraph-level diff between consecutive versions of one document."""
    versions = con.execute(
        "select version, content_hash, valid_from from marts.doc_versions where doc_id=? order by version", [doc_id]
    ).fetchall()
    out = []
    for (v1, h1, _), (v2, h2, at) in itertools.pairwise(versions):
        a = [f"[{p.anchor}] {p.text}" for p in doc_paragraphs(con, doc_id, h1)]
        b = [f"[{p.anchor}] {p.text}" for p in doc_paragraphs(con, doc_id, h2)]
        changes = [
            line
            for line in difflib.unified_diff(a, b, lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        ]
        out.append({"from_version": v1, "to_version": v2, "at": str(at), "changes": changes})
    return out


def main() -> None:  # pragma: no cover - thin CLI
    import argparse

    from radar.extract import DEFAULT_MODEL, AnthropicExtractor

    ap = argparse.ArgumentParser(description="Build the knowledge base")
    ap.add_argument("step", choices=["paragraphs", "extract"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default=None, help="comma-separated act numbers, e.g. 55,56")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()
    with connect() as con:
        if args.step == "paragraphs":
            print({"new_paragraphs": build_paragraphs(con)})
        else:
            only = set(args.only.split(",")) if args.only else None
            print(extract_all(con, AnthropicExtractor(model=args.model), args.model, args.limit, only))


if __name__ == "__main__":  # pragma: no cover
    main()
