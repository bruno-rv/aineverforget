# Troubleshooting — knowledge-retrieval

## Symptom: Empty Results (candidate_count == 0)

**Most likely causes:**

1. **Query terms are not in the corpus.** The user is asking about something that was
   never ingested. Check with scroll to see what documents exist: `aineverforget scroll --json`.
   If the corpus is small or new, empty results are expected and valid.

2. **Query is too specific.** A long, precise question may not match any chunk because
   the phrasing differs from how the content was indexed. Apply the shortening strategy
   from `patterns/query-reformulation.md`.

3. **Embeddings not yet available.** If a document was very recently ingested (same
   session), dense retrieval may not reflect it yet. Lexscan (`aineverforget lexscan`)
   operates on text and is immune to this — use it for immediate post-ingest recall.

**Corrective action in the retriever:** Try one reformulation. If still empty, set
`verdict: "empty"` and return. Skill decides next step.

---

## Symptom: gate_pass == false with candidate_count > 0

This means results were returned but the gate failed. The most common sub-case is
`citationable_count == 0` — candidates exist but none have a `document_path`. This
indicates a data integrity issue in the index (chunks without source path metadata).

**Corrective action:** Return with `gate_pass: false`, `verdict: "empty"`. Do not
reformulate — the query worked; the data is the problem. The skill should surface this
to the user.

The other sub-case is `dense_hits == 0 AND sparse_hits == 0` with `candidate_count > 0`.
This should not occur if the CLI is functioning correctly, as it would mean candidates
appeared from nowhere. If observed, treat it as a `cli_error` and report accordingly.

---

## Symptom: Partial Modality Hits (one of dense_hits or sparse_hits is 0)

Dense-only hits: the query matched semantically similar chunks but the exact terms are
absent. Common for paraphrased or conceptual queries. Result is still valid if
`candidate_count >= 1` and `citationable_count >= 1`.

Sparse-only hits: the exact terms matched but the query is too domain-specific for
dense embeddings to catch. Result is still valid under the same conditions.

**Corrective action:** None. Partial modality hits are not a failure mode. Do not
reformulate. The gate OR-clause (`dense_hits >= 1 OR sparse_hits >= 1`) is
intentionally permissive.

---

## Symptom: lexscan Returns Zero Chunks for a Known Term

**Most likely causes:**

1. **Inflection mismatch.** Lexscan is an exact-match sweep. "marmots" will not match
   "marmot". If the user asked for a singular form but the corpus uses plural (or vice
   versa), try the alternate inflection via reformulation in the skill (not the retriever —
   this is the skill's concern for enumeration queries).

2. **Term appears in a PDF page not yet OCR-processed.** Text extraction from PDFs
   can be incomplete. The chunk may exist without the term in queryable text.

3. **Term is only in a heading.** Depending on chunking strategy, heading text may be
   included in the `heading_path` field but not in `text`. Lexscan targets `text`.

**Corrective action in the retriever:** None. Gate does not apply; return `verdict: "pass"`
with `candidate_count: 0`. Skill surfaces to user.

---

## Symptom: scroll Returns All Documents (No Filter Applied)

When `aineverforget scroll --json` is called without `--tag` or `--since`, it returns
the full document inventory. This is the correct behavior for "what do I have?" queries.
It is not an error.

If the skill dispatched scroll without filters but the user intended a filtered view,
that is a skill-level dispatch error, not a retriever error. The retriever executes
exactly the command given by the dispatch payload.

---

## Symptom: CLI Exits Non-Zero

Check stderr for the specific error. Common causes:

| Error message pattern | Likely cause |
|----------------------|-------------|
| `ConnectionRefusedError` / `connection refused` | Qdrant is not running; restart it |
| `collection not found` | Collection was dropped or never created |
| `invalid token` / `auth` | Qdrant API key mismatch |
| `command not found: aineverforget` | CLI not on PATH; check virtualenv activation |

**Corrective action in the retriever:** Return `verdict: "cli_error"` with the stderr
message in the `metadata.query` field suffix (append ` [cli_error: <stderr>]`). Do not
reformulate for CLI errors — the problem is infrastructure, not the query.
