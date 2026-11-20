# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Evaluation harness: grounded Q&A and rule extraction, scored by code.

Q&A metrics (per item, then averaged):
  * fact_recall   — every `must_include` alternative group appears in the answer;
  * citation_hit  — at least one verified citation is a gold (circular, anchor);
  * refusal_ok    — unanswerable questions are refused, answerable ones are not;
  * rejected      — citations the gate threw away (model quoted words not in the paragraph).
Extraction metric: field accuracy against `golden_rules.json`, plus how many
fields survived the citation gate.

Results go to evals/results/*.json so a prompt or model change can be compared
with the previous run before it ships (`--baseline`).
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.rag import Answer, Answerer, answer, anthropic_llm
from radar.rules import IncentiveRule
from radar.store import Store
from radar.text import canon

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"


def _has(text: str, alternatives: str) -> bool:
    """Any alternative occurs as a run of whole words; words of 5+ letters may be a prefix.

    "no" does not match "non" or "anno", "24" does not match "2024", while "esclus"
    still matches "esclusi" / "esclusione".
    """
    words = canon(text).split()
    for alt in alternatives.split("|"):
        toks = canon(alt).split()
        if not toks:
            continue
        for i in range(len(words) - len(toks) + 1):
            if all(
                w == t or (len(t) >= 5 and t.isalpha() and w.startswith(t))
                for w, t in zip(words[i : i + len(toks)], toks, strict=True)
            ):
                return True
    return False


def number_of(doc_label: str) -> str:
    m = re.search(r"n\. (\d+)", doc_label)
    return m.group(1) if m else ""


def score_item(item: dict[str, Any], ans: Answer) -> dict[str, Any]:
    if item.get("unanswerable"):
        return {
            "id": item["id"],
            "split": item.get("split", "dev"),
            "kind": "unanswerable",
            "refusal_ok": not ans.answerable,
            "rejected": ans.rejected_citations,
        }
    facts = all(_has(ans.answer, g) for g in item["must_include"]) if ans.answerable else False
    gold = {(n, a) for n, a in item["gold"]}
    hit = any((number_of(c["doc_label"]), c["anchor"]) in gold for c in ans.citations)
    # Faithfulness checked by code: some cited paragraph itself states every expected fact.
    groups = [
        "|".join(a for a in g.split("|") if a.strip().lower() not in {"no", "si", "sì"}) for g in item["must_include"]
    ]
    groups = [g for g in groups if g]
    supports = bool(groups) and any(all(_has(c.get("paragraph", ""), g) for g in groups) for c in ans.citations)
    return {
        "id": item["id"],
        "split": item.get("split", "dev"),
        "kind": "answerable",
        "refusal_ok": ans.answerable,
        "fact_recall": facts,
        "citation_hit": hit,
        "citation_supports": supports,
        "rejected": ans.rejected_citations,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ans = [r for r in rows if r["kind"] == "answerable"]
    una = [r for r in rows if r["kind"] == "unanswerable"]

    def rate(xs: list[dict[str, Any]], k: str) -> float:
        return round(sum(bool(x[k]) for x in xs) / len(xs), 3) if xs else 0.0

    return {
        "n_answerable": len(ans),
        "n_unanswerable": len(una),
        "fact_recall": rate(ans, "fact_recall"),
        "citation_hit": rate(ans, "citation_hit"),
        "citation_supports": rate(ans, "citation_supports"),
        "answered_when_answerable": rate(ans, "refusal_ok"),
        "refused_when_unanswerable": rate(una, "refusal_ok"),
        "rejected_citations": sum(r["rejected"] for r in rows),
    }


def run_qa(store: Store, llm: Answerer, golden: Path = EVALS / "golden_qa.json", repeats: int = 1) -> dict[str, Any]:
    """Run the golden set ``repeats`` times; the model is not deterministic, so report spread."""
    items = json.loads(golden.read_text(encoding="utf-8"))["items"]
    assert store.retriever is not None, "empty knowledge base"
    runs, usage, t0 = [], {"input_tokens": 0, "output_tokens": 0}, time.perf_counter()
    for _ in range(repeats):
        rows = []
        for it in items:
            a = answer(it["q"], store.passages, store.retriever, llm)
            for k in usage:
                usage[k] += a.usage.get(k, 0)
            row = score_item(it, a)
            row.update(q=it["q"], answer=a.answer, citations=[(c["doc_label"], c["anchor"]) for c in a.citations])
            rows.append(row)
        runs.append(rows)
    per_run = [
        {"all": summarize(r), **{sp: summarize([x for x in r if x["split"] == sp]) for sp in ("dev", "holdout")}}
        for r in runs
    ]
    return {
        "repeats": repeats,
        "summary": aggregate([p["all"] for p in per_run]),
        "by_split": {sp: aggregate([p[sp] for p in per_run]) for sp in ("dev", "holdout")},
        "per_run": per_run,
        "usage": usage,
        "seconds": round(time.perf_counter() - t0, 1),
        "items": runs[-1],
    }


def aggregate(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean of each rate across runs, plus min and max; counts are summed."""
    out: dict[str, Any] = {}
    for k, v in summaries[0].items():
        vals = [s[k] for s in summaries]
        if isinstance(v, float):
            out[k] = round(sum(vals) / len(vals), 3)
            out[k + "_min"], out[k + "_max"] = min(vals), max(vals)
        else:
            out[k] = v if k.startswith("n_") else sum(vals)
    return out


LOWER_IS_BETTER = {"rejected_citations"}


def regressions(base: dict[str, Any], new: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Metrics that got worse vs a baseline result file (QA means, extraction accuracy, rejections)."""
    b = {**base["qa"]["summary"], "field_accuracy": base["extraction"]["field_accuracy"]}
    n = {**new["qa"]["summary"], "field_accuracy": new["extraction"]["field_accuracy"]}
    worse = {}
    for k, v in n.items():
        if k.endswith(("_min", "_max")) or k.startswith("n_") or k not in b:
            continue
        if (k in LOWER_IS_BETTER and v > b[k]) or (k not in LOWER_IS_BETTER and isinstance(v, float) and v < b[k]):
            worse[k] = (b[k], v)
    return worse


def _eq(expected: Any, got: Any) -> bool:
    if isinstance(expected, list):
        return got is not None and {str(x).lower() for x in expected} == {str(x).lower() for x in got}
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return got is not None and float(got) == float(expected)
    return bool(expected == got)


def score_rules(rule_rows: list[dict[str, Any]], golden: Path = EVALS / "golden_rules.json") -> dict[str, Any]:
    spec = json.loads(golden.read_text(encoding="utf-8"))["rules"]
    per, right, total = [], 0, 0
    for g in spec:
        cands: list[IncentiveRule] = [
            r["rule"]
            for r in rule_rows
            if r["number"] == g["circular"] and g["match"] in (r["rule"].incentive_id + r["rule"].name).lower()
        ]
        rule = cands[0] if cands else None
        fields = {}
        for name, exp in g["expect"].items():
            got = getattr(rule, name).value if rule else None
            ok = _eq(exp, got)
            fields[name] = {
                "expected": exp,
                "got": got,
                "ok": ok,
                "status": getattr(rule, name).status if rule else "missing",
            }
            right += ok
            total += 1
        per.append({"circular": g["circular"], "found": rule is not None, "fields": fields})
    return {"field_accuracy": round(right / total, 3) if total else 0.0, "fields": total, "rules": per}


def main() -> None:
    ap = argparse.ArgumentParser(description="Run evals and save a dated result file")
    ap.add_argument("--model", default=None, help="Claude model id for answers")
    ap.add_argument("--baseline", type=Path, help="previous result json to compare against")
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()
    store = Store()
    llm = anthropic_llm(args.model) if args.model else anthropic_llm()
    out: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "qa": run_qa(store, llm, repeats=args.repeats),
        "extraction": score_rules(store.rule_rows),
    }
    (EVALS / "results").mkdir(parents=True, exist_ok=True)
    path = EVALS / "results" / f"eval-{datetime.now(UTC):%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "qa": out["qa"]["summary"],
                "holdout": out["qa"]["by_split"]["holdout"],
                "extraction": out["extraction"]["field_accuracy"],
                "usage": out["qa"]["usage"],
                "file": str(path),
            },
            indent=2,
        )
    )
    if args.baseline:
        worse = regressions(json.loads(args.baseline.read_text(encoding="utf-8")), out)
        print("REGRESSIONS:" if worse else "no regression vs baseline", worse or "")
        if worse:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
