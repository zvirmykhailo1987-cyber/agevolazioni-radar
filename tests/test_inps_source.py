# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from datetime import date

import httpx
import pytest
from conftest import C55_SEL, TS_14_MAY, fake_inps, listing_item

from radar.inps_source import detail_url, is_relevant, listing_url, parse_detail, parse_listing_item

ORIGINAL_PAGE0 = (
    "https://www.inps.it/content/scorporati/search/jcr:content.search."
    "2f636f6e74656e742f64616d2f696e70732d736974652f69742f73636f72706f726174692f636972636f6c6172692d652d6d65737361676769"
    ".30.3130.67696f726e6f.44455343.636972636f6c6172692d652d6d65737361676769.695f.json"
)


def test_listing_url_matches_the_site_front_end():
    assert listing_url(0) == ORIGINAL_PAGE0
    assert ".31.3130." in listing_url(1) and ".3132.3130." in listing_url(12)


def test_publication_date_uses_rome_time_not_utc():
    item = parse_listing_item(listing_item(C55_SEL, "Bonus%20Giovani", TS_14_MAY))
    assert item["published_on"] == "2026-05-14"  # UTC would give 2026-05-13
    assert item["doc_id"] == 15258 and item["subject"] == "Bonus Giovani"
    assert item["url"].endswith(C55_SEL + ".html")


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Bonus Giovani 2026. Prime indicazioni", True),
        ("Esonero contributivo per le assunzioni nella ZES unica", True),
        ("Incentivo all'occupazione femminile", True),
        ("Istruzioni operative per la ZES unica", True),
        ("Convenzione per la riscossione dei contributi sindacali", False),
        ("Cassa integrazione: istruzioni operative", False),
    ],
)
def test_relevance_filter(subject, expected):
    assert is_relevant(subject) is expected


def test_parse_detail(c55_detail):
    d = parse_detail(C55_SEL, c55_detail)
    assert d["doc_id"] == 15258 and d["number"] == "55" and d["doc_type"] == "Circolare"
    assert "Bonus Giovani 2026" in d["body_html"] or "Premessa" in d["body_html"]
    assert d["attachments"][0].startswith("https://www.inps.it/content/dam/") and d["content_hash"]
    assert detail_url(C55_SEL).endswith(f"dettaglio.content-fragment-detail.{C55_SEL}.json")


def test_listing_stops_at_cutoff_and_pages_forward():
    newer = [listing_item(f"a.b_{i}", "x", TS_14_MAY + 86400000 * (20 - i)) for i in range(10)]
    older = [listing_item("a.b_99", "y", TS_14_MAY + 86400000), listing_item("a.b_98", "z", TS_14_MAY - 86400000 * 30)]
    client, calls = fake_inps([newer, older, [listing_item("never", "q", TS_14_MAY)]], {})
    items = list(client.iter_listing(date(2026, 5, 1)))
    assert [i["doc_id"] for i in items] == [*list(range(10)), 99]
    assert len(calls) == 2, "must not fetch the page after the cut-off"


def test_empty_page_ends_listing():
    client, calls = fake_inps([], {})
    assert list(client.iter_listing(date(2026, 1, 1))) == [] and len(calls) == 1


def test_retries_transient_errors_then_succeeds(c55_detail):
    client, calls = fake_inps([], {C55_SEL: c55_detail}, fail_first=2)
    assert client.fetch_detail(C55_SEL)["doc_id"] == 15258
    assert len(calls) == 3


def test_gives_up_after_three_transient_errors():
    client, _ = fake_inps([], {}, fail_first=10)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_json(listing_url(0))


def test_client_errors_are_not_retried():
    client, calls = fake_inps([], {})
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_detail("missing_1")
    assert len(calls) == 1


def test_polite_delay_between_requests():
    slept = []
    client, _ = fake_inps([], {})
    client.sleep, client.delay_s = slept.append, 1.5
    client.get_json(listing_url(0))
    client.get_json(listing_url(1))
    assert slept == [1.5]


@pytest.mark.parametrize(
    "subject",
    [
        "Agevolazioni per la frequenza di asili nido pubblici e privati",
        "Contributo per sostenere le spese relative alle sessioni di psicoterapia",
        "Gestioni artigiani ed esercenti attività commerciali. Esonero parziale",
        "Bonus nuovi nati di cui all'articolo 1, commi da 206 a 208",
    ],
)
def test_family_and_self_employed_benefits_are_out_of_scope(subject):
    assert is_relevant(subject) is False
