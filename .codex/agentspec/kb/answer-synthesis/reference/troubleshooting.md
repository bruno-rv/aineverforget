# Troubleshooting: Answer Synthesis

> Known failure modes and their fixes. Check here before improvising.

---

## Groundedness Failures

### Claim Uses Synonym Not Present in Chunk

| Symptom | `groundedness_pass: false` — Bash overlap check finds no shared key terms between claim and chunk text, but the claim is semantically correct |
|---------|---|
| Cause | LLM paraphrased the chunk's wording rather than quoting or closely paraphrasing it — e.g., chunk says "stores embeddings" but claim says "persists vector representations" |
| Fix | Rewrite the claim to use at least one noun, verb, or proper name that appears literally in the chunk text. The check is lexical, not semantic — synonyms fail it. |
| Fallback | If you cannot reword without distorting meaning, cite the chunk verbatim in quotes: `"stores embeddings"` and then add interpretation as an uncited editorial aside — or omit the claim entirely. |

### Paraphrase Drift Across Multiple Chunks

| Symptom | Answer synthesizes a conclusion that no single chunk states; `groundedness_pass` fails for the claim |
|---------|---|
| Cause | Map-reduce step combined information from multiple chunks and stated a conclusion that only follows from their combination, not from either chunk individually |
| Fix | Break the conclusion into its atomic components. Each component must be supportable by a single chunk. If the combined conclusion is the only meaningful thing to say, it must be explicitly framed as an inference: "Based on [claim A from chunk X] and [claim B from chunk Y], this suggests…" — and the inference itself remains uncited. |

### Stopword-Only Overlap

| Symptom | Bash overlap check passes but the "shared terms" are all stopwords ("that", "from", "with") — `groundedness_pass: true` but answer may still be weakly grounded |
|---------|---|
| Cause | The claim and chunk text share only short or common words; the overlap threshold is 4-character non-stopword tokens |
| Fix | This should not happen if stopwords are correctly filtered. If it does, verify the stopword list in the Bash gate script includes the failing words. Add new stopwords to the script as needed. |

---

## coverage_verdict Inconsistency

### partial verdict without qualification

| Symptom | `coverage_ledger_consistent: false` — `coverage_verdict` is "partial" but `qualification` is `null` |
|---------|---|
| Cause | Forgot to populate qualification when setting partial coverage |
| Fix | Whenever coverage_verdict is "partial", qualification must be a non-null string naming the sub-queries with no content: "Based on available notes: [answer]. The following sub-questions had no matching content: [list]." |

### complete verdict with empty sub-queries

| Symptom | `coverage_ledger_consistent: false` — ledger has "empty" entries but `coverage_verdict` is "complete" |
|---------|---|
| Cause | Incorrectly set coverage_verdict to "complete" when some sub-queries returned no ranked_chunks |
| Fix | Count the "empty" values in `sub_query_ledger`. Any "empty" → partial. There are no exceptions. |

### output and self_report coverage_verdict mismatch

| Symptom | `output.coverage_verdict == "complete"` but `self_report.coverage_verdict == "partial"` (or vice versa) |
|---------|---|
| Cause | The two fields were set independently and drifted |
| Fix | Set one authoritative value based on the ledger, then copy it to both locations. They must always be identical. |

---

## Hallucination Guards

### Claim Not Traceable to Any Chunk

| Symptom | During synthesis, a claim is written for which no `chunk_id` can be assigned; or `all_cited_ids_in_input: false` because a fabricated uuid was used |
|---------|---|
| Cause | LLM drew on training knowledge rather than chunk text |
| Fix | Remove the claim entirely. If the information is genuinely important and not in any chunk, qualify it: "This is not in your current notes." Do not introduce training-knowledge claims into grounded answers. |

### chunk_id Not in Input

| Symptom | `all_cited_ids_in_input: false` — a `chunk_id` in citations does not appear in the input `ranked_chunks` |
|---------|---|
| Cause | `point_id` was incorrectly copied, or a `document_id` was used instead of `point_id`, or a chunk_id was fabricated |
| Fix | Every `chunk_id` must come directly from `ranked_chunks[i].point_id`. Never fabricate. If you cannot find the supporting chunk_id in the input, the claim must be removed. |

---

## All Chunks Marginally Relevant

| Symptom | The input ranked_chunks are topically related but none directly answers the question; synthesis produces a weakly grounded answer |
|---------|---|
| Cause | Retrieval returned borderline matches; the question is more specific than any chunk covers |
| Fix | Ground only what the chunks actually say. For the parts of the question not addressed, use the partial coverage path: set `coverage_verdict: partial`, add a qualification, and do not fabricate specifics. A partial honest answer beats a complete hallucinated one. |
| Downstream | The skill may re-synthesize with opus on `verdict: fail` from groundedness; a partial qualified answer with all booleans true is preferred over a failed synthesis. |

---

## Schema and Provenance Errors

| Symptom | Provenance field in citations differs from what was in the input ranked_chunk |
|---------|---|
| Cause | Field was derived or inferred rather than copied verbatim |
| Fix | Copy `document_path`, `title`, `heading_path`, `pdf_page`, `producer` verbatim from `ranked_chunk.citation`. If the input value is `null`, the output value must be `null`. Never derive these from `document_path` patterns or title guesses. |

| Symptom | `heading_path` appears in inline citation when input had `null` |
|---------|---|
| Cause | Agent invented a heading path |
| Fix | Inline citation format includes heading only when `citation.heading_path` is non-null. When null, format is `[source: Title, chunk N]` — the heading segment is omitted entirely. |
