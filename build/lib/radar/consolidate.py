# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Merge one incentive described by several acts into a single, traceable rule.

INPS first publishes a circular, then messages with operating details. A later act
that does not mention something has not repealed it, so "latest wins" would be
wrong. Policy:

* the act that introduced the incentive is the primary source;
* a later act fills fields the primary leaves unstated (each field keeps its act);
* when a later act states a *different* value for a structured field (number, date,
  flag, list), the difference is recorded for a human to review and NOT applied;
* free-text fields (legal basis, procedure) are not compared: wording differs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from radar.rules import CITED_FIELDS, IncentiveRule, coverage

FREE_TEXT = {"legal_basis", "application_procedure"}
UNION = {"other_conditions"}  # conditions from every act apply: union them, never "differ"


@dataclass
class Merged:
    rule: IncentiveRule
    primary: dict[str, Any]  # the act that introduced the incentive
    sources: list[dict[str, Any]] = field(default_factory=list)
    field_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    differences: list[dict[str, Any]] = field(default_factory=list)

    @property
    def coverage(self) -> dict[str, int]:
        return coverage(self.rule)


def it_date(iso: str) -> str:
    y, m, d = str(iso)[:10].split("-")
    return f"{d}/{m}/{y}"


def source_of(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_id": row.get("doc_id"),
        "doc_type": row["doc_type"],
        "number": row["number"],
        "published_on": row["published_on"],
        "url": row["url"],
        "label": f"{row['doc_type']} n. {row['number']} del {it_date(row['published_on'])}",
        "short": f"{row['doc_type'][:4]}. {row['number']}",
    }


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return {str(x).lower() for x in a} == {str(x).lower() for x in b}
    return bool(a == b)


def order_key(row: dict[str, Any]) -> tuple[str, int, int]:
    """Date, then circular before message (a circular introduces, a message operates), then number."""
    num = int(row["number"]) if str(row["number"]).isdigit() else 0
    return (str(row["published_on"]), 0 if str(row["doc_type"]).startswith("Circ") else 1, num)


def difference_key(incentive_id: str, field_name: str, other_value: Any, other_label: str) -> str:
    """Identity of one difference: a decision applies to this exact pair of values, not to the field forever."""
    raw = json.dumps([incentive_id, field_name, other_value, other_label], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def consolidate(rule_rows: list[dict[str, Any]]) -> dict[str, Merged]:
    """``rule_rows`` as returned by ``kb.rules``."""
    out: dict[str, Merged] = {}
    for row in sorted(rule_rows, key=order_key):
        rule: IncentiveRule = row["rule"]
        src = source_of(row)
        m = out.get(rule.incentive_id)
        if m is None:
            m = out[rule.incentive_id] = Merged(rule=rule.model_copy(deep=True), primary=src, sources=[src])
            m.field_sources = {f: src for f in CITED_FIELDS if getattr(rule, f).status == "verified"}
            continue
        m.sources.append(src)
        fills = {}
        for f in CITED_FIELDS:
            new = getattr(rule, f)
            if new.status != "verified":
                continue
            old = getattr(m.rule, f)
            if f in UNION and old.status == "verified":
                seen = {c.text.strip().lower() for c in old.value or []}
                extra = [c for c in new.value or [] if c.text.strip().lower() not in seen]
                if extra:
                    fills[f] = old.model_copy(update={"value": [*(old.value or []), *extra]})
                continue
            if old.status != "verified":
                fills[f] = new.model_copy(deep=True)
                m.field_sources[f] = src
            elif f not in FREE_TEXT and not _same(old.value, new.value):
                m.differences.append(
                    {
                        "field": f,
                        "kept": old.value,
                        "kept_in": m.field_sources[f]["label"],
                        "other": new.value,
                        "other_in": src["label"],
                        "other_citation": new.citation.model_dump() if new.citation else None,
                        "other_cited": new.model_dump(),
                        "key": difference_key(rule.incentive_id, f, new.value, src["label"]),
                        "other_source": src,
                    }
                )
        m.rule = m.rule.model_copy(update=fills)
    return out
