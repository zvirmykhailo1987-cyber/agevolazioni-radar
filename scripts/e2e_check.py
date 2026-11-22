# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""End-to-end check of a running Agevolazioni Radar, from every side: visitor, two consultants,
attacker, live questions, limits.

    python scripts/e2e_check.py http://localhost:8040          # pages, isolation, security (no API calls)
    python scripts/e2e_check.py URL --live 2                   # + 2 live questions to the model (~$0.02 each)
    python scripts/e2e_check.py URL --limits                   # + rate limits (blocks this IP for a while)

Run it against the production container before a release, not only against the test client: the
container is read-only, runs as uid 10001 behind a proxy, and that is where deploy bugs hide.
"""

from __future__ import annotations

import re
import sys
import time
import uuid

import httpx

BASE = sys.argv[1].rstrip("/")
LIVE = int(sys.argv[sys.argv.index("--live") + 1]) if "--live" in sys.argv else 0
LIMITS = "--limits" in sys.argv
DEMO = {"username": "consulente", "password": "consulente-demo-2026"}
OK: list[str] = []
BAD: list[str] = []


def check(name: str, cond: bool, info: str = "") -> bool:
    (OK if cond else BAD).append(f"{name} {info}".strip())
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{info}]" if info else ""))
    return cond


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, follow_redirects=False, timeout=180)


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def login(c: httpx.Client, nxt: str = "/recupero") -> httpx.Response:
    return c.post("/accesso", data={**DEMO, "next": nxt})


def rejected(r: httpx.Response) -> bool:
    """A refused write: 4xx, never a redirect that would mean it was recorded."""
    return 400 <= r.status_code < 500


print("== 1. anonymous visitor")
anon = client()
check("health", anon.get("/health").status_code == 200)
for p in ["/", "/catalogo", "/verifica", "/chiedi", "/recupero", "/stato", "/accesso"]:
    r = anon.get(p)
    check(f"GET {p}", r.status_code == 200, str(r.status_code))
h = anon.get("/").headers
check("CSP header", "default-src 'self'" in h.get("content-security-policy", ""))
check("X-Frame-Options DENY", h.get("x-frame-options") == "DENY")
check("nosniff", h.get("x-content-type-options") == "nosniff")
cat = anon.get("/catalogo").text
inc_ids = sorted(set(re.findall(r'href="/incentivo/([\w-]+)"', cat)))
api = anon.get("/api/incentivi").json()
check("catalogue lists incentives", len(inc_ids) > 0, str(len(inc_ids)))
check("catalogue = API list", len(inc_ids) == len({i["incentive_id"] for i in api}), f"{len(inc_ids)} vs {len(api)}")
bad_inc = [i for i in inc_ids if anon.get(f"/incentivo/{i}").status_code != 200]
check("every incentive page opens", not bad_inc, ",".join(bad_inc))
check("API incentive detail", all(anon.get(f"/api/incentivi/{i}").status_code == 200 for i in inc_ids[:5]))
check("catalogue filter by name", anon.get("/catalogo", params={"q": "zes"}).status_code == 200)
check(
    "catalogue filter with no match shows empty state",
    anon.get("/catalogo", params={"q": "zzzz-nothing"}).status_code == 200,
)
check("catalogue query is escaped", "<script>x" not in anon.get("/catalogo", params={"q": "<script>x</script>"}).text)
check("API docs off", anon.get("/docs").status_code == 404 and anon.get("/openapi.json").status_code == 404)
check("unknown incentive 404", anon.get("/incentivo/nope-123").status_code == 404)
check("unknown company 404", anon.get("/recupero/nope-123").status_code == 404)
check("path traversal 404", anon.get("/recupero/..%2f..%2fetc%2fpasswd").status_code == 404)
check("static traversal 404", anon.get("/static/..%2f..%2fweb.py").status_code == 404)
css = re.findall(r'href="(/static/[^"]+\.css)', anon.get("/").text)
check("stylesheet", bool(css) and all(anon.get(u).status_code == 200 for u in css), str(css))
fonts = re.findall(r'url\("?([^")]+\.woff2)', anon.get(css[0]).text) if css else []
font_urls = [f if f.startswith("/") else "/static/" + f.lstrip("./") for f in fonts]
check("fonts", bool(fonts) and all(anon.get(u).status_code == 200 for u in font_urls), f"{len(fonts)} fonts")
icons = re.findall(r'(/static/[^"#]+\.svg)', anon.get("/").text)
check("icons", all(anon.get(u).status_code == 200 for u in set(icons)), f"{len(set(icons))} files")

companies = sorted(set(re.findall(r'href="/recupero/([\w-]+)"', anon.get("/recupero").text)))
check("example companies listed", len(companies) >= 1, ",".join(companies))
for cid in companies:
    r = anon.get(f"/recupero/{cid}")
    check(f"Recupero {cid} opens", r.status_code == 200, str(r.status_code))
    csv = anon.get(f"/recupero/{cid}.csv")
    check(
        f"CSV {cid}",
        csv.status_code == 200
        and "text/csv" in csv.headers.get("content-type", "")
        and "attachment" in csv.headers.get("content-disposition", ""),
    )
    rows = csv.text.splitlines()[1:]
    check(
        f"CSV {cid} has no formula cells",
        not any(c[:1] in "=+-@" for row in rows for c in row.split(";") if c),
        f"{len(rows)} rows",
    )
for raw in ["nan", "inf", "-1", "abc", "1e309", "0.3"]:
    check(
        f"Recupero rate={raw} handled", anon.get(f"/recupero/{companies[0]}", params={"rate": raw}).status_code == 200
    )

inc = inc_ids[0]
form = {
    "incentive_id": inc,
    "hire_date": "2026-03-02",
    "worker_birth_date": "1999-05-10",
    "worker_gender": "",
    "contract_type": "",
    "workplace_region": "",
    "employer_employees": "12",
}
r = anon.post("/verifica", data=form)
check("pre-check returns a result", r.status_code == 200 and 'name="incentive_id"' in r.text, str(r.status_code))
check("pre-check bad date 422", anon.post("/verifica", data={**form, "hire_date": "2026-13-45"}).status_code == 422)
check("pre-check bad gender 422", anon.post("/verifica", data={**form, "worker_gender": "x"}).status_code == 422)
check(
    "pre-check negative staff 422", anon.post("/verifica", data={**form, "employer_employees": "-5"}).status_code == 422
)
check(
    "pre-check unknown incentive 404", anon.post("/verifica", data={**form, "incentive_id": "nope"}).status_code == 404
)
r = anon.post(f"/api/verifica/{inc}", json={"hire_date": "2026-03-02"})
check("API pre-check", r.status_code == 200, str(r.status_code))
check("API pre-check rejects junk", anon.post(f"/api/verifica/{inc}", json={"hire_date": "x"}).status_code == 422)

page0 = anon.get(f"/recupero/{companies[0]}").text
check("anonymous sees no decision buttons", 'name="decision"' not in page0)
r = anon.post(f"/recupero/{companies[0]}/decisione", data={"finding_id": "x", "decision": "approvata", "csrf": "x"})
check("anonymous decision -> sign in", r.status_code == 303 and "/accesso" in r.headers["location"])
r = anon.post(f"/incentivo/{inc}/differenza", data={"diff_key": "x", "choice": "kept", "csrf": "x"})
check("anonymous difference -> sign in", r.status_code == 303 and "/accesso" in r.headers["location"])
check("sign-in page shows the demo credentials", DEMO["password"] in anon.get("/accesso").text)

print("== 2. two consultants on the shared demo account")
v1, v2 = client(), client()
r = login(v1)
check("consultant 1 signs in", r.status_code == 303 and r.headers["location"] == "/recupero")
ck = r.headers.get("set-cookie", "").lower()
check("cookie HttpOnly + SameSite", "httponly" in ck and "samesite=lax" in ck)
if BASE.startswith("https://"):
    check("cookie Secure over HTTPS", "secure" in ck)
check("consultant 2 signs in", login(v2).status_code == 303)
cid = companies[0]
p1 = v1.get(f"/recupero/{cid}").text
tok1 = csrf_of(p1)
check("consultant 1 still signed in after consultant 2", tok1 != "" and 'name="decision"' in p1)
fids = re.findall(r'name="finding_id" value="([^"]+)"', p1)
check("queue offers decisions", bool(fids), str(len(fids)))
fid = fids[0] if fids else "x"
marker = "e2e-" + uuid.uuid4().hex[:10]
r = v1.post(
    f"/recupero/{cid}/decisione",
    data={"finding_id": fid, "decision": "respinta", "note": marker, "csrf": tok1, "rate": "0.3"},
)
check("consultant 1 decision accepted", r.status_code == 303, str(r.status_code))
check("decision redirect stays on site", r.headers.get("location", "").startswith(f"/recupero/{cid}"))
check("decision visible to consultant 1", marker in v1.get(f"/recupero/{cid}").text)
check("decision NOT visible to consultant 2", marker not in v2.get(f"/recupero/{cid}").text)
check("decision NOT visible to anonymous", marker not in anon.get(f"/recupero/{cid}").text)
check(
    "audit log scoped to consultant 1",
    marker in v1.get("/stato").text and marker not in v2.get("/stato").text and marker not in anon.get("/stato").text,
)
check("CSV untouched by a private decision", marker not in anon.get(f"/recupero/{cid}.csv").text)
r = v1.post(f"/incentivo/{inc}/differenza", data={"diff_key": "x", "choice": "kept", "csrf": tok1})
check("demo: differences between acts are read-only", r.status_code in (400, 403), str(r.status_code))

print("== 3. attacker")
dec = f"/recupero/{cid}/decisione"
base = {"finding_id": fid, "decision": "approvata"}
check("no CSRF token -> refused", rejected(v1.post(dec, data=base)))
check("wrong CSRF token -> 403", v1.post(dec, data={**base, "csrf": "x" * 40}).status_code == 403)
tok2 = csrf_of(v2.get(f"/recupero/{cid}").text)
check("another consultant's token -> 403", v1.post(dec, data={**base, "csrf": tok2}).status_code == 403)
check("unknown finding 400", v1.post(dec, data={**base, "finding_id": "zzz", "csrf": tok1}).status_code == 400)
check("overlong finding 422", v1.post(dec, data={**base, "finding_id": "x" * 500, "csrf": tok1}).status_code == 422)
check("bad decision 400", v1.post(dec, data={**base, "decision": "drop", "csrf": tok1}).status_code == 400)
check("overlong note 422", v1.post(dec, data={**base, "note": "n" * 600, "csrf": tok1}).status_code == 422)
check(
    "decision on unknown company 404",
    v1.post("/recupero/nope/decisione", data={**base, "csrf": tok1}).status_code == 404,
)
r = v1.post(dec, data={**base, "csrf": tok1, "rate": "javascript:alert(1)"})
check("rate in redirect is sanitised", r.status_code == 303 and "javascript" not in r.headers.get("location", ""))
xss = "<script>alert(1)</script>"
v1.post(dec, data={**base, "note": xss, "csrf": tok1})
check("XSS in a note is escaped (queue)", "<script>alert(1)" not in v1.get(f"/recupero/{cid}").text)
check("XSS in a note is escaped (audit)", "<script>alert(1)" not in v1.get("/stato").text)
check("logout with wrong CSRF 403", v1.post("/esci", data={"csrf": "nope"}).status_code == 403)
for target in ["//evil.example", "https://evil.example", "/\\evil.example", "javascript:alert(1)"]:
    r = client().post("/accesso", data={**DEMO, "next": target})
    loc = r.headers.get("location", "")
    check(f"open redirect blocked ({target})", loc.startswith("/") and not loc.startswith(("//", "/\\")), loc)
check(
    "login page next is sanitised", "evil.example" not in anon.get("/accesso", params={"next": "//evil.example"}).text
)
check(
    "wrong password rejected",
    "Credenziali non valide" in client().post("/accesso", data={**DEMO, "password": "x"}).text,
)
check("overlong password 422", client().post("/accesso", data={**DEMO, "password": "p" * 300}).status_code == 422)
forged = client()
r = forged.get(f"/recupero/{cid}", headers={"Cookie": "radar_session=forged"})
check("forged cookie = anonymous", r.status_code == 200 and 'name="decision"' not in r.text)
check("question too short 422", anon.post("/chiedi", data={"q": "ab"}).status_code == 422)
check("question too long 422", anon.post("/chiedi", data={"q": "q" * 400}).status_code == 422)
r = v1.post("/esci", data={"csrf": tok1})
check("logout", r.status_code == 303)
r = v1.post(dec, data={**base, "csrf": tok1})
check("old session dead after logout", r.status_code == 303 and "/accesso" in r.headers.get("location", ""))

if LIVE:
    print(f"== 4. live questions ({LIVE}, real model)")
    qs = [
        "Qual è il tetto mensile del Bonus Donne nella ZES?",
        "Qual è la ricetta della carbonara secondo l'INPS?",
        "Qual è il limite massimo mensile del Bonus Giovani per ciascun lavoratore?",
    ]
    # answerable questions come from the golden set: a short free-form question can miss retrieval
    # (README, limits) and is then refused - by design, not a failure of the deployment
    for i in range(LIVE):
        q = qs[i % len(qs)]
        t0 = time.time()
        r = anon.post("/chiedi", data={"q": q})
        took = f"{r.status_code} in {time.time() - t0:.1f}s"
        if not check(f"live question #{i + 1}", r.status_code == 200 and 'class="card answer' in r.text, took):
            print(r.text[-800:])
            continue
        refused = "card answer refused" in r.text
        cites = r.text.count("<li><span>")
        if "carbonara" in q:
            check("off-topic question refused", refused, f"citations={cites}")
        else:
            check("answer carries checked citations", cites > 0 and not refused, f"citations={cites} refused={refused}")
        print("       ", re.sub(r"\s+", " ", re.search(r'<p class="ans">(.*?)</p>', r.text, re.S).group(1))[:160])
    check("Stato logs the model calls", "<td>qa</td>" in anon.get("/stato").text)

if LIMITS:
    print("== 5. limits")
    codes = []
    for i in range(10):
        r = client().post(
            "/chiedi", data={"q": "Qual è il tetto del Bonus Giovani?"}, headers={"X-Forwarded-For": f"10.0.0.{i}"}
        )
        codes.append("limit" if 'role="alert"' in r.text else str(r.status_code))
    first = codes.index("limit") if "limit" in codes else 99
    check(
        "questions per IP limited, X-Forwarded-For spoof ignored",
        first <= 8 and set(codes[first:]) == {"limit"},
        str(codes),
    )
    bf = client()
    res = [
        "blocked"
        if "Troppi" in (t := bf.post("/accesso", data={**DEMO, "password": f"w{i}"}).text) or ("Credenziali" not in t)
        else "denied"
        for i in range(12)
    ]
    first = res.index("blocked") if "blocked" in res else 99
    check("password guessing blocked within 10 failures", first <= 10 and set(res[first:]) == {"blocked"}, str(res))
    r = bf.post("/accesso", data=DEMO)
    check("blocked IP cannot sign in even with the right password", r.status_code != 303, str(r.status_code))

print(f"\nRESULT: {len(OK)} ok, {len(BAD)} failed")
for b in BAD:
    print("  FAILED:", b)
sys.exit(1 if BAD else 0)
