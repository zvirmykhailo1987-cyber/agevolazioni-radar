# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""dlt source for INPS circulars and messages (public, no login).

INPS publishes every circular/message as an AEM content fragment. Two public
JSON endpoints back the website:

* a paged listing (newest first, 10 per page) with number, date and subject;
* a detail document with the full text as percent-encoded HTML.

The source walks the listing back to a cut-off date, keeps only documents whose
subject mentions hiring incentives, and loads their full text. It is polite by
design: one request at a time, a fixed delay, a descriptive User-Agent, and an
incremental cursor so a daily run touches only the first page or two.
robots.txt (checked 2026-11-20) allows these paths.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import dlt
import httpx

from radar.text import content_hash, decode_field, normalize_space

BASE = "https://www.inps.it"
_FOLDER = "/content/dam/inps-site/it/scorporati/circolari-e-messaggi"
_KIND = "circolari-e-messaggi"
ROME = ZoneInfo("Europe/Rome")  # listing timestamps are midnight, Rome time
USER_AGENT = "agevolazioni-radar/0.1 (+https://github.com/zvirmykhailo1987-cyber/agevolazioni-radar)"

# Subjects that concern employer incentives. Deliberately broad: a false
# positive costs one extra request, a false negative hides an incentive.
RELEVANT = re.compile(
    r"esoner|incentiv|bonus|agevolaz|decontribu|sgravi|riduzion\w* contribut|"
    r"\bZES\b|zona economica speciale|under\s?3[05]|occupazione (giovanile|femminile|stabile)",
    re.IGNORECASE,
)


def _hex(s: str) -> str:
    return s.encode("utf-8").hex()


def listing_url(page: int) -> str:
    """URL of one listing page (selectors are hex-encoded by the INPS front end)."""
    sel = ".".join([_hex(_FOLDER), _hex(str(page)), _hex("10"), _hex("giorno"), _hex("DESC"), _hex(_KIND), _hex("i_")])
    return f"{BASE}/content/scorporati/search/jcr:content.search.{sel}.json"


def detail_url(selectors: str) -> str:
    return (
        f"{BASE}/it/it/inps-comunica/atti/circolari-messaggi-e-normativa/"
        f"dettaglio.content-fragment-detail.{selectors}.json"
    )


def page_url(selectors: str) -> str:
    return f"{BASE}/it/it/inps-comunica/atti/circolari-messaggi-e-normativa/dettaglio.{selectors}.html"


# Scope: contribution measures a private employer applies through payroll. Benefits paid to
# families or to self-employed contributors are out of scope even when called "bonus".
OUT_OF_SCOPE = re.compile(
    r"asili? nido|psicoterap|bonus psicologo|nuovi nati|una tantum di 1\.000|"
    r"artigiani|esercenti attivit|coltivatori diretti|gestioni? special|iscritti per la prima volta|"
    r"INPS Mobile|esodo|autoimprenditorial|attivit\w* imprenditorial|Poli ultraregionali|"
    r"sentenz|Regolamento \(UE\) 2022/2472",
    re.IGNORECASE,
)


def is_relevant(subject: str) -> bool:
    text = normalize_space(unquote(subject or ""))
    return bool(RELEVANT.search(text)) and not OUT_OF_SCOPE.search(text)


def parse_listing_item(item: dict[str, Any]) -> dict[str, Any]:
    ts = int(item["dataPubblicazioneTimestamp"])
    sel = item["selectors"]
    return {
        "doc_id": int(sel.rsplit("_", 1)[1]),
        "selectors": sel,
        "doc_type": item.get("tipo", ""),
        "number": str(item.get("numero", "")),
        "published_ts": ts,
        "published_on": datetime.fromtimestamp(ts / 1000, ROME).date().isoformat(),
        "subject": normalize_space(unquote(item.get("oggetto", ""))),
        "url": page_url(sel),
    }


def parse_detail(selectors: str, payload: dict[str, Any]) -> dict[str, Any]:
    def val(key: str) -> Any:
        node = payload.get(key) or {}
        return node.get("value") if isinstance(node, dict) else None

    body = decode_field(val("testoCompleto"))
    pdfs = [a.get("path", "") for a in (payload.get("allegati") or {}).get("value") or []]
    return {
        "doc_id": int(val("id") or selectors.rsplit("_", 1)[1]),
        "selectors": selectors,
        "doc_type": unquote(val("tipo") or ""),
        "number": str(val("numero") or ""),
        "subject_html": decode_field(val("oggetto")),
        "summary_html": decode_field(val("sommario")),
        "body_html": body,
        "senders": normalize_space(decode_field(val("mittenti")).replace("<br/>", " ")),
        "attachments": [BASE + p for p in pdfs if p],
        "url": page_url(selectors),
        "content_hash": content_hash(body),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


class InpsClient:
    """Tiny polite HTTP client; ``sleep`` is injectable so tests run instantly."""

    def __init__(
        self,
        http: httpx.Client | None = None,
        delay_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = http or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
        )
        self.delay_s = delay_s
        self.sleep = sleep
        self.requests = 0

    def get_json(self, url: str) -> dict[str, Any]:
        if self.requests:
            self.sleep(self.delay_s)
        self.requests += 1
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self.http.get(url)
                if r.status_code in (429, 502, 503, 504):
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                data: dict[str, Any] = r.json()
                return data
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status < 500 and status != 429:
                    raise
                last = exc
                self.sleep(self.delay_s * (2 ** (attempt + 1)))
        assert last is not None
        raise last

    def iter_listing(self, since: date, max_pages: int = 400) -> Iterator[dict[str, Any]]:
        """Newest-first listing items published on or after ``since``."""
        for page in range(max_pages):
            data = self.get_json(listing_url(page)).get("data") or {}
            results = data.get("results") or []
            if not results:
                return
            for raw in results:
                item = parse_listing_item(raw)
                if item["published_on"] < since.isoformat():
                    return
                yield item

    def fetch_detail(self, selectors: str) -> dict[str, Any]:
        return parse_detail(selectors, self.get_json(detail_url(selectors)))


@dlt.source(name="inps")
def inps_source(
    since: str = "2026-01-01",
    client: InpsClient | None = None,
    lookback_days: int = 120,
    today: date | None = None,
) -> Any:
    """Listing of acts + full text of incentive-related ones, with version detection.

    INPS can correct an act after publication, so every run re-reads the last
    ``lookback_days`` of the listing (never earlier than ``since``) and re-fetches
    the text of relevant acts. A document row is written only when its text hash
    is new for that act: ``documents`` is append-only, one row per version, and
    ``first_seen_at`` is the moment that version was first loaded.
    """
    c = client or InpsClient()

    @dlt.resource(name="acts", primary_key="doc_id", write_disposition="merge")
    def acts() -> Iterator[dict[str, Any]]:
        window = (today or date.today()) - timedelta(days=lookback_days)
        start = (
            max(date.fromisoformat(since), window)
            if dlt.current.source_state().get("loaded")
            else (date.fromisoformat(since))
        )
        for item in c.iter_listing(start):
            item["relevant"] = is_relevant(item["subject"])
            yield item
        dlt.current.source_state()["loaded"] = True

    @dlt.transformer(data_from=acts, name="documents", write_disposition="append")
    def documents(item: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if not item["relevant"]:
            return
        seen: dict[str, list[str]] = dlt.current.resource_state().setdefault("seen_hashes", {})
        doc = c.fetch_detail(item["selectors"])
        hashes = seen.setdefault(str(doc["doc_id"]), [])
        if doc["content_hash"] in hashes:
            return
        hashes.append(doc["content_hash"])
        doc["published_on"] = item["published_on"]
        doc["subject"] = item["subject"]
        doc["first_seen_at"] = doc.pop("fetched_at")
        yield doc

    return acts, documents
