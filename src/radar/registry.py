# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Canonical incentive registry: the human-approved join key between acts.

The model proposes an ``incentive_id`` for every incentive it reads. Different acts about the
same incentive often get different slugs ("bonus-giovani-2024" vs "bonus-giovani-art22-c1").
Only aliases listed in ``registry/incentives.yaml`` are merged; anything else stays separate
and is reported as unmapped, so a wrong merge needs a person to make it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from radar.paths import HOME
from radar.rules import CITED_FIELDS

ROOT = HOME
REGISTRY_PATH = ROOT / "registry" / "incentives.yaml"
GROUPS = {"giovani", "donne", "territorio", "stabilizzazione", "lavoratori", "imprese"}
SIDES = {"datore", "lavoratore"}
DEFAULT_REQUIRED = ("hire_from", "contract_types")


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    group: str
    side: str
    aliases: tuple[str, ...]
    required: tuple[str, ...] = DEFAULT_REQUIRED


@dataclass
class Registry:
    entries: dict[str, Entry] = field(default_factory=dict)
    by_alias: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = REGISTRY_PATH) -> Registry:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        reg = cls()
        for raw in data.get("incentives", []):
            e = Entry(
                raw["id"],
                raw["name"],
                raw["group"],
                raw["side"],
                tuple(raw.get("aliases", [])),
                tuple(raw.get("required", DEFAULT_REQUIRED)),
            )
            unknown = [f for f in e.required if f not in CITED_FIELDS]
            if unknown:
                raise ValueError(f"{e.id}: unknown required fields {unknown}")
            if e.group not in GROUPS or e.side not in SIDES:
                raise ValueError(f"{e.id}: unknown group/side {e.group}/{e.side}")
            if e.id in reg.entries:
                raise ValueError(f"duplicate id {e.id}")
            reg.entries[e.id] = e
            for alias in (e.id, *e.aliases):
                if reg.by_alias.get(alias, e.id) != e.id:
                    raise ValueError(f"alias {alias} maps to two incentives")
                reg.by_alias[alias] = e.id
        return reg

    def canonical(self, model_id: str) -> str | None:
        return self.by_alias.get(model_id)

    def apply(self, rule_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Rewrite model ids to canonical ids; return (mapped rows, unmapped rows)."""
        mapped, unmapped = [], []
        for row in rule_rows:
            cid = self.canonical(row["rule"].incentive_id)
            if cid is None:
                unmapped.append(row)
                continue
            entry = self.entries[cid]
            rule = row["rule"].model_copy(update={"incentive_id": cid, "name": entry.name})
            mapped.append({**row, "rule": rule, "model_incentive_id": row["rule"].incentive_id})
        return mapped, unmapped
