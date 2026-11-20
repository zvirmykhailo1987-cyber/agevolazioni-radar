# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Read-only facade over the knowledge base, shared by the MCP server and the web app."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from radar import kb
from radar.consolidate import Merged, consolidate
from radar.eligibility import HireProfile, assess
from radar.pipeline import DB_PATH
from radar.rag import Passage, build_retriever
from radar.rules import IncentiveRule


@dataclass
class Store:
    db_path: Path = DB_PATH
    passages: list[Passage] = field(default_factory=list)
    rule_rows: list[dict[str, Any]] = field(default_factory=list)
    freshness: dict[str, Any] = field(default_factory=dict)
    merged: dict[str, Merged] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._mtime = 0.0
        self._checked = 0.0
        self.reload()

    def refresh(self, every_s: float = 30.0) -> Store:
        """Reload when the pipeline has rewritten the database (checked at most every ``every_s``)."""
        now = time.monotonic()
        if now - self._checked >= every_s:
            self._checked = now
            try:
                mtime = self.db_path.stat().st_mtime
            except OSError:
                return self
            if mtime > self._mtime:
                self.reload()
        return self

    def reload(self) -> None:
        try:
            self._mtime = self.db_path.stat().st_mtime
        except OSError:
            self._mtime = 0.0
        with kb.connect(self.db_path, read_only=True) as con:
            rows = con.execute(
                "select p.doc_id, d.doc_type || ' n. ' || d.number || ' del ' || strftime(d.published_on, '%d/%m/%Y'),"
                " d.url, p.anchor, p.heading, p.text, d.subject from kb.paragraphs p"
                " join marts.incentive_documents d using (doc_id, content_hash)"
                " order by d.published_on, p.doc_id"
            ).fetchall()
            self.passages = [Passage(*r) for r in rows]
            self.rule_rows = kb.rules(con)
            self.merged = consolidate(self.rule_rows)
            last_pub, n_acts, n_rel = con.execute(
                "select max(published_on), count(*), sum(relevant::int) from staging.stg_acts"
            ).fetchone() or (None, 0, 0)
            n_docs = con.execute("select count(*) from marts.incentive_documents").fetchone()
            self.freshness = {
                "latest_act_published_on": str(last_pub) if last_pub else None,
                "acts_scanned": n_acts,
                "incentive_acts": n_rel,
                "documents_in_kb": n_docs[0] if n_docs else 0,
                "days_since_latest_act": (date.today() - last_pub).days if last_pub else None,
            }
        self.retriever = build_retriever(self.passages) if self.passages else None

    # --- queries -----------------------------------------------------------
    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        if not self.retriever:
            return []
        hits = self.retriever.invoke(query)[:k]
        return [
            {
                "doc": self.passages[d.metadata["i"]].doc_label,
                "anchor": self.passages[d.metadata["i"]].anchor,
                "url": self.passages[d.metadata["i"]].url,
                "text": self.passages[d.metadata["i"]].text,
            }
            for d in hits
        ]

    def incentives(self) -> list[dict[str, Any]]:
        return [
            {
                "incentive_id": iid,
                "name": m.rule.name,
                "source": m.primary["label"],
                "url": m.primary["url"],
                "sources": [x["label"] for x in m.sources],
                "differences": len(m.differences),
                "fields": m.coverage,
            }
            for iid, m in self.merged.items()
        ]

    def rule(self, incentive_id: str) -> tuple[IncentiveRule, dict[str, Any]] | None:
        m = self.merged.get(incentive_id)
        if m is None:
            return None
        meta = {**m.primary, "sources": m.sources, "field_sources": m.field_sources, "differences": m.differences}
        return m.rule, meta

    def check(self, incentive_id: str, profile: HireProfile) -> dict[str, Any] | None:
        found = self.rule(incentive_id)
        if not found:
            return None
        rule, meta = found
        out = assess(rule, profile, pending_differences=len(meta["differences"])).as_dict()
        out["source"] = {"doc": meta["label"], "url": meta["url"]}
        out["sources"] = [x["label"] for x in meta["sources"]]
        out["disclaimer"] = (
            "Pre-verifica automatica, non consulenza: la decisione spetta a un consulente del lavoro (L. 12/1979)."
        )
        return out
