# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from radar.consolidate import consolidate
from radar.rules import IncentiveRule


def cited(v, anchor="1.p1"):
    return {"value": v, "citation": {"anchor": anchor, "quote": "q"}, "status": "verified"}


def row(number, day, doc_type=None, **fields):
    rule = IncentiveRule.model_validate({"incentive_id": "bonus-x", "name": f"Bonus X ({number})", **fields})
    return {
        "rule": rule,
        "doc_type": doc_type or ("Circolare" if number < 100 else "Messaggio"),
        "number": str(number),
        "published_on": day,
        "url": f"u{number}",
    }


def test_later_act_fills_gaps_but_does_not_override():
    rows = [
        row(
            1966,
            "2026-06-11",
            monthly_cap_eur=cited(999),
            application_procedure=cited("Portale, SPID"),
            contract_types=cited(["tempo_indeterminato"]),
            requires_durc=cited(True),
        ),
        row(
            55,
            "2026-05-14",
            monthly_cap_eur=cited(500),
            contract_types=cited(["tempo_indeterminato", "somministrazione"]),
            application_procedure=cited("Portale"),
        ),
    ]
    m = consolidate(rows)["bonus-x"]
    assert m.primary["number"] == "55" and [s["number"] for s in m.sources] == ["55", "1966"]
    assert m.rule.monthly_cap_eur.value == 500, "the introducing act keeps its value"
    assert m.rule.requires_durc.value is True and m.field_sources["requires_durc"]["number"] == "1966"
    fields = {d["field"] for d in m.differences}
    assert fields == {"monthly_cap_eur", "contract_types"}, "free text is not compared"
    assert m.coverage["verified"] == 4


def test_same_list_in_other_order_is_not_a_difference():
    rows = [
        row(55, "2026-05-14", contract_types=cited(["apprendistato", "tempo_indeterminato"])),
        row(1966, "2026-06-11", contract_types=cited(["tempo_indeterminato", "apprendistato"])),
    ]
    assert consolidate(rows)["bonus-x"].differences == []


def test_unverified_values_never_fill_or_differ():
    rows = [row(55, "2026-05-14"), row(1966, "2026-06-11", monthly_cap_eur={"value": None, "status": "unverified"})]
    m = consolidate(rows)["bonus-x"]
    assert m.rule.monthly_cap_eur.status == "absent" and m.differences == []


def test_circular_is_primary_over_same_day_message_with_lower_number():
    rows = [
        row(3, "2026-05-14", "Messaggio", monthly_cap_eur=cited(999)),
        row(56, "2026-05-14", "Circolare", monthly_cap_eur=cited(650)),
    ]
    m = consolidate(rows)["bonus-x"]
    assert m.primary["number"] == "56" and m.rule.monthly_cap_eur.value == 650, "circular introduces, message operates"


def test_conditions_from_every_act_are_united_not_compared():
    a = {
        "value": [{"text": "nessun licenziamento", "applies_to": "datore"}],
        "status": "verified",
        "citation": {"anchor": "1.p1", "quote": "q"},
    }
    b = {
        "value": [
            {"text": "Nessun licenziamento", "applies_to": "datore"},
            {"text": "madre di tre figli", "applies_to": "lavoratore"},
        ],
        "status": "verified",
        "citation": {"anchor": "1.p1", "quote": "q"},
    }
    m = consolidate([row(55, "2026-05-14", other_conditions=a), row(1966, "2026-06-11", other_conditions=b)])["bonus-x"]
    assert [c.text for c in m.rule.other_conditions.value] == ["nessun licenziamento", "madre di tre figli"]
    assert m.differences == []
