# Answer Synthesis Quick Reference

> Fast lookup for synthesizing grounded answers from ranked chunks.

---

## Gate Evaluation Checklist (pre-return)

| # | Check | How to verify | Pass condition |
|---|-------|--------------|---------------|
| G1 | `all_claims_cited` | Every factual sentence in `output.answer` has a `citations` entry whose `claim` matches it | `true` when all claims are mapped; trivially `true` for refusal (no claims) |
| G2 | `all_cited_ids_in_input` | Every `chunk_id` in `citations` was in the input `ranked_chunks` as a `point_id` | Set membership: `cited_ids ⊆ input_point_ids`; trivially `true` when citations is `[]` |
| G3 | `groundedness_pass` | Each citation's cited chunk text shares ≥1 key term (4+ char, non-stopword) with the `claim` | Bash lexical overlap; trivially `true` for refusal |
| G4 | `coverage_ledger_consistent` | Ledger has any "empty" → verdict "partial" + qualification non-null; all "answered" → verdict "complete" | Boolean logic on `sub_query_ledger` values |

All four must be `true` for `verdict: pass`. Any `false` → `verdict: fail` (skill re-synthesizes).

---

## coverage_verdict Decision Table

| sub_query_ledger state | coverage_verdict | qualification | unresolved_sub_queries |
|-----------------------|-----------------|--------------|------------------------|
| All entries "answered" | "complete" | `null` | `[]` |
| One or more entries "empty" | "partial" | Non-null string naming empty sub-queries | List of "empty" keys |
| All entries "empty" (total miss) | "partial" | Non-null string | All ledger keys |
| `ranked_chunks` empty list (refusal) | "partial" | Non-null string | All ledger keys (or [question]) |
| Ledger has one key "answered" (recall pass) | "complete" | `null` | `[]` |

**Rule:** `output.coverage_verdict == self_report.coverage_verdict` always. They must be identical.

---

## Refusal Triggers

Refusal applies to `ask_type: recall` and `ask_type: synthesis` only.

| Condition | What to do |
|-----------|-----------|
| `ranked_chunks` is `[]` (recall/synthesis) | Produce refusal output (answer = can't confirm; citations = []; verdict = pass) |
| All sub-query ledger entries are "empty" (synthesis) | Produce refusal output; qualify with all sub-query names |
| No chunk text overlaps the question after attempting synthesis | Produce partial answer for what is supported; refuse/qualify for what is not |
| `ranked_chunks` key entirely absent from payload | Return triple with `verdict: parse_error` |
| Enumeration (lexscan/scroll) returns zero results | NOT a refusal. Lexscan is exhaustive — zero hits = complete answer. `coverage_verdict: complete`; answer states zero count explicitly. |

**Never:** imply full coverage when partial. **Never:** hallucinate claims without chunk support. **Never:** return `verdict: fail` for a refusal — refusal is a passing output. **Never:** treat zero enumeration results as refusal or partial.

---

## Inline Citation Format

```
...claim text [source: Title, chunk N]...
...claim text [source: Title, heading, chunk N]...
```

- **Title** = `citation.title` from the ranked_chunk
- **heading** = `citation.heading_path` — include only if non-null; omit the segment entirely if null
- **N** = `citation.chunk_index` (integer from the chunk's payload) — this is NOT the `point_id` uuid
- Place the citation immediately after the sentence or phrase it supports
- One citation per claim, not per sentence when multiple claims share a paragraph

---

## Dedup Rules (synthesis mode)

| Scenario | Action |
|----------|--------|
| Same `chunk_id` cited for two different claims | Two `citations` entries, same `chunk_id`, different `claim` fields |
| Same claim, same `chunk_id` from two sub-queries | One `citations` entry (dedup on `(claim, chunk_id)` tuple) |
| Same chunk retrieved by two sub-queries, zero claims from it | No citation entry — unused chunks are not cited |

---

## Self-Report Fields — Source of Truth

| Field | Source |
|-------|--------|
| `all_claims_cited` | LLM judgment during synthesis + Bash verification that claim phrases appear in answer |
| `all_cited_ids_in_input` | Bash set-membership check |
| `groundedness_pass` | Bash lexical overlap (≥1 shared key term per citation) |
| `coverage_verdict` | Must mirror `output.coverage_verdict` exactly |
| `coverage_ledger_consistent` | Bash logic on `sub_query_ledger` vs `output.coverage_verdict` + `output.qualification` |
| `unresolved_sub_queries` | List of ledger keys whose value is "empty" |
| `verdict` | "pass" if all 4 booleans true; "fail" otherwise; "parse_error" for malformed payload |

---

## Common Pitfalls

| Don't | Do |
|-------|----|
| Fabricate a claim not in any chunk | Omit the claim or produce explicit refusal |
| Use a `point_id` uuid as the chunk N in inline citations | Use `citation.chunk_index` (integer) for inline; `point_id` is `chunk_id` in structured citations |
| Set coverage_verdict "complete" when any ledger entry is "empty" | coverage_verdict must be "partial"; add qualification |
| Leave qualification `null` when coverage_verdict is "partial" | qualification must be non-null with "partial" |
| Return `verdict: fail` for a refusal | Refusal is `verdict: pass`; fail means a gate boolean is `false` |
| Copy `heading_path` as non-null when it is null in input | Always copy verbatim; `null` stays `null` |
| Add extra fields to the FROZEN schema | Schema is immutable |

---

## Related Documentation

| Topic | Path |
|-------|------|
| Answer/citation contract | `concepts/answer-contract.md` |
| Inline citation + dedup patterns | `patterns/citation-contract.md` |
| Groundedness failures + guards | `reference/troubleshooting.md` |
| FROZEN self-report schema | `../../shared/self-report-contract.md` |
| Upstream retriever | `../../agents/dev/knowledge-retriever.md` |
