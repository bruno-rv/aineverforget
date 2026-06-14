"""Run Journal — append-only JSONL+SQLite event log for ingest and ask runs."""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

VALID_EVENTS: frozenset[str] = frozenset({
    "RUN_START", "DISPATCH_START", "GATE_PASS", "GATE_FAIL",
    "INDEX_SUSPECT", "SOFT_WARN", "RUN_CLOSE",
    "ASK_START", "ASK_CLOSE", "TELEMETRY",
})

_DETAIL_ALLOWLIST: dict[str, frozenset[str]] = {
    "RUN_START":      frozenset({"paths"}),
    "DISPATCH_START": frozenset({"source", "dispatches_used", "sub_query_id"}),
    "GATE_PASS":      frozenset({"source", "document_id", "sub_query_id"}),
    "GATE_FAIL":      frozenset({"source", "document_id", "sub_query_id"}),
    "INDEX_SUSPECT":  frozenset({"document_id", "source"}),
    "SOFT_WARN":      frozenset({"dispatches_used"}),
    "RUN_CLOSE":      frozenset({"indexed", "failed", "dispatches"}),
    "ASK_START":      frozenset({"question", "ask_type"}),
    "ASK_CLOSE":      frozenset({"ask_type", "dispatches", "coverage"}),
    "TELEMETRY":      frozenset(),  # tokens/spend go top-level via _TOP_LEVEL_FIELDS
}

_TOP_LEVEL_FIELDS: frozenset[str] = frozenset({
    "run_id", "ask_id", "ingest_id", "attempt_id",
    "agent", "gate", "gate_score", "verdict", "model", "escalated",
    "tokens", "spend",
})

_REDACT_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9+/=]{20,}"),       # JWT
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),           # hex 32+
    re.compile(r"\b[A-Za-z0-9+]{40,}={0,2}\b"),   # base64 40+ (no / — avoids mangling abs paths)
]
_REDACT_KEY_NAMES: frozenset[str] = frozenset({
    "password", "secret", "token", "api_key", "apikey",
    "credential", "auth", "bearer", "access_token", "refresh_token",
})

_CREATE_DDL = """\
CREATE TABLE IF NOT EXISTS journal (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    event    TEXT NOT NULL,
    run_id   TEXT,
    ask_id   TEXT,
    ingest_id TEXT,
    attempt_id TEXT,
    agent    TEXT,
    gate     TEXT,
    gate_score REAL,
    verdict  TEXT,
    model    TEXT,
    escalated INTEGER,
    tokens   INTEGER,
    spend    REAL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_j_event  ON journal(event);
CREATE INDEX IF NOT EXISTS idx_j_ts     ON journal(ts);
CREATE INDEX IF NOT EXISTS idx_j_run_id ON journal(run_id);
"""


_schema_initialized: set[str] = set()


def _ensure_schema(db_path: Path, con: sqlite3.Connection) -> None:
    key = str(db_path)
    if key not in _schema_initialized:
        con.executescript(_CREATE_DDL)
        _schema_initialized.add(key)


def _journal_dir() -> Path:
    env = os.environ.get("AINF_JOURNAL_DIR")
    if env:
        return Path(env)
    return Path(__file__).parent.parent.parent / "runs"


def redact(value: Any) -> Any:
    """Recursively redact secrets from a value."""
    if isinstance(value, str):
        result = value
        for pattern in _REDACT_PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k.lower() in _REDACT_KEY_NAMES else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _jsonl_append(record: dict) -> None:
    path = _journal_dir() / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _db_insert(record: dict) -> None:
    db_path = _journal_dir() / "journal.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))  # PRAGMA busy_timeout below owns retry; timeout= would be overridden
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        _ensure_schema(db_path, con)
        detail = record.get("detail")
        detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        con.execute(
            """INSERT INTO journal
               (ts, event, run_id, ask_id, ingest_id, attempt_id,
                agent, gate, gate_score, verdict, model, escalated, tokens, spend, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["ts"], record["event"],
                record.get("run_id"), record.get("ask_id"),
                record.get("ingest_id"), record.get("attempt_id"),
                record.get("agent"), record.get("gate"),
                record.get("gate_score"), record.get("verdict"),
                record.get("model"), record.get("escalated"),
                record.get("tokens"), record.get("spend"),
                detail_json,
            ),
        )
        con.commit()
    finally:
        con.close()


def append_event(event: str, **kwargs: Any) -> dict:
    """Append a journal event and return the persisted record.

    Raises ValueError for unknown event names.
    Both JSONL and SQLite writes are independently non-fatal.
    If both fail, a one-line warning is printed to stderr.
    """
    if event not in VALID_EVENTS:
        raise ValueError(f"Unknown event: {event!r}. Valid: {sorted(VALID_EVENTS)}")

    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record: dict[str, Any] = {"ts": ts, "event": event}

    allowed_detail = _DETAIL_ALLOWLIST.get(event, frozenset())
    detail: dict[str, Any] = {}

    for key, value in kwargs.items():
        if key in _TOP_LEVEL_FIELDS:
            record[key] = redact(value)
        elif key in allowed_detail:
            detail[key] = redact(value)
        # unknown keys silently dropped

    if detail:
        record["detail"] = detail

    errors: list[str] = []
    try:
        _jsonl_append(record)
    except Exception as exc:
        errors.append(f"jsonl:{exc}")

    try:
        _db_insert(record)
    except Exception as exc:
        errors.append(f"sqlite:{exc}")

    if errors:
        print(
            f"[aineverforget] journal write failed: {'; '.join(errors)}",
            file=sys.stderr,
        )

    return record


def recent_events(n: int = 10) -> list[dict]:
    """Return the n most recent journal events, oldest first."""
    db_path = _journal_dir() / "journal.db"
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT * FROM journal ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        print(f"[aineverforget] journal read failed: {exc}", file=sys.stderr)
        return []
    result = []
    for row in reversed(rows):
        r = dict(row)
        if r.get("detail") and isinstance(r["detail"], str):
            try:
                r["detail"] = json.loads(r["detail"])
            except json.JSONDecodeError:
                pass
        result.append(r)
    return result


def recent_runs(n: int = 5) -> list[dict]:
    """Return recent run summaries (ASK_START + RUN_START) with dispatch counts."""
    db_path = _journal_dir() / "journal.db"
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                WITH starts AS (
                    SELECT run_id, event, ts, detail
                    FROM journal
                    WHERE event IN ('ASK_START', 'RUN_START') AND run_id IS NOT NULL
                    ORDER BY ts DESC
                    LIMIT ?
                )
                SELECT
                    s.run_id,
                    s.event,
                    s.ts,
                    s.detail,
                    (SELECT COUNT(*) FROM journal d
                     WHERE d.event = 'DISPATCH_START' AND d.run_id = s.run_id) AS dispatches
                FROM starts s
                ORDER BY s.ts DESC
                """,
                (n,),
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        print(f"[aineverforget] journal read failed: {exc}", file=sys.stderr)
        return []
    result = []
    for row in rows:
        r = dict(row)
        if r.get("detail") and isinstance(r["detail"], str):
            try:
                r["detail"] = json.loads(r["detail"])
            except json.JSONDecodeError:
                pass
        result.append(r)
    return result
