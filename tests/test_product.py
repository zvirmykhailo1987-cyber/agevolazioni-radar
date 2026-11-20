# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Catalog, Recupero, sign-in, reviews, status page and the app-state store."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeLLM
from fastapi.testclient import TestClient

from radar import appstate
from radar.appstate import AppState, check_password, hash_password
from radar.store import Store
from radar.web import create_app, safe_next


@pytest.fixture()
def app_state(tmp_path):
    return AppState(tmp_path / "app.db")


@pytest.fixture()
def client(extracted_db, app_state, monkeypatch):
    monkeypatch.setenv("RADAR_COOKIE_SECURE", "0")
    return TestClient(
        create_app(
            Store(extracted_db), llm=FakeLLM({"answerable": False, "answer": "", "citations": []}), state=app_state
        )
    )


def login(client):
    r = client.post(
        "/accesso",
        data={"username": "consulente", "password": "consulente-demo-2026", "next": "/recupero/trattoria-puglia"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/recupero/trattoria-puglia"
    page = client.get("/recupero/trattoria-puglia").text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1), re.search(
        r'name="finding_id" value="([^"]+)"', page
    ).group(1)


def test_catalog_filters(client):
    assert "Bonus Giovani 2026" in client.get("/catalogo").text
    assert "Bonus Giovani 2026" in client.get("/catalogo?q=giovani&group=giovani&status=attiva&side=datore").text
    r = client.get("/catalogo?q=nessuno")
    assert "Nessun incentivo corrisponde" in r.text and 'data-state="empty"' in r.text


def test_recupero_pages_and_csv(client):
    assert "Trattoria del Porto" in client.get("/recupero").text
    page = client.get("/recupero/trattoria-puglia?rate=0.30").text
    assert "Luca Bianchi" in page and "Bonus Giovani 2026" in page and "Accedi come consulente" in page
    assert client.get("/recupero/nope").status_code == 404
    assert 'value="0.60"' in client.get("/recupero/trattoria-puglia?rate=9").text, "rate is clamped"
    assert 'value="0.30"' in client.get("/recupero/trattoria-puglia?rate=abc").text
    csv = client.get("/recupero/trattoria-puglia.csv")
    assert csv.headers["content-type"].startswith("text/csv") and "Bonus Giovani 2026" in csv.text


def test_sign_in_review_and_audit(client):
    csrf, fid = login(client)
    r = client.post(
        "/recupero/trattoria-puglia/decisione",
        data={"finding_id": fid, "decision": "approvata", "note": "visto estratto", "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "approvata da consulente" in client.get("/recupero/trattoria-puglia").text
    status = client.get("/stato").text
    assert "visto estratto" in status and "Registro delle decisioni" in status
    bad = client.post(
        "/recupero/trattoria-puglia/decisione", data={"finding_id": fid, "decision": "approvata", "csrf": "forged"}
    )
    assert bad.status_code == 403
    assert (
        client.post(
            "/recupero/trattoria-puglia/decisione", data={"finding_id": "x" * 16, "decision": "approvata", "csrf": csrf}
        ).status_code
        == 400
    )
    assert client.post("/esci", data={"csrf": "forged"}).status_code == 403, "logout needs the CSRF token"
    client.post("/esci", data={"csrf": csrf})
    anon = client.post(
        "/recupero/trattoria-puglia/decisione",
        data={"finding_id": fid, "decision": "approvata", "csrf": csrf},
        follow_redirects=False,
    )
    assert anon.status_code == 303 and anon.headers["location"].startswith("/accesso")


def test_wrong_password_and_login_rate_limit(client):
    for _ in range(10):
        r = client.post("/accesso", data={"username": "consulente", "password": "nope"})
        assert "Credenziali non valide" in r.text
    assert "Troppe domande" in client.post("/accesso", data={"username": "consulente", "password": "nope"}).text


@pytest.mark.parametrize(
    "target,expected",
    [
        ("/recupero", "/recupero"),
        ("/\t/evil.example", "/"),
        ("//evil.example", "/"),
        ("https://evil.example", "/"),
        ("/\\evil", "/"),
    ],
)
def test_safe_next(target, expected):
    assert safe_next(target) == expected


def test_incentive_page_has_timeline(client):
    page = client.get("/incentivo/bonus-giovani-2026").text
    assert "Atti che descrivono" in page and "introduce" in page


def test_app_state_passwords_sessions_reviews(app_state, monkeypatch):
    h = hash_password("s3cret")
    assert check_password("s3cret", h) and not check_password("other", h) and not check_password("x", "broken")
    app_state.ensure_user("u", "s3cret")
    app_state.ensure_user("u", "changed")  # idempotent: does not overwrite
    assert app_state.login("u", "changed") is None
    token, csrf = app_state.login("u", "s3cret")
    assert app_state.session(token)["csrf"] == csrf and app_state.session(None) is None
    later = datetime.now(UTC) + timedelta(hours=9)
    monkeypatch.setattr(appstate, "_now", lambda: later)
    assert app_state.session(token) is None, "sessions expire"
    app_state.logout(token)
    with pytest.raises(ValueError):
        app_state.record_review(
            finding_id="f",
            company_id="c",
            hire_id="h",
            incentive_id="i",
            decision="maybe",
            note="",
            reviewer="u",
            evidence={},
        )
    app_state.log_llm("qa", "m", 10, 5, 120, True)
    app_state.log_llm("qa", "m", 10, 5, 80, False)
    stats = app_state.llm_stats()["by_purpose"][0]
    assert stats["n"] == 2 and stats["errors"] == 1 and stats["i"] == 20


def test_resolving_a_difference_applies_the_choice_and_logs_it(extracted_db, app_state, monkeypatch):
    monkeypatch.setenv("RADAR_COOKIE_SECURE", "0")
    st = Store(extracted_db)
    other = st.merged["bonus-giovani-2026"].rule.monthly_cap_eur.model_copy(update={"value": 550.0}).model_dump()
    diff = {
        "field": "monthly_cap_eur",
        "kept": 500.0,
        "kept_in": "Circolare n. 55",
        "other": 550.0,
        "other_in": "Messaggio n. 9",
        "other_citation": None,
        "other_cited": other,
        "key": "k1",
        "other_source": {"label": "Messaggio n. 9", "url": "u", "short": "Mess. 9"},
    }
    st._pristine["bonus-giovani-2026"].differences.append(diff)
    st.apply_resolutions({}, version=-1)
    c = TestClient(create_app(st, llm=FakeLLM({}), state=app_state))
    assert "Differenze tra atti" in c.get("/incentivo/bonus-giovani-2026").text
    assert c.get("/catalogo").text.count("1 da verificare") == 1
    assert (
        c.post(
            "/incentivo/bonus-giovani-2026/differenza",
            data={"diff_key": "k1", "choice": "other", "csrf": "x"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    csrf, _ = login(c)
    assert (
        c.post(
            "/incentivo/bonus-giovani-2026/differenza", data={"diff_key": "k1", "choice": "other", "csrf": "forged"}
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/incentivo/bonus-giovani-2026/differenza", data={"diff_key": "nope", "choice": "other", "csrf": csrf}
        ).status_code
        == 400
    )
    c.post(
        "/incentivo/bonus-giovani-2026/differenza",
        data={"diff_key": "k1", "choice": "other", "note": "messaggio successivo", "csrf": csrf},
    )
    page = c.get("/incentivo/bonus-giovani-2026").text
    assert "risolta" in page and "messaggio successivo" in page
    assert st.merged["bonus-giovani-2026"].rule.monthly_cap_eur.value == 550.0
    assert "1 da verificare" not in c.get("/catalogo").text
    # changing the decision back really reverts: merged rules are rebuilt from the pristine merge
    c.post("/incentivo/bonus-giovani-2026/differenza", data={"diff_key": "k1", "choice": "kept", "csrf": csrf})
    c.get("/catalogo")
    assert st.merged["bonus-giovani-2026"].rule.monthly_cap_eur.value == 500.0
    # a decision is tied to one difference: a new difference on the same field is not resolved by it
    st._pristine["bonus-giovani-2026"].differences.append({**diff, "key": "k2", "other": 600.0})
    st.apply_resolutions(*app_state.resolutions()[:1], version=-2)
    assert [d.get("resolved") is not None for d in st.merged["bonus-giovani-2026"].differences] == [True, False]
    with pytest.raises(ValueError):
        app_state.record_resolution(diff_key="k", incentive_id="x", field="f", choice="both", note="", reviewer="u")


def test_public_demo_mode_keeps_visitors_apart(extracted_db, tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_COOKIE_SECURE", "0")
    monkeypatch.setenv("RADAR_DEMO_MODE", "1")
    state = AppState(tmp_path / "demo.db")
    st = Store(extracted_db)
    a = TestClient(create_app(st, llm=FakeLLM({}), state=state))
    b = TestClient(create_app(st, llm=FakeLLM({}), state=state))
    csrf_a, fid = login(a)
    a.post("/recupero/trattoria-puglia/decisione", data={"finding_id": fid, "decision": "respinta", "csrf": csrf_a})
    assert "respinta da consulente" in a.get("/recupero/trattoria-puglia").text
    login(b)
    assert "respinta da consulente" not in b.get("/recupero/trattoria-puglia").text, "visitor B sees a clean slate"
    assert (
        a.post(
            "/incentivo/bonus-giovani-2026/differenza", data={"diff_key": "k", "choice": "other", "csrf": csrf_a}
        ).status_code
        == 403
    )


def test_csv_is_spreadsheet_safe_and_italian():
    from radar.web import csv_cell, it_number

    assert csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and csv_cell("@x") == "'@x" and csv_cell("Mario") == "Mario"
    assert it_number(12960.0) == "12960,00" and it_number(None) == ""


def test_login_does_not_reveal_unknown_users_by_timing(app_state):
    import time

    app_state.ensure_user("u", "pw")
    t0 = time.perf_counter()
    app_state.login("nobody", "pw")
    t_unknown = time.perf_counter() - t0
    t0 = time.perf_counter()
    app_state.login("u", "wrong")
    t_known = time.perf_counter() - t0
    assert t_unknown > t_known / 3, "unknown users still pay the scrypt cost"
