# KB Domain: knowledge-retrieval

## What This Domain Covers

The `knowledge-retrieval` domain governs how the `knowledge-retriever` agent translates a
skill-dispatched query into a CLI call, maps the raw response into the FROZEN self-report
contract, and reports counts for the skill's gate evaluation. It does not cover ingestion,
answer synthesis, or skill orchestration.

---

## The Three Search Modes

### hybrid (search --json)

Used for `query_type: recall` and `query_type: synthesis_sub`. The CLI performs dense
vector search plus sparse BM25 retrieval, fuses results via Reciprocal Rank Fusion (RRF),
and returns the top-k candidates. Pre-fusion per-modality counts (`dense_hits`,
`sparse_hits`) are returned alongside the fused `candidates` list, which enables the skill
to evaluate the gate without re-running individual searches.

The gate applies to this mode: `candidate_count >= 1 AND (dense_hits >= 1 OR sparse_hits >= 1) AND citationable_count >= 1`.

### lexscan (lexscan --json)

Used for `query_type: enumeration_content`. Performs an exhaustive keyword sweep across
all indexed chunks. Returns every chunk containing the term, plus per-document and total
occurrence counts. Designed for exact-term lookups where completeness matters more than
relevance ranking. No gate applies — an empty result is valid (the term is simply absent).
`dense_hits` and `sparse_hits` are always `-1` in the self-report.

### scroll (scroll --json)

Used for `query_type: enumeration_metadata`. Returns document-level metadata (no chunk
text, no scores) across the entire corpus, optionally filtered by `--tag` or `--since`.
`ranked_chunks` is always `[]` in the self-report for this mode — there is no text to
surface. Useful for "what do I have?" questions before committing to a search. No gate
applies. `dense_hits` and `sparse_hits` are always `-1`.

---

## When Each Mode Is Used

| query_type | search_mode | Gate applies? | CLI command |
|------------|-------------|--------------|-------------|
| recall | hybrid | Yes | `aineverforget search --json "<query>"` |
| synthesis_sub | hybrid | Yes | `aineverforget search --json "<sub-query>"` |
| enumeration_content | lexscan | No | `aineverforget lexscan --json "<term>"` |
| enumeration_metadata | scroll | No | `aineverforget scroll --json [--tag X] [--since Y]` |

---

## Scope of This Domain

| In scope | Out of scope |
|----------|-------------|
| CLI invocation patterns per mode | Ingestion pipeline (`aineverforget ingest`) |
| Count mapping (candidate_count, dense_hits, sparse_hits, citationable_count) | Answer synthesis (answer-synthesizer agent) |
| ranked_chunks construction per mode | Skill orchestration (fan-out, gate enforcement) |
| One-reformulation rule | Multi-reformulation or opus escalation (skill's domain) |
| Gate condition expression | Running the gate enforcement (skill runs it) |

---

## Files in This Domain

| File | Purpose |
|------|---------|
| `index.md` | This file — domain overview and mode table |
| `quick-reference.md` | CLI flags per mode; gate evaluation table; count source table |
| `concepts/query-contract.md` | upstream→retriever→downstream data flow; sub_query_id protocol |
| `patterns/query-reformulation.md` | When and how to reformulate; one-reformulation limit |
| `reference/troubleshooting.md` | Known failure modes and corrective actions |
