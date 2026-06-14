# Pattern: Query Reformulation

## When to Reformulate

Reformulation is triggered on `query_type: recall` or `query_type: synthesis_sub` only.
It is never triggered for lexscan or scroll — those modes treat empty results as valid.

Apply reformulation when the initial search produces:
- `candidate_count == 0` (CLI returned no results at all), OR
- `gate_pass == false` after evaluating the three-clause gate condition
  (e.g., candidate_count > 0 but both `dense_hits == 0` and `sparse_hits == 0`, or
  `citationable_count == 0` despite candidates being returned)

If neither condition is met, do not reformulate even if the ranked list seems short.

---

## The One-Reformulation Limit

The retriever is allowed exactly **one** reformulation attempt per dispatch. This is a hard
constraint, not a guideline.

```
reformulations_tried = 0  → can attempt one reformulation
reformulations_tried = 1  → must return current result regardless of gate_pass
```

After a reformulation, re-run the CLI and re-evaluate the gate. Set `reformulations_tried: 1`
in metadata regardless of whether the reformulation succeeded. The skill reads this value to
decide whether to escalate to opus or accept the empty result.

Never loop. Never try a third query. Never escalate from within the retriever.

---

## Reformulation Strategies

Apply in priority order. Stop at the first strategy that produces a different query string.

### 1. Shorten the query

Remove filler words and qualifiers; keep only the core noun phrase or verb phrase.

```
Before: "How does the system handle payload filtering in Qdrant?"
After:  "Qdrant payload filtering"
```

### 2. Synonym substitution

Replace technical terms with common synonyms. Consult the term used in the original query
and substitute one alternative.

```
Before: "vector similarity search"
After:  "nearest neighbor search"

Before: "chunk embedding"
After:  "chunk vector"
```

### 3. Remove stop words and connectives

Strip articles, prepositions, and conjunctions from the query, leaving a keyword cluster.

```
Before: "notes about the architecture of the retrieval pipeline"
After:  "architecture retrieval pipeline"
```

---

## What Not to Do

- Do not expand the query by adding new concepts not present in the original.
- Do not split the query into multiple sub-queries — that is the skill's job at dispatch time.
- Do not change `query_type` during reformulation; a recall stays a recall.
- Do not use a reformulation strategy that produces the same string as the original query
  (verify the strings differ before running).
- Do not apply more than one strategy per reformulation attempt. Keep the change minimal
  and traceable.

---

## Recording Reformulation in the Triple

When a reformulation was tried, the `metadata.query` field reflects the **original**
dispatch query (not the reformulated one), and `reformulations_tried` is `1`. The
reformulated query string is not separately stored in the triple — the skill infers it
occurred from `reformulations_tried == 1` and the verdict.

If the reformulation succeeded (`gate_pass: true`), `verdict: "pass"`.
If the reformulation also failed (`gate_pass: false`), `verdict: "empty"`.

---

## Partial Modality Hits

A partial modality hit means `dense_hits > 0 AND sparse_hits == 0` (or vice versa). The
gate still passes if the other conditions hold. Do not reformulate for a partial modality
hit alone — it is an expected outcome when a query uses domain jargon that one modality
handles better than the other. Reformulation is reserved for zero-results situations, not
imbalanced modality distribution.
