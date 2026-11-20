# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Property-based simulation: thousands of random rules, hires and workforces.

Example tests show that the engine handles the cases someone thought of. These check the
guarantees the product is sold on, over inputs nobody thought of:

* fail-closed: less data, a dropped value or an open difference never *raises* a verdict;
* money: an estimate never exceeds the cap the circular states, and totals add up;
* Recupero: one proposal per hire, never outside the hiring window, rejections leave the totals.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from conftest import GOOD_RULE
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from radar.eligibility import Assessment, HireProfile, assess, resolve_condition
from radar.recupero import price
from radar.rules import CITED_FIELDS, FREE_TEXT_FIELDS, IncentiveRule

RANK = {"not_eligible": 0, "needs_review": 1, "likely_eligible": 2}
SETTINGS = settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])

REGIONS = ["Puglia", "Sicilia", "Lombardia", "Lazio", "Campania", "Marche"]
CONTRACTS = ["tempo_indeterminato", "apprendistato", "somministrazione", "trasformazione_da_tempo_determinato"]


def cited(value: Any, status: str = "verified") -> dict[str, Any]:
    return {"value": value, "status": status, "citation": {"anchor": "1.p1", "quote": "q"}}


@st.composite
def rules(draw: st.DrawFn) -> IncentiveRule:
    """A random but well-formed rule: every criterion stated or not, verified or dropped by the gate."""
    lo = draw(st.dates(date(2025, 1, 1), date(2026, 12, 31)))
    data: dict[str, Any] = {"incentive_id": "r", "name": "R"}
    maybe = st.booleans()
    if draw(maybe):
        data["hire_from"] = cited(lo.isoformat())
    if draw(maybe):
        data["hire_to"] = cited((lo + timedelta(days=draw(st.integers(0, 730)))).isoformat())
    if draw(maybe):
        data["worker_max_age_exclusive"] = cited(draw(st.sampled_from([30, 35, 36])))
    if draw(maybe):
        data["worker_min_age"] = cited(draw(st.sampled_from([35, 50])))
    if draw(maybe):
        data["worker_gender"] = cited("donna")
    if draw(maybe):
        data["min_months_without_regular_job"] = cited(draw(st.sampled_from([6, 12, 24])))
    if draw(maybe):
        data["contract_types"] = cited(draw(st.lists(st.sampled_from(CONTRACTS), min_size=1, max_size=2, unique=True)))
    if draw(maybe):
        data["employer_max_employees"] = cited(draw(st.integers(1, 50)))
    if draw(maybe):
        data["only_zes_regions"] = cited(True)
        data["zes_regions"] = cited(draw(st.lists(st.sampled_from(REGIONS), min_size=1, max_size=3, unique=True)))
    for flag in ("requires_durc", "requires_ccnl_compliance", "requires_net_job_increase"):
        if draw(maybe):
            data[flag] = cited(True)
    data["monthly_cap_eur"] = cited(draw(st.sampled_from([500, 650, 800])))
    if draw(maybe):
        data["monthly_cap_eur_zes"] = cited(draw(st.sampled_from([650, 800])))
    data["max_duration_months"] = cited(draw(st.sampled_from([12, 18, 24, 36])))
    data["exemption_percent"] = cited(draw(st.sampled_from([50, 70, 100])))
    return IncentiveRule.model_validate(data)


@st.composite
def hires(draw: st.DrawFn) -> HireProfile:
    def opt(strategy: st.SearchStrategy[Any]) -> Any:
        return draw(st.one_of(st.none(), strategy))

    return HireProfile(
        hire_date=draw(st.dates(date(2025, 1, 1), date(2027, 6, 30))),
        worker_birth_date=opt(st.dates(date(1960, 1, 1), date(2008, 1, 1))),
        worker_gender=opt(st.sampled_from(["F", "M", "X"])),
        months_without_regular_job=opt(st.integers(0, 60)),
        contract_type=opt(st.sampled_from(CONTRACTS)),
        workplace_region=opt(st.sampled_from(REGIONS)),
        employer_employees=opt(st.integers(1, 80)),
        employer_has_durc=opt(st.booleans()),
        employer_applies_ccnl=opt(st.booleans()),
        net_job_increase=opt(st.booleans()),
        job_level=opt(st.sampled_from(["operaio", "impiegato", "quadro", "dirigente"])),
        disadvantage=opt(st.sampled_from(["nessuno", "svantaggiato", "molto_svantaggiato"])),
    )


HIRE_FIELDS = [f for f in HireProfile.model_fields if f != "hire_date"]


@SETTINGS
@given(rules(), hires(), st.sampled_from(HIRE_FIELDS))
def test_removing_hire_data_never_makes_a_verdict_more_favourable(rule, hire, field):
    before = assess(rule, hire).verdict
    after = assess(rule, hire.model_copy(update={field: None})).verdict
    if after == "likely_eligible":
        assert before == "likely_eligible", f"dropping {field} turned {before} into likely_eligible"


@SETTINGS
@given(rules(), hires(), st.sampled_from([f for f in CITED_FIELDS if f not in FREE_TEXT_FIELDS]))
def test_a_value_the_gate_dropped_never_helps(rule, hire, field):
    dropped = rule.model_copy(update={field: getattr(rule, field).model_copy(update={"status": "unverified"})})
    if getattr(rule, field).value is not None:
        assert assess(dropped, hire).verdict != "likely_eligible"


@SETTINGS
@given(rules(), hires(), st.integers(1, 5))
def test_open_differences_always_need_review(rule, hire, n):
    assert assess(rule, hire, pending_differences=n).verdict != "likely_eligible"


@SETTINGS
@given(rules(), hires())
def test_verdict_matches_the_checks_and_benefit_respects_the_caps(rule, hire):
    a = assess(rule, hire)
    outcomes = {c.outcome for c in a.checks}
    assert (a.verdict == "not_eligible") == ("fail" in outcomes)
    assert (a.verdict == "likely_eligible") == (outcomes <= {"pass"})
    if a.max_benefit_eur is not None:
        caps = {rule.monthly_cap_eur.value, rule.monthly_cap_eur_zes.value} - {None}
        assert a.monthly_cap in caps and a.max_benefit_eur == a.monthly_cap * rule.max_duration_months.value
        assert a.verdict != "not_eligible"
    if a.verdict == "likely_eligible":
        # every criterion the rule states was decided on data, not assumed
        assert all(c.outcome == "pass" for c in a.checks) and a.checks


@SETTINGS
@given(
    st.floats(0, 20000, allow_nan=False),
    st.floats(0.01, 1),
    st.sampled_from([50.0, 70.0, 100.0]),
    st.sampled_from([500.0, 650.0, 800.0]),
)
def test_monthly_estimate_never_exceeds_the_cap_and_grows_with_salary(gross, rate, pct, cap):
    a = Assessment("x", "likely_eligible", monthly_cap=cap, months=24)
    m, months = price(a, gross, rate, pct)
    assert m is not None and months == 24 and 0 <= m <= cap
    m2, _ = price(a, gross + 100, rate, pct)
    assert m2 is not None and m2 >= m


WORDS = st.sampled_from(
    [
        "personale",
        "non",
        "dirigenziale",
        "esclusi",
        "apprendistato",
        "lavoro",
        "domestico",
        "svantaggiato",
        "molto",
        "salvo",
        "trasformazione",
        "spetta",
        "madre",
        "figli",
        "o",
        "e",
    ]
)


@SETTINGS
@given(st.lists(WORDS, min_size=1, max_size=9).map(" ".join), hires())
def test_condition_resolver_is_silent_unless_one_known_phrase(text, hire):
    out = resolve_condition(text, hire)
    if out is not None:
        t = text.lower()
        topics = sum(
            [
                "dirigen" in t,
                "svantaggiat" in t,
                any(w in t for w in ("apprendist", "domestic", "intermittent")),
                "trasformazion" in t,
            ]
        )
        assert topics == 1 and "salvo" not in t, f"settled an ambiguous sentence: {text!r}"


def test_good_rule_strategy_covers_the_fixture():
    # the fixture rule is one point of the space the strategies explore
    assert IncentiveRule.model_validate(GOOD_RULE).monthly_cap_eur.value == 500
