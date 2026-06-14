"""Tests for scripts/gate_synthesis.py."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = ROOT / "scripts" / "gate_synthesis.py"


def test_gate_fails_when_answer_contains_uncited_claim(tmp_path: Path) -> None:
    """Every factual answer sentence must be covered by a citation claim."""
    synth = {
        "output": {
            "answer": (
                "Blue-green deployment was chosen. "
                "Rate limiting will use token buckets."
            ),
            "citations": [
                {
                    "claim": "Blue-green deployment was chosen.",
                    "chunk_id": "aaaa0000-0000-0000-0000-000000000001",
                }
            ],
            "coverage_verdict": "complete",
            "qualification": None,
        },
        "metadata": {"sub_query_ledger": {}},
        "self_report": {"verdict": "pass"},
    }
    ranked_chunks = [
        {
            "point_id": "aaaa0000-0000-0000-0000-000000000001",
            "text": "The team chose blue-green deployment for the migration.",
        }
    ]

    synth_path = tmp_path / "synth.json"
    chunks_path = tmp_path / "chunks.json"
    synth_path.write_text(json.dumps(synth), encoding="utf-8")
    chunks_path.write_text(json.dumps(ranked_chunks), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), str(synth_path), str(chunks_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    diagnostics = json.loads(proc.stdout)
    assert diagnostics["all_claims_cited"]["pass"] is False
    assert "Rate limiting" in diagnostics["all_claims_cited"]["uncited_claims"][0]


def test_gate_ignores_inline_citation_markup_for_claim_coverage(tmp_path: Path) -> None:
    """Inline citation labels are provenance, not extra factual claims."""
    synth = {
        "output": {
            "answer": "Blue-green deployment was chosen [source: Migration, chunk 1].",
            "citations": [
                {
                    "claim": "Blue-green deployment was chosen.",
                    "chunk_id": "aaaa0000-0000-0000-0000-000000000001",
                }
            ],
            "coverage_verdict": "complete",
            "qualification": None,
        },
        "metadata": {"ask_type": "recall", "sub_query_ledger": {}},
        "self_report": {"verdict": "pass"},
    }
    ranked_chunks = [
        {
            "point_id": "aaaa0000-0000-0000-0000-000000000001",
            "text": "The team chose blue-green deployment for the migration.",
        }
    ]

    synth_path = tmp_path / "synth.json"
    chunks_path = tmp_path / "chunks.json"
    synth_path.write_text(json.dumps(synth), encoding="utf-8")
    chunks_path.write_text(json.dumps(ranked_chunks), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), str(synth_path), str(chunks_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0
    diagnostics = json.loads(proc.stdout)
    assert diagnostics["all_claims_cited"]["pass"] is True


def test_gate_allows_content_enumeration_with_multiple_citations_in_one_sentence(
    tmp_path: Path,
) -> None:
    """Content enumeration may cite one source per item in a compound sentence."""
    synth = {
        "output": {
            "answer": (
                "Migration appears in Alpha [source: Alpha, chunk 1] "
                "and Beta [source: Beta, chunk 2]."
            ),
            "citations": [
                {
                    "claim": "Migration appears in Alpha.",
                    "chunk_id": "aaaa0000-0000-0000-0000-000000000001",
                },
                {
                    "claim": "Migration appears in Beta.",
                    "chunk_id": "bbbb0000-0000-0000-0000-000000000002",
                },
            ],
            "coverage_verdict": "complete",
            "qualification": None,
        },
        "metadata": {"ask_type": "enumeration", "sub_query_ledger": {}},
        "self_report": {"verdict": "pass"},
    }
    ranked_chunks = [
        {
            "point_id": "aaaa0000-0000-0000-0000-000000000001",
            "text": "Migration appears in Alpha during the launch planning.",
        },
        {
            "point_id": "bbbb0000-0000-0000-0000-000000000002",
            "text": "Migration appears in Beta during the rollout review.",
        },
    ]

    synth_path = tmp_path / "synth.json"
    chunks_path = tmp_path / "chunks.json"
    synth_path.write_text(json.dumps(synth), encoding="utf-8")
    chunks_path.write_text(json.dumps(ranked_chunks), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), str(synth_path), str(chunks_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0
    diagnostics = json.loads(proc.stdout)
    assert diagnostics["all_claims_cited"]["pass"] is True
