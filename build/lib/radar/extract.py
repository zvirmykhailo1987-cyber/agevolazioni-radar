# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""LLM extraction of incentive rules into the strict schema, then the citation gate.

The model reads anchored paragraphs and must fill the schema through a single
forced tool call (structured output). It is told to leave a field null rather
than guess. Whatever it returns goes through ``rules.verify``: values whose
quotes are not literally in the cited paragraph are dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from radar.rules import IncentiveRule, verify
from radar.text import Paragraph

PROMPT_VERSION = "extract-v6"
DEFAULT_MODEL = os.environ.get("RADAR_EXTRACT_MODEL", "claude-sonnet-5-5")

SYSTEM = """Sei un analista di normativa previdenziale italiana. Leggi una circolare o un messaggio INPS,
diviso in paragrafi con un'ancora tra parentesi quadre, ad esempio [4.p2].

Compito: per ogni incentivo all'assunzione descritto nel documento compila lo schema.
Regole ferree:
1. Ogni valore deve avere una citazione: l'ancora del paragrafo e una frase COPIATA PAROLA PER PAROLA
   da quel paragrafo (5-40 parole). Non parafrasare, non unire paragrafi.
2. Se il documento non lo dice esplicitamente, lascia value=null. Non dedurre, non usare conoscenze esterne.
3. Date in formato YYYY-MM-DD; importi in euro come numero; percentuali da 0 a 100.
4. worker_max_age_exclusive: l'eta' che il lavoratore NON deve aver compiuto (es. "non abbiano compiuto 35 anni" -> 35).
5. Se un requisito ha piu' alternative (es. "da almeno ventiquattro mesi" oppure "da almeno dodici mesi"
   per categorie particolari), riporta quello del caso generale, cioe' la prima condizione enunciata.
6. other_conditions: elenca le condizioni di ammissibilita' che gli altri campi non possono esprimere,
   ognuna con applies_to:
   - "lavoratore" se riguarda la persona assunta (es. "madre di almeno tre figli minori",
     "rapporto a termine instaurato entro il 30 aprile 2026"); non ripetere qui eta', mesi senza impiego,
     genere o tipo di contratto, che hanno gia' un campo;
   - "datore" se riguarda il datore o e' una condizione generale (es. nessun licenziamento nei sei mesi
     precedenti, regime de minimis, clausola Deggendorf, non cumulabilita').
7. Se il documento non descrive alcun incentivo, restituisci una lista vuota."""


def registry_hint() -> str:
    """Known incentive ids, so the same incentive gets the same id in every act."""
    from radar.registry import Registry

    reg = Registry.load()
    rows = [f"- {e.id}: {e.name}" for e in reg.entries.values()]
    head = (
        "INCENTIVI GIA' NOTI (usa questo incentive_id se il documento parla dello stesso incentivo;\n"
        "altrimenti proponi un id nuovo in kebab-case):\n"
    )
    return head + "\n".join(rows)


def render(paras: list[Paragraph], title: str) -> str:
    lines = [registry_hint(), "", f"DOCUMENTO: {title}", ""]
    last = None
    for p in paras:
        if p.heading != last and p.heading:
            lines.append(f"\n## {p.heading}")
            last = p.heading
        lines.append(f"[{p.anchor}] {p.text}")
    return "\n".join(lines)


def tool_schema() -> dict[str, Any]:
    schema = IncentiveRule.model_json_schema()
    # The model never sets the gate status; keep the schema smaller and honest.
    for d in schema.get("$defs", {}).values():
        d.get("properties", {}).pop("status", None)
    return {
        "name": "record_incentives",
        "description": "Record every hiring incentive described in the document.",
        "input_schema": {
            "type": "object",
            "properties": {"incentives": {"type": "array", "items": {"$ref": "#/$defs/IncentiveRule"}}},
            "required": ["incentives"],
            "$defs": {**schema.get("$defs", {}), "IncentiveRule": {k: v for k, v in schema.items() if k != "$defs"}},
        },
    }


class Extractor(Protocol):
    def __call__(self, system: str, user: str, tool: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]: ...


@dataclass
class AnthropicExtractor:
    model: str = DEFAULT_MODEL
    max_tokens: int = 16000

    def __call__(self, system: str, user: str, tool: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        from radar.llm import call_json

        return call_json(system, user, tool["input_schema"], self.model, self.max_tokens, purpose="extract")


@dataclass
class ExtractionResult:
    rules: list[IncentiveRule]
    raw: dict[str, Any]
    usage: dict[str, int]


def extract_rules(paras: list[Paragraph], title: str, llm: Extractor) -> ExtractionResult:
    raw, usage = llm(SYSTEM, render(paras, title), tool_schema())
    rules = []
    for item in raw.get("incentives", []):
        rule = IncentiveRule.model_validate(item)
        rules.append(verify(rule, paras))
    return ExtractionResult(rules, raw, usage)


def dumps(rules: list[IncentiveRule]) -> str:
    return json.dumps([r.model_dump() for r in rules], ensure_ascii=False)
