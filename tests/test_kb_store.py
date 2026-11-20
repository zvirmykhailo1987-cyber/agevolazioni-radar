# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from datetime import date

import duckdb
from conftest import FakeLLM

from radar import kb
from radar.eligibility import HireProfile
from radar.store import Store


def test_pipeline_and_dbt_build_the_layers(built_db):
    with kb.connect(built_db, read_only=True) as con:
        assert con.execute("select count(*), sum(relevant::int) from raw.acts").fetchone() == (2, 1)
        row = con.execute("select number, published_on, version from marts.incentive_documents").fetchone()
        assert row == ("55", date(2026, 5, 14), 1)
        assert con.execute("select count(*) from kb.paragraphs").fetchone()[0] > 150


def test_rebuilding_paragraphs_is_idempotent(built_db):
    with kb.connect(built_db) as con:
        assert kb.build_paragraphs(con) == 0


def test_extraction_is_cached_by_hash_prompt_and_model(extracted_db):
    llm = FakeLLM({"incentives": []})
    with kb.connect(extracted_db) as con:
        assert kb.extract_all(con, llm, "fake-model")["cached"] == 1 and llm.calls == []
        rows = kb.rules(con, model="fake-model")
    assert rows[0]["rule"].monthly_cap_eur.value == 500
    assert rows[0]["coverage"]["unverified"] == 1  # the invented zes_regions quote


def test_new_text_version_creates_diff(tmp_path):
    con = duckdb.connect(str(tmp_path / "v.duckdb"))
    con.execute(kb.DDL)
    con.execute("create schema marts")
    con.execute(
        "create or replace table marts.doc_versions as select * from (values "
        "(15258, 'h1', 1, timestamp '2026-05-14', timestamp '2026-06-01', false),"
        "(15258, 'h2', 2, timestamp '2026-06-01', null, true)) t(doc_id, content_hash, version, valid_from, valid_to, is_current)"
    )
    con.execute(
        "insert into kb.paragraphs values (15258,'h1','1.p1','1','1. P','vecchio testo'),"
        "(15258,'h2','1.p1','1','1. P','nuovo testo')"
    )
    diff = kb.diff_versions(con, 15258)
    con.close()
    assert diff[0]["from_version"] == 1 and "-[1.p1] vecchio testo" in diff[0]["changes"]


def test_store_queries(extracted_db):
    st = Store(extracted_db)
    assert st.freshness["acts_scanned"] == 2 and st.freshness["documents_in_kb"] == 1
    assert st.search("tetto mensile ZES", 2)[0]["doc"].startswith("Circolare n. 55")
    assert st.incentives()[0]["incentive_id"] == "bonus-giovani-2026"
    out = st.check("bonus-giovani-2026", HireProfile(hire_date=date(2026, 6, 1)))
    assert out["verdict"] == "needs_review" and "L. 12/1979" in out["disclaimer"]
    assert st.check("nope", HireProfile(hire_date=date(2026, 6, 1))) is None and st.rule("nope") is None


def test_corrected_act_becomes_a_new_version_end_to_end(tmp_path, c55_detail, monkeypatch):
    """dlt -> dbt with a fake INPS that corrects circular 55 between two runs."""
    import copy
    from datetime import date as d

    from conftest import C55_SEL, ROOT, TS_14_MAY, fake_inps, listing_item
    from dbt.cli.main import dbtRunner

    from radar.pipeline import run

    db = tmp_path / "v.duckdb"
    monkeypatch.setenv("RADAR_DB", str(db))
    pages = [[listing_item(C55_SEL, "Bonus Giovani 2026", TS_14_MAY)]]
    corrected = copy.deepcopy(c55_detail)
    corrected["testoCompleto"]["value"] = corrected["testoCompleto"]["value"].replace("500", "550", 1)

    def load_and_build(detail):
        client, _ = fake_inps(pages, {C55_SEL: detail})
        run(since="2026-05-01", db_path=db, client=client, today=d(2026, 6, 1))
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

    load_and_build(c55_detail)
    load_and_build(corrected)
    load_and_build(corrected)  # unchanged text: no new version
    with kb.connect(db, read_only=True) as con:
        versions = con.execute("select version, is_current from marts.doc_versions order by version").fetchall()
        current = con.execute("select content_hash from marts.incentive_documents").fetchone()[0]
        raw_rows = con.execute("select count(*) from raw.documents").fetchone()[0]
    assert versions == [(1, False), (2, True)] and raw_rows == 2
    from radar.text import content_hash
    from radar.text import decode_field as dec

    assert current == content_hash(dec(corrected["testoCompleto"]["value"]))
