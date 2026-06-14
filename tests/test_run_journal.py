"""Tests for run_journal.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from aineverforget.run_journal import (
    VALID_EVENTS,
    append_event,
    recent_events,
    recent_runs,
    redact,
)


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------

class TestRedact:
    def test_plain_string_unchanged(self):
        assert redact("hello world") == "hello world"

    def test_jwt_redacted(self):
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = redact(token)
        assert "[REDACTED]" in result
        assert "eyJ" not in result

    def test_hex32_redacted(self):
        hex_val = "a" * 32
        result = redact(hex_val)
        assert "[REDACTED]" in result

    def test_short_hex_unchanged(self):
        # 31 hex chars: below threshold
        short = "a" * 31
        assert redact(short) == short

    def test_dict_sensitive_key_redacted(self):
        d = {"password": "s3cr3t", "username": "alice"}
        result = redact(d)
        assert result["password"] == "[REDACTED]"
        assert result["username"] == "alice"

    def test_dict_api_key_redacted(self):
        d = {"api_key": "sk-123abc", "model": "gpt-4"}
        result = redact(d)
        assert result["api_key"] == "[REDACTED]"
        assert result["model"] == "gpt-4"

    def test_nested_dict_redacted(self):
        d = {"config": {"token": "secret123"}, "name": "test"}
        result = redact(d)
        assert result["config"]["token"] == "[REDACTED]"
        assert result["name"] == "test"

    def test_list_recursed(self):
        lst = ["hello", {"password": "x"}, "world"]
        result = redact(lst)
        assert result[0] == "hello"
        assert result[1]["password"] == "[REDACTED]"
        assert result[2] == "world"

    def test_non_string_passthrough(self):
        assert redact(42) == 42
        assert redact(3.14) == 3.14
        assert redact(None) is None
        assert redact(True) is True

    def test_absolute_path_not_redacted(self):
        path = "/Users/bruno/Dev/aineverforget/tests/fixtures/sample.md"
        assert redact(path) == path

    def test_short_abs_path_not_redacted(self):
        assert redact("/tmp/x.md") == "/tmp/x.md"


# ---------------------------------------------------------------------------
# append_event
# ---------------------------------------------------------------------------

@pytest.fixture()
def journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AINF_JOURNAL_DIR", str(tmp_path))
    return tmp_path


class TestAppendEvent:
    def test_invalid_event_raises(self, journal_dir: Path):
        with pytest.raises(ValueError, match="Unknown event"):
            append_event("BOGUS")

    def test_valid_event_returns_record(self, journal_dir: Path):
        record = append_event("RUN_START", run_id="r1", paths=3)
        assert record["event"] == "RUN_START"
        assert record["run_id"] == "r1"
        assert record["detail"]["paths"] == 3
        assert "ts" in record

    def test_jsonl_written(self, journal_dir: Path):
        append_event("GATE_PASS", run_id="r1", agent="note-summarizer", source="/tmp/x.md")
        lines = (journal_dir / "journal.jsonl").read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event"] == "GATE_PASS"
        assert parsed["agent"] == "note-summarizer"
        assert parsed["detail"]["source"] == "/tmp/x.md"

    def test_sqlite_row_written(self, journal_dir: Path):
        import sqlite3
        append_event("DISPATCH_START", run_id="r2", agent="knowledge-indexer", dispatches_used=2)
        con = sqlite3.connect(str(journal_dir / "journal.db"))
        rows = con.execute("SELECT event, run_id, agent FROM journal").fetchall()
        con.close()
        assert len(rows) == 1
        assert rows[0] == ("DISPATCH_START", "r2", "knowledge-indexer")

    def test_unknown_kwargs_silently_dropped(self, journal_dir: Path):
        record = append_event("GATE_PASS", run_id="r1", agent="x", unknown_field="ignored")
        assert "unknown_field" not in record
        assert record.get("detail") is None or "unknown_field" not in record.get("detail", {})

    def test_top_level_fields_not_in_detail(self, journal_dir: Path):
        record = append_event("GATE_FAIL", run_id="r1", agent="x", verdict="fail", gate_score=0.5)
        assert record["verdict"] == "fail"
        assert record["gate_score"] == 0.5
        assert record.get("detail") is None  # verdict + gate_score are top-level, no detail fields passed

    def test_detail_allowlist_respected(self, journal_dir: Path):
        # paths is in RUN_START detail allowlist; dispatches_used is in DISPATCH_START detail
        record = append_event("RUN_START", run_id="r1", paths=5, dispatches_used=99)
        assert record["detail"]["paths"] == 5
        assert "dispatches_used" not in record.get("detail", {})  # not in RUN_START allowlist

    def test_both_writes_non_fatal(self, journal_dir: Path, capsys: pytest.CaptureFixture):
        def bad_jsonl(_: dict) -> None:
            raise OSError("disk full")

        def bad_db(_: dict) -> None:
            raise OSError("db error")

        import aineverforget.run_journal as rj
        orig_jsonl = rj._jsonl_append
        orig_db = rj._db_insert
        try:
            rj._jsonl_append = bad_jsonl
            rj._db_insert = bad_db
            record = append_event("SOFT_WARN", run_id="r1", dispatches_used=9)
        finally:
            rj._jsonl_append = orig_jsonl
            rj._db_insert = orig_db

        captured = capsys.readouterr()
        assert "journal write failed" in captured.err
        assert record["event"] == "SOFT_WARN"

    def test_single_backend_failure_warns(self, journal_dir: Path, capsys: pytest.CaptureFixture):
        def bad_jsonl(_: dict) -> None:
            raise OSError("disk full")

        import aineverforget.run_journal as rj
        orig_jsonl = rj._jsonl_append
        try:
            rj._jsonl_append = bad_jsonl
            record = append_event("SOFT_WARN", run_id="r1", dispatches_used=9)
        finally:
            rj._jsonl_append = orig_jsonl

        captured = capsys.readouterr()
        assert "journal write failed" in captured.err
        assert record["event"] == "SOFT_WARN"

    def test_redaction_applied(self, journal_dir: Path):
        # JWT header must be 20+ base64 chars after eyJ to match the pattern
        record = append_event(
            "ASK_START",
            run_id="r1",
            question="token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload",
        )
        assert "[REDACTED]" in record["detail"]["question"]

    def test_multiple_events_accumulate(self, journal_dir: Path):
        for i in range(5):
            append_event("GATE_PASS", run_id=f"r{i}", agent="note-summarizer")
        lines = (journal_dir / "journal.jsonl").read_text().splitlines()
        assert len(lines) == 5


def test_script_runs_from_src_layout_checkout_without_install(tmp_path: Path):
    """The documented script entrypoint must work before editable install."""
    env = {
        "AINF_JOURNAL_DIR": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
    }
    proc = subprocess.run(
        [sys.executable, "-S", "scripts/run_journal.py", "--list", "1"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == []


# ---------------------------------------------------------------------------
# recent_events
# ---------------------------------------------------------------------------

class TestRecentEvents:
    def test_empty_when_no_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AINF_JOURNAL_DIR", str(tmp_path))
        assert recent_events() == []

    def test_read_error_logs_to_stderr(self, journal_dir: Path, capsys: pytest.CaptureFixture):
        import aineverforget.run_journal as rj
        import sqlite3
        append_event("RUN_START", run_id="r1")
        orig_connect = sqlite3.connect

        def bad_connect(*a, **kw):
            raise sqlite3.OperationalError("permission denied")

        import unittest.mock as mock
        with mock.patch("aineverforget.run_journal.sqlite3.connect", side_effect=bad_connect):
            result = recent_events(5)
        assert result == []
        captured = capsys.readouterr()
        assert "journal read failed" in captured.err

    def test_returns_oldest_first(self, journal_dir: Path):
        append_event("RUN_START", run_id="r1")
        append_event("DISPATCH_START", run_id="r1", agent="x")
        append_event("GATE_PASS", run_id="r1", agent="x")
        events = recent_events(3)
        assert [e["event"] for e in events] == ["RUN_START", "DISPATCH_START", "GATE_PASS"]

    def test_limits_to_n(self, journal_dir: Path):
        for _ in range(10):
            append_event("GATE_PASS", run_id="r1", agent="x")
        events = recent_events(4)
        assert len(events) == 4

    def test_detail_deserialized(self, journal_dir: Path):
        append_event("RUN_START", run_id="r1", paths=7)
        events = recent_events(1)
        assert events[0]["detail"]["paths"] == 7


# ---------------------------------------------------------------------------
# recent_runs
# ---------------------------------------------------------------------------

class TestRecentRuns:
    def test_empty_when_no_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AINF_JOURNAL_DIR", str(tmp_path))
        assert recent_runs() == []

    def test_returns_run_start_events(self, journal_dir: Path):
        append_event("RUN_START", run_id="r1")
        runs = recent_runs()
        assert len(runs) == 1
        assert runs[0]["event"] == "RUN_START"
        assert runs[0]["run_id"] == "r1"

    def test_dispatch_count_correct(self, journal_dir: Path):
        append_event("RUN_START", run_id="r1")
        append_event("DISPATCH_START", run_id="r1", agent="note-summarizer")
        append_event("DISPATCH_START", run_id="r1", agent="knowledge-indexer")
        runs = recent_runs()
        assert runs[0]["dispatches"] == 2

    def test_ask_start_included(self, journal_dir: Path):
        append_event("ASK_START", run_id="ask-1", question="What is X?")
        runs = recent_runs()
        assert runs[0]["event"] == "ASK_START"

    def test_limits_to_n(self, journal_dir: Path):
        for i in range(5):
            append_event("RUN_START", run_id=f"r{i}")
        runs = recent_runs(2)
        assert len(runs) == 2

    def test_unrelated_events_not_in_runs(self, journal_dir: Path):
        append_event("GATE_PASS", run_id="r1", agent="x")
        append_event("GATE_FAIL", run_id="r1", agent="x")
        runs = recent_runs()
        assert len(runs) == 0  # no RUN_START or ASK_START
