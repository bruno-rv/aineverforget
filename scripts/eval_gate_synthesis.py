#!/usr/bin/env python3
"""scripts/eval_gate_synthesis.py — run gate_synthesis.py against answer_synthesizer.yaml fixtures.

Usage:
    python3 scripts/eval_gate_synthesis.py
    python3 scripts/eval_gate_synthesis.py --fixtures tests/eval/fixtures/answer_synthesizer.yaml

Exit: 0 all cases pass, 1 one or more fail, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

_PROJECT_ROOT = Path(__file__).parent.parent
_DEFAULT_FIXTURES = _PROJECT_ROOT / "tests/eval/fixtures/answer_synthesizer.yaml"
_GATE_SCRIPT = Path(__file__).parent / "gate_synthesis.py"


def _load_fixtures(fixtures_path: Path) -> list[dict]:
    """Load fixture cases from YAML or bail out."""
    if not fixtures_path.exists():
        print(f"ERROR: fixtures file not found: {fixtures_path}", file=sys.stderr)
        sys.exit(2)

    raw = fixtures_path.read_text(encoding="utf-8")

    if HAS_YAML:
        data = yaml.safe_load(raw)
    else:
        print("ERROR: PyYAML is not installed. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    cases = data.get("cases", [])
    if not cases:
        print("ERROR: no cases found in fixtures (expected top-level key 'cases')", file=sys.stderr)
        sys.exit(2)
    return cases


def _run_case(case: dict) -> bool:
    """Run gate_synthesis.py for a single fixture case. Returns True if PASS."""
    case_id = case.get("id", "unknown")
    synth_output = case.get("synth_output")
    ranked_chunks = case.get("ranked_chunks", [])
    expected = case.get("expected", {})
    expected_exit_code = expected.get("exit_code", 0)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix=f"ainf_eval_synth_{case_id}_",
        delete=False,
    ) as sf:
        json.dump(synth_output, sf)
        synth_path = sf.name

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix=f"ainf_eval_chunks_{case_id}_",
        delete=False,
    ) as cf:
        json.dump(ranked_chunks, cf)
        chunks_path = cf.name

    try:
        proc = subprocess.run(
            [sys.executable, str(_GATE_SCRIPT), synth_path, chunks_path],
            capture_output=True,
            text=True,
        )
    finally:
        Path(synth_path).unlink(missing_ok=True)
        Path(chunks_path).unlink(missing_ok=True)

    actual_exit = proc.returncode

    passed = True
    detail_lines: list[str] = []

    if actual_exit != expected_exit_code:
        passed = False
        detail_lines.append(
            f"exit code: got {actual_exit}, expected {expected_exit_code}"
        )

    if proc.stdout.strip():
        try:
            gate_result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            passed = False
            detail_lines.append(f"gate stdout is not valid JSON: {exc}")
            gate_result = {}
    else:
        gate_result = {}

    field_checks = {
        "all_claims_cited": expected.get("all_claims_cited"),
        "all_cited_ids_in_input": expected.get("all_cited_ids_in_input"),
        "groundedness_pass": expected.get("groundedness_pass"),
        "coverage_ledger_consistent": expected.get("coverage_ledger_consistent"),
    }

    for field, expected_value in field_checks.items():
        if expected_value is None:
            continue
        nested = gate_result.get(field, {})
        actual_value = nested.get("pass") if isinstance(nested, dict) else None
        if actual_value != expected_value:
            passed = False
            detail_lines.append(
                f"{field}.pass: got {actual_value!r}, expected {expected_value!r}"
            )

    label = "PASS" if passed else "FAIL"
    print(f"{label}  [{case_id}]")
    for line in detail_lines:
        print(f"      {line}")
    if not passed and proc.stderr.strip():
        print(f"      stderr: {proc.stderr.strip()[:200]}")

    return passed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run gate_synthesis.py against answer_synthesizer.yaml fixtures."
    )
    parser.add_argument(
        "--fixtures",
        default=str(_DEFAULT_FIXTURES),
        metavar="PATH",
        help=f"Path to answer_synthesizer.yaml (default: {_DEFAULT_FIXTURES})",
    )
    args = parser.parse_args()

    fixtures_path = Path(args.fixtures)
    cases = _load_fixtures(fixtures_path)

    print(f"Running {len(cases)} fixture case(s) from {fixtures_path}")
    print()

    pass_count = 0
    fail_count = 0

    for case in cases:
        ok = _run_case(case)
        if ok:
            pass_count += 1
        else:
            fail_count += 1

    print()
    print(f"Summary: {pass_count} passed, {fail_count} failed out of {len(cases)} cases.")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
