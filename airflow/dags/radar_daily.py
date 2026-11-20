# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Daily refresh: load INPS -> dbt freshness + build -> KB paragraphs -> extraction (cached) -> eval alert.

Each task is the same CLI a developer runs locally, so the DAG holds no logic of its own.
Extraction is cached by (text hash, prompt version, model): on a quiet day it costs nothing.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator

RADAR_HOME = os.environ.get("RADAR_HOME", "/opt/agevolazioni-radar")  # checkout with dbt/ and evals/
DEFAULT_ARGS = {"owner": "data", "retries": 2, "retry_delay": timedelta(minutes=10), "cwd": RADAR_HOME}

with DAG(
    dag_id="agevolazioni_radar_daily",
    description="INPS incentives knowledge base refresh",
    schedule="0 6 * * 1-5",  # INPS publishes on working days
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Rome"),  # cron is read in this timezone
    catchup=False,
    default_args=DEFAULT_ARGS,
    tags=["knowledge-base", "inps"],
) as dag:
    load = BashOperator(task_id="dlt_load", bash_command="radar-load --since 2026-01-01")
    freshness = BashOperator(
        task_id="dbt_source_freshness", bash_command="dbt source freshness --project-dir dbt --profiles-dir dbt"
    )
    build = BashOperator(task_id="dbt_build", bash_command="dbt build --project-dir dbt --profiles-dir dbt")
    paragraphs = BashOperator(task_id="kb_paragraphs", bash_command="radar-kb paragraphs")
    extract = BashOperator(task_id="kb_extract", bash_command="radar-kb extract")
    # An alert, not a gate: the knowledge base is already written when this runs. A failing run
    # pages the owner; ungrounded values never reach users anyway because the citation gate drops them.
    evals = BashOperator(
        task_id="evals_alert", bash_command="radar-evals --repeats 1 --baseline evals/results/baseline.json"
    )

    load >> freshness >> build >> paragraphs >> extract >> evals
