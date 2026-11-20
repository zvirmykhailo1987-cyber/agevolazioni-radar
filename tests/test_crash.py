# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Crash tests of the full cycle: what happens when the outside world misbehaves.

INPS down or returning junk, the model failing or answering with hostile text, a daily
spending cap, many consultants clicking at once, hostile query strings. The rule in every
case: no wrong money, no 500, nothing silently lost.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
from conftest import C55_SEL, GOOD_RULE, TS_14_MAY, FakeLLM, fake_inps, listing_item
from fastapi.testclient import TestClient

from radar import kb
from radar.appstate import AppState
from radar.inps_source import InpsClient
from radar.pipeline import run
from radar.store import Store
from radar.web import create_app


def copy_db(src: Path, dst: Path) -> None:
    """Copy a DuckDB file that this test process may still hold open (dbt runs in-process, and
    Windows refuses to read a locked file), through DuckDB itself."""
    with kb.connect(src) as con:
        con.execute(f"attach '{dst.as_posix()}' as copy_target")
        rels = con.execute(
            "select schema_name, table_name from duckdb_tables() where database_name = current_database()"
            " union all select schema_name, view_name from duckdb_views()"
            " where database_name = current_database() and not internal"
        ).fetchall()
        for schema, name in rels:  # views are materialised: the copy only has to be read
            con.execute(f'create schema if not exists copy_target."{schema}"')
            con.execute(f'create table copy_target."{schema}"."{name}" as select * from "{schema}"."{name}"')
        con.execute("detach copy_target")


# --- the source -----------------------------------------------------------------
def test_inps_outage_fails_loudly_and_keeps_yesterdays_data(built_db, tmp_path):
    db = tmp_path / "copy.duckdb"
    copy_db(built_db, db)
    with duckdb.connect(str(db), read_only=True) as con:
        before = con.execute("select count(*) from raw.acts").fetchone()
    client, calls = fake_inps([[]], {}, fail_first=10_000)
    with pytest.raises(Exception, match=r"503|Service Unavailable|HTTPStatusError|PipelineStepFailed"):
        run(since="2026-05-01", db_path=db, client=client)
    assert len(calls) == 3, "three tries, then give up: no hammering"
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("select count(*) from raw.acts").fetchone() == before


def test_listing_with_junk_rows_loads_the_good_ones(tmp_path, c55_detail):
    junk = {"selectors": "", "oggetto": None}  # a row INPS might return half-filled
    pages = [[junk, listing_item(C55_SEL, "Bonus Giovani 2026", TS_14_MAY)]]
    client, _ = fake_inps(pages, {C55_SEL: c55_detail})
    db = tmp_path / "j.duckdb"
    try:
        run(since="2026-05-01", db_path=db, client=client, today=None)
    except Exception as exc:  # pragma: no cover - the assertion below explains the failure
        pytest.fail(f"one malformed listing row aborted the whole load: {exc}")
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("select count(*) from raw.acts").fetchone()[0] >= 1


# --- the model ------------------------------------------------------------------------
def _kb_with_docs(path: Path, n: int) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute(kb.DDL)
    con.execute("create schema if not exists marts")
    con.execute(
        "create table marts.incentive_documents (doc_id bigint, content_hash varchar, version int, doc_type varchar,"
        " number varchar, published_on date, subject varchar, url varchar, body_html varchar)"
    )
    for i in range(n):
        con.execute(
            "insert into marts.incentive_documents values (?, ?, 1, 'Circolare', ?, date '2026-01-01' + ?, 'S', 'u', '')",
            [i, f"h{i}", str(i), i],
        )
        con.execute("insert into kb.paragraphs values (?, ?, '1.p1', '1', '1. T', 'testo')", [i, f"h{i}"])
    return con


class FlakyLLM(FakeLLM):
    def __init__(self, fail_on: set[int]) -> None:
        super().__init__({"incentives": []})
        self.fail_on = fail_on

    def __call__(self, system: str, user: str, tool: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        if len(self.calls) in self.fail_on:
            self.calls.append((system, user, tool))
            raise httpx.ReadTimeout("simulated timeout")
        return super().__call__(system, user, tool)


def test_daily_cap_spends_on_the_newest_acts_and_skips_cached_ones(tmp_path):
    con = _kb_with_docs(tmp_path / "c.duckdb", 6)
    llm = FakeLLM({"incentives": []})
    first = kb.extract_all(con, llm, "m", limit=2)
    assert first["extracted"] == 2 and first["deferred"] == 4
    done = {r[0] for r in con.execute("select doc_id from kb.extractions").fetchall()}
    assert done == {5, 4}, "newest first"
    second = kb.extract_all(con, llm, "m", limit=2)
    assert second["cached"] == 2 and second["extracted"] == 2, "cached acts must not use up the cap"
    assert kb.extract_all(con, llm, "m", limit=2)["extracted"] == 2
    assert kb.extract_all(con, llm, "m", limit=2) == {
        "cached": 6,
        "extracted": 0,
        "failed": 0,
        "deferred": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def test_one_failed_model_call_does_not_stop_the_refresh_and_is_retried(tmp_path):
    con = _kb_with_docs(tmp_path / "f.duckdb", 4)
    s = kb.extract_all(con, FlakyLLM(fail_on={1}), "m")
    assert s["extracted"] == 3 and s["failed"] == 1
    again = kb.extract_all(con, FakeLLM({"incentives": []}), "m")
    assert again["extracted"] == 1 and again["cached"] == 3


HOSTILE = '<img src=x onerror="alert(1)">'


def test_hostile_text_from_the_model_or_the_act_is_escaped_everywhere(built_db, tmp_path, monkeypatch):
    db = tmp_path / "h.duckdb"
    copy_db(built_db, db)
    rule = json.loads(json.dumps(GOOD_RULE))
    rule["name"] = f"Bonus {HOSTILE}"
    rule["legal_basis"] = {"value": HOSTILE, "citation": {"anchor": "1.p1", "quote": HOSTILE}}
    rule["other_conditions"] = {
        "value": [{"text": f'=HYPERLINK("http://x") {HOSTILE}', "applies_to": "lavoratore"}],
        "citation": {"anchor": "1.p3", "quote": "privi di un impiego regolarmente retribuito da almeno"},
    }
    with kb.connect(db) as con:
        kb.extract_all(con, FakeLLM({"incentives": [rule]}), "hostile-model")
    monkeypatch.setenv("RADAR_COOKIE_SECURE", "0")
    c = TestClient(create_app(Store(db), llm=None, state=AppState(tmp_path / "a.db")))
    pages = [
        "/",
        "/catalogo",
        "/incentivo/bonus-giovani-2026",
        "/recupero/trattoria-puglia",
        "/stato",
        "/api/incentivi",
    ]
    for url in pages:
        r = c.get(url)
        assert r.status_code == 200, url
        assert 'onerror="alert(1)"' not in r.text, f"unescaped model text on {url}"
    csv = c.get("/recupero/trattoria-puglia.csv").text
    assert "\n=HYPERLINK" not in csv and ";=HYPERLINK" not in csv and ',"=HYPERLINK' not in csv


# --- people ------------------------------------------------------------------------------
def test_many_consultants_at_once_lose_no_decision_and_never_500(extracted_db, tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_COOKIE_SECURE", "0")
    state = AppState(tmp_path / "busy.db")
    app = create_app(Store(extracted_db), llm=None, state=state)
    errors: list[str] = []

    def consultant(i: int) -> None:
        with TestClient(app) as c:
            r = c.post(
                "/accesso",
                data={"username": "consulente", "password": "consulente-demo-2026", "next": "/recupero"},
                headers={"x-forwarded-for": f"10.0.0.{i}"},
                follow_redirects=False,
            )
            if r.status_code != 303:
                errors.append(f"login {r.status_code}")
                return
            page = c.get("/recupero/trattoria-puglia").text
            csrf = re.search(r'name="csrf" value="([^"]+)"', page)
            fid = re.search(r'name="finding_id" value="([^"]+)"', page)
            if not csrf or not fid:
                errors.append("no form")
                return
            for k in range(5):
                r = c.post(
                    "/recupero/trattoria-puglia/decisione",
                    data={
                        "finding_id": fid.group(1),
                        "decision": "approvata",
                        "note": f"{i}-{k}",
                        "csrf": csrf.group(1),
                    },
                    follow_redirects=False,
                )
                if r.status_code != 303:
                    errors.append(f"decision {r.status_code}")
                for url in ("/recupero/trattoria-puglia", "/stato", "/catalogo"):
                    if (s := c.get(url).status_code) >= 500:
                        errors.append(f"{url} {s}")

    threads = [threading.Thread(target=consultant, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(state.audit(limit=1000)) == 8 * 5, "every decision is in the audit log"


@pytest.mark.parametrize(
    "url",
    [
        "/catalogo?q=" + "a" * 5000,
        "/catalogo?status=%00&group=../../etc&side=<x>",
        "/recupero/trattoria-puglia?rate=-1",
        "/recupero/trattoria-puglia?rate=nan",
        "/recupero/trattoria-puglia?rate=1e308",
        "/recupero/..%2F..%2Fpyproject",
        "/recupero/trattoria-puglia.csv?rate=inf",
        "/incentivo/%E2%80%AE%F0%9F%92%A5",
        "/static/../pyproject.toml",
        "/accesso?next=//evil.example",
        "/api/incentivi/%27%3B%20drop%20table",
    ],
)
def test_hostile_urls_never_crash(extracted_db, tmp_path, url):
    c = TestClient(create_app(Store(extracted_db), llm=None, state=AppState(tmp_path / "u.db")))
    r = c.get(url, follow_redirects=False)
    assert r.status_code < 500, (url, r.status_code)
    assert "[project]" not in r.text, "no file outside static/ is served"
    assert "evil.example" not in r.headers.get("location", "")


def test_precheck_api_rejects_nonsense_without_crashing(extracted_db, tmp_path):
    c = TestClient(create_app(Store(extracted_db), llm=None, state=AppState(tmp_path / "p.db")))
    bad = [
        {},
        {"hire_date": "not-a-date"},
        {"hire_date": "2026-06-01", "employer_employees": -5, "months_without_regular_job": 10**12},
        {"hire_date": "2026-06-01", "worker_gender": "Q"},
    ]
    for body in bad:
        assert c.post("/api/verifica/bonus-giovani-2026", json=body).status_code in (200, 422)
    r = c.post("/verifica", data={"incentive_id": "bonus-giovani-2026", "hire_date": "2026-06-01"})
    assert r.status_code == 200 and "verificare" in r.text.lower()


def test_store_survives_a_missing_or_half_written_database(extracted_db, tmp_path):
    db = tmp_path / "swap.duckdb"
    copy_db(extracted_db, db)
    s = Store(db)
    n = len(s.incentives())
    db.write_bytes(b"not a database")  # e.g. a copy interrupted mid-way
    s.refresh(every_s=0)
    assert len(s.incentives()) == n, "a broken file keeps the last good knowledge base in memory"
    db.unlink()
    copy_db(extracted_db, db)
    s.refresh(every_s=0)
    assert len(s.incentives()) == n


def test_inps_client_handles_a_non_json_body():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = InpsClient(httpx.Client(transport=httpx.MockTransport(handler)), delay_s=0, sleep=lambda s: None)
    with pytest.raises(ValueError):  # json.JSONDecodeError is a ValueError: the run fails, nothing is written
        client.get_json("https://www.inps.it/x.json")
