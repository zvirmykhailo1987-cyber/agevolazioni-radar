# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Web demo: browse incentives with citations, pre-screen a hire, ask a grounded question."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import os
import threading
import time
from collections import Counter, OrderedDict, deque
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from radar import llm as llm_module
from radar.appstate import AppState
from radar.eligibility import HireProfile
from radar.paths import HOME
from radar.rag import Answerer, answer, anthropic_llm
from radar.recupero import DEFAULT_EMPLOYER_RATE, example_companies, load_workforce, scan
from radar.rules import CITED_FIELDS
from radar.store import Store

HERE = Path(__file__).resolve().parent
ROOT = HOME
templates = Jinja2Templates(directory=str(HERE / "templates"))


def fmt_value(v: Any) -> str:
    """Human formatting for rule values in the Italian UI."""
    if v is True:
        return "sì"
    if v is False:
        return "no"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, int):
        return f"{v:,}".replace(",", ".")
    if isinstance(v, list):
        return ", ".join(fmt_value(x) for x in v)
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-":
        return f"{v[8:]}/{v[5:7]}/{v[:4]}"
    return str(v).replace("_", " ")


templates.env.filters["fmt"] = fmt_value


def fmt_eur(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.0f} €".replace(",", ".")


templates.env.filters["eur"] = fmt_eur

CRITERIA = {
    "hire_window": "Periodo di assunzione",
    "worker_age": "Età massima",
    "worker_min_age": "Età minima",
    "worker_gender": "Genere",
    "months_without_regular_job": "Mesi senza impiego retribuito",
    "contract_type": "Tipo di contratto",
    "employer_size": "Dimensione del datore",
    "zes_region": "Regione ZES",
    "durc": "DURC",
    "ccnl_compliance": "Rispetto del CCNL",
    "net_job_increase": "Incremento occupazionale netto",
    "differenze_tra_atti": "Differenze tra atti",
    "condizione_lavoratore": "Condizione sul lavoratore",
}
YES_NO = {"si": True, "no": False}
SESSION_COOKIE = "radar_session"
DEMO_PASSWORD = "consulente-demo-2026"  # noqa: S105  # nosec B105 - public demo account, shown on sign-in
DEMO_PASSWORD_HINT = "consulente / consulente-demo-2026"  # noqa: S105  # nosec B105


def csv_cell(value: Any) -> str:
    """Spreadsheet-safe cell: text that starts like a formula is prefixed with an apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def it_number(value: float | None) -> str:
    """Decimal comma, as Italian Excel expects with a ';' separator."""
    return "" if value is None else f"{value:.2f}".replace(".", ",")


def safe_next(target: str) -> str:
    """Only same-site paths: '/x' yes; '//evil', 'https://evil', backslashes and control characters no."""
    if not target.startswith("/") or any(ord(c) < 32 or c in "\\" for c in target):
        return "/"
    parts = urlsplit(target)
    return target if not parts.scheme and not parts.netloc and not target.startswith("//") else "/"


LABELS = {
    "legal_basis": "Base normativa",
    "exemption_percent": "Esonero (%)",
    "monthly_cap_eur": "Tetto mensile (€)",
    "monthly_cap_eur_zes": "Tetto mensile ZES (€)",
    "max_duration_months": "Durata massima (mesi)",
    "hire_from": "Assunzioni dal",
    "hire_to": "Assunzioni fino al",
    "worker_max_age_exclusive": "Età: non compiuti",
    "worker_min_age": "Età minima",
    "worker_gender": "Genere",
    "min_months_without_regular_job": "Mesi senza impiego retribuito",
    "contract_types": "Contratti",
    "employer_max_employees": "Dipendenti max",
    "only_zes_regions": "Solo regioni ZES",
    "zes_regions": "Regioni ZES",
    "requires_net_job_increase": "Incremento occupazionale netto",
    "requires_durc": "DURC",
    "requires_ccnl_compliance": "Rispetto CCNL",
    "other_conditions": "Altre condizioni",
    "application_procedure": "Procedura",
}


CRITERIA.update(
    {
        "hire_window_end": "Data finale delle assunzioni",
        "other_conditions": "Condizioni aggiuntive",
    }
)


def criterion_label(c: str) -> str:
    if c.startswith("manca:"):
        return f"{LABELS.get(c[6:], c[6:])} (non letto dagli atti)"
    return CRITERIA.get(c) or LABELS.get(c, c)


templates.env.filters["criterion"] = criterion_label

# What the consultant has to do about an open criterion: one queue per kind of work.
QUEUE_KINDS: list[tuple[str, str, str, frozenset[str]]] = [
    (
        "lavoratore",
        "Da confermare sul lavoratore",
        "people",
        frozenset(
            {"condizione_lavoratore", "worker_age", "worker_min_age", "worker_gender", "months_without_regular_job"}
        ),
    ),
    ("contratto", "Dati del contratto", "doc", frozenset({"contract_type", "zes_region"})),
    (
        "datore",
        "Dati del datore da attestare",
        "approve",
        frozenset({"durc", "ccnl_compliance", "net_job_increase", "employer_size"}),
    ),
    ("atti", "Differenze tra atti da decidere", "timeline", frozenset({"differenze_tra_atti"})),
]


def work_queue(findings: list[Any]) -> list[dict[str, Any]]:
    """Group the open criteria of the current proposals by the work they need; the rest is
    "read in the text" (values the gate dropped, criteria the acts did not yield, open end dates)."""
    queues: dict[str, dict[str, Any]] = {
        k: {"key": k, "label": lbl, "icon": icon, "items": []} for k, lbl, icon, _ in QUEUE_KINDS
    }
    queues["testo"] = {"key": "testo", "label": "Da leggere nel testo dell'atto", "icon": "book", "items": []}
    for f in findings:
        for c in f.assessment["checks"]:
            if c["outcome"] != "unknown":
                continue
            kind = next((k for k, _, _, names in QUEUE_KINDS if c["criterion"] in names), "testo")
            queues[kind]["items"].append(
                {
                    "hire_id": f.hire_id,
                    "hire": f.hire_name,
                    "incentive": f.incentive_name,
                    "incentive_id": f.incentive_id,
                    "what": criterion_label(c["criterion"]),
                    "detail": c["detail"],
                }
            )
    for q in queues.values():  # one entry per hire x incentive, its open points listed under it
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for it in q["items"]:
            g = groups.setdefault(
                (it["hire_id"], it["incentive_id"]),
                {k: it[k] for k in ("hire_id", "hire", "incentive", "incentive_id")} | {"points": []},
            )
            g["points"].append({"what": it["what"], "detail": it["detail"]})
        q["count"] = len(q["items"])
        q["groups"] = list(groups.values())
    return [q for q in queues.values() if q["items"]]


def meter(f: Any) -> dict[str, int]:
    """Criteria of one finding: decided yes, open, no."""
    out = {"pass": 0, "unknown": 0, "fail": 0}
    for c in f.assessment["checks"]:
        out[c["outcome"]] += 1
    return out


templates.env.filters["meter"] = meter


class RateLimiter:
    """Limits on paid questions: per-IP sliding window, per-IP daily cap, global daily cap.

    The client IP is whatever the ASGI server reports; uvicorn only honours
    X-Forwarded-For from FORWARDED_ALLOW_IPS (the reverse proxy), so a client cannot
    mint fresh buckets by sending its own header. Buckets live in a bounded LRU, so
    a flood of addresses cannot grow memory without limit.
    """

    def __init__(self, per_ip: int, window_s: int, daily: int, per_ip_daily: int = 25, max_ips: int = 10_000) -> None:
        self.per_ip, self.window_s, self.daily, self.per_ip_daily = per_ip, window_s, daily, per_ip_daily
        self.max_ips = max_ips
        self.hits: OrderedDict[str, deque[float]] = OrderedDict()
        self.day_hits: Counter[str] = Counter()
        self.day, self.count = date.today(), 0
        self.lock = threading.Lock()

    def blocked(self, ip: str, now: float | None = None) -> str | None:
        """Would ``ip`` be refused right now? Records nothing (used to count only failed logins)."""
        now = time.time() if now is None else now
        with self.lock:
            if self.count >= self.daily or self.day_hits[ip] >= self.per_ip_daily:
                return "Troppi tentativi oggi. Riprova domani."
            q = [t for t in self.hits.get(ip, ()) if now - t <= self.window_s]
            return "Troppi tentativi in poco tempo. Riprova tra qualche minuto." if len(q) >= self.per_ip else None

    def allow(self, ip: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        with self.lock:
            if date.today() != self.day:
                self.day, self.count = date.today(), 0
                self.day_hits.clear()
            if self.count >= self.daily:
                return "Limite giornaliero della demo raggiunto. Riprova domani."
            if self.day_hits[ip] >= self.per_ip_daily:
                return "Hai raggiunto il limite giornaliero di domande. Riprova domani."
            q = self.hits.pop(ip, deque())
            while q and now - q[0] > self.window_s:
                q.popleft()
            self.hits[ip] = q  # most recently used at the end
            while len(self.hits) > self.max_ips:
                self.hits.popitem(last=False)
            if len(q) >= self.per_ip:
                return "Troppe domande in poco tempo. Riprova tra qualche minuto."
            q.append(now)
            self.day_hits[ip] += 1
            self.count += 1
            return None


def create_app(store: Store | None = None, llm: Answerer | None = None, state: AppState | None = None) -> FastAPI:
    app = FastAPI(title="Agevolazioni Radar", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    st = store or Store()
    app_state = state or AppState()
    app_state.ensure_user(
        "consulente",
        os.environ.get("RADAR_DEMO_PASSWORD", DEMO_PASSWORD),
        reset="RADAR_DEMO_PASSWORD" in os.environ,  # a password set by the operator always wins
    )
    llm_module.RECORDER = app_state.log_llm
    login_limiter = RateLimiter(per_ip=10, window_s=900, daily=1_000_000, per_ip_daily=50)  # per-IP only
    secure_cookie = os.environ.get("RADAR_COOKIE_SECURE", "1") == "1"
    # Public demo: everyone shares one sign-in, so nothing a visitor does may change what others see.
    demo_mode = os.environ.get("RADAR_DEMO_MODE", "0") == "1"
    # the shared demo password is printed on the sign-in page only when this is the public demo
    hint = DEMO_PASSWORD_HINT if demo_mode and "RADAR_DEMO_PASSWORD" not in os.environ else None
    limiter = RateLimiter(
        int(os.environ.get("RADAR_QA_PER_IP", "8")), 3600, int(os.environ.get("RADAR_QA_DAILY", "150"))
    )
    qa_enabled = llm is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))
    ask_llm = llm

    def latest_eval() -> dict[str, Any] | None:
        files = sorted((ROOT / "evals" / "results").glob("eval-*.json"))
        return json.loads(files[-1].read_text(encoding="utf-8")) if files else None

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Any:
        if not request.url.path.startswith(("/static", "/health")):
            st.refresh()
            st.apply_resolutions(*app_state.resolutions())
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return resp

    def user_of(request: Request) -> dict[str, Any] | None:
        return app_state.session(request.cookies.get(SESSION_COOKIE))

    def scope_of(request: Request) -> str:
        """In the public demo each visitor's decisions are private to their own session."""
        token = request.cookies.get(SESSION_COOKIE) or ""
        return hashlib.sha256(token.encode()).hexdigest()[:16] if demo_mode and token else ""

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            name,
            {"fresh": st.freshness, "qa_enabled": qa_enabled, "user": user_of(request), "demo_mode": demo_mode, **ctx},
        )

    def eval_history() -> list[dict[str, Any]]:
        out = []
        for f in sorted((ROOT / "evals" / "results").glob("eval-*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            qa = d["qa"]["summary"]
            hold = d["qa"].get("by_split", {}).get("holdout", {})
            out.append(
                {
                    "at": d["at"][:16].replace("T", " "),
                    "fact_recall": qa.get("fact_recall"),
                    "holdout": hold.get("fact_recall"),
                    "refusal": qa.get("refused_when_unanswerable"),
                    "extraction": d["extraction"]["field_accuracy"],
                    "recupero": d.get("recupero", {}).get("recall"),
                    "repeats": d["qa"].get("repeats", 1),
                }
            )
        return out

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "documents": st.freshness.get("documents_in_kb", 0),
            "retrieval": "hybrid" if st.embedder is not None else "bm25",
        }

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        items = st.incentives()
        # one real, verified value with its quote: the product's promise shown, not described
        proof = None
        for pick in ("bonus-zes-2026", "bonus-giovani-2026", *(i["incentive_id"] for i in items)):
            found = st.rule(pick)
            if found and found[0].monthly_cap_eur.status == "verified" and found[0].monthly_cap_eur.citation:
                rule, meta = found
                proof = {
                    "name": rule.name,
                    "field": LABELS["monthly_cap_eur"],
                    "value": rule.monthly_cap_eur.value,
                    "citation": rule.monthly_cap_eur.citation,
                    "doc": meta["label"],
                    "id": pick,
                }
                break
        return page(
            request, "home.html", incentives=items, ev=latest_eval(), proof=proof, state="ready" if items else "empty"
        )

    @app.get("/incentivo/{incentive_id}", response_class=HTMLResponse)
    def incentive(request: Request, incentive_id: str) -> HTMLResponse:
        found = st.rule(incentive_id)
        if not found:
            raise HTTPException(404, "Incentivo non trovato")
        rule, meta = found
        rows = [{"field": f, "label": LABELS.get(f, f), **getattr(rule, f).model_dump()} for f in CITED_FIELDS]
        diffs = st.diffs([s["doc_id"] for s in meta["sources"] if s.get("doc_id") is not None])
        entry = st.registry.entries.get(incentive_id)
        return page(request, "incentive.html", rule=rule, meta=meta, rows=rows, labels=LABELS, diffs=diffs, entry=entry)

    @app.post("/incentivo/{incentive_id}/differenza")
    def resolve_difference(
        request: Request,
        incentive_id: str,
        diff_key: str = Form(..., max_length=32),
        choice: str = Form(...),
        note: str = Form("", max_length=500),
        csrf: str = Form(...),
    ) -> Response:
        user = user_of(request)
        if user is None:
            return RedirectResponse(f"/accesso?next=/incentivo/{incentive_id}", status_code=303)
        if not hmac.compare_digest(csrf.encode(), user["csrf"].encode()):
            raise HTTPException(403, "Token CSRF non valido")
        if demo_mode:
            raise HTTPException(403, "Nella demo pubblica le differenze tra atti sono di sola lettura.")
        m = st.merged.get(incentive_id)
        diff = next((d for d in (m.differences if m else []) if d.get("key") == diff_key), None)
        if diff is None or choice not in ("kept", "other"):
            raise HTTPException(400, "Richiesta non valida")
        app_state.record_resolution(
            diff_key=diff_key,
            incentive_id=incentive_id,
            field=diff["field"],
            choice=choice,
            note=note,
            reviewer=user["username"],
        )
        return RedirectResponse(f"/incentivo/{incentive_id}#differenze", status_code=303)

    @app.get("/verifica", response_class=HTMLResponse)
    def check_form(request: Request) -> HTMLResponse:
        return page(request, "check.html", incentives=st.incentives(), result=None, form={})

    @app.post("/verifica", response_class=HTMLResponse)
    def check_post(
        request: Request,
        incentive_id: str = Form(...),
        hire_date: date = Form(...),
        worker_birth_date: date | None = Form(None),
        worker_gender: str | None = Form(None),
        months_without_regular_job: int | None = Form(None),
        contract_type: str | None = Form(None),
        workplace_region: str | None = Form(None),
        employer_employees: int | None = Form(None),
        net_job_increase: str | None = Form(None),
        employer_has_durc: str | None = Form(None),
        employer_applies_ccnl: str | None = Form(None),
        job_level: str | None = Form(None),
        disadvantage: str | None = Form(None),
    ) -> HTMLResponse:
        try:
            hire = HireProfile(
                hire_date=hire_date,
                worker_birth_date=worker_birth_date,
                worker_gender=worker_gender or None,  # type: ignore[arg-type]
                months_without_regular_job=months_without_regular_job,
                contract_type=contract_type or None,
                workplace_region=workplace_region or None,
                employer_employees=employer_employees,
                net_job_increase=YES_NO.get(net_job_increase or ""),
                employer_has_durc=YES_NO.get(employer_has_durc or ""),
                employer_applies_ccnl=YES_NO.get(employer_applies_ccnl or ""),
                job_level=job_level or None,  # type: ignore[arg-type]
                disadvantage=disadvantage or None,  # type: ignore[arg-type]
            )
        except ValidationError as exc:
            raise HTTPException(422, "Dati della pre-verifica non validi") from exc
        result = st.check(incentive_id, hire)
        if result is None:
            raise HTTPException(404, "Incentivo non trovato")
        return page(
            request,
            "check.html",
            incentives=st.incentives(),
            result=result,
            form=hire.model_dump(mode="json") | {"incentive_id": incentive_id},
        )

    @app.get("/chiedi", response_class=HTMLResponse)
    def ask_form(request: Request) -> HTMLResponse:
        ev = latest_eval()
        examples = [i for i in (ev or {}).get("qa", {}).get("items", []) if i.get("refusal_ok")][:6]
        return page(request, "ask.html", result=None, examples=examples, error=None, q="")

    @app.post("/chiedi", response_class=HTMLResponse)
    def ask_post(request: Request, q: str = Form(..., min_length=5, max_length=300)) -> HTMLResponse:
        nonlocal ask_llm
        if not qa_enabled or st.retriever is None:
            return page(
                request,
                "ask.html",
                result=None,
                examples=[],
                q=q,
                error="Le domande dal vivo sono disattivate in questa istanza.",
            )
        ip = request.client.host if request.client else "?"
        if (msg := limiter.allow(ip)) is not None:
            return page(request, "ask.html", result=None, examples=[], q=q, error=msg)
        ask_llm = ask_llm or anthropic_llm()
        res = answer(q, st.passages, st.retriever, ask_llm)
        return page(request, "ask.html", result=res, examples=[], q=q, error=None)

    @app.get("/catalogo", response_class=HTMLResponse)
    def catalog(request: Request, q: str = "", group: str = "", status: str = "", side: str = "") -> HTMLResponse:
        items = st.incentives()
        needle = q.strip().lower()[:80]
        shown = [
            i
            for i in items
            if (not group or i["group"] == group)
            and (not status or i["status"] == status)
            and (not side or i["side"] == side)
            and (not needle or needle in i["name"].lower())
        ]
        return page(
            request,
            "catalog.html",
            items=shown,
            total=len(items),
            q=q[:80],
            group=group,
            status=status,
            side=side,
            groups=sorted({i["group"] for i in items}),
            state="ready" if shown else "empty",
        )

    @app.get("/recupero", response_class=HTMLResponse)
    def recupero_list(request: Request) -> HTMLResponse:
        companies = [{"id": cid, "wf": load_workforce(p)} for cid, p in example_companies().items()]
        return page(request, "recupero_list.html", companies=companies)

    def report_for(cid: str, rate: float) -> tuple[Any, Any]:
        files = example_companies()
        if cid not in files:
            raise HTTPException(404, "Azienda non trovata")
        wf = load_workforce(files[cid])
        return wf, scan(st, wf, employer_rate=rate)

    def rate_of(raw: str | None) -> float:
        try:
            val = float(raw or DEFAULT_EMPLOYER_RATE)
        except ValueError:
            return DEFAULT_EMPLOYER_RATE
        if not math.isfinite(val):  # 'nan' and 'inf' parse as floats
            return DEFAULT_EMPLOYER_RATE
        return min(max(val, 0.05), 0.6)

    @app.get("/recupero/{cid}.csv")
    def recupero_csv(cid: str, rate: str | None = None) -> Response:
        _, rep = report_for(cid, rate_of(rate))
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(
            [
                "dipendente",
                "incentivo",
                "esito",
                "proposta",
                "risparmio_mensile_eur",
                "mesi",
                "risparmio_totale_eur",
                "dati_da_verificare",
                "fonte",
            ]
        )
        for f in rep.findings:
            w.writerow(
                [
                    csv_cell(x)
                    for x in (
                        f.hire_name,
                        f.incentive_name,
                        f.verdict,
                        "si" if f.proposed else "alternativa",
                        it_number(f.monthly_saving_eur),
                        f.months or "",
                        it_number(f.total_saving_eur),
                        ", ".join(f.missing),
                        f.source["label"],
                    )
                ]
            )
        return Response(
            buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="recupero-{cid}.csv"'},
        )

    @app.get("/recupero/{cid}", response_class=HTMLResponse)
    def recupero_page(request: Request, cid: str, rate: str | None = None) -> HTMLResponse:
        r = rate_of(rate)
        wf, rep = report_for(cid, r)
        reviews = app_state.latest_reviews(cid, scope_of(request))
        rejected = frozenset(k for k, v in reviews.items() if v["decision"] == "respinta")
        by_hire: dict[str, list[Any]] = {}
        for f in rep.findings:
            by_hire.setdefault(f.hire_id, []).append(f)
        current = rep.effective(rejected)
        # a hire counts as decided once any of its findings has a decision (a rejection is a decision too)
        hires_with_findings = {f.hire_id for f in rep.findings}
        decided = len({f.hire_id for f in rep.findings if f.finding_id in reviews})
        return page(
            request,
            "recupero.html",
            wf=wf,
            rep=rep,
            queue=work_queue(current),
            current_ids={f.finding_id for f in current},
            decided=decided,
            hires_with_findings=len(hires_with_findings),
            totals=rep.totals(rejected),
            by_hire=by_hire,
            rate=r,
            reviews=reviews,
            state="ready" if rep.findings else "empty",
        )

    @app.post("/recupero/{cid}/decisione")
    def recupero_decision(
        request: Request,
        cid: str,
        finding_id: str = Form(..., max_length=32),
        decision: str = Form(...),
        note: str = Form("", max_length=500),
        csrf: str = Form(...),
        rate: str | None = Form(None),
    ) -> Response:
        user = user_of(request)
        if user is None:
            return RedirectResponse(f"/accesso?next=/recupero/{cid}", status_code=303)
        if not hmac.compare_digest(csrf.encode(), user["csrf"].encode()):
            raise HTTPException(403, "Token CSRF non valido")
        _, rep = report_for(cid, rate_of(rate))
        f = next((x for x in rep.findings if x.finding_id == finding_id), None)
        if f is None or decision not in ("approvata", "respinta"):
            raise HTTPException(400, "Decisione non valida")
        app_state.record_review(
            finding_id=f.finding_id,
            company_id=cid,
            hire_id=f.hire_id,
            incentive_id=f.incentive_id,
            decision=decision,
            note=note,
            reviewer=user["username"],
            evidence={
                "verdict": f.verdict,
                "source": f.source,
                "total_saving_eur": f.total_saving_eur,
                "missing": f.missing,
                "rule_fingerprint": f.rule_fingerprint,
            },
            scope=scope_of(request),
        )
        return RedirectResponse(f"/recupero/{cid}?rate={rate_of(rate)}#{f.hire_id}", status_code=303)

    @app.get("/accesso", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/recupero") -> HTMLResponse:
        return page(request, "login.html", next=safe_next(next), error=None, demo_password=hint)

    @app.post("/accesso", response_class=HTMLResponse)
    def login_post(
        request: Request,
        username: str = Form(..., max_length=40),
        password: str = Form(..., max_length=200),
        next: str = Form("/recupero"),
    ) -> Response:
        ip = request.client.host if request.client else "?"
        # only failures count: visitors of the shared demo account behind one address are not locked out
        if (msg := login_limiter.blocked(ip)) is not None:
            return page(request, "login.html", next=safe_next(next), error=msg, demo_password=hint)
        got = app_state.login(username.strip(), password)
        if got is None:
            login_limiter.allow(ip)  # record the failure
            return page(
                request,
                "login.html",
                next=safe_next(next),
                error="Credenziali non valide.",
                demo_password=hint,
            )
        resp = RedirectResponse(safe_next(next), status_code=303)
        resp.set_cookie(SESSION_COOKIE, got[0], httponly=True, secure=secure_cookie, samesite="lax", max_age=8 * 3600)
        return resp

    @app.post("/esci")
    def logout(request: Request, csrf: str = Form("")) -> Response:
        user = user_of(request)
        if user is not None and not hmac.compare_digest(csrf.encode(), user["csrf"].encode()):
            raise HTTPException(403, "Token CSRF non valido")
        app_state.logout(request.cookies.get(SESSION_COOKIE))
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.get("/stato", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        return page(
            request,
            "status.html",
            loads=st.loads(),
            history=eval_history(),
            llm=app_state.llm_stats(),
            audit=app_state.audit(20, scope_of(request)),
            unmapped=st.unmapped,
        )

    @app.get("/api/incentivi")
    def api_list() -> list[dict[str, Any]]:
        return st.incentives()

    @app.get("/api/incentivi/{incentive_id}")
    def api_get(incentive_id: str) -> dict[str, Any]:
        found = st.rule(incentive_id)
        if not found:
            raise HTTPException(404, "unknown incentive")
        return found[0].model_dump()

    @app.post("/api/verifica/{incentive_id}")
    def api_check(incentive_id: str, hire: HireProfile) -> JSONResponse:
        out = st.check(incentive_id, hire)
        if out is None:
            raise HTTPException(404, "unknown incentive")
        return JSONResponse(out)

    @app.get("/api/stato")
    def api_status() -> dict[str, Any]:
        ev = latest_eval()
        return {
            "freshness": st.freshness,
            "eval": ev
            and {"at": ev["at"], "qa": ev["qa"]["summary"], "extraction": ev["extraction"]["field_accuracy"]},
        }

    return app


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(
        create_app(),
        host="0.0.0.0",  # noqa: S104  # nosec B104 - container entrypoint
        port=int(os.environ.get("PORT", "8040")),
        proxy_headers=True,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
