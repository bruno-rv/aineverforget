#!/usr/bin/env python3
"""scripts/eval_note_summarizer.py — validate note-summarizer output against fixtures.

Does NOT invoke the note-summarizer agent. Validates a pre-saved agent output JSON
against the expected fields in note_summarizer.yaml.

Usage:
    python3 scripts/eval_note_summarizer.py --output <agent_output.json> [--fixtures PATH]

    --output   Path to saved agent output JSON {output, metadata, self_report}
    --fixtures Path to note_summarizer.yaml (default: tests/eval/fixtures/note_summarizer.yaml)

Exit: 0 all checks pass, 1 one or more fail, 2 usage error.

Deterministic checks (matches gate_synthesis.py pattern):
  - structure_present == True
  - missing_sections == []
  - compression_in_bounds == True
  - missing_entities == []

Optional semantic checks (from fixture must_contain_entities):
  - each entity appears in summary_text (case-insensitive substring match)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

_PROJECT_ROOT = Path(__file__).parent.parent
_DEFAULT_FIXTURES = _PROJECT_ROOT / "tests/eval/fixtures/note_summarizer.yaml"


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


def _run_gate_checks(agent_output: dict, case: dict) -> list[tuple[str, bool, str]]:
    """Run deterministic gate checks. Returns list of (name, passed, detail)."""
    results: list[tuple[str, bool, str]] = []
    self_report: dict = agent_output.get("self_report", {})

    structure_present = self_report.get("structure_present", False)
    ok = structure_present is True
    results.append(("structure_present", ok, f"got {structure_present!r}"))

    missing_sections = self_report.get("missing_sections", None)
    ok = isinstance(missing_sections, list) and len(missing_sections) == 0
    results.append((
        "missing_sections == []",
        ok,
        f"got {missing_sections!r}",
    ))

    compression_in_bounds = self_report.get("compression_in_bounds", False)
    ok = compression_in_bounds is True
    results.append(("compression_in_bounds", ok, f"got {compression_in_bounds!r}"))

    missing_entities = self_report.get("missing_entities", None)
    ok = isinstance(missing_entities, list) and len(missing_entities) == 0
    results.append((
        "missing_entities == []",
        ok,
        f"got {missing_entities!r}",
    ))

    return results


def _run_entity_checks(agent_output: dict, case: dict) -> list[tuple[str, bool, str]]:
    """Check that must_contain_entities appear in summary_text (case-insensitive)."""
    results: list[tuple[str, bool, str]] = []
    entities: list[str] = case.get("must_contain_entities", [])
    if not entities:
        return results

    output: dict = agent_output.get("output", {})
    summary_text: str = output.get("summary_text", "")
    summary_lower = summary_text.lower()

    for entity in entities:
        found = entity.lower() in summary_lower
        results.append((
            f"entity '{entity}' in summary_text",
            found,
            "found" if found else "NOT FOUND",
        ))

    return results


def _run_case(agent_output: dict, case: dict) -> bool:
    """Run all checks for a single fixture case. Returns True if all pass."""
    case_id = case.get("id", "unknown")
    print(f"Case [{case_id}]:")

    gate_results = _run_gate_checks(agent_output, case)
    entity_results = _run_entity_checks(agent_output, case)
    all_results = gate_results + entity_results

    case_passed = True
    for name, passed, detail in all_results:
        label = "PASS" if passed else "FAIL"
        print(f"  {label}  {name}  ({detail})")
        if not passed:
            case_passed = False

    return case_passed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate note-summarizer output against note_summarizer.yaml fixtures."
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="Path to saved agent output JSON {output, metadata, self_report}",
    )
    parser.add_argument(
        "--fixtures",
        default=str(_DEFAULT_FIXTURES),
        metavar="PATH",
        help=f"Path to note_summarizer.yaml (default: {_DEFAULT_FIXTURES})",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    if not output_path.exists():
        print(f"ERROR: agent output file not found: {output_path}", file=sys.stderr)
        return 2

    try:
        agent_output = json.loads(output_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: could not parse agent output JSON: {exc}", file=sys.stderr)
        return 2

    fixtures_path = Path(args.fixtures)
    cases = _load_fixtures(fixtures_path)

    print(f"Validating against {len(cases)} fixture case(s) from {fixtures_path}")
    print()

    pass_count = 0
    fail_count = 0

    for case in cases:
        ok = _run_case(agent_output, case)
        print()
        if ok:
            pass_count += 1
        else:
            fail_count += 1

    print(f"Summary: {pass_count} passed, {fail_count} failed out of {len(cases)} cases.")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
