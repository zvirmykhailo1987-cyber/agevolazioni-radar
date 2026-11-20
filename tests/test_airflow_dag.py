# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""The DAG is wiring only; check its order and commands without installing Airflow."""

import importlib.util
import sys
import types
from pathlib import Path

DAG_FILE = Path(__file__).resolve().parents[1] / "airflow" / "dags" / "radar_daily.py"


class FakeDAG:
    current = None

    def __init__(self, dag_id, **kw):
        self.dag_id, self.kw, self.tasks = dag_id, kw, []

    def __enter__(self):
        FakeDAG.current = self
        return self

    def __exit__(self, *a):
        FakeDAG.current = None


class FakeOp:
    def __init__(self, task_id, bash_command):
        self.task_id, self.cmd, self.downstream = task_id, bash_command, []
        self.default_args = FakeDAG.current.kw["default_args"]
        FakeDAG.current.tasks.append(self)

    def __rshift__(self, other):
        self.downstream.append(other)
        return other


def test_dag_runs_the_cli_steps_in_order(monkeypatch):
    airflow = types.ModuleType("airflow")
    airflow.DAG = FakeDAG
    ops = types.ModuleType("airflow.providers.standard.operators.bash")
    ops.BashOperator = FakeOp
    for name in ("airflow.providers", "airflow.providers.standard", "airflow.providers.standard.operators"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    pendulum = types.ModuleType("pendulum")
    pendulum.datetime = lambda *a, **k: ("datetime", a, k)
    monkeypatch.setitem(sys.modules, "pendulum", pendulum)
    monkeypatch.setitem(sys.modules, "airflow.providers.standard.operators.bash", ops)
    spec = importlib.util.spec_from_file_location("radar_daily", DAG_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dag = mod.dag
    order, t = [], dag.tasks[0]
    while t:
        order.append(t.task_id)
        t = t.downstream[0] if t.downstream else None
    assert order == ["dlt_load", "dbt_source_freshness", "dbt_build", "kb_paragraphs", "kb_extract", "evals_alert"]
    assert dag.kw["catchup"] is False and "1-5" in dag.kw["schedule"]
    assert dag.kw["start_date"][2] == {"tz": "Europe/Rome"}, "cron must be read in Rome time"
    assert dag.kw["default_args"]["cwd"], "relative dbt/ and evals/ paths need a working directory"
    assert all(op.cmd.split()[0] in {"radar-load", "dbt", "radar-kb", "radar-evals"} for op in dag.tasks)
