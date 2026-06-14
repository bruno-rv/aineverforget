#!/usr/bin/env python3
"""scripts/eval_retrieval.py — retrieval eval against knowledge_retriever.yaml fixtures.

Requires:
  - Live Qdrant at 127.0.0.1:6333 (or QDRANT_URL env var)
  - Corpus ingested: python3 scripts/eval_retrieval.py --ingest first
  - aineverforget CLI on PATH (or run from project root with .venv active)

Usage:
    python3 scripts/eval_retrieval.py [--ingest] [--fixtures PATH] [--k 1,3,5]

    --ingest   Ingest the frozen corpus into Qdrant before running eval.
               Uses --source-id <repo-relative-path> for stable document_ids.
    --fixtures Path to knowledge_retriever.yaml (default: tests/eval/fixtures/knowledge_retriever.yaml)
    --k        Comma-separated k values for recall@k (default: 1,3,5)

Exit: 0 all cases pass, 1 one or more fail, 2 setup error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

_PROJECT_ROOT = Path(__file__).parent.parent
_DEFAULT_FIXTURES = _PROJECT_ROOT / "tests/eval/fixtures/knowledge_retriever.yaml"
_CORPUS_DIR = _PROJECT_ROOT / "tests/eval/corpus"


def recall_at_k(ranked_doc_ids: list[str], relevant_ids: set[str], k: int) -> float:
    """Fraction of relevant_ids found in top-k ranked_doc_ids (binary)."""
    if not relevant_ids:
        return 1.0
    for doc_id in ranked_doc_ids[:k]:
        if doc_id in relevant_ids:
            return 1.0
    return 0.0


def mrr(ranked_doc_ids: list[str], relevant_ids: set[str]) -> float:
    """Reciprocal rank of the first relevant document in the ranked list."""
    for position, doc_id in enumerate(ranked_doc_ids, start=1):
        if doc_id in relevant_ids:
            return 1.0 / position
    return 0.0


def _load_fixtures(fixtures_path: Path) -> list[dict]:
    """Load fixture cases from YAML or bail out with exit 2."""
    if not fixtures_path.exists():
        print(f"ERROR: fixtures file not found: {fixtures_path}", file=sys.stderr)
        sys.exit(2)

    raw = fixtures_path.read_text(encoding="utf-8")

    if not HAS_YAML:
        print("ERROR: PyYAML is not installed. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    data = yaml.safe_load(raw)
    cases = data.get("cases", [])
    if not cases:
        print("ERROR: no cases found in fixtures (expected top-level key 'cases')", file=sys.stderr)
        sys.exit(2)
    return cases


def _run_ingest(corpus_dir: Path) -> bool:
    """Ingest all .md files in corpus_dir using stable repo-relative source_ids."""
    md_files = sorted(corpus_dir.glob("*.md"))
    if not md_files:
        print(f"ERROR: no .md files found in corpus directory: {corpus_dir}", file=sys.stderr)
        return False

    print(f"Ingesting {len(md_files)} corpus file(s)...")
    all_ok = True

    for f in md_files:
        sid = f"tests/eval/corpus/{f.name}"
        print(f"  ingest {f.name} (source-id={sid})")
        proc = subprocess.run(
            ["aineverforget", "ingest", "--source-id", sid, "--json", str(f)],
            capture_output=True,
            text=True,
        )
        if proc.returncode not in (0, 4):
            print(f"  ERROR: ingest failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
            all_ok = False
        else:
            try:
                result = json.loads(proc.stdout)
                print(f"    success={result.get('success_count')}, suspect={result.get('index_suspect_count')}")
            except json.JSONDecodeError:
                print(f"    exit {proc.returncode} (non-JSON output)")

    return all_ok


def _run_hybrid_case(case: dict, k_values: list[int]) -> bool:
    """Run a hybrid search case. Returns True if all thresholds met."""
    case_id = case.get("id", "unknown")
    query = case.get("query", "")
    expected = case.get("expected", {})
    relevant_ids: set[str] = set(expected.get("relevant_document_ids", []))
    thresholds: dict[str, float] = expected.get("recall_thresholds", {})
    mrr_threshold: float | None = expected.get("mrr_threshold")

    proc = subprocess.run(
        ["aineverforget", "search", "--json", query],
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        print(f"FAIL  [{case_id}]  search returned exit {proc.returncode}")
        if proc.stderr.strip():
            print(f"      stderr: {proc.stderr.strip()[:200]}")
        return False

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"FAIL  [{case_id}]  search output is not valid JSON: {exc}")
        return False

    candidates = result.get("candidates", [])
    ranked_doc_ids = [c.get("document_id", "") for c in candidates]

    passed = True
    detail_lines: list[str] = []

    for k in k_values:
        score = recall_at_k(ranked_doc_ids, relevant_ids, k)
        threshold_key = f"recall@{k}"
        threshold = thresholds.get(threshold_key)
        if threshold is not None and score < threshold:
            passed = False
            detail_lines.append(
                f"{threshold_key}={score:.3f} < threshold {threshold:.3f}"
            )
        else:
            detail_lines.append(f"{threshold_key}={score:.3f}")

    mrr_score = mrr(ranked_doc_ids, relevant_ids)
    if mrr_threshold is not None and mrr_score < mrr_threshold:
        passed = False
        detail_lines.append(f"MRR={mrr_score:.3f} < threshold {mrr_threshold:.3f}")
    else:
        detail_lines.append(f"MRR={mrr_score:.3f}")

    label = "PASS" if passed else "FAIL"
    print(f"{label}  [{case_id}]  " + "  ".join(detail_lines))
    return passed


def _run_lexscan_case(case: dict) -> bool:
    """Run a lexscan case. Returns True if total_hits >= expected min_hits."""
    case_id = case.get("id", "unknown")
    term = case.get("term", "")
    expected = case.get("expected", {})
    min_hits: int = expected.get("min_hits", 0)

    proc = subprocess.run(
        ["aineverforget", "lexscan", "--json", term],
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        print(f"FAIL  [{case_id}]  lexscan returned exit {proc.returncode}")
        if proc.stderr.strip():
            print(f"      stderr: {proc.stderr.strip()[:200]}")
        return False

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"FAIL  [{case_id}]  lexscan output is not valid JSON: {exc}")
        return False

    chunk_count: int = result.get("chunk_count", 0)
    passed = chunk_count >= min_hits

    label = "PASS" if passed else "FAIL"
    print(f"{label}  [{case_id}]  chunk_count={chunk_count} (min_hits={min_hits})")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retrieval eval against knowledge_retriever.yaml fixtures."
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        default=False,
        help="Ingest the frozen corpus into Qdrant before running eval.",
    )
    parser.add_argument(
        "--fixtures",
        default=str(_DEFAULT_FIXTURES),
        metavar="PATH",
        help=f"Path to knowledge_retriever.yaml (default: {_DEFAULT_FIXTURES})",
    )
    parser.add_argument(
        "--k",
        default="1,3,5",
        metavar="K",
        help="Comma-separated k values for recall@k (default: 1,3,5)",
    )
    args = parser.parse_args()

    try:
        k_values = [int(x.strip()) for x in args.k.split(",") if x.strip()]
    except ValueError as exc:
        print(f"ERROR: --k must be comma-separated integers: {exc}", file=sys.stderr)
        return 2

    if args.ingest:
        ok = _run_ingest(_CORPUS_DIR)
        if not ok:
            print("ERROR: corpus ingest failed — aborting eval.", file=sys.stderr)
            return 2
        print()

    fixtures_path = Path(args.fixtures)
    cases = _load_fixtures(fixtures_path)

    print(f"Running {len(cases)} fixture case(s) from {fixtures_path}")
    print(f"k values: {k_values}")
    print()

    pass_count = 0
    fail_count = 0

    for case in cases:
        case_type = case.get("type", "hybrid")
        if case_type == "lexscan":
            ok = _run_lexscan_case(case)
        else:
            ok = _run_hybrid_case(case, k_values)

        if ok:
            pass_count += 1
        else:
            fail_count += 1

    print()
    print(f"Summary: {pass_count} passed, {fail_count} failed out of {len(cases)} cases.")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
