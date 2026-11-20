# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Scale simulation against the real knowledge base (no API calls).

Generates a synthetic company with N hires (seeded, reproducible), runs Recupero, checks the
invariants the product promises on every finding, times the scan, and drives the web app with
concurrent readers and consultants. Writes docs/simulation.json.

    python scripts/simulate.py --hires 2000 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from radar.appstate import AppState  # noqa: E402
from radar.recupero import Company, Hire, Workforce, scan  # noqa: E402
from radar.store import Store  # noqa: E402
from radar.web import create_app  # noqa: E402

REGIONS = ["Puglia", "Sicilia", "Campania", "Calabria", "Sardegna", "Lombardia", "Lazio", "Veneto", "Marche", "Umbria"]
CONTRACTS = ["tempo_indeterminato"] * 6 + ["apprendistato", "tempo_determinato", "somministrazione"]


def workforce(n: int, seed: int) -> Workforce:
    rnd = random.Random(seed)

    def maybe(value: object, p_missing: float = 0.15) -> object:
        return None if rnd.random() < p_missing else value

    hires = []
    for i in range(n):
        hire_day = date(2025, 1, 1) + timedelta(days=rnd.randrange(0, 730))
        age = rnd.randrange(18, 62)
        hires.append(
            Hire(
                id=f"H{i:05d}",
                full_name=f"Persona {i}",
                role="ruolo",
                birth_date=maybe(hire_day - timedelta(days=int(age * 365.25) + rnd.randrange(0, 365))),
                gender=maybe(rnd.choice(["F", "M"])),
                hire_date=hire_day,
                contract_type=maybe(rnd.choice(CONTRACTS)),
                workplace_region=maybe(rnd.choice(REGIONS)),
                months_without_regular_job=maybe(rnd.choice([0, 3, 6, 12, 24, 30, 48])),
                gross_monthly_eur=maybe(round(rnd.uniform(900, 4500), 2), 0.05),
                net_job_increase=maybe(rnd.random() < 0.8),
                job_level=maybe(rnd.choice(["operaio", "impiegato", "impiegato", "quadro", "dirigente"])),
                disadvantage=maybe(rnd.choice(["nessuno", "nessuno", "svantaggiato", "molto_svantaggiato"])),
            )
        )
    company = Company(id="sim", name="Simulazione Srl", employees=40, has_durc=True, applies_ccnl=True)
    return Workforce(company=company, hires=hires)


def check_invariants(rep, store: Store) -> list[str]:  # type: ignore[no-untyped-def]
    problems = []
    by_hire: dict[str, list] = {}  # type: ignore[type-arg]
    for f in rep.findings:
        by_hire.setdefault(f.hire_id, []).append(f)
        window = next(c for c in f.assessment["checks"] if c["criterion"] == "hire_window")
        if window["outcome"] != "pass":
            problems.append(f"{f.finding_id}: proposed outside a confirmed hiring window")
        if f.verdict == "not_eligible":
            problems.append(f"{f.finding_id}: not_eligible finding kept")
        cap = f.assessment.get("monthly_cap")
        if f.monthly_saving_eur is not None and cap is not None and f.monthly_saving_eur > cap + 1e-9:
            problems.append(f"{f.finding_id}: monthly {f.monthly_saving_eur} above cap {cap}")
        if f.verdict == "likely_eligible" and f.missing:
            problems.append(f"{f.finding_id}: likely with unknown criteria {f.missing}")
    for hire, fs in by_hire.items():
        if sum(f.proposed for f in fs) != 1:
            problems.append(f"{hire}: {sum(f.proposed for f in fs)} proposals")
    t = rep.totals()
    total = sum(f.total_saving_eur or 0 for f in rep.proposed)
    if abs(t["likely_eur"] + t["to_check_eur"] - total) > 0.01:
        problems.append("totals do not add up")
    rejected = frozenset(f.finding_id for f in rep.proposed[: len(rep.proposed) // 2])
    t2 = rep.totals(rejected)
    if t2["rejected"] != len(rejected) or t2["likely_eur"] + t2["to_check_eur"] > total + 0.01:
        problems.append("rejections not reflected in totals")
    return problems


def web_load(store: Store, readers: int, requests_each: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(store, llm=None, state=AppState(Path(tmp) / "sim.db"))
        latencies: list[float] = []
        statuses: Counter[int] = Counter()
        lock = threading.Lock()
        urls = ["/", "/catalogo", "/incentivo/bonus-giovani-2026", "/recupero/trattoria-puglia", "/stato"]

        def reader(k: int) -> None:
            with TestClient(app) as c:
                for i in range(requests_each):
                    t0 = time.perf_counter()
                    s = c.get(urls[(k + i) % len(urls)]).status_code
                    with lock:
                        latencies.append((time.perf_counter() - t0) * 1000)
                        statuses[s] += 1

        threads = [threading.Thread(target=reader, args=(k,)) for k in range(readers)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
    lat = sorted(latencies)
    return {
        "concurrent_clients": readers,
        "requests": len(lat),
        "status_codes": dict(statuses),
        "p50_ms": round(statistics.median(lat), 1),
        "p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1),
        "throughput_rps": round(len(lat) / wall, 1),
        "note": "in-process ASGI client on a laptop; measures the app, not the network",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hires", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--readers", type=int, default=16)
    ap.add_argument("--out", default=str(ROOT / "docs" / "simulation.json"))
    args = ap.parse_args()

    store = Store()
    if not store.merged:
        raise SystemExit("No knowledge base: build data/radar.duckdb first.")
    wf = workforce(args.hires, args.seed)
    t0 = time.perf_counter()
    rep = scan(store, wf)
    scan_s = time.perf_counter() - t0
    problems = check_invariants(rep, store)
    verdicts = Counter(f.verdict for f in rep.proposed)
    reasons = Counter(m for f in rep.proposed for m in f.missing)
    out = {
        "measured": date.today().isoformat(),
        "seed": args.seed,
        "hires": args.hires,
        "incentives_checked": rep.incentives_checked,
        "scan_seconds": round(scan_s, 2),
        "hires_per_second": round(args.hires / scan_s, 1),
        "findings": len(rep.findings),
        "hires_with_proposal": len({f.hire_id for f in rep.proposed}),
        "proposals_by_verdict": dict(verdicts),
        "top_reasons_to_check": dict(reasons.most_common(6)),
        "totals_eur": {k: v for k, v in rep.totals().items() if k.endswith("_eur")},
        "invariant_violations": problems[:20],
        "invariant_violations_count": len(problems),
        "web": web_load(store, args.readers, 25),
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if problems or any(code >= 500 for code in out["web"]["status_codes"]):  # type: ignore[union-attr]
        raise SystemExit(1)


if __name__ == "__main__":
    main()
