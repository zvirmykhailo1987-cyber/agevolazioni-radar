# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from datetime import date

import pytest
from conftest import GOOD_RULE

from radar.eligibility import HireProfile, age_on, assess
from radar.rules import IncentiveRule


def rule(**over):
    data = {k: v for k, v in GOOD_RULE.items() if k != "zes_regions"}
    for k, v in over.items():
        data[k] = {"value": v, "citation": {"anchor": "1.p1", "quote": "q"}}
    for k, v in data.items():
        if isinstance(v, dict) and v.get("value") is not None:
            v["status"] = "verified"
    return IncentiveRule.model_validate(data)


BASE = dict(
    hire_date=date(2026, 6, 1),
    worker_birth_date=date(1995, 6, 2),
    months_without_regular_job=30,
    contract_type="tempo_indeterminato",
    net_job_increase=True,
)


def outcomes(a):
    return {c.criterion: c.outcome for c in a.checks}


def test_complete_profile_is_likely_eligible_with_max_benefit():
    a = assess(rule(), HireProfile(**BASE))
    assert a.verdict == "likely_eligible", outcomes(a)
    assert a.max_benefit_eur == 500 * 24


def test_age_boundary_turning_35_on_hire_day_fails():
    a = assess(rule(), HireProfile(**{**BASE, "worker_birth_date": date(1991, 6, 1)}))
    assert outcomes(a)["worker_age"] == "fail" and a.verdict == "not_eligible" and a.max_benefit_eur is None


def test_day_before_35th_birthday_passes():
    assert age_on(date(1991, 6, 2), date(2026, 6, 1)) == 34


def test_missing_data_is_unknown_never_no():
    a = assess(rule(), HireProfile(hire_date=date(2026, 6, 1)))
    o = outcomes(a)
    assert o["worker_age"] == o["months_without_regular_job"] == o["contract_type"] == "unknown"
    assert a.verdict == "needs_review" and a.max_benefit_eur == 12000


def test_hire_outside_window_fails():
    a = assess(rule(), HireProfile(**{**BASE, "hire_date": date(2027, 1, 2)}))
    assert outcomes(a)["hire_window"] == "fail"


def test_rule_without_window_or_contracts_is_unknown():
    r = rule()
    r = r.model_copy(
        update={
            "hire_from": r.hire_from.model_copy(update={"value": None}),
            "contract_types": r.contract_types.model_copy(update={"value": None}),
        }
    )
    o = outcomes(assess(r, HireProfile(**BASE)))
    assert o["hire_window"] == "unknown" and o["contract_type"] == "unknown"


def test_zes_rule_checks_region_size_and_uses_zes_cap():
    r = rule(
        only_zes_regions=True,
        zes_regions=["Puglia", "Sicilia"],
        employer_max_employees=10,
        worker_min_age=35,
        monthly_cap_eur=650,
        monthly_cap_eur_zes=800,
        worker_gender="donna",
        requires_durc=True,
        requires_ccnl_compliance=True,
    )
    r = r.model_copy(update={"worker_max_age_exclusive": r.worker_max_age_exclusive.model_copy(update={"value": None})})
    hire = HireProfile(
        **{
            **BASE,
            "worker_birth_date": date(1980, 1, 1),
            "workplace_region": "puglia",
            "employer_employees": 8,
            "worker_gender": "F",
            "employer_has_durc": True,
            "employer_applies_ccnl": True,
        }
    )
    a = assess(r, hire)
    assert a.verdict == "likely_eligible", outcomes(a)
    assert a.max_benefit_eur == 800 * 24
    bad = assess(
        r,
        hire.model_copy(
            update={
                "workplace_region": "Lombardia",
                "employer_employees": 11,
                "worker_gender": "M",
                "employer_has_durc": False,
            }
        ),
    )
    o = outcomes(bad)
    assert o["zes_region"] == o["employer_size"] == o["worker_gender"] == o["durc"] == "fail"
    young = assess(r, hire.model_copy(update={"worker_birth_date": date(2000, 1, 1)}))
    assert outcomes(young)["worker_min_age"] == "fail"
    unknown = assess(r, HireProfile(hire_date=date(2026, 6, 1)))
    o = outcomes(unknown)
    assert {
        o[k]
        for k in (
            "zes_region",
            "employer_size",
            "worker_gender",
            "durc",
            "ccnl_compliance",
            "worker_min_age",
            "net_job_increase",
        )
    } == {"unknown"}


@pytest.mark.parametrize("months,expected", [(23, "fail"), (24, "pass")])
def test_unemployment_threshold(months, expected):
    a = assess(rule(), HireProfile(**{**BASE, "months_without_regular_job": months}))
    assert outcomes(a)["months_without_regular_job"] == expected


def test_as_dict_serializes_citations():
    d = assess(rule(), HireProfile(**BASE)).as_dict()
    assert d["checks"][0]["citation"]["anchor"]


def test_unverified_condition_and_pending_differences_fail_closed():
    r = rule()
    r = r.model_copy(update={"only_zes_regions": r.only_zes_regions.model_copy(update={"status": "unverified"})})
    a = assess(r, HireProfile(**BASE))
    assert a.verdict == "needs_review" and outcomes(a)["only_zes_regions"] == "unknown"
    b = assess(rule(), HireProfile(**BASE), pending_differences=2)
    assert b.verdict == "needs_review" and outcomes(b)["differenze_tra_atti"] == "unknown"


def test_window_with_one_bound_still_rejects_out_of_range_hires():
    r = rule()
    r = r.model_copy(update={"hire_from": r.hire_from.model_copy(update={"value": None})})
    assert outcomes(assess(r, HireProfile(**{**BASE, "hire_date": date(2027, 3, 1)})))["hire_window"] == "fail"
    assert outcomes(assess(r, HireProfile(**BASE)))["hire_window"] == "unknown"


def test_worker_conditions_lower_the_verdict_employer_ones_are_attested():
    from radar.rules import Cited, Condition

    conds = Cited[list[Condition]](
        value=[
            Condition(text="madre di almeno tre figli", applies_to="lavoratore"),
            Condition(text="nessun licenziamento nei sei mesi precedenti", applies_to="datore"),
        ],
        status="verified",
    )
    a = assess(rule().model_copy(update={"other_conditions": conds}), HireProfile(**BASE))
    assert a.verdict == "needs_review" and outcomes(a)["condizione_lavoratore"] == "unknown"
    assert a.attestations == ["nessun licenziamento nei sei mesi precedenti"]
    employer_only = Cited[list[Condition]](value=[conds.value[1]], status="verified")
    b = assess(rule().model_copy(update={"other_conditions": employer_only}), HireProfile(**BASE))
    assert b.verdict == "likely_eligible" and b.attestations


@pytest.mark.parametrize(
    "text,field,value,expected",
    [
        ("Personale non dirigenziale", "job_level", "impiegato", "pass"),
        ("Personale non dirigenziale", "job_level", "dirigente", "fail"),
        ("Lavoratore svantaggiato o molto svantaggiato", "disadvantage", "svantaggiato", "pass"),
        ("Lavoratore svantaggiato o molto svantaggiato", "disadvantage", "nessuno", "fail"),
        ("Lavoratrici molto svantaggiate", "disadvantage", "svantaggiato", "fail"),
        ("Lavoratrici molto svantaggiate", "disadvantage", "molto_svantaggiato", "pass"),
        ("Esclusi apprendistato e lavoro domestico", "contract_type", "apprendistato", "fail"),
        ("Esclusi apprendistato e lavoro domestico", "contract_type", "tempo_indeterminato", "pass"),
        (
            "Non spetta per la trasformazione a tempo indeterminato",
            "contract_type",
            "trasformazione_da_tempo_determinato",
            "fail",
        ),
        ("Non spetta per la trasformazione a tempo indeterminato", "contract_type", "tempo_indeterminato", "pass"),
    ],
)
def test_worker_conditions_settled_from_profile(text, field, value, expected):
    from radar.eligibility import resolve_condition

    hire = HireProfile(hire_date=date(2026, 6, 1), **{field: value})
    assert resolve_condition(text, hire)[0] == expected
    assert resolve_condition(text, HireProfile(hire_date=date(2026, 6, 1))) is None, "no data, no verdict"


@pytest.mark.parametrize(
    "text",
    [
        # each of these would have been a wrong PASS with keyword matching (independent review, round 2)
        "Esclusi personale dirigente, lavoro domestico, apprendistato e lavoro intermittente",
        "In deroga al requisito dei 24 mesi, non spetta se il lavoratore era occupato",
        "Spetta anche per trasformazioni; esclusi apprendistato",
        "Lavoratore non svantaggiato",
        "Personale non dirigenziale, salvo i quadri",
        "Madre di almeno tre figli",
        # two topics in one sentence: the first topic alone would say "pass" for an apprentice
        "Lavoratore svantaggiato o molto svantaggiato, esclusi apprendistato",
    ],
)
def test_ambiguous_conditions_stay_with_the_consultant(text):
    from radar.eligibility import resolve_condition

    hire = HireProfile(
        hire_date=date(2026, 6, 1),
        job_level="impiegato",
        disadvantage="molto_svantaggiato",
        contract_type="apprendistato",
    )
    assert resolve_condition(text, hire) is None


def test_required_criterion_missing_from_the_acts_is_unknown():
    r = rule()
    r = r.model_copy(update={"worker_max_age_exclusive": r.worker_max_age_exclusive.model_copy(update={"value": None})})
    a = assess(r, HireProfile(**BASE), required=("worker_max_age_exclusive",))
    assert a.verdict == "needs_review" and outcomes(a)["manca:worker_max_age_exclusive"] == "unknown"
    assert assess(r, HireProfile(**BASE)).verdict == "likely_eligible", "without the registry it would silently pass"


def test_employer_tagged_condition_is_still_checked_against_data():
    from radar.rules import Cited, Condition

    cond = Cited[list[Condition]](
        value=[Condition(text="Esclusi lavoro domestico e apprendistato", applies_to="datore")], status="verified"
    )
    a = assess(
        rule().model_copy(update={"other_conditions": cond}), HireProfile(**{**BASE, "contract_type": "apprendistato"})
    )
    assert a.verdict == "not_eligible" and a.attestations == []


# --- independent review, round 3: each case was a wrong verdict on real text -----------------
@pytest.mark.parametrize(
    "text,value,expected",
    [
        (
            "Lavoratore molto svantaggiato o svantaggiato secondo le categorie del regolamento (UE) 651/2014",
            "svantaggiato",
            "pass",
        ),
        ("giovane svantaggiato o molto svantaggiato ai sensi del regolamento (UE) 651/2014", "svantaggiato", "pass"),
        (
            "assunzione di donne svantaggiate o molto svantaggiate secondo i criteri del regolamento (UE) 651/2014",
            "molto_svantaggiato",
            "pass",
        ),
        ("Lavoratrice svantaggiata", "nessuno", "fail"),
    ],
)
def test_real_disadvantage_phrasings(text, value, expected):
    from radar.eligibility import resolve_condition

    assert resolve_condition(text, HireProfile(hire_date=date(2026, 6, 1), disadvantage=value))[0] == expected


@pytest.mark.parametrize(
    "text",
    [
        "Lavoratori svantaggiati di età superiore a 50 anni",
        "Lavoratori svantaggiati residenti in aree ZES",
        "Lavoratori svantaggiati iscritti al programma GOL",
        "Esclusi settore agricolo, lavoro domestico e apprendistato",
        "Esclusi i lavoratori con rapporto di lavoro intermittente, anche a tempo indeterminato",
    ],
)
def test_extra_clauses_stay_with_the_consultant(text):
    from radar.eligibility import resolve_condition

    hire = HireProfile(hire_date=date(2026, 6, 1), disadvantage="svantaggiato", contract_type="tempo_indeterminato")
    assert resolve_condition(text, hire) is None


@pytest.mark.parametrize(
    "text,contract,expected",
    [
        ("Esclusi i rapporti di lavoro domestico", "apprendistato", "pass"),
        (
            "Esclusi i contratti di apprendistato, di lavoro domestico e a tempo determinato",
            "tempo_determinato",
            "fail",
        ),
        (
            "Esclusi i rapporti di lavoro domestico e di apprendistato e il lavoro intermittente",
            "apprendistato",
            "fail",
        ),
        ("Esclusi personale dirigente, lavoro domestico, apprendistato e lavoro intermittente", "apprendistato", None),
        ("Qualifica di operaio, impiegato o quadro; escluso il personale dirigenziale", "x", None),
    ],
)
def test_contract_exclusions_use_the_words_in_the_text(text, contract, expected):
    from radar.eligibility import resolve_condition

    out = resolve_condition(text, HireProfile(hire_date=date(2026, 6, 1), contract_type=contract))
    assert (out[0] if out else None) == expected


def test_executive_phrase_from_the_2024_acts():
    from radar.eligibility import resolve_condition

    t = "Qualifica di operaio, impiegato o quadro; escluso il personale dirigenziale"
    assert resolve_condition(t, HireProfile(hire_date=date(2026, 6, 1), job_level="quadro"))[0] == "pass"
    assert resolve_condition(t, HireProfile(hire_date=date(2026, 6, 1), job_level="dirigente"))[0] == "fail"


def test_conditions_dropped_by_the_gate_are_not_silently_passed():
    from radar.rules import Cited, Condition

    dropped = Cited[list[Condition]](value=None, status="unverified")
    a = assess(rule().model_copy(update={"other_conditions": dropped}), HireProfile(**BASE))
    assert a.verdict == "needs_review" and outcomes(a)["other_conditions"] == "unknown"


def test_a_contested_field_cannot_exclude_the_hire():
    late = HireProfile(**{**BASE, "hire_date": date(2027, 2, 1)})
    assert assess(rule(), late).verdict == "not_eligible"
    a = assess(rule(), late, pending_differences=1, contested=frozenset({"hire_to"}))
    assert a.verdict == "needs_review" and outcomes(a)["hire_window"] == "unknown"
