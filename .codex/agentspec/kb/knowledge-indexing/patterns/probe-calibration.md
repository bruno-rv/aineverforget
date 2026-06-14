# Probe Calibration

> How to read probe_results from `aineverforget verify --json`, what cold_start means, and when negative_deferred is correct rather than problematic.

---

## The Three Probe Types

The CLI's verify step runs up to three probe types against the pending generation in the verification view. The view is a Qdrant filter that includes both active Corpus Chunks and the newly-upserted pending Chunks — probes compete against realistic candidates, not just the Document's own content.

| Probe type | Query strategy | Pass condition |
|------------|---------------|----------------|
| `topical` | Topic-level query (from document title or first 10 words of first chunk). Hybrid dense+sparse RRF search. | The pending generation's `document_id` appears in top-k results. |
| `specific` | A distinctive single word from a chunk (≥5 chars, not a stopword). Lexical MatchText scroll. | A pending-gen chunk contains the `expected_substring` in its text. |
| `negative` | Fixed unrelated nonsense query (`"xyzzy_nonexistent_term_for_negative_probe_aineverforget"`). Hybrid search. | The pending `document_id` does NOT appear in top-k results. |

---

## Reading probe_results

Each entry in the `probe_results[]` array from `verify --json` has these fields:

```json
{
  "probe_type": "topical",
  "query": "Meeting summary Q2 budget review",
  "expected_substring": null,
  "passed": true,
  "deferred": false,
  "matched_chunk_ids": ["...uuid..."],
  "detail": "topical PASS: document_id=... gen=2 found in top-10 hybrid results for query='Meeting summary Q2 budget review'"
}
```

**Mapping to `probe_verdicts` in self_report:**
- `passed == true` → `"pass"`
- `passed == false AND deferred == false` → `"fail"`
- `passed == true AND deferred == true` → `"pass"` (deferred counts as pass)

The `detail` field is diagnostic text. It always starts with the probe type and verdict in caps: `topical PASS`, `specific FAIL`, `negative DEFERRED`, etc.

---

## What Cold-Start Means

**Cold-start** is the condition where no other active Document exists in the Corpus at the time of ingest. This happens when:
- The document being ingested is the very first ingest ever (empty Corpus), or
- All previously active documents have been gc'd, re-ingested as no_op, or retired, leaving only the current pending batch with no unrelated active Documents.

Detection: the CLI checks whether any active Chunks exist with `document_id != <current document_id>`. If none, cold_start is detected.

**Consequence:** the negative probe is meaningless in a cold-start context. With only one Document in the Corpus (the one being indexed), the negative probe's nonsense query will simply return no results — vacuously passing for the wrong reason — or may surface the document if the embedding model assigns it low-but-positive similarity. Neither result is informative.

**Resolution:** the CLI defers the negative probe. The `ProbeResult.deferred = true` and `VerifyVerdict.negative_deferred = true`. The overall `passed` remains `true` so long as topical and specific probes pass. Deferral is correct behavior.

---

## When negative_deferred Is Correct

`negative_deferred = true` in the verify output (and `cold_start = true` in self_report) is expected and correct in these situations:

1. **First ever ingest** — Corpus was empty. Negative probe deferred. Return `verdict: "indexed"`.
2. **Second ingest, same source, no_op** — The first succeeded, so G=1 active exists. BUT if this is the only document, cold_start still applies for a fresh content-changed re-ingest.
3. **Bulk initial load** — If all ingests in a session are the first batch, earlier successful ingests become "unrelated active" for later ones in the same session. The second document's negative probe will NOT be deferred if the first already promoted.

**`negative_deferred` is NOT correct** (and should appear as a troubleshooting flag) when:
- Multiple active Documents exist and negative_deferred is still true — this would indicate a bug in the cold_start detection logic, not expected usage.

---

## Probe Verdict Summary Table

| `passed` | `deferred` | Meaning | probe_verdicts entry |
|----------|-----------|---------|---------------------|
| `true` | `false` | Probe ran and passed | `"pass"` |
| `false` | `false` | Probe ran and failed | `"fail"` |
| `true` | `true` | Negative probe deferred (cold_start) | `"pass"` |
| `false` | `true` | Should not occur (deferred probes are always marked passed=true by the CLI) | Treat as `"pass"` + log anomaly |

---

## Specific Probe Limitation: Multi-Word Terms

The `specific` probe uses Qdrant MatchText, which tokenizes the query string. Multi-word queries may not match if the tokenizer breaks them differently than expected. The CLI's auto-derived specific probe uses a **single word** (first word ≥5 chars, non-stopword from chunk text) to avoid this limitation.

If a specific probe fails (`passed = false`), the failure detail will contain:
```
specific FAIL: expected_substring='<word>' not found in any of N pending-gen chunks
```

This means the specific word was not found in the chunk text using case-insensitive substring match. This is a genuine content retrieval failure — not a MatchText tokenization issue — because the check is a Python `in` operator after MatchText scroll, not pure MatchText. See `troubleshooting.md` for diagnosis steps.

---

## Self-Report Assembly from Verify Output

```python
# Pseudo-code for probe_verdicts mapping
probe_verdicts = {}
cold_start = verify_output["negative_deferred"]

for probe_result in verify_output["probe_results"]:
    ptype = probe_result["probe_type"]  # "topical", "specific", "negative"
    if probe_result["passed"]:          # includes deferred (deferred=passed=true)
        probe_verdicts[ptype] = "pass"
    else:
        probe_verdicts[ptype] = "fail"
```

The `negative_deferred` field from verify output maps directly to both `probe_verdicts.negative = "pass"` (because `passed=true` when deferred) and `cold_start = true` in self_report.
