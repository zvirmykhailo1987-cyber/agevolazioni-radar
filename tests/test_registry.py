# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from datetime import date

import pytest
from conftest import GOOD_RULE

from radar.registry import Registry
from radar.rules import IncentiveRule
from radar.store import status_of


def test_shipped_registry_is_consistent():
    reg = Registry.load()
    assert reg.canonical("bonus-giovani-art22-c1") == reg.canonical("bonus-giovani-2024") == "bonus-giovani-dl60-2024"
    assert reg.canonical("something-new") is None


def write(tmp_path, body):
    p = tmp_path / "r.yaml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "body,msg",
    [
        ("incentives:\n  - {id: a, name: A, group: x, side: datore}\n", "unknown group"),
        (
            "incentives:\n  - {id: a, name: A, group: giovani, side: datore}\n  - {id: a, name: B, group: giovani, side: datore}\n",
            "duplicate",
        ),
        (
            "incentives:\n  - {id: a, name: A, group: giovani, side: datore, aliases: [x]}\n  - {id: b, name: B, group: giovani, side: datore, aliases: [x]}\n",
            "two incentives",
        ),
    ],
)
def test_registry_rejects_ambiguous_files(tmp_path, body, msg):
    with pytest.raises(ValueError, match=msg):
        Registry.load(write(tmp_path, body))


def test_apply_renames_mapped_and_reports_unmapped(tmp_path):
    reg = Registry.load(
        write(
            tmp_path,
            "incentives:\n  - {id: bg, name: Bonus G, group: giovani, side: datore, aliases: [bonus-giovani-2026]}\n",
        )
    )
    rule = IncentiveRule.model_validate(GOOD_RULE)
    other = rule.model_copy(update={"incentive_id": "mystery"})
    mapped, unmapped = reg.apply([{"rule": rule, "number": "55"}, {"rule": other, "number": "9"}])
    assert mapped[0]["rule"].incentive_id == "bg" and mapped[0]["rule"].name == "Bonus G"
    assert mapped[0]["model_incentive_id"] == "bonus-giovani-2026" and unmapped[0]["number"] == "9"


@pytest.mark.parametrize(
    "lo,hi,expected",
    [
        ("2026-01-01", "2026-12-31", "attiva"),
        ("2025-01-01", "2025-12-31", "scaduta"),
        ("2027-01-01", None, "futura"),
        (None, None, "da verificare"),
    ],
)
def test_status_from_hiring_window(lo, hi, expected):
    assert status_of(lo, hi, today=date(2026, 6, 1)) == expected
