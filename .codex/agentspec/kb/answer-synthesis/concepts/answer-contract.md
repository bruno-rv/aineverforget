# Answer Contract

## Position in the /ask Pipeline

Answer synthesis is the terminal step of the `/ask` skill:

```
/ask skill
  → decompose (synthesis only)
  → knowledge-retriever ×N (one per sub-query or one for recall)
  → skill assembles ranked_chunks + sub_query_ledger
  → answer-synthesizer
  → {output, metadata, self_report}
  → skill runs gates, reiterate if verdict: fail
  → user receives answer
```

`answer-synthesizer` receives control after the skill has gathered all ranked_chunks and assembled the sub_query_ledger. It never touches the CLI and never calls other agents.

---

## Consumes

| Input | Source | Notes |
|-------|--------|-------|
| `question` | Original user question | Passed verbatim; echoed in `metadata.question` |
| `ranked_chunks` | Output of `knowledge-retriever` (one or N sets) | Each entry has `point_id`, `text`, `citation` block with provenance |
| `sub_query_ledger` | Assembled by the skill | Maps each sub-query string to "answered" or "empty" |
| `ask_type` | Set by the skill | "recall", "synthesis", or "enumeration" |

For **recall**: `ranked_chunks` is a single flat list; `sub_query_ledger` has one key equal to the original question.

For **synthesis**: `ranked_chunks` is provided per sub-query (or as a deduplicated union with sub-query provenance); `sub_query_ledger` has one key per sub-query.

For **enumeration**: `ranked_chunks` contains lexscan document-level entries (content) or is `[]` with a document list in the metadata (metadata enumeration).

---

## Produces

Returns exactly `{output, metadata, self_report}`. Nothing written to disk. No side effects.

| Field | Contract |
|-------|---------|
| `output.answer` | Grounded natural-language answer with inline citations; explicit refusal text when unsupported |
| `output.citations` | Array of citation objects; one entry per claim-to-chunk mapping (see citation-contract.md for dedup) |
| `output.coverage_verdict` | "complete" or "partial" only; must match `self_report.coverage_verdict` |
| `output.qualification` | `null` when complete; non-null description of gaps when partial |
| `metadata.sub_query_ledger` | Echoed verbatim from input; not modified |
| `self_report.*` | Four deterministic booleans + unresolved_sub_queries list + verdict |

---

## What Makes a Claim "Grounded"

A claim is grounded when:

1. **Lexical support exists:** the cited chunk's `text` shares at least one key noun or proper name (≥ 4 characters, not a common stopword) with the claim as stated.
2. **Provenance is real:** the `chunk_id` in the citation is the `point_id` of an actual chunk in the input `ranked_chunks`.
3. **No inference beyond the text:** the claim does not assert relationships, causes, or conclusions that the chunk text does not state. Paraphrasing is permitted; adding information is not.

A claim is NOT grounded when:
- It uses a synonym not present in the chunk text (paraphrase drift) — see troubleshooting.
- It combines information from multiple chunks without attributing each component separately.
- It asserts something the chunk implies but does not state.

When a claim cannot be grounded in any available chunk, it must be omitted from the answer or replaced with explicit qualification.

---

## Guarantees

1. Every factual claim in `output.answer` has a corresponding `citations` entry.
2. Every `chunk_id` in `citations` was present in the input `ranked_chunks` as a `point_id`.
3. `output.coverage_verdict` and `self_report.coverage_verdict` are always identical.
4. When `sub_query_ledger` has any "empty" entry, `coverage_verdict` is "partial" and `qualification` is non-null.
5. Refusal (`ranked_chunks` empty or no supporting text) produces a valid, gate-passing output — never silence, never hallucination.
6. Provenance fields (`document_path`, `title`, `heading_path`, `pdf_page`, `producer`) are copied verbatim from the input chunk's `citation` block. `null` stays `null`.
7. The FROZEN schema is never extended, renamed, or reduced.

---

## Downstream Consumer

The `/ask` skill reads the FROZEN triple. It runs all four gate checks on `self_report`. If `verdict: fail`, the skill re-synthesizes (sonnet → opus → "can't confirm"). If `verdict: pass`, the skill surfaces `output.answer` and `output.citations` to the user, logs the Run Journal event, and records Cost Telemetry.

The user receives `output.answer` as the visible answer and `output.citations` as the verifiable source list. `self_report` is internal — it is used for gate enforcement and Run Journal, never shown directly to the user.
