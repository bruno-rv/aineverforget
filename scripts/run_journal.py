#!/usr/bin/env python3
"""CLI driver for run_journal — called from orchestrator skills via Bash.

Usage:
    python3 scripts/run_journal.py <EVENT> [options]
    python3 scripts/run_journal.py --list N
    python3 scripts/run_journal.py --runs N

Always exits 0 (journal failures are non-fatal).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from aineverforget.run_journal import append_event, recent_events, recent_runs

_INT_FIELDS: frozenset[str] = frozenset({
    "paths", "dispatches_used", "indexed", "failed", "dispatches",
    "tokens", "escalated",
})
_FLOAT_FIELDS: frozenset[str] = frozenset({"gate_score", "spend"})

# argparse dest → journal kwarg name (dests use underscores; flags use dashes)
_DEST_TO_FIELD: dict[str, str] = {
    "run_id":          "run_id",
    "ask_id":          "ask_id",
    "ingest_id":       "ingest_id",
    "attempt_id":      "attempt_id",
    "agent":           "agent",
    "gate":            "gate",
    "gate_score":      "gate_score",
    "verdict":         "verdict",
    "model":           "model",
    "escalated":       "escalated",
    "tokens":          "tokens",
    "spend":           "spend",
    "source":          "source",
    "document_id":     "document_id",
    "sub_query_id":    "sub_query_id",
    "paths":           "paths",
    "dispatches_used": "dispatches_used",
    "indexed":         "indexed",
    "failed":          "failed",
    "dispatches":      "dispatches",
    "detail":          "detail",
    "question":        "question",
    "ask_type":        "ask_type",
    "coverage":        "coverage",
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Append a run-journal event or query recent events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("event", nargs="?", help="Event type (e.g. RUN_START, ASK_START)")

    # Top-level fields
    p.add_argument("--run-id")
    p.add_argument("--ask-id")
    p.add_argument("--ingest-id")
    p.add_argument("--attempt-id")
    p.add_argument("--agent")
    p.add_argument("--gate")
    p.add_argument("--gate-score")
    p.add_argument("--verdict")
    p.add_argument("--model")
    p.add_argument("--escalated")
    p.add_argument("--tokens")
    p.add_argument("--spend")

    # Detail fields (event-specific)
    p.add_argument("--source")
    p.add_argument("--document-id")
    p.add_argument("--sub-query-id")
    p.add_argument("--paths")
    p.add_argument("--dispatches-used")
    p.add_argument("--indexed")
    p.add_argument("--failed")
    p.add_argument("--dispatches")
    p.add_argument("--detail")
    p.add_argument("--question")
    p.add_argument("--ask-type")
    p.add_argument("--coverage")

    # Query / output modes
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="Print emitted record as JSON")
    p.add_argument("--list", dest="list_n", type=int, metavar="N",
                   help="Print recent N journal events as JSON array (no event required)")
    p.add_argument("--runs", dest="runs_n", type=int, metavar="N",
                   help="Print recent N run summaries as JSON array (no event required)")
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_n is not None:
        print(json.dumps(recent_events(args.list_n), ensure_ascii=False))
        return

    if args.runs_n is not None:
        print(json.dumps(recent_runs(args.runs_n), ensure_ascii=False))
        return

    if not args.event:
        parser.error("event is required unless --list or --runs is given")

    raw = vars(args)
    kwargs: dict = {}
    for dest, field in _DEST_TO_FIELD.items():
        val = raw.get(dest)
        if val is None:
            continue
        if field in _INT_FIELDS:
            try:
                val = int(val)
            except (ValueError, TypeError):
                pass
        elif field in _FLOAT_FIELDS:
            try:
                val = float(val)
            except (ValueError, TypeError):
                pass
        kwargs[field] = val

    try:
        record = append_event(args.event, **kwargs)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(0)  # non-fatal — journal errors never abort a run

    if args.json_out:
        print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
