# Citation Contract

## Two Representations of a Citation

There are two distinct citation representations used in the FROZEN triple. Do not conflate them.

| Representation | Where | Key identifier | What N means |
|---------------|-------|---------------|-------------|
| Inline citation | `output.answer` text | Title + heading (if any) + chunk_index | `citation.chunk_index` — integer index of the chunk within the document (from chunk payload) |
| Structured citation | `output.citations[]` | `chunk_id` (uuid) | `point_id` — the Qdrant point UUID of the specific chunk |

The `chunk_id` uuid and the `chunk_index` integer are different fields and must never be swapped. The inline citation shows a human-readable `chunk N` (chunk_index); the structured entry uses the `point_id` for machine-readable traceability.

---

## Inline Citation Format

```
...claim text [source: Title, chunk N]...
...claim text [source: Title, heading, chunk N]...
```

### Rules

- **Title**: use `citation.title` from the ranked_chunk exactly — do not abbreviate or paraphrase.
- **heading**: use `citation.heading_path` if non-null. If null, omit the heading segment entirely: `[source: Title, chunk N]`.
- **N**: use `citation.chunk_index` (integer). Do not use the `point_id` uuid here.
- Place the citation bracket immediately after the sentence or phrase it supports, before any trailing punctuation of the next sentence.
- When multiple consecutive sentences in the same paragraph all come from the same chunk, one citation at the end of the paragraph is acceptable — but only when all claims are from that single chunk. If claims come from different chunks, cite each claim individually.

### Examples

```
The embedding model stores dense vectors of size 1024 [source: aineverforget PLAN, Architecture, chunk 3].

BGE-M3 supports both dense and sparse retrieval in a single pass [source: ADR-0002, chunk 1].

Notes are ingested through a loader registry keyed by Source type [source: aineverforget PLAN, Identity model, chunk 2].
```

---

## Structured Citations Array

Each entry in `output.citations` has:

| Field | Source | Notes |
|-------|--------|-------|
| `claim` | Verbatim phrase from `output.answer` | Must appear literally in the answer text |
| `chunk_id` | `point_id` from the input ranked_chunk | Must be present in input `ranked_chunks` |
| `document_path` | `citation.document_path` from ranked_chunk | Copy verbatim; never derive |
| `title` | `citation.title` from ranked_chunk | Copy verbatim |
| `heading_path` | `citation.heading_path` from ranked_chunk | Copy verbatim; `null` stays `null` |
| `pdf_page` | `citation.pdf_page` from ranked_chunk | Copy verbatim; `null` for markdown sources |
| `producer` | `citation.producer` from ranked_chunk | Copy verbatim |

**Rule:** All provenance fields are copied verbatim from the input chunk's `citation` block. Never derive or infer them from other context.

---

## Dedup Rules for Synthesis Mode

In synthesis mode the skill fans out N `knowledge-retriever` calls and assembles a union of ranked_chunks, with provenance per sub-query. The same chunk may appear in multiple sub-queries' results. Dedup is on `(claim_text, chunk_id)` tuples:

| Scenario | Citations entry count | Rule |
|----------|----------------------|------|
| Chunk C supports claim X; C was retrieved by sub-queries sq-1 and sq-2 | 1 entry | Same claim + same chunk_id → one entry; sub-query provenance is dropped |
| Chunk C supports claim X in sq-1 AND claim Y in sq-2 (distinct claims) | 2 entries | Different claims → different entries; `chunk_id` repeats |
| Chunk C is retrieved but no claim in the answer is drawn from it | 0 entries | Unused chunks are never cited |
| Chunk C retrieved by sq-1; paraphrase of same claim appears twice | 1 entry | Dedup on the normalized claim text; pick the cleaner phrasing |

**Consequence:** `chunk_id` may repeat across entries in `citations`. That is correct and expected. `all_cited_ids_in_input` checks membership only — it does not require `chunk_id` uniqueness across entries.

---

## Mapping Claims to chunk_id

**During synthesis (LLM judgment step):** for each factual claim you write into `output.answer`, identify the specific ranked_chunk whose `text` most directly supports it. Use the `point_id` of that chunk as the `chunk_id` in the structured citation entry.

**Do not:**
- Cite a chunk for a claim it does not contain (even if it's the closest match).
- Cite multiple chunks for one claim in a single citation entry — one entry, one chunk_id.
- Fabricate a `chunk_id` or use a `document_id` where a `point_id` is required.

**When multiple chunks jointly support one claim:** write the claim at the level of detail supported by each chunk individually and cite each with its own entry (one claim per chunk_id, not a multi-chunk single entry).

---

## Refusal — Citation Array Behavior

When the answer is a refusal (no supporting chunks):

- `citations` is `[]` — an empty array, not null, not omitted.
- `all_claims_cited: true` — trivially true (no claims → no missing citations).
- `all_cited_ids_in_input: true` — trivially true (empty set ⊆ anything).
- `groundedness_pass: true` — trivially true (no citations to check).

A refusal answer with `citations: []` and all booleans `true` is a correct gate-passing output.
