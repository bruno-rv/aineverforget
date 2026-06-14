# Concept: Query Contract

## Overview

The `knowledge-retriever` sits between the `/ask` skill (upstream) and the
`answer-synthesizer` (downstream). The query contract defines what each party is
responsible for and what data flows across each boundary.

---

## Upstream: Skill Decomposes the Question

The `/ask` skill receives the user's raw question and determines the query strategy.
For recall queries it dispatches a single retriever instance. For synthesis queries it
decomposes the question into N sub-queries and dispatches N retriever instances in
parallel (fan-out). For enumeration queries it dispatches a single retriever with the
appropriate `query_type`.

The skill passes to each retriever:

| Field | Type | Notes |
|-------|------|-------|
| `query` | string | The question or sub-query string |
| `query_type` | enum | `recall`, `synthesis_sub`, `enumeration_content`, or `enumeration_metadata` |
| `sub_query_id` | string or null | Assigned by skill for synthesis fan-out; null for recall/enumeration |
| `filters` | object or null | For scroll: `{tag, since}`; unused for other modes |

The retriever treats these fields as authoritative. It does not re-interpret the original
user question and does not decide query_type.

---

## The sub_query_id Contract

For synthesis fan-out, the skill assigns a unique `sub_query_id` to each sub-query before
dispatch. The retriever echoes this value in `metadata.sub_query_id` without modification.
The downstream synthesizer uses `sub_query_id` to reassemble parallel results into a
coherent ranked set before synthesis.

Rules:
- Retriever never generates a `sub_query_id`. It only echoes what the skill assigned.
- For `query_type: recall` or any enumeration type, `sub_query_id` is always `null`.
- The skill guarantees sub_query_ids are unique per `/ask` invocation.
- If the skill omits `sub_query_id` on a `synthesis_sub` dispatch, the retriever sets it to `null` and includes a note in `metadata.query` indicating the gap.

---

## Downstream: answer-synthesizer Consumes ranked_chunks

The answer-synthesizer receives the FROZEN triple from one or more retriever instances.
It consumes `output.ranked_chunks` to ground its answer and `metadata` to understand
context. Key downstream expectations:

- `ranked_chunks` entries each carry a complete `citation` sub-object. The synthesizer
  uses this directly for inline citation — it does not re-look up metadata.
- For synthesis fan-out, the synthesizer receives N triples (one per sub-query), identified
  by `sub_query_id`. It merges ranked_chunks across all sub-queries, deduplicates by
  `point_id`, and re-ranks by score.
- For scroll mode, `ranked_chunks` is `[]`. The synthesizer uses the document metadata
  from `self_report.candidate_count` to answer "what do I have?" questions without chunk
  content.
- The synthesizer never sees the raw CLI output. It only ever sees the FROZEN triple.
  The retriever is the sole translator between CLI JSON and the contract schema.

---

## Data Flow Diagram

```
User question
      |
      v
  /ask skill
  ├── decompose into query/sub-queries
  ├── assign query_type, sub_query_id
  └── dispatch → knowledge-retriever (1..N instances)
                        |
                   [run CLI]
                   [map JSON]
                   [evaluate gate → self_report]
                        |
                        v
              {output, metadata, self_report}
                        |
                        v
             answer-synthesizer
             ├── merge ranked_chunks
             ├── ground answer in text
             └── construct citations from citation sub-objects
```

---

## What the Retriever Never Does

- Does not re-score or re-rank candidates beyond CLI output order.
- Does not filter candidates by score threshold (that is a skill or synthesizer concern).
- Does not modify `sub_query_id`.
- Does not merge results from multiple CLI calls (except the optional single reformulation,
  which replaces rather than merges the original result).
- Does not communicate directly with the synthesizer — it only returns the triple to the skill.
