# Shared Self-Report Contract — aineverforget Phase B

All agents return exactly `{output, metadata, self_report}`.
The skill (orchestrator) runs all Quality Gates on `self_report` — agents never gate themselves.

---

## note-summarizer

```json
{
  "output": {
    "summary_path": "<absolute path to summary.md>",
    "summary_text": "<full text of summary>",
    "word_count": 450,
    "section_count": 5
  },
  "metadata": {
    "source_path": "<input path>",
    "source_word_count": 2000
  },
  "self_report": {
    "structure_present": true,
    "required_sections_found": ["## TL;DR", "## Key Concepts", "## Key Decisions", "## Action Items"],
    "missing_sections": [],
    "compression_ratio": 0.225,
    "compression_in_bounds": true,
    "entities_in_source": ["Alice", "Project X"],
    "entities_in_summary": ["Alice", "Project X"],
    "missing_entities": [],
    "verdict": "pass"
  }
}
```

**Gate (skill-run, deterministic):**
- `structure_present == true` AND `missing_sections == []`
- `compression_ratio < 1.0` AND `compression_ratio > 0.05` (non-trivial content)
- `missing_entities == []`
- Fail → retry → opus → `needs_user`

---

## knowledge-indexer

```json
{
  "output": {
    "document_id": "<uuid>",
    "ingest_generation": 2,
    "chunk_count": 12,
    "ingest_state": "active",
    "cli_result": "<raw JSON string from `aineverforget ingest --json`>"
  },
  "metadata": {
    "source_path": "<input path>",
    "tags": []
  },
  "self_report": {
    "probe_verdicts": {
      "topical": "pass",
      "specific": "pass",
      "negative": "pass"
    },
    "negative_deferred": false,
    "cold_start": false,
    "verdict": "indexed"
  }
}
```

**Gate (skill-run, deterministic):**
- `ingest_state == "active"` (the CLI verify→promote already ran; state is authoritative)
- `probe_verdicts.topical == "pass"` AND `probe_verdicts.specific == "pass"`
- `probe_verdicts.negative == "pass"` OR `negative_deferred == true`
- Any `"error"` or `"index_suspect"` CLI result → `INDEX_SUSPECT` journal event, no retry

---

## knowledge-retriever

```json
{
  "output": {
    "ranked_chunks": [
      {
        "rank": 1,
        "point_id": "<uuid>",
        "document_id": "<uuid>",
        "document_path": "<path>",
        "title": "<title>",
        "heading_path": "<pipe-joined heading path or null>",
        "pdf_page": null,
        "text": "<chunk text>",
        "score": 0.95,
        "citation": {
          "source_id": "<source_id>",
          "document_path": "<path>",
          "title": "<title>",
          "chunk_index": 3,
          "heading_path": "<heading_path or null>",
          "pdf_page": null,
          "producer": "<producer>"
        }
      }
    ]
  },
  "metadata": {
    "query": "<original question or sub-query>",
    "query_type": "recall|synthesis_sub|enumeration_content|enumeration_metadata",
    "search_mode": "hybrid|lexscan|scroll",
    "sub_query_id": "<for synthesis fan-out, null for recall>",
    "reformulations_tried": 0
  },
  "self_report": {
    "candidate_count": 5,
    "dense_hits": 4,
    "sparse_hits": 3,
    "citationable_count": 5,
    "gate_pass": true,
    "verdict": "pass"
  }
}
```

**Gate (skill-run, deterministic):**
```
candidate_count >= 1
AND (dense_hits >= 1 OR sparse_hits >= 1)
AND citationable_count >= 1
```
- Pre-fusion per-modality counts from `search --json` (NOT RRF score floor)
- Fail → reformulate/broaden → opus → empty result
- For `lexscan`/`scroll` modes: `candidate_count >= 0` (empty is valid, not a gate failure)

---

## answer-synthesizer

```json
{
  "output": {
    "answer": "<grounded answer text with inline citations>",
    "citations": [
      {
        "claim": "<verbatim sentence or phrase from answer that this citation supports>",
        "chunk_id": "<point_id uuid>",
        "document_path": "<path>",
        "title": "<title>",
        "heading_path": "<heading_path or null>",
        "pdf_page": null,
        "producer": "<producer>"
      }
    ],
    "coverage_verdict": "complete",
    "qualification": null
  },
  "metadata": {
    "question": "<original question>",
    "ask_type": "recall|synthesis|enumeration",
    "input_chunk_count": 5,
    "sub_query_ledger": {
      "<sub_query>": "answered|empty"
    }
  },
  "self_report": {
    "all_claims_cited": true,
    "all_cited_ids_in_input": true,
    "groundedness_pass": true,
    "coverage_verdict": "complete",
    "coverage_ledger_consistent": true,
    "unresolved_sub_queries": [],
    "verdict": "pass"
  }
}
```

**Gate (skill-run, deterministic):**
- `all_claims_cited == true` — every factual claim in `answer` has a corresponding `citations` entry
- `all_cited_ids_in_input == true` — every `chunk_id` in `citations` was present in the input ranked_chunks
- `groundedness_pass == true` — lexical overlap check: each cited chunk's text must share ≥1 key term with the claim it supports (deterministic, no LLM judge)
- `coverage_ledger_consistent == true` — if `sub_query_ledger` has any `"empty"` entries, `coverage_verdict` must be `"partial"` and `qualification` must be non-null
- Fail → re-synthesize → opus → "can't confirm from your notes" / explicit qualification

> **Phase C note — judgment fields:** `all_claims_cited`, `all_cited_ids_in_input`, `groundedness_pass`, and `coverage_ledger_consistent` are agent self-assessments. The skill **must recompute** `all_cited_ids_in_input` by joining `citations[*].chunk_id` against the skill-retained `ranked_chunks[*].point_id` — it must **not gate blindly on the agent's booleans**. The agent's `citations` list is the authoritative output; the booleans are its own consistency check, not a ground truth the skill should skip verifying. `groundedness_pass` in particular should be re-verified by the skill using the same lexical-overlap rule (cited chunk text shares ≥1 key term with the claim).
