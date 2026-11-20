# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Deterministic eligibility check: code decides, the model never does.

Each criterion returns ``pass``, ``fail`` or ``unknown``. ``unknown`` is used
both when the rule does not state the criterion and when the hire profile lacks
the data. Overall verdict: any ``fail`` -> not eligible; otherwise any
``unknown`` -> needs a professional's check; otherwise likely eligible. The
output is a pre-screening for a consulente del lavoro, never a decision
(Legge 12/1979 reserves that to the professional).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel

from radar.rules import CITED_FIELDS, FREE_TEXT_FIELDS, Citation, IncentiveRule

Outcome = Literal["pass", "fail", "unknown"]
Verdict = Literal["likely_eligible", "needs_review", "not_eligible"]


class HireProfile(BaseModel):
    """Synthetic hire, shaped after public payroll-API fields (employee, hiring, company)."""

    hire_date: date
    worker_birth_date: date | None = None
    worker_gender: Literal["F", "M", "X"] | None = None
    months_without_regular_job: int | None = None
    contract_type: str | None = None
    workplace_region: str | None = None
    employer_employees: int | None = None
    employer_has_durc: bool | None = None
    employer_applies_ccnl: bool | None = None
    net_job_increase: bool | None = None


@dataclass
class Check:
    criterion: str
    outcome: Outcome
    detail: str
    citation: Citation | None = None


@dataclass
class Assessment:
    incentive_id: str
    verdict: Verdict
    checks: list[Check] = field(default_factory=list)
    max_benefit_eur: float | None = None

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        for c, src in zip(d["checks"], self.checks, strict=True):
            c["citation"] = src.citation.model_dump() if src.citation else None
        return d


def _it(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def age_on(birth: date, day: date) -> int:
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def _norm_region(s: str) -> str:
    return s.strip().lower().replace("’", "'")


def assess(rule: IncentiveRule, hire: HireProfile, pending_differences: int = 0) -> Assessment:
    checks: list[Check] = []

    def add(criterion: str, outcome: Outcome, detail: str, cit: Citation | None = None) -> None:
        checks.append(Check(criterion, outcome, detail, cit))

    # Hiring window
    lo, hi = rule.hire_from, rule.hire_to
    if lo.value and hi.value:
        ok = date.fromisoformat(lo.value) <= hire.hire_date <= date.fromisoformat(hi.value)
        add("hire_window", "pass" if ok else "fail", f"dal {_it(lo.value)} al {_it(hi.value)}", lo.citation)
    else:
        add("hire_window", "unknown", "periodo non indicato")

    # Age
    max_age = rule.worker_max_age_exclusive
    if max_age.value is not None:
        if hire.worker_birth_date is None:
            add("worker_age", "unknown", "data di nascita mancante")
        else:
            a = age_on(hire.worker_birth_date, hire.hire_date)
            add(
                "worker_age",
                "pass" if a < max_age.value else "fail",
                f"età {a}, deve essere inferiore a {max_age.value}",
                max_age.citation,
            )
    min_age = rule.worker_min_age
    if min_age.value is not None:
        if hire.worker_birth_date is None:
            add("worker_min_age", "unknown", "data di nascita mancante")
        else:
            a = age_on(hire.worker_birth_date, hire.hire_date)
            add(
                "worker_min_age",
                "pass" if a >= min_age.value else "fail",
                f"età {a}, deve essere almeno {min_age.value}",
                min_age.citation,
            )

    # Gender
    g = rule.worker_gender
    if g.value == "donna":
        if hire.worker_gender is None:
            add("worker_gender", "unknown", "genere mancante")
        else:
            add("worker_gender", "pass" if hire.worker_gender == "F" else "fail", "solo lavoratrici", g.citation)

    # Unemployment / no regular job
    m = rule.min_months_without_regular_job
    if m.value is not None:
        if hire.months_without_regular_job is None:
            add("months_without_regular_job", "unknown", f"servono almeno {m.value} mesi, dato mancante")
        else:
            ok = hire.months_without_regular_job >= m.value
            add(
                "months_without_regular_job",
                "pass" if ok else "fail",
                f"{hire.months_without_regular_job} mesi, servono almeno {m.value}",
                m.citation,
            )

    # Contract type
    ct = rule.contract_types
    if ct.value:
        if hire.contract_type is None:
            add("contract_type", "unknown", "tipo di contratto mancante")
        else:
            add(
                "contract_type",
                "pass" if hire.contract_type in ct.value else "fail",
                f"{hire.contract_type}; ammessi: {', '.join(ct.value)}",
                ct.citation,
            )
    else:
        add("contract_type", "unknown", "contratti ammessi non indicati")

    # Employer size
    emp = rule.employer_max_employees
    if emp.value is not None:
        if hire.employer_employees is None:
            add("employer_size", "unknown", "numero di dipendenti mancante")
        else:
            add(
                "employer_size",
                "pass" if hire.employer_employees <= emp.value else "fail",
                f"{hire.employer_employees} dipendenti, massimo {emp.value}",
                emp.citation,
            )

    # Region (ZES)
    in_zes: bool | None = None
    zr = rule.zes_regions
    if zr.value and hire.workplace_region:
        in_zes = _norm_region(hire.workplace_region) in {_norm_region(r) for r in zr.value}
    if rule.only_zes_regions.value:
        if in_zes is None:
            add("zes_region", "unknown", "regione o elenco ZES mancante")
        else:
            add("zes_region", "pass" if in_zes else "fail", f"{hire.workplace_region}", zr.citation)

    # Employer-side conditions
    for crit, need, have in (
        ("durc", rule.requires_durc, hire.employer_has_durc),
        ("ccnl_compliance", rule.requires_ccnl_compliance, hire.employer_applies_ccnl),
        ("net_job_increase", rule.requires_net_job_increase, hire.net_job_increase),
    ):
        if need.value:
            if have is None:
                add(crit, "unknown", "dato del datore di lavoro mancante")
            else:
                add(crit, "pass" if have else "fail", "richiesto", need.citation)

    # Fail closed: a condition the model stated but the gate could not ground is not "passed".
    for name in CITED_FIELDS:
        if name not in FREE_TEXT_FIELDS and getattr(rule, name).status == "unverified":
            add(name, "unknown", "valore scartato dal controllo delle citazioni: verificare nel testo")
    if pending_differences:
        noun = "differenza" if pending_differences == 1 else "differenze"
        add("differenze_tra_atti", "unknown", f"{pending_differences} {noun} tra atti da risolvere")

    outcomes = {c.outcome for c in checks}
    verdict: Verdict = (
        "not_eligible" if "fail" in outcomes else ("needs_review" if "unknown" in outcomes else "likely_eligible")
    )

    cap = rule.monthly_cap_eur_zes.value if in_zes and rule.monthly_cap_eur_zes.value else rule.monthly_cap_eur.value
    months = rule.max_duration_months.value
    benefit = float(cap * months) if cap is not None and months is not None and verdict != "not_eligible" else None
    return Assessment(rule.incentive_id, verdict, checks, benefit)
