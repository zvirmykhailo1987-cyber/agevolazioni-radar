# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Recupero: scan a company's hires against every incentive and estimate the saving.

Deterministic by design. For every hire x incentive the eligibility engine decides
(pass / fail / unknown per criterion); this module only orders, prices and groups the
outcomes. The saving is an estimate:

    monthly = min(gross monthly salary x employer contribution rate x exemption %, monthly cap)
    total   = monthly x maximum duration

with the employer contribution rate as an explicit, editable assumption: INPS circulars
state caps and percentages, not the company's actual contribution base.

Incentives are treated as not cumulable for the same hire: the highest estimate is the
proposal, the others are listed as alternatives for the consultant to weigh.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from radar.eligibility import Assessment, HireProfile, assess
from radar.paths import HOME
from radar.store import Store, pending

ROOT = HOME
COMPANIES_DIR = ROOT / "examples" / "companies"
DEFAULT_EMPLOYER_RATE = 0.30  # assumption shown in the UI; editable per run


class Company(BaseModel):
    """Shape modelled on public payroll-API objects (company, employees, hirings). Synthetic data only."""

    id: str
    name: str
    ccnl: str | None = None
    employees: int | None = None
    has_durc: bool | None = None
    applies_ccnl: bool | None = None


class Hire(BaseModel):
    id: str
    full_name: str
    role: str | None = None
    birth_date: date | None = None
    gender: Literal["F", "M", "X"] | None = None
    hire_date: date
    contract_type: str | None = None
    workplace_region: str | None = None
    months_without_regular_job: int | None = None
    gross_monthly_eur: float | None = Field(default=None, ge=0)
    net_job_increase: bool | None = None
    job_level: Literal["operaio", "impiegato", "quadro", "dirigente"] | None = None
    disadvantage: Literal["nessuno", "svantaggiato", "molto_svantaggiato"] | None = None


class Workforce(BaseModel):
    company: Company
    hires: list[Hire]


@dataclass
class Finding:
    finding_id: str
    hire_id: str
    hire_name: str
    incentive_id: str
    incentive_name: str
    verdict: str
    monthly_saving_eur: float | None
    months: int | None
    total_saving_eur: float | None
    proposed: bool
    missing: list[str]
    assessment: dict[str, Any]
    source: dict[str, Any]
    rule_fingerprint: str = ""


@dataclass
class Report:
    company: dict[str, Any]
    employer_rate: float
    findings: list[Finding] = field(default_factory=list)
    hires_scanned: int = 0
    incentives_checked: int = 0
    # incentives whose hiring window is not stated in full: never proposed automatically
    not_assessable: list[str] = field(default_factory=list)

    @property
    def proposed(self) -> list[Finding]:
        return [f for f in self.findings if f.proposed]

    def effective(self, rejected: frozenset[str] = frozenset()) -> list[Finding]:
        """Per hire, the best finding the consultant has not rejected."""
        best: dict[str, Finding] = {}
        for f in self.findings:  # best first within each hire
            if f.finding_id not in rejected and f.hire_id not in best:
                best[f.hire_id] = f
        return list(best.values())

    def totals(self, rejected: frozenset[str] = frozenset()) -> dict[str, Any]:
        """Sums over proposals; findings a consultant rejected are left out.

        When the proposal for a hire is rejected, the next alternative for that hire (findings are
        stored best first) takes its place: rejecting one incentive does not mean the hire is worth 0.
        """
        kept = self.effective(rejected)
        sure = [f for f in kept if f.verdict == "likely_eligible" and f.total_saving_eur]
        review = [f for f in kept if f.verdict == "needs_review" and f.total_saving_eur]
        by_inc: dict[str, float] = {}
        for f in kept:
            if f.total_saving_eur:
                by_inc[f.incentive_name] = by_inc.get(f.incentive_name, 0.0) + f.total_saving_eur
        return {
            "likely_eur": round(sum(f.total_saving_eur or 0 for f in sure), 2),
            "to_check_eur": round(sum(f.total_saving_eur or 0 for f in review), 2),
            "hires_with_proposal": len({f.hire_id for f in kept}),
            "rejected": len(rejected & {f.finding_id for f in self.findings}),
            "by_incentive": dict(sorted(by_inc.items(), key=lambda kv: -kv[1])),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "employer_rate": self.employer_rate,
            "hires_scanned": self.hires_scanned,
            "incentives_checked": self.incentives_checked,
            "not_assessable": self.not_assessable,
            "totals": self.totals(),
            "findings": [asdict(f) for f in self.findings],
        }


def load_workforce(path: Path) -> Workforce:
    return Workforce.model_validate(json.loads(path.read_text(encoding="utf-8")))


def example_companies() -> dict[str, Path]:
    return {p.stem: p for p in sorted(COMPANIES_DIR.glob("*.json"))}


def profile_of(company: Company, hire: Hire) -> HireProfile:
    return HireProfile(
        hire_date=hire.hire_date,
        worker_birth_date=hire.birth_date,
        worker_gender=hire.gender,
        months_without_regular_job=hire.months_without_regular_job,
        contract_type=hire.contract_type,
        workplace_region=hire.workplace_region,
        employer_employees=company.employees,
        employer_has_durc=company.has_durc,
        employer_applies_ccnl=company.applies_ccnl,
        net_job_increase=hire.net_job_increase,
        job_level=hire.job_level,
        disadvantage=hire.disadvantage,
    )


def price(
    a: Assessment, gross: float | None, rate: float, exemption_pct: float | None
) -> tuple[float | None, int | None]:
    """Monthly saving and months; the monthly figure is None when the cap, salary or percentage is unknown."""
    if a.monthly_cap is None or gross is None or exemption_pct is None:
        return None, a.months
    return round(min(gross * rate * exemption_pct / 100.0, a.monthly_cap), 2), a.months


def finding_id(company_id: str, hire_id: str, incentive_id: str) -> str:
    """Stable across rule updates, so a consultant's decision survives a refresh; the rule version
    the decision was taken on is kept separately (``rule_fingerprint``) and shown when it changed."""
    raw = f"{company_id}|{hire_id}|{incentive_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def scan(
    store: Store, workforce: Workforce, employer_rate: float = DEFAULT_EMPLOYER_RATE, side: str = "datore"
) -> Report:
    if not 0 < employer_rate <= 1:
        raise ValueError("employer_rate must be in (0, 1]")
    report = Report(
        company=workforce.company.model_dump(mode="json"),
        employer_rate=employer_rate,
        hires_scanned=len(workforce.hires),
    )
    ids = [i for i, e in store.registry.entries.items() if e.side == side and i in store.merged]
    report.incentives_checked = len(ids)
    report.not_assessable = sorted(store.merged[i].rule.name for i in ids if not store.merged[i].rule.hire_from.value)
    for hire in workforce.hires:
        profile = profile_of(workforce.company, hire)
        candidates: list[Finding] = []
        for iid in ids:
            m = store.merged[iid]
            open_diffs = pending(m)
            a = assess(
                m.rule,
                profile,
                pending_differences=len(open_diffs),
                required=store.required(iid),
                contested=frozenset(d["field"] for d in open_diffs),
            )
            window = next((c.outcome for c in a.checks if c.criterion == "hire_window"), "unknown")
            if a.verdict == "not_eligible" or window != "pass":
                continue
            monthly, months = price(a, hire.gross_monthly_eur, employer_rate, m.rule.exemption_percent.value)
            total = round(monthly * months, 2) if monthly is not None and months is not None else None
            fingerprint = hashlib.sha256(m.rule.model_dump_json().encode()).hexdigest()[:12]
            candidates.append(
                Finding(
                    finding_id=finding_id(workforce.company.id, hire.id, iid),
                    hire_id=hire.id,
                    hire_name=hire.full_name,
                    incentive_id=iid,
                    incentive_name=m.rule.name,
                    verdict=a.verdict,
                    monthly_saving_eur=monthly,
                    months=months,
                    total_saving_eur=total,
                    proposed=False,
                    missing=[c.criterion for c in a.checks if c.outcome == "unknown"],
                    assessment=a.as_dict(),
                    source={"label": m.primary["label"], "url": m.primary["url"]},
                    rule_fingerprint=fingerprint,
                )
            )
        # likely before needs_review, then highest estimate: that one is the proposal
        candidates.sort(key=lambda f: (f.verdict != "likely_eligible", -(f.total_saving_eur or 0)))
        if candidates:
            candidates[0].proposed = True
        report.findings.extend(candidates)
    return report
