# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Shared fixtures: an offline INPS, a fake LLM, and a real dlt + dbt build in a temp DuckDB."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from radar.inps_source import InpsClient

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
C55_SEL = "circolari-e-messaggi.2026.05.circolare-numero-55-del-14-05-2026_15258"
TS_14_MAY = 1778709600000  # 2026-05-14 00:00 Europe/Rome


def listing_item(sel: str, subject: str, ts: int, tipo: str = "Circolare", numero: str = "55") -> dict[str, Any]:
    return {
        "selectors": sel,
        "dataPubblicazioneTimestamp": str(ts),
        "tipo": tipo,
        "numero": numero,
        "oggetto": subject,
        "path": "/x",
        "contentFragmentModel": "m",
        "dataPubblicazione": "14/05/2026",
    }


def fake_inps(
    pages: list[list[dict[str, Any]]], details: dict[str, dict[str, Any]], fail_first: int = 0
) -> tuple[InpsClient, list[str]]:
    calls: list[str] = []
    state = {"fails": fail_first}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append(url)
        if state["fails"] > 0:
            state["fails"] -= 1
            return httpx.Response(503)
        if "jcr:content.search" in url:
            page_hex = url.split("jcr:content.search.")[1].split(".")[1]
            page = int(bytes.fromhex(page_hex).decode())
            res = pages[page] if page < len(pages) else []
            return httpx.Response(200, json={"result": "OK", "data": {"page": page, "results": res}})
        for sel, payload in details.items():
            if sel in url:
                return httpx.Response(200, json=payload)
        return httpx.Response(404)

    client = InpsClient(httpx.Client(transport=httpx.MockTransport(handler)), delay_s=0, sleep=lambda s: None)
    return client, calls


@pytest.fixture(autouse=True)
def isolated_app_db(tmp_path, monkeypatch):
    """Never write the real app database from tests."""
    monkeypatch.setenv("RADAR_APP_DB", str(tmp_path / "app.db"))


@pytest.fixture(scope="session")
def c55_detail() -> dict[str, Any]:
    return json.loads((FIX / "detail_c55.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def built_db(tmp_path_factory: pytest.TempPathFactory, c55_detail: dict[str, Any]) -> Path:
    """dlt load from the fake INPS + the real dbt project, into a temp DuckDB."""
    from radar import kb
    from radar.pipeline import run

    db = tmp_path_factory.mktemp("kb") / "radar.duckdb"
    pages = [
        [
            listing_item(
                "x.2026.11.messaggio-numero-9-del-20-11-2026_16000",
                "Convenzione sindacale",
                TS_14_MAY + 86400000 * 180,
                "Messaggio",
                "9",
            ),
            listing_item(C55_SEL, "Articolo 2 del decreto-legge n. 62. Bonus Giovani 2026", TS_14_MAY),
            listing_item("x.2026.04.circolare-numero-1-del-01-04-2026_15000", "Old", TS_14_MAY - 86400000 * 60),
        ]
    ]
    client, _ = fake_inps(pages, {C55_SEL: c55_detail})
    run(since="2026-05-01", db_path=db, client=client)

    from dbt.cli.main import dbtRunner

    os.environ["RADAR_DB"] = str(db)
    res = dbtRunner().invoke(
        [
            "build",
            "--project-dir",
            str(ROOT / "dbt"),
            "--profiles-dir",
            str(ROOT / "dbt"),
            "--exclude",
            "source:*",
            "--quiet",
        ]
    )
    assert res.success, res.exception
    with kb.connect(db) as con:
        kb.build_paragraphs(con)
    return db


GOOD_RULE: dict[str, Any] = {
    "incentive_id": "bonus-giovani-2026",
    "name": "Bonus Giovani 2026",
    "exemption_percent": {
        "value": 100,
        "citation": {"anchor": "1.p13", "quote": "pari al 100 per cento dei complessivi contributi previdenziali"},
    },
    "monthly_cap_eur": {
        "value": 500,
        "citation": {"anchor": "1.p13", "quote": "500 euro su base mensile per ciascun lavoratore"},
    },
    "monthly_cap_eur_zes": {"value": 650, "citation": {"anchor": "1.p14", "quote": "650 euro su base mensile"}},
    "max_duration_months": {
        "value": 24,
        "citation": {"anchor": "1.p3", "quote": "per un periodo massimo di ventiquattro mesi"},
    },
    "hire_from": {
        "value": "2026-01-01",
        "citation": {"anchor": "1.p9", "quote": "dal 1° gennaio 2026 al 31 dicembre 2026"},
    },
    "hire_to": {
        "value": "2026-12-31",
        "citation": {"anchor": "1.p9", "quote": "dal 1° gennaio 2026 al 31 dicembre 2026"},
    },
    "worker_max_age_exclusive": {
        "value": 35,
        "citation": {"anchor": "1.p3", "quote": "non hanno compiuto il trentacinquesimo anno di età"},
    },
    "min_months_without_regular_job": {
        "value": 24,
        "citation": {
            "anchor": "1.p3",
            "quote": "privi di un impiego regolarmente retribuito da almeno ventiquattro mesi",
        },
    },
    "contract_types": {
        "value": ["tempo_indeterminato"],
        "citation": {"anchor": "1.p3", "quote": "contratto di lavoro subordinato a tempo indeterminato"},
    },
    "requires_net_job_increase": {
        "value": True,
        "citation": {"anchor": "1.p12", "quote": "subordinato al requisito dell’incremento occupazionale netto"},
    },
    "zes_regions": {
        "value": ["Puglia"],
        "citation": {"anchor": "1.p9", "quote": "an invented quote that is not in the text"},
    },
}


class FakeLLM:
    """Returns canned JSON and records prompts."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload, self.calls = payload, []  # type: ignore[var-annotated]

    def __call__(self, system: str, user: str, tool: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        self.calls.append((system, user, tool))
        return json.loads(json.dumps(self.payload)), {"input_tokens": 10, "output_tokens": 5}


@pytest.fixture()
def fake_rule_llm() -> FakeLLM:
    return FakeLLM({"incentives": [GOOD_RULE]})


@pytest.fixture(scope="session")
def extracted_db(built_db: Path) -> Path:
    from radar import kb

    with kb.connect(built_db) as con:
        kb.extract_all(con, FakeLLM({"incentives": [GOOD_RULE]}), "fake-model")
    return built_db
