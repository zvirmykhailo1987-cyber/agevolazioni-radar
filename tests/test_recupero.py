# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
import json

import pytest

from radar import evals, recupero
from radar.recupero import Workforce, example_companies, load_workforce, scan
from radar.store import Store

WF = {
    "company": {"id": "c1", "name": "Test Srl", "employees": 5, "has_durc": True, "applies_ccnl": True},
    "hires": [
        {
            "id": "A",
            "full_name": "A",
            "birth_date": "1995-06-02",
            "gender": "M",
            "hire_date": "2026-06-01",
            "contract_type": "tempo_indeterminato",
            "workplace_region": "Puglia",
            "months_without_regular_job": 30,
            "gross_monthly_eur": 2000,
            "net_job_increase": True,
        },
        {
            "id": "B",
            "full_name": "B",
            "birth_date": "1995-06-02",
            "gender": "M",
            "hire_date": "2026-06-01",
            "contract_type": "apprendistato",
            "months_without_regular_job": 30,
            "gross_monthly_eur": 2000,
        },
        {"id": "C", "full_name": "C", "hire_date": "2026-06-01"},
    ],
}


@pytest.fixture()
def store(extracted_db):
    return Store(extracted_db)


def test_scan_prices_with_cap_and_flags_proposal(store):
    rep = scan(store, Workforce.model_validate(WF), employer_rate=0.30)
    by = {(f.hire_id, f.incentive_id): f for f in rep.findings}
    a = by[("A", "bonus-giovani-2026")]
    # the fixture's zes_regions quote is invented, so it is dropped: not "likely", and the ZES cap is not used
    assert a.verdict == "needs_review" and a.proposed
    assert a.monthly_saving_eur == 500.0 and a.months == 24 and a.total_saving_eur == 12000.0
    assert ("B", "bonus-giovani-2026") not in by, "apprendistato is excluded"
    c = by[("C", "bonus-giovani-2026")]
    assert c.monthly_saving_eur is None and "worker_age" in c.missing
    t = rep.totals()
    assert t["to_check_eur"] == 12000.0 and t["hires_with_proposal"] == 2
    assert rep.as_dict()["incentives_checked"] == 1
    # a proposal the consultant rejected leaves the money totals
    after = rep.totals(frozenset({a.finding_id}))
    assert after["to_check_eur"] == 0 and after["rejected"] == 1 and after["by_incentive"] == {}
    assert after["hires_with_proposal"] == t["hires_with_proposal"] - 1


def test_lower_salary_is_below_the_cap(store):
    wf = json.loads(json.dumps(WF))
    wf["hires"][0]["gross_monthly_eur"] = 1000
    a = next(f for f in scan(store, Workforce.model_validate(wf)).findings if f.hire_id == "A")
    assert a.monthly_saving_eur == 300.0


@pytest.mark.parametrize("rate", [0, -0.1, 1.5])
def test_employer_rate_must_be_a_share(store, rate):
    with pytest.raises(ValueError):
        scan(store, Workforce.model_validate(WF), employer_rate=rate)


def test_finding_ids_are_stable(store):
    one = [f.finding_id for f in scan(store, Workforce.model_validate(WF)).findings]
    two = [f.finding_id for f in scan(store, Workforce.model_validate(WF)).findings]
    assert one == two and len(set(one)) == len(one)


def test_shipped_example_companies_are_valid():
    files = example_companies()
    assert {"trattoria-puglia", "studio-milano"} <= set(files)
    for p in files.values():
        assert load_workforce(p).hires


def test_agent_eval_scoring(store, tmp_path, monkeypatch):
    d = tmp_path / "companies"
    d.mkdir()
    (d / "c1.json").write_text(json.dumps(WF), encoding="utf-8")
    monkeypatch.setattr(recupero, "COMPANIES_DIR", d)
    gold = tmp_path / "g.json"
    gold.write_text(
        json.dumps({"companies": {"c1": {"A": {"bonus-giovani-2026": "likely"}, "B": {"x": "review"}}}}),
        encoding="utf-8",
    )
    out = evals.score_recupero(store, gold)
    assert out["recall"] == 0.5 and out["precision"] == 0.5 and out["verdict_accuracy"] == 0.0
    assert {i["result"] for i in out["issues"]} >= {"missed", "verdict needs_review", "extra (needs_review)"}


def test_incentive_without_a_stated_hiring_window_is_never_proposed(store):
    m = store.merged["bonus-giovani-2026"]
    none = m.rule.hire_to.model_copy(update={"value": None})
    m.rule = m.rule.model_copy(update={"hire_to": none, "hire_from": none})
    rep = scan(store, Workforce.model_validate(WF))
    assert rep.findings == [] and rep.not_assessable == ["Bonus Giovani 2026"]


def test_open_ended_window_is_proposed_with_the_end_date_to_check(store):
    m = store.merged["bonus-giovani-2026"]
    m.rule = m.rule.model_copy(update={"hire_to": m.rule.hire_to.model_copy(update={"value": None})})
    a = next(f for f in scan(store, Workforce.model_validate(WF)).findings if f.hire_id == "A")
    assert a.verdict == "needs_review" and "hire_window_end" in a.missing
