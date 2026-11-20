# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Read-only facade over the knowledge base, shared by the MCP server and the web app."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from radar import kb
from radar.consolidate import Merged, consolidate
from radar.eligibility import HireProfile, assess
from radar.pipeline import DB_PATH
from radar.rag import Embedder, Passage, build_retriever, try_embedder
from radar.registry import Registry
from radar.rules import IncentiveRule


def pending(m: Merged) -> list[dict[str, Any]]:
    return [d for d in m.differences if not d.get("resolved")]


def status_of(hire_from: str | None, hire_to: str | None, today: date | None = None) -> str:
    """attiva / scaduta / futura from the hiring window; 'da verificare' when the window is not stated."""
    day = (today or date.today()).isoformat()
    if not hire_to and not hire_from:
        return "da verificare"
    if hire_to and day > hire_to:
        return "scaduta"
    if hire_from and day < hire_from:
        return "futura"
    return "attiva"


@dataclass
class Store:
    db_path: Path = DB_PATH
    rule_rows: list[dict[str, Any]] = field(default_factory=list)
    freshness: dict[str, Any] = field(default_factory=dict)
    merged: dict[str, Merged] = field(default_factory=dict)
    unmapped: list[dict[str, Any]] = field(default_factory=list)
    registry: Registry = field(default_factory=Registry.load)

    def __post_init__(self) -> None:
        self._mtime = 0.0
        self._checked = 0.0
        self._pristine: dict[str, Merged] = {}
        self._res_version: Any = None
        self.index: tuple[list[Passage], Any] = ([], None)
        self.embedder: Embedder | None = None
        self._embedder_tried = False
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
                try:
                    self.reload()
                except Exception:  # e.g. mid-swap: keep serving the previous state, retry next time
                    return self
        return self

    def reload(self) -> None:
        """Build everything into locals, then swap: a failure leaves the previous state intact,
        and readers never see new passages with an old index."""
        try:
            mtime = self.db_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        with kb.connect(self.db_path, read_only=True) as con:
            rows = con.execute(
                "select p.doc_id, d.doc_type || ' n. ' || d.number || ' del ' || strftime(d.published_on, '%d/%m/%Y'),"
                " d.url, p.anchor, p.heading, p.text, d.subject from kb.paragraphs p"
                " join marts.incentive_documents d using (doc_id, content_hash)"
                " order by d.published_on, p.doc_id"
            ).fetchall()
            passages = [Passage(*r) for r in rows]
            rule_rows = kb.rules(con)
            mapped, unmapped = self.registry.apply(rule_rows)
            merged = consolidate(mapped)
            last_pub, n_acts, n_rel = con.execute(
                "select max(published_on), count(*), sum(relevant::int) from staging.stg_acts"
            ).fetchone() or (None, 0, 0)
            n_docs = con.execute("select count(*) from marts.incentive_documents").fetchone()
        if self.embedder is None and not self._embedder_tried:
            self.embedder, self._embedder_tried = try_embedder(), True
        retriever = (
            build_retriever(passages, embedder=self.embedder, cache_dir=self.db_path.parent / "emb")
            if passages
            else None
        )
        freshness = {
            "latest_act_published_on": str(last_pub) if last_pub else None,
            "acts_scanned": n_acts,
            "incentive_acts": n_rel,
            "documents_in_kb": n_docs[0] if n_docs else 0,
            "days_since_latest_act": (date.today() - last_pub).days if last_pub else None,
        }
        # one assignment for the pair that must stay consistent, then the rest
        self.index = (passages, retriever)
        self.rule_rows, self.unmapped, self.freshness = rule_rows, unmapped, freshness
        self._pristine = merged
        self.merged = copy.deepcopy(merged)
        self._res_version = None
        self._mtime = mtime

    @property
    def passages(self) -> list[Passage]:
        return self.index[0]

    @property
    def retriever(self) -> Any:
        return self.index[1]

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
                "differences": len(pending(m)),
                "fields": m.coverage,
                "group": self.registry.entries[iid].group,
                "side": self.registry.entries[iid].side,
                "hire_from": m.rule.hire_from.value,
                "hire_to": m.rule.hire_to.value,
                "status": status_of(m.rule.hire_from.value, m.rule.hire_to.value),
            }
            for iid, m in self.merged.items()
        ]

    def rule(self, incentive_id: str) -> tuple[IncentiveRule, dict[str, Any]] | None:
        m = self.merged.get(incentive_id)
        if m is None:
            return None
        meta = {**m.primary, "sources": m.sources, "field_sources": m.field_sources, "differences": m.differences}
        return m.rule, meta

    def required(self, incentive_id: str) -> tuple[str, ...]:
        entry = self.registry.entries.get(incentive_id)
        return entry.required if entry else ()

    def check(self, incentive_id: str, profile: HireProfile) -> dict[str, Any] | None:
        found = self.rule(incentive_id)
        if not found:
            return None
        rule, meta = found
        out = assess(
            rule,
            profile,
            pending_differences=len([d for d in meta["differences"] if not d.get("resolved")]),
            required=self.required(incentive_id),
        ).as_dict()
        out["source"] = {"doc": meta["label"], "url": meta["url"]}
        out["sources"] = [x["label"] for x in meta["sources"]]
        out["disclaimer"] = (
            "Pre-verifica automatica, non consulenza: la decisione spetta a un consulente del lavoro (L. 12/1979)."
        )
        return out

    def apply_resolutions(self, res: dict[str, dict[str, Any]], version: Any = None) -> None:
        """Rebuild the merged rules from the pristine merge and apply consultants' decisions.

        ``res`` maps a difference key to the latest decision on it. Rebuilding (instead of mutating
        in place) means a reverted decision really reverts, and a decision never applies to a
        different, newer difference on the same field. ``version`` skips the work when nothing changed.
        """
        if version is not None and version == self._res_version:
            return
        merged = copy.deepcopy(self._pristine)
        for m in merged.values():
            for d in m.differences:
                r = res.get(d.get("key", ""))
                if r is None:
                    continue
                if r["choice"] == "other":
                    m.rule = m.rule.model_copy(
                        update={d["field"]: type(getattr(m.rule, d["field"])).model_validate(d["other_cited"])}
                    )
                    m.field_sources[d["field"]] = d["other_source"]
                d["resolved"] = r["id"]
                d["resolution"] = r
        self.merged = merged
        self._res_version = version

    def diffs(self, doc_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Text changes between versions, for the acts that have more than one version."""
        out: dict[int, list[dict[str, Any]]] = {}
        with kb.connect(self.db_path, read_only=True) as con:
            for doc_id in doc_ids:
                d = kb.diff_versions(con, doc_id)
                if d:
                    out[doc_id] = d
        return out

    def loads(self, limit: int = 10) -> list[dict[str, Any]]:
        """Recent dlt load packages (the pipeline run log)."""
        with kb.connect(self.db_path, read_only=True) as con:
            try:
                rows = con.execute(
                    "select load_id, status, inserted_at from raw._dlt_loads order by inserted_at desc limit ?",
                    [limit],
                ).fetchall()
            except duckdb.Error:
                return []
        return [{"load_id": a, "status": b, "inserted_at": str(c)[:19]} for a, b, c in rows]
