# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Break the code on purpose and check that the tests notice. Writes docs/mutation-results.json.

Coverage says a line ran. This says a test fails when the line is wrong. Each mutation removes one
guarantee from a temporary copy of the project, and the whole suite runs against that copy. A mutation
nothing catches is reported as "survived", not hidden: the fix is a missing test, never deleting the
mutation.

    python scripts/mutation_check.py           # all mutations (~1 suite run each, stops at the first failure)
    python scripts/mutation_check.py --check   # only verify every mutation still applies to the code
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess  # nosec B404 — runs this repo's own test suite only
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
SKIP = {".venv", "data", ".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "docs", ".coverage"}

MUTATIONS = [
    (
        "Citation gate accepts a value whose quote is not in the paragraph",
        "src/radar/rules.py",
        'and quote_found(cit["quote"], by_anchor.get(cit["anchor"], ""))',
        "and True",
    ),
    (
        "Citation gate accepts a quote that does not state the value",
        "src/radar/rules.py",
        'and value_in_quote(name, field["value"], cit["quote"])',
        "and True",
    ),
    (
        "Quotes shorter than four words accepted",
        "src/radar/text.py",
        "return len(q.split()) >= min_words and",
        "return bool(q) and",
    ),
    (
        "Quote matched inside words (1 inside 10)",
        "src/radar/text.py",
        'and f" {q} " in f" {canon(source)} "',
        "and q in canon(source)",
    ),
    (
        "Answer keeps citations the gate could not verify",
        "src/radar/rag.py",
        'if p and quote_found(str(c.get("quote", "")), p.text):',
        "if p:",
    ),
    (
        "Invented number next to a real quote is shown",
        "src/radar/rag.py",
        "and bool(good) and not unsupported",
        "and bool(good)",
    ),
    (
        "Answer is shown even with no verified citation",
        "src/radar/rag.py",
        'ok = bool(raw.get("answerable")) and bool(good)',
        'ok = bool(raw.get("answerable")) or bool(good)',
    ),
    (
        "Worker who turned 35 on the hiring day passes the under-35 check",
        "src/radar/eligibility.py",
        '"pass" if a < max_age.value else "fail"',
        '"pass" if a <= max_age.value else "fail"',
    ),
    (
        "Missing unemployment data counts as 'no'",
        "src/radar/eligibility.py",
        'add("months_without_regular_job", "unknown",',
        'add("months_without_regular_job", "fail",',
    ),
    (
        "Unknown criteria are reported as eligible",
        "src/radar/eligibility.py",
        '("needs_review" if "unknown" in outcomes',
        '("likely_eligible" if "unknown" in outcomes',
    ),
    (
        "Ungrounded condition treated as passed in the pre-check",
        "src/radar/eligibility.py",
        'add(name, "unknown", "valore scartato dal controllo delle citazioni: verificare nel testo")',
        "pass",
    ),
    (
        "ZES cap ignored for hires in a ZES region",
        "src/radar/eligibility.py",
        "if in_zes and rule.monthly_cap_eur_zes.value",
        "if False and rule.monthly_cap_eur_zes.value",
    ),
    (
        "Publication dates computed in UTC (one day early)",
        "src/radar/inps_source.py",
        "datetime.fromtimestamp(ts / 1000, ROME).date()",
        "datetime.fromtimestamp(ts / 1000, UTC).date()",
    ),
    (
        "Listing keeps paging past the cut-off date",
        "src/radar/inps_source.py",
        'if item["published_on"] < since.isoformat():',
        "if False:",
    ),
    ("Relevance filter forgets ZES acts", "src/radar/inps_source.py", r'r"\bZES\b|', 'r"'),
    (
        "Client errors (404) are retried like outages",
        "src/radar/inps_source.py",
        "if status is not None and status < 500 and status != 429:",
        "if False:",
    ),
    (
        "Polite delay between requests removed",
        "src/radar/inps_source.py",
        "        if self.requests:",
        "        if False:",
    ),
    (
        "Unchanged text stored again as a new version",
        "src/radar/inps_source.py",
        'if doc["content_hash"] in hashes:',
        "if False:",
    ),
    (
        "Table of contents parsed as content",
        "src/radar/text.py",
        '(text.upper() == "INDICE" or m)',
        '(text.upper() == "INDICE")',
    ),
    (
        "Extraction cache ignored (pays the API again on every run)",
        "src/radar/kb.py",
        "        if hit:",
        "        if False:",
    ),
    (
        "Same-day message outranks the circular that introduced the incentive",
        "src/radar/consolidate.py",
        '0 if str(row["doc_type"]).startswith("Circ") else 1',
        "0",
    ),
    ("Per-IP limit on paid questions removed", "src/radar/web.py", "if len(q) >= self.per_ip:", "if False:"),
    ("Per-IP daily cap removed", "src/radar/web.py", "if self.day_hits[ip] >= self.per_ip_daily:", "if False:"),
    (
        "Content-Security-Policy header dropped",
        "src/radar/web.py",
        'resp.headers["Content-Security-Policy"] = (',
        'resp.headers["X-Unused"] = (',
    ),
    (
        "CSRF check skipped when deciding a Recupero finding",
        "src/radar/web.py",
        'if not hmac.compare_digest(csrf.encode(), user["csrf"].encode()):\n            raise HTTPException(403, "Token CSRF non valido")\n        _, rep',
        'if False:\n            raise HTTPException(403, "Token CSRF non valido")\n        _, rep',
    ),
    (
        "CSRF check skipped when resolving a difference",
        "src/radar/web.py",
        'if not hmac.compare_digest(csrf.encode(), user["csrf"].encode()):\n            raise HTTPException(403, "Token CSRF non valido")\n        if demo_mode:',
        'if False:\n            raise HTTPException(403, "Token CSRF non valido")\n        if demo_mode:',
    ),
    (
        "Any password accepted",
        "src/radar/appstate.py",
        "return hmac.compare_digest(candidate, digest_hex)",
        "return True",
    ),
    (
        "Expired sessions still valid",
        "src/radar/appstate.py",
        'if row is None or row["expires_at"] < _now().isoformat():',
        "if row is None:",
    ),
    (
        "Recupero proposes incentives whose hiring window is unknown",
        "src/radar/recupero.py",
        'if a.verdict == "not_eligible" or window != "pass":',
        'if a.verdict == "not_eligible":',
    ),
    (
        "Saving not capped at the monthly limit",
        "src/radar/recupero.py",
        "min(gross * rate * exemption_pct / 100.0, a.monthly_cap)",
        "gross * rate * exemption_pct / 100.0",
    ),
    (
        "Executives pass the non-executive condition",
        "src/radar/eligibility.py",
        'return ("pass" if hire.job_level != "dirigente" else "fail"',
        'return ("pass"',
    ),
    (
        "Later acts' conditions dropped instead of united",
        "src/radar/consolidate.py",
        'if f in UNION and old.status == "verified":',
        "if f in UNION and False:",
    ),
    (
        "Disadvantage resolver accepts extra clauses (age, region, programme)",
        "src/radar/eligibility.py",
        "if m := _SVANTAGGIO.fullmatch(t):",
        "if m := _SVANTAGGIO.match(t):",
    ),
    (
        "Contract exclusions read from a fixed word list instead of the text",
        "src/radar/eligibility.py",
        "        bad = hire.contract_type in excluded",
        '        bad = hire.contract_type in {"apprendistato", "lavoro_domestico", "lavoro_intermittente"}',
    ),
    (
        "Conditions dropped by the citation gate are ignored",
        "src/radar/eligibility.py",
        'descriptive = name in FREE_TEXT_FIELDS and name != "other_conditions"',
        "descriptive = name in FREE_TEXT_FIELDS",
    ),
    (
        "A contested field still gives a hard no",
        "src/radar/eligibility.py",
        'if outcome == "fail" and contested & set(CRITERION_FIELDS.get(criterion, ())):',
        "if False:",
    ),
    (
        "Daily extraction cap counts cached acts (new acts never extracted)",
        "src/radar/kb.py",
        'if limit is not None and stats["extracted"] + stats["failed"] >= limit:',
        'if limit is not None and stats["extracted"] + stats["failed"] + stats["cached"] >= limit:',
    ),
    (
        "One failed model call aborts the whole refresh",
        "src/radar/kb.py",
        "        except Exception as exc:  # network, rate limit, malformed output: retry tomorrow, keep going",
        "        except ZeroDivisionError as exc:",
    ),
    (
        "One malformed INPS listing row aborts the load",
        "src/radar/inps_source.py",
        "except (KeyError, ValueError, IndexError, TypeError):",
        "except ZeroDivisionError:",
    ),
    (
        "Operator password ignored once the demo user exists",
        "src/radar/appstate.py",
        'elif reset and not check_password(password, row["pw_hash"]):',
        "elif False:",
    ),
    (
        "Successful sign-ins use up the login limit",
        "src/radar/web.py",
        "if (msg := login_limiter.blocked(ip)) is not None:",
        "if (msg := login_limiter.allow(ip)) is not None:",
    ),
    (
        "Required criterion missing from the acts counts as passed",
        "src/radar/eligibility.py",
        'add(f"manca:{name}", "unknown",',
        'add(f"manca:{name}", "pass",',
    ),
    (
        "Rejected proposals still counted in the totals",
        "src/radar/recupero.py",
        "if f.finding_id not in rejected and f.hire_id not in best:",
        "if f.hire_id not in best:",
    ),
    (
        "Public demo: one visitor's decisions visible to all",
        "src/radar/web.py",
        'return hashlib.sha256(token.encode()).hexdigest()[:16] if demo_mode and token else ""',
        'return ""',
    ),
    (
        "Spreadsheet formula injection in the CSV export",
        "src/radar/web.py",
        'return "\'" + text if text[:1] in',
        "return text if text[:1] in",
    ),
    (
        "A decision resolves a newer difference on the same field",
        "src/radar/store.py",
        'r = res.get(d.get("key", ""))',
        "r = next(iter(res.values()), None)",
    ),
]


def copy_project(target: Path) -> None:
    shutil.copytree(ROOT, target, ignore=lambda _d, names: [n for n in names if n in SKIP], dirs_exist_ok=True)


def run_tests(cwd: Path) -> tuple[bool, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("DATA_DIR", None)
    # fixed argv (this interpreter + pytest), no shell
    done = subprocess.run(  # nosec B603
        [PYTHON, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    found = re.findall(r"^FAILED (\S+)", done.stdout, re.M) or re.findall(r"^ERROR (\S+)", done.stdout, re.M)
    return done.returncode == 0, found[0] if found else ""


def apply(text: str, old: str, new: str, name: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"Mutation no longer applies exactly once, fix the script: {name}")
    return text.replace(old, new, 1)


def main() -> None:
    for name, relative, old, new in MUTATIONS:
        apply((ROOT / relative).read_text(encoding="utf-8"), old, new, name)
    if "--check" in sys.argv:
        print(len(MUTATIONS), "mutations apply")
        return
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "copy"
        copy_project(work)
        passed, failing = run_tests(work)
        if not passed:
            raise SystemExit(f"The unmodified copy does not pass ({failing}), so nothing here could be trusted.")
        for name, relative, old, new in MUTATIONS:
            path = work / relative
            original = path.read_text(encoding="utf-8")
            path.write_text(apply(original, old, new, name), encoding="utf-8", newline="\n")
            passed, caught_by = run_tests(work)
            path.write_text(original, encoding="utf-8", newline="\n")
            results.append(
                {"bug": name, "file": relative, "caught": not passed, "caught_by": caught_by if not passed else None}
            )
            print(("caught  " if not passed else "SURVIVED"), name, caught_by, flush=True)
    summary = {
        "measured": dt.date.today().isoformat(),
        "bugs_injected": len(results),
        "caught": sum(row["caught"] for row in results),
        "results": results,
    }
    (ROOT / "docs" / "mutation-results.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    print(summary["caught"], "of", summary["bugs_injected"])


if __name__ == "__main__":
    main()
