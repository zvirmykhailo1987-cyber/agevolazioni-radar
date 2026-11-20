# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Writable application state, kept apart from the read-only knowledge base.

SQLite file (``RADAR_APP_DB``): users, sessions, the consultants' review decisions on
Recupero findings (an append-only audit log), and a log of every LLM call. The web app
writes here and only here; the DuckDB knowledge base is opened read-only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from radar.paths import HOME

ROOT = HOME
APP_DB = Path(os.environ.get("RADAR_APP_DB", ROOT / "data" / "app.db"))
SESSION_HOURS = 8

SCHEMA = """
create table if not exists users (
    username text primary key, pw_hash text not null, role text not null check (role in ('consulente', 'admin')));
create table if not exists sessions (
    token_hash text primary key, username text not null references users(username), csrf text not null,
    expires_at text not null);
create table if not exists reviews (
    id integer primary key autoincrement, finding_id text not null, company_id text not null, hire_id text not null,
    incentive_id text not null, decision text not null check (decision in ('approvata', 'respinta')),
    note text not null default '', reviewer text not null, reviewed_at text not null, evidence text not null);
create index if not exists reviews_finding on reviews(finding_id);
create table if not exists difference_decisions (
    id integer primary key autoincrement, diff_key text not null, incentive_id text not null, field text not null,
    choice text not null check (choice in ('kept', 'other')), note text not null default '',
    reviewer text not null, resolved_at text not null);
create table if not exists llm_calls (
    id integer primary key autoincrement, at text not null, purpose text not null, model text not null,
    input_tokens integer not null, output_tokens integer not null, ms integer not null, ok integer not null);
"""


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    candidate = hash_password(password, bytes.fromhex(salt_hex)).split("$")[2]
    return hmac.compare_digest(candidate, digest_hex)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _now() -> datetime:
    return datetime.now(UTC)


class AppState:
    def __init__(self, path: Path | None = None) -> None:
        path = path or Path(os.environ.get("RADAR_APP_DB", APP_DB))
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.db() as con:
            con.executescript(SCHEMA)
            cols = {r["name"] for r in con.execute("pragma table_info(reviews)").fetchall()}
            if "scope" not in cols:  # migration for databases created before per-visitor scopes
                con.execute("alter table reviews add column scope text not null default ''")

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            con = sqlite3.connect(self.path)
            con.row_factory = sqlite3.Row
            try:
                yield con
                con.commit()
            finally:
                con.close()

    # --- users and sessions -------------------------------------------------
    def ensure_user(self, username: str, password: str, role: str = "consulente") -> None:
        with self.db() as con:
            if con.execute("select 1 from users where username=?", [username]).fetchone() is None:
                con.execute("insert into users values (?,?,?)", [username, hash_password(password), role])

    def login(self, username: str, password: str) -> tuple[str, str] | None:
        """Return (session token, csrf token) or None. Only the token's hash is stored."""
        with self.db() as con:
            row = con.execute("select pw_hash from users where username=?", [username]).fetchone()
            # same scrypt cost whether or not the user exists: no username probing by timing
            stored = row["pw_hash"] if row is not None else _DUMMY_HASH
            if not check_password(password, stored) or row is None:
                return None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            expires = (_now() + timedelta(hours=SESSION_HOURS)).isoformat()
            con.execute("delete from sessions where expires_at < ?", [_now().isoformat()])
            con.execute(
                "insert into sessions values (?,?,?,?)",
                [hashlib.sha256(token.encode()).hexdigest(), username, csrf, expires],
            )
            return token, csrf

    def session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.db() as con:
            row = con.execute(
                "select s.username, s.csrf, s.expires_at, u.role from sessions s join users u using (username) "
                "where s.token_hash=?",
                [hashlib.sha256(token.encode()).hexdigest()],
            ).fetchone()
        if row is None or row["expires_at"] < _now().isoformat():
            return None
        return dict(row)

    def logout(self, token: str | None) -> None:
        if token:
            with self.db() as con:
                con.execute("delete from sessions where token_hash=?", [hashlib.sha256(token.encode()).hexdigest()])

    # --- reviews (append-only audit log) -------------------------------------
    def record_review(
        self,
        *,
        finding_id: str,
        company_id: str,
        hire_id: str,
        incentive_id: str,
        decision: str,
        note: str,
        reviewer: str,
        evidence: dict[str, Any],
        scope: str = "",
    ) -> None:
        """Append one decision. ``scope`` separates visitors of a public demo that share one account."""
        if decision not in ("approvata", "respinta"):
            raise ValueError("decision must be 'approvata' or 'respinta'")
        with self.db() as con:
            con.execute(
                "insert into reviews (finding_id, company_id, hire_id, incentive_id, decision, note, reviewer, "
                "reviewed_at, evidence, scope) values (?,?,?,?,?,?,?,?,?,?)",
                [
                    finding_id,
                    company_id,
                    hire_id,
                    incentive_id,
                    decision,
                    note.strip()[:500],
                    reviewer,
                    _now().isoformat(timespec="seconds"),
                    json.dumps(evidence, ensure_ascii=False, default=str),
                    scope,
                ],
            )

    def latest_reviews(self, company_id: str, scope: str = "") -> dict[str, dict[str, Any]]:
        with self.db() as con:
            rows = con.execute(
                "select * from reviews where company_id=? and scope=? order by id", [company_id, scope]
            ).fetchall()
        return {r["finding_id"]: dict(r) for r in rows}  # last decision per finding wins

    def audit(self, limit: int = 50, scope: str = "") -> list[dict[str, Any]]:
        with self.db() as con:
            rows = con.execute(
                "select id, finding_id, company_id, hire_id, incentive_id, decision, note, reviewer, "
                "reviewed_at from reviews where scope=? order by id desc limit ?",
                [scope, limit],
            ).fetchall()
        return [dict(r) for r in rows]

    # --- differences between acts --------------------------------------------------
    def record_resolution(
        self, *, diff_key: str, incentive_id: str, field: str, choice: str, note: str, reviewer: str
    ) -> None:
        if choice not in ("kept", "other"):
            raise ValueError("choice must be 'kept' or 'other'")
        with self.db() as con:
            con.execute(
                "insert into difference_decisions (diff_key, incentive_id, field, choice, note, reviewer, resolved_at) "
                "values (?,?,?,?,?,?,?)",
                [
                    diff_key,
                    incentive_id,
                    field,
                    choice,
                    note.strip()[:500],
                    reviewer,
                    _now().isoformat(timespec="seconds"),
                ],
            )

    def resolutions(self) -> tuple[dict[str, dict[str, Any]], int]:
        """Latest decision per difference, and a version (max id) to skip work when nothing changed."""
        with self.db() as con:
            rows = con.execute("select * from difference_decisions order by id").fetchall()
        return {r["diff_key"]: dict(r) for r in rows}, (rows[-1]["id"] if rows else 0)

    # --- LLM call log -----------------------------------------------------------
    def log_llm(self, purpose: str, model: str, input_tokens: int, output_tokens: int, ms: int, ok: bool) -> None:
        with self.db() as con:
            con.execute(
                "insert into llm_calls (at, purpose, model, input_tokens, output_tokens, ms, ok) "
                "values (?,?,?,?,?,?,?)",
                [_now().isoformat(timespec="seconds"), purpose, model, input_tokens, output_tokens, ms, int(ok)],
            )

    def llm_stats(self, days: int = 30) -> dict[str, Any]:
        since = (_now() - timedelta(days=days)).isoformat()
        with self.db() as con:
            rows = con.execute(
                "select purpose, count(*) n, sum(input_tokens) i, sum(output_tokens) o, avg(ms) ms, "
                "sum(1 - ok) errors from llm_calls where at >= ? group by purpose order by n desc",
                [since],
            ).fetchall()
        return {"days": days, "by_purpose": [dict(r) for r in rows]}


class Timer:
    def __enter__(self) -> Timer:
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = int((time.perf_counter() - self.t0) * 1000)
