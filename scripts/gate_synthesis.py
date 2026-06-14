#!/usr/bin/env python3
"""scripts/gate_synthesis.py — runtime Quality Gate for answer-synthesizer.

Recomputes the four judgment fields that the skill must NOT trust from
the agent's self_report booleans (Phase C note in self-report-contract.md):

  1. all_claims_cited — every factual answer sentence is covered by a
     citation claim phrase.
  2. all_cited_ids_in_input — set join of citations[*].chunk_id against
     the skill-retained ranked_chunks[*].point_id.
  3. groundedness_pass — each cited chunk's text shares ≥1 key term with
     the claim it supports (deterministic lexical overlap; no LLM judge).
  4. coverage_ledger_consistent — if any sub_query_ledger entry is "empty",
     coverage_verdict must be "partial" AND qualification must be non-null.

Usage:
    python3 scripts/gate_synthesis.py <synth_output.json> <ranked_chunks.json>

    <synth_output.json>  — full {output, metadata, self_report} returned by
                           answer-synthesizer agent.
    <ranked_chunks.json> — JSON array of ranked_chunk objects (point_id, text,
                           ...) that the skill passed to the synthesizer.

Exit codes:
    0   all four gates pass
    1   one or more gates fail (diagnostic JSON on stdout)
    2   usage error or malformed input (diagnostic JSON on stdout)

Stdout is always a JSON object; route on exit code only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "in", "of", "to", "and", "or", "for",
    "on", "at", "by", "this", "that", "with", "from", "as", "are",
    "was", "were", "be", "been", "have", "has", "had", "not", "no",
    "it", "its", "i", "we", "you", "he", "she", "they", "do", "did",
    "but", "so", "if", "when", "which", "who", "what", "where", "how",
    "can", "will", "would", "could", "should", "may", "might", "shall",
    "all", "any", "each", "into", "than", "then", "their", "there",
    "these", "those", "my", "your", "our", "also", "just", "more",
    "about", "up", "out", "over", "after", "before", "such", "between",
    "s", "t", "re", "ve", "ll", "d", "m",
})

_REFUSAL_MARKERS = (
    "can't confirm",
    "cannot confirm",
    "could not confirm",
    "could not find",
    "do not have enough",
    "don't have enough",
    "not enough information",
    "no references",
    "no mentions",
    "not in your notes",
)

_INLINE_CITATION_RE = re.compile(r"\[source:[^\]]+\]", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.split(r"[^a-z0-9]+", text.lower())
        if w and w not in _STOPWORDS and len(w) > 1
    }


def check_cited_ids_in_input(
    citations: list[dict], chunk_id_set: set[str]
) -> tuple[bool, list[str]]:
    missing = [
        c["chunk_id"]
        for c in citations
        if c.get("chunk_id") not in chunk_id_set
    ]
    return len(missing) == 0, missing


def _sentences(answer: str) -> list[str]:
    answer = _INLINE_CITATION_RE.sub("", answer)
    parts = re.split(r"(?<=[.!?])\s+", answer.strip())
    return [p.strip() for p in parts if p.strip()]


def _is_non_claim_sentence(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def check_all_claims_cited(
    answer: str,
    citations: list[dict],
    *,
    ask_type: str,
    ranked_chunks: list[dict],
) -> tuple[bool, list[str]]:
    """Best-effort deterministic coverage of answer claims by citation claims."""
    if ask_type == "enumeration" and not ranked_chunks:
        return True, []

    answer_claims = [
        sentence
        for sentence in _sentences(answer)
        if _tokens(sentence) and not _is_non_claim_sentence(sentence)
    ]
    if not answer_claims:
        return True, []

    citation_claims = [
        str(c.get("claim", "")).strip()
        for c in citations
        if str(c.get("claim", "")).strip()
    ]
    if not citation_claims:
        return False, answer_claims

    citation_token_sets = [_tokens(claim) for claim in citation_claims]
    uncited = []
    for claim in answer_claims:
        claim_tokens = _tokens(claim)
        if not any(claim_tokens.issubset(cited_tokens) for cited_tokens in citation_token_sets):
            uncited.append(claim)
    return len(uncited) == 0, uncited


def check_groundedness(
    citations: list[dict], chunk_text_map: dict[str, str]
) -> tuple[bool, list[dict]]:
    failures = []
    for cit in citations:
        chunk_id = cit.get("chunk_id", "")
        claim = cit.get("claim", "")
        chunk_text = chunk_text_map.get(chunk_id, "")
        claim_tokens = _tokens(claim)
        if not claim_tokens:
            continue
        chunk_tokens = _tokens(chunk_text)
        if not claim_tokens.intersection(chunk_tokens):
            failures.append({
                "chunk_id": chunk_id,
                "claim_preview": claim[:120],
                "claim_key_tokens": sorted(claim_tokens)[:10],
            })
    return len(failures) == 0, failures


def check_coverage_ledger(
    output: dict, ledger: dict[str, str]
) -> tuple[bool, str | None]:
    has_empty = any(v == "empty" for v in ledger.values())
    if not has_empty:
        return True, None
    coverage_verdict = output.get("coverage_verdict", "")
    qualification = output.get("qualification")
    if coverage_verdict == "partial" and qualification is not None:
        return True, None
    parts = []
    if coverage_verdict != "partial":
        parts.append(f"coverage_verdict={coverage_verdict!r} (expected 'partial')")
    if qualification is None:
        parts.append("qualification=null (must be non-null when sub-queries are empty)")
    return False, "; ".join(parts)


def main() -> int:
    if len(sys.argv) != 3:
        print(json.dumps({"error": "usage: gate_synthesis.py <synth.json> <chunks.json>"}))
        return 2

    try:
        synth = json.loads(Path(sys.argv[1]).read_text())
        chunks_raw = json.loads(Path(sys.argv[2]).read_text())
    except Exception as exc:
        print(json.dumps({"error": f"could not read input: {exc}"}))
        return 2

    synth_output: dict = synth.get("output", {})
    answer = str(synth_output.get("answer", ""))
    citations: list[dict] = synth_output.get("citations", [])
    metadata = synth.get("metadata", {})
    ask_type = str(metadata.get("ask_type", ""))
    ledger: dict[str, str] = metadata.get("sub_query_ledger", {})

    ranked_chunks: list[dict] = (
        chunks_raw if isinstance(chunks_raw, list)
        else chunks_raw.get("ranked_chunks", [])
    )
    chunk_id_set = {c["point_id"] for c in ranked_chunks if "point_id" in c}
    chunk_text_map = {
        c["point_id"]: c.get("text", "") for c in ranked_chunks if "point_id" in c
    }

    claims_ok, uncited_claims = check_all_claims_cited(
        answer,
        citations,
        ask_type=ask_type,
        ranked_chunks=ranked_chunks,
    )
    ids_ok, bad_ids = check_cited_ids_in_input(citations, chunk_id_set)
    grnd_ok, grnd_failures = check_groundedness(citations, chunk_text_map)
    cov_ok, cov_reason = check_coverage_ledger(synth_output, ledger)

    all_pass = claims_ok and ids_ok and grnd_ok and cov_ok

    result = {
        "overall": "pass" if all_pass else "fail",
        "all_claims_cited": {"pass": claims_ok, "uncited_claims": uncited_claims},
        "all_cited_ids_in_input": {"pass": ids_ok, "bad_ids": bad_ids},
        "groundedness_pass": {"pass": grnd_ok, "failures": grnd_failures},
        "coverage_ledger_consistent": {"pass": cov_ok, "reason": cov_reason},
    }
    print(json.dumps(result))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
