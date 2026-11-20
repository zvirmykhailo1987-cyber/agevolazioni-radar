# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from conftest import GOOD_RULE

from radar.extract import SYSTEM, extract_rules, render, tool_schema
from radar.llm import strict_schema
from radar.rules import CITED_FIELDS, IncentiveRule, coverage, verify
from radar.text import Paragraph

P = [
    Paragraph("1.p1", "1", "1. Premessa", "L’esonero è pari al 100 per cento, nel limite di 500 euro su base mensile.")
]


def test_gate_keeps_grounded_values_and_drops_invented_ones():
    rule = IncentiveRule.model_validate(
        {
            "incentive_id": "x",
            "name": "X",
            "exemption_percent": {"value": 100, "citation": {"anchor": "1.p1", "quote": "pari al 100 per cento"}},
            "monthly_cap_eur": {"value": 650, "citation": {"anchor": "1.p1", "quote": "nel limite di 650 euro"}},
            "max_duration_months": {"value": 24, "citation": {"anchor": "9.p9", "quote": "pari al 100 per cento"}},
            "employer_max_employees": {"value": 10},
            "worker_min_age": {"value": None, "citation": {"anchor": "1.p1", "quote": "x"}},
        }
    )
    v = verify(rule, P)
    assert (v.exemption_percent.status, v.exemption_percent.value) == ("verified", 100)
    assert (v.monthly_cap_eur.status, v.monthly_cap_eur.value) == ("unverified", None)
    assert v.max_duration_months.status == "unverified", "right quote, wrong paragraph"
    assert v.employer_max_employees.status == "unverified", "a value without citation is not trusted"
    assert (v.worker_min_age.status, v.worker_min_age.citation) == ("absent", None)
    c = coverage(v)
    assert c == {"verified": 1, "unverified": 3, "absent": len(CITED_FIELDS) - 4}


def test_extract_rules_applies_gate(fake_rule_llm, c55_detail):
    from radar.text import decode_field, paragraphs

    paras = paragraphs(decode_field(c55_detail["testoCompleto"]["value"]))
    res = extract_rules(paras, "Circolare 55", fake_rule_llm)
    r = res.rules[0]
    assert r.monthly_cap_eur.status == "verified" and r.zes_regions.status == "unverified"
    assert res.usage["input_tokens"] == 10
    system, user, tool = fake_rule_llm.calls[0]
    assert system == SYSTEM and "[1.p13]" in user and tool["name"] == "record_incentives"


def test_tool_schema_hides_gate_status_and_is_strict():
    schema = tool_schema()["input_schema"]
    assert "status" not in str(schema["$defs"])
    strict = strict_schema(schema)
    rule = strict["$defs"]["IncentiveRule"]
    assert rule["additionalProperties"] is False and set(rule["required"]) == set(rule["properties"])
    assert "default" not in str(strict) and "maxLength" not in str(strict)


def test_render_groups_by_heading():
    text = render([*P, Paragraph("1.p2", "1", "1. Premessa", "b")], "T")
    assert text.count("## 1. Premessa") == 1 and "[1.p2] b" in text


def test_good_rule_fixture_is_valid():
    assert IncentiveRule.model_validate(GOOD_RULE).incentive_id == "bonus-giovani-2026"


def test_gate_requires_whole_words_and_the_value_in_the_quote():
    from radar.rules import value_in_quote
    from radar.text import quote_found

    src = "devono occupare fino a un massimo di 10 dipendenti, pari al 100 per cento, dal 1° gennaio 2026"
    assert not quote_found("anni", "35 anni di età"), "too short to prove anything"
    assert not quote_found("massimo di 1 dipendenti", src), "whole words only: 1 is not 10"
    assert not quote_found("fino a un massimo di 1", src), "a quote ending mid-number is not a quote"
    assert quote_found("fino a un massimo di 10 dipendenti", src)
    assert value_in_quote("employer_max_employees", 10, "fino a un massimo di 10 dipendenti")
    assert not value_in_quote("employer_max_employees", 1, "fino a un massimo di 10 dipendenti")
    assert value_in_quote("max_duration_months", 24, "per un periodo massimo di ventiquattro mesi")
    assert value_in_quote("monthly_cap_eur", 1500.0, "nel limite di 1.500 euro mensili")
    assert value_in_quote("hire_from", "2026-01-01", "assunzioni dal 1° gennaio 2026 al 31 dicembre 2026")
    assert not value_in_quote("hire_from", "2026-02-01", "assunzioni dal 1° gennaio 2026 al 31 dicembre 2026")
    assert value_in_quote("zes_regions", ["Puglia", "Sicilia"], "regioni Puglia e Sicilia della ZES unica")
    assert not value_in_quote("zes_regions", ["Lombardia"], "regioni Puglia e Sicilia della ZES unica")
    assert value_in_quote("worker_gender", "donna", "assunzione di donne di qualsiasi età")
    assert not value_in_quote("worker_gender", "donna", "assunzione di giovani lavoratori")
    assert value_in_quote("requires_durc", True, "regolarità contributiva attestata dal DURC")


def test_quote_without_the_value_is_unverified():
    rule = IncentiveRule.model_validate(
        {
            "incentive_id": "x",
            "name": "X",
            "exemption_percent": {
                "value": 90,
                "citation": {"anchor": "1.p1", "quote": "L’esonero è pari al 100 per cento"},
            },
        }
    )
    assert verify(rule, P).exemption_percent.status == "unverified"
