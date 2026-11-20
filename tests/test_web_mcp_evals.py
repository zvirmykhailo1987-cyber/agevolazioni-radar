# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
import asyncio
import json

import pytest
from conftest import FakeLLM
from fastapi.testclient import TestClient

from radar import evals
from radar.rag import Answer
from radar.store import Store
from radar.web import RateLimiter, create_app

GOOD_ANSWER = {
    "answerable": True,
    "answer": "Il tetto è 500 euro al mese.",
    "citations": [{"passage_id": "D15258 1.p13", "quote": "500 euro su base mensile per ciascun lavoratore"}],
}


@pytest.fixture()
def client(extracted_db):
    return TestClient(create_app(Store(extracted_db), llm=FakeLLM(GOOD_ANSWER)))


def test_pages_render_and_carry_security_headers(client):
    for url in ("/", "/verifica", "/chiedi", "/incentivo/bonus-giovani-2026"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
    assert "Bonus Giovani 2026" in client.get("/").text
    page = client.get("/incentivo/bonus-giovani-2026").text
    assert "verificato" in page and "scartato" in page and "§1.p13" in page
    assert client.get("/incentivo/nope").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/health").json()["ok"] is True


def test_precheck_form(client):
    r = client.post(
        "/verifica",
        data={
            "incentive_id": "bonus-giovani-2026",
            "hire_date": "2026-06-01",
            "worker_birth_date": "1995-06-02",
            "months_without_regular_job": "30",
            "contract_type": "tempo_indeterminato",
            "net_job_increase": "si",
        },
    )
    # zes_regions in the fixture carries an invented quote: the gate drops it, so the
    # pre-check fails closed ("to be checked") instead of treating the condition as met.
    assert r.status_code == 200 and "Da verificare" in r.text and "12.000 €" in r.text
    assert "scartato dal controllo" in r.text
    r = client.post("/verifica", data={"incentive_id": "bonus-giovani-2026", "hire_date": "2026-06-01"})
    assert "Da verificare" in r.text
    assert client.post("/verifica", data={"incentive_id": "x", "hire_date": "2026-06-01"}).status_code == 404


def test_ask_shows_verified_citation(client):
    r = client.post("/chiedi", data={"q": "Limite massimo di importo in euro su base mensile per ciascun lavoratore?"})
    assert "500 euro al mese" in r.text and "§1.p13" in r.text


def test_ask_disabled_without_key(extracted_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(create_app(Store(extracted_db)))
    assert "disattivate" in c.post("/chiedi", data={"q": "Qual è il tetto?"}).text


def test_api_endpoints(client):
    assert client.get("/api/incentivi").json()[0]["incentive_id"] == "bonus-giovani-2026"
    assert client.get("/api/incentivi/bonus-giovani-2026").json()["monthly_cap_eur"]["value"] == 500
    assert client.get("/api/incentivi/x").status_code == 404
    r = client.post("/api/verifica/bonus-giovani-2026", json={"hire_date": "2026-06-01"})
    assert r.json()["verdict"] == "needs_review"
    assert client.post("/api/verifica/x", json={"hire_date": "2026-06-01"}).status_code == 404
    assert client.get("/api/stato").json()["freshness"]["documents_in_kb"] == 1


def test_rate_limiter_per_ip_and_daily():
    rl = RateLimiter(per_ip=2, window_s=60, daily=3)
    assert rl.allow("a", 0) is None and rl.allow("a", 1) is None
    assert "Troppe" in rl.allow("a", 2)
    assert rl.allow("a", 100) is None
    assert "giornaliero" in rl.allow("b", 101)


def test_ask_rate_limited(extracted_db, monkeypatch):
    monkeypatch.setenv("RADAR_QA_PER_IP", "1")
    c = TestClient(create_app(Store(extracted_db), llm=FakeLLM(GOOD_ANSWER)))
    c.post("/chiedi", data={"q": "Qual è il tetto?"})
    assert "Troppe domande" in c.post("/chiedi", data={"q": "Qual è il tetto?"}).text


def test_mcp_tools(extracted_db, monkeypatch):
    from radar import mcp_server

    monkeypatch.setattr(mcp_server, "store", lambda: Store(extracted_db))

    async def go():
        names = {t.name for t in await mcp_server.server.list_tools()}
        assert names == {
            "search_passages",
            "list_incentives",
            "get_incentive",
            "check_eligibility",
            "diff_versions",
            "kb_status",
        }
        r = await mcp_server.server.call_tool("get_incentive", {"incentive_id": "bonus-giovani-2026"})
        assert "1.p13" in str(r)
        r = await mcp_server.server.call_tool("get_incentive", {"incentive_id": "zzz"})
        assert "unknown incentive_id" in str(r)
        r = await mcp_server.server.call_tool(
            "check_eligibility", {"incentive_id": "bonus-giovani-2026", "hire": {"hire_date": "2026-06-01"}}
        )
        assert "needs_review" in str(r)
        r = await mcp_server.server.call_tool("diff_versions", {"doc_id": 15258})
        assert r.is_error is False

    asyncio.run(go())


def test_eval_scoring():
    item = {"id": "g", "q": "q", "must_include": ["500", "mese|mensile"], "gold": [["55", "1.p13"]]}
    good = Answer("q", "500 euro al mese", True, [{"doc_label": "Circolare n. 55 del 14/05/2026", "anchor": "1.p13"}])
    s = evals.score_item(item, good)
    assert s["fact_recall"] and s["citation_hit"] and s["refusal_ok"]
    wrong_cite = Answer("q", "500 euro al mese", True, [{"doc_label": "Circolare n. 56", "anchor": "1.p13"}])
    assert not evals.score_item(item, wrong_cite)["citation_hit"]
    refused = Answer("q", "no", False)
    assert evals.score_item(item, refused)["fact_recall"] is False
    una = evals.score_item({"id": "u", "q": "q", "unanswerable": True}, refused)
    assert una["refusal_ok"] is True
    summary = evals.summarize([s, una])
    assert summary["fact_recall"] == 1.0 and summary["refused_when_unanswerable"] == 1.0
    assert evals.summarize([])["fact_recall"] == 0.0


def test_eval_runs_end_to_end_and_scores_rules(extracted_db, tmp_path):
    golden = tmp_path / "g.json"
    golden.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "g",
                        "q": "limite massimo di importo pari a 500 euro su base mensile per ciascun lavoratore",
                        "must_include": ["500"],
                        "gold": [["55", "1.p13"]],
                    },
                    {"id": "u", "q": "IRPEF", "unanswerable": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    st = Store(extracted_db)
    out = evals.run_qa(st, FakeLLM(GOOD_ANSWER), golden)
    # the fake model always cites 1.p13, which IRPEF retrieval does not return -> gate refuses
    assert out["summary"]["fact_recall"] == 1.0 and out["summary"]["refused_when_unanswerable"] == 1.0
    rules = tmp_path / "r.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "circular": "55",
                        "match": "giovani",
                        "expect": {
                            "monthly_cap_eur": 500,
                            "max_duration_months": 12,
                            "contract_types": ["tempo_indeterminato"],
                        },
                    },
                    {"circular": "99", "match": "x", "expect": {"monthly_cap_eur": 1}},
                ]
            }
        ),
        encoding="utf-8",
    )
    scored = evals.score_rules(st.rule_rows, rules)
    assert scored["fields"] == 4 and scored["field_accuracy"] == 0.5
    assert scored["rules"][1]["found"] is False


def test_rate_limiter_per_ip_daily_cap_and_bounded_memory():
    rl = RateLimiter(per_ip=100, window_s=1, daily=1000, per_ip_daily=2, max_ips=3)
    assert rl.allow("a", 0) is None and rl.allow("a", 5) is None
    assert "limite giornaliero di domande" in rl.allow("a", 10)
    for i in range(10):
        rl.allow(f"ip{i}", 20 + i)
    assert len(rl.hits) <= 3


def test_store_reloads_when_database_changes(extracted_db):
    import os
    import time as t

    st = Store(extracted_db)
    calls = []
    st.reload = lambda: calls.append(1)
    st._checked = 0.0
    st.refresh(every_s=0)
    assert calls == []  # unchanged file
    os.utime(extracted_db, (t.time() + 10, t.time() + 10))
    st._checked = 0.0
    st.refresh(every_s=0)
    assert calls == [1]


def test_eval_matching_is_on_whole_words():
    assert evals._has("Sì, è escluso", "esclus|no")
    assert not evals._has("non spetta per l'anno 2024", "no|24")
    assert evals._has("fino a 8.000 euro annui", "8 000|8000")
    assert evals._has("dal 1° agosto 2026", "1 agosto 2026")


def test_aggregate_and_regression_gate():
    agg = evals.aggregate(
        [
            {"n_answerable": 2, "fact_recall": 1.0, "rejected_citations": 0},
            {"n_answerable": 2, "fact_recall": 0.5, "rejected_citations": 2},
        ]
    )
    assert agg["fact_recall"] == 0.75 and agg["fact_recall_min"] == 0.5 and agg["rejected_citations"] == 2
    base = {"qa": {"summary": {"fact_recall": 0.8, "rejected_citations": 0}}, "extraction": {"field_accuracy": 1.0}}
    good = {
        "qa": {"summary": {"fact_recall": 0.8, "fact_recall_min": 0.1, "rejected_citations": 0}},
        "extraction": {"field_accuracy": 1.0},
    }
    assert evals.regressions(base, good) == {}
    bad = {"qa": {"summary": {"fact_recall": 0.9, "rejected_citations": 3}}, "extraction": {"field_accuracy": 0.9}}
    assert set(evals.regressions(base, bad)) == {"rejected_citations", "field_accuracy"}
