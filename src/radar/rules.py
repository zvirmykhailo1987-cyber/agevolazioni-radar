# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""The incentive rule schema and the citation gate.

A rule is the machine-readable form of one incentive described in a circular.
Every field carries the value *and* the paragraph anchor + verbatim quote it
was read from. ``verify`` keeps a value only when its quote is literally found
in the cited paragraph; otherwise the value becomes ``None`` ("not stated"),
which downstream code treats as *unknown*, never as *no*.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

from radar.text import Paragraph, canon, quote_found

T = TypeVar("T")


class Citation(BaseModel):
    anchor: str = Field(description="Paragraph anchor such as '4.p2'")
    quote: str = Field(description="Exact words copied from that paragraph, 5-40 words", max_length=400)


class Cited[T](BaseModel):
    value: T | None = Field(default=None, description="null when the text does not state it")
    citation: Citation | None = None
    status: Literal["verified", "unverified", "absent"] = "absent"


ContractType = Literal[
    "tempo_indeterminato",
    "trasformazione_da_tempo_determinato",
    "apprendistato",
    "tempo_determinato",
    "somministrazione",
]
Gender = Literal["donna", "qualsiasi"]


class IncentiveRule(BaseModel):
    """One incentive as stated by one document version."""

    incentive_id: str = Field(description="short slug, e.g. 'bonus-giovani-2026'")
    name: str
    legal_basis: Cited[str] = Field(default_factory=Cited)
    exemption_percent: Cited[float] = Field(
        default_factory=Cited, description="share of employer contributions waived, 0-100"
    )
    monthly_cap_eur: Cited[float] = Field(default_factory=Cited)
    monthly_cap_eur_zes: Cited[float] = Field(
        default_factory=Cited, description="higher cap for hires in a ZES region, if any"
    )
    max_duration_months: Cited[int] = Field(default_factory=Cited)
    hire_from: Cited[str] = Field(default_factory=Cited, description="first eligible hiring date, YYYY-MM-DD")
    hire_to: Cited[str] = Field(default_factory=Cited, description="last eligible hiring date, YYYY-MM-DD")
    worker_max_age_exclusive: Cited[int] = Field(
        default_factory=Cited, description="worker must NOT have reached this age at hiring"
    )
    worker_min_age: Cited[int] = Field(default_factory=Cited)
    worker_gender: Cited[Gender] = Field(default_factory=Cited)
    min_months_without_regular_job: Cited[int] = Field(default_factory=Cited)
    contract_types: Cited[list[ContractType]] = Field(default_factory=Cited)
    employer_max_employees: Cited[int] = Field(default_factory=Cited)
    only_zes_regions: Cited[bool] = Field(default_factory=Cited, description="true if the hire must be in a ZES region")
    zes_regions: Cited[list[str]] = Field(
        default_factory=Cited, description="regions named as ZES in the text, Italian names"
    )
    requires_net_job_increase: Cited[bool] = Field(default_factory=Cited)
    requires_durc: Cited[bool] = Field(default_factory=Cited)
    requires_ccnl_compliance: Cited[bool] = Field(default_factory=Cited)
    application_procedure: Cited[str] = Field(default_factory=Cited)


CITED_FIELDS = [
    name
    for name, f in IncentiveRule.model_fields.items()
    if f.annotation is not None and getattr(f.annotation, "__pydantic_generic_metadata__", {}).get("origin") is Cited
]


NUMBER_WORDS = {
    "tre": 3,
    "sei": 6,
    "dieci": 10,
    "dodici": 12,
    "diciotto": 18,
    "ventiquattro": 24,
    "trenta": 30,
    "trentacinque": 35,
    "trentacinquesimo": 35,
    "trentasei": 36,
    "trentaseiesimo": 36,
    "cinquanta": 50,
    "sessanta": 60,
    "cento": 100,
}
MONTHS = [
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
]
FREE_TEXT_FIELDS = {"legal_basis", "application_procedure"}


def value_in_quote(name: str, value: Any, quote: str) -> bool:
    """Does the quote actually state this value? Existence of the quote is not enough.

    Numbers must appear as a whole token (digits, Italian thousands grouping, or a number
    word); dates need day, month name and year; list items must each be named; booleans
    and free text are judged by the quote alone (the gate already requires 4+ words).
    """
    words = canon(quote).split()
    text = f" {' '.join(words)} "
    if isinstance(value, bool) or name in FREE_TEXT_FIELDS:
        return True
    if isinstance(value, int | float):
        n = int(value) if float(value).is_integer() else value
        variants = {str(n), f"{n:,}".replace(",", " ")}
        return any(f" {v} " in text for v in variants) or any(NUMBER_WORDS.get(w) == n for w in words)
    if name in ("hire_from", "hire_to") and isinstance(value, str):
        y, m, d = value.split("-")
        return f" {int(d)} {MONTHS[int(m) - 1]} {y} " in text
    if name == "worker_gender":
        return value == "qualsiasi" or any(w.startswith(("donn", "lavoratric")) for w in words)
    if isinstance(value, list):
        return all(canon(str(v).replace("_", " ")).split()[0][:8] in text for v in value)
    return True


def verify(rule: IncentiveRule, paras: list[Paragraph]) -> IncentiveRule:
    """Apply the citation gate in place-safe fashion and return a new rule."""
    by_anchor = {p.anchor: p.text for p in paras}
    data: dict[str, Any] = rule.model_dump()
    for name in CITED_FIELDS:
        field = data[name]
        cit = field.get("citation")
        if field.get("value") is None:
            field.update(status="absent", citation=None)
        elif (
            cit
            and quote_found(cit["quote"], by_anchor.get(cit["anchor"], ""))
            and value_in_quote(name, field["value"], cit["quote"])
        ):
            field["status"] = "verified"
        else:
            # Model claimed a value it could not ground: drop the value, keep the trace.
            field.update(status="unverified", value=None)
    return IncentiveRule.model_validate(data)


def coverage(rule: IncentiveRule) -> dict[str, int]:
    counts = {"verified": 0, "unverified": 0, "absent": 0}
    for name in CITED_FIELDS:
        counts[getattr(rule, name).status] += 1
    return counts
