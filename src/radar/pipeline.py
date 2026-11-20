# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Load INPS acts into DuckDB with dlt (the `raw` layer that dbt builds on)."""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path
from typing import Any

import dlt

from radar.inps_source import InpsClient, inps_source

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("RADAR_DB", ROOT / "data" / "radar.duckdb"))


def run(
    since: str = "2026-01-01",
    db_path: Path = DB_PATH,
    client: InpsClient | None = None,
    full_refresh: bool = False,
    today: date | None = None,
    lookback_days: int = 120,
) -> Any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline = dlt.pipeline(
        pipeline_name="inps_radar",
        destination=dlt.destinations.duckdb(str(db_path)),
        dataset_name="raw",
        pipelines_dir=str(db_path.parent / ".dlt"),
    )
    # full_refresh drops this source's tables and incremental state, then reloads.
    source = inps_source(since=since, client=client, lookback_days=lookback_days, today=today)
    if full_refresh:
        return pipeline.run(source, refresh="drop_sources")
    return pipeline.run(source)


def main() -> None:
    ap = argparse.ArgumentParser(description="Load INPS circulars/messages into DuckDB")
    ap.add_argument("--since", default="2026-01-01", help="first publication date (YYYY-MM-DD)")
    ap.add_argument("--full-refresh", action="store_true", help="drop loaded tables and state, reload")
    args = ap.parse_args()
    print(run(since=args.since, full_refresh=args.full_refresh))


if __name__ == "__main__":
    main()
