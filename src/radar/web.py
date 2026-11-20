# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Web demo: browse incentives with citations, pre-screen a hire, ask a grounded question."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, OrderedDict, deque
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from radar.eligibility import HireProfile
from radar.rag import Answerer, answer, anthropic_llm
from radar.rules import CITED_FIELDS
from radar.store import Store

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
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
    if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-":
        return f"{v[8:]}/{v[5:7]}/{v[:4]}"
    return str(v).replace("_", " ")


templates.env.filters["fmt"] = fmt_value

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
}
YES_NO = {"si": True, "no": False}

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
    "application_procedure": "Procedura",
}


templates.env.filters["criterion"] = lambda c: CRITERIA.get(c) or LABELS.get(c, c)


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


def create_app(store: Store | None = None, llm: Answerer | None = None) -> FastAPI:
    app = FastAPI(title="Agevolazioni Radar", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    st = store or Store()
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
        st.refresh()
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return resp

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"fresh": st.freshness, "qa_enabled": qa_enabled, **ctx})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "documents": st.freshness.get("documents_in_kb", 0)}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        items = st.incentives()
        return page(request, "home.html", incentives=items, ev=latest_eval(), state="ready" if items else "empty")

    @app.get("/incentivo/{incentive_id}", response_class=HTMLResponse)
    def incentive(request: Request, incentive_id: str) -> HTMLResponse:
        found = st.rule(incentive_id)
        if not found:
            raise HTTPException(404, "Incentivo non trovato")
        rule, meta = found
        rows = [{"field": f, "label": LABELS.get(f, f), **getattr(rule, f).model_dump()} for f in CITED_FIELDS]
        return page(request, "incentive.html", rule=rule, meta=meta, rows=rows, labels=LABELS)

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
    ) -> HTMLResponse:
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
        )
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
