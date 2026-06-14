# Troubleshooting: Knowledge Indexing

> Known failure modes and their diagnosis steps. For each scenario: what the self_report looks like, why it happened, and what the skill should do. The agent itself never acts on failures — it returns them as-is and the skill decides recovery.

---

## index_suspect: MatchText Multi-Word Failure

**Symptom:** `outcome == "index_suspect"`, verify output shows:
```
specific FAIL: expected_substring='<word>' not found in any of N pending-gen chunks
```
But the document clearly contains the content.

**Cause:** The CLI derives the `specific` probe word by taking the first word ≥5 characters, non-stopword, from chunk text. If the chunker split content such that the specific word lands at a chunk boundary and is partially cut, MatchText may find the chunk but the Python substring check (`sub in chunk.text.lower()`) may fail if the word is truncated.

More commonly this indicates that the probe word, while present in the source file, did not land in any active Chunk — either because chunking split the relevant sentence across two chunks and neither half contains the full word, or because the source file's encoding produced a different character sequence than expected.

**What self_report looks like:**
```json
{
  "self_report": {
    "probe_verdicts": {"topical": "pass", "specific": "fail", "negative": "pass"},
    "verdict": "index_suspect"
  }
}
```

**Recovery (skill-level):** The skill may re-run with a different `--source-id` or check if the source file is well-formed. If the document consistently fails specific probes, the skill should surface this to the user — the content structure may be incompatible with the auto-derived specific probe word selection.

---

## index_suspect: Hash Collision no_op Confusion

**Symptom:** Skill expected a fresh ingest but got `outcome == "no_op"`, `chunk_count == 0`.

**Cause:** The CLI computes `document_sha256` from the loaded text content. If the source file appears to have changed but the loaded text is identical (e.g., only metadata, encoding, or whitespace changed in ways the loader normalizes), the hash matches the active generation and the ingest is a no_op. This is correct behavior — the Corpus already contains this content.

**What self_report looks like:**
```json
{
  "output": {"document_id": "<uuid>", "ingest_generation": null, "chunk_count": 0, "ingest_state": "active", "cli_result": "..."},
  "self_report": {"verdict": "no_op"}
}
```

**Recovery (skill-level):** No action needed. The Corpus is already up to date. If the skill is certain content changed and a re-index is required, the source file content (not just metadata) must differ. `gc` can be run to clean up superseded generations.

---

## cold_start Confusion: negative_deferred Unexpected

**Symptom:** `cold_start == true` in self_report when the Corpus is not empty.

**Cause:** Cold-start is detected by checking whether any active Chunks exist with `document_id != <current document_id>`. If previous Documents were gc'd or if their active generations were deleted, the Corpus may have Chunks only for the current document being indexed, triggering cold_start even though this is not the first-ever ingest.

This is not an error. The negative probe is meaningless when the current document is the only active one. `negative_deferred = true` is correct regardless of whether this is the absolute first ingest or just the only currently-active document.

**What self_report looks like:**
```json
{
  "self_report": {
    "probe_verdicts": {"topical": "pass", "specific": "pass", "negative": "pass"},
    "negative_deferred": true,
    "cold_start": true,
    "verdict": "indexed"
  }
}
```

**Recovery:** No action needed. The ingest succeeded. The negative probe will run on the next ingest of a different document, when an unrelated active Document exists as a competitor.

---

## Lock Overlap: Concurrent Ingest Running

**Symptom:** `aineverforget ingest` exits with code 3. JSON output:
```json
{"error": "lock_overlap", "message": "..."}
```

**Cause:** A concurrent `aineverforget ingest` process already holds the run lock. The CLI is single-writer enforced via a lock file in the `runs/` directory. Parallel ingest calls from multiple agents or skill invocations will collide.

**What self_report looks like:**
```json
{
  "output": {"document_id": null, "ingest_generation": null, "chunk_count": 0, "ingest_state": null, "cli_result": "{\"error\":\"lock_overlap\",...}"},
  "self_report": {"verdict": "lock_overlap"}
}
```

**Recovery (skill-level):** The skill should wait for the concurrent ingest to complete (the lock is released on process exit) and retry. The agent never retries — it returns lock_overlap and the skill decides timing.

---

## error: Loader Verdict (encrypted / scanned PDF)

**Symptom:** `outcome == "skipped"`, `loader_verdict == "encrypted"` or `"scanned"`.

**Cause:** The PDF loader detected that the file is password-protected (encrypted) or image-only (scanned without OCR). The CLI does not error — it returns `skipped` as the outcome for these paths. This is expected behavior, not a failure.

**What self_report looks like:**
```json
{
  "output": {"chunk_count": 0, "ingest_state": null, "cli_result": "..."},
  "self_report": {"verdict": "skipped"}
}
```

**Recovery (skill-level):** Report to user that the PDF cannot be indexed. Encrypted PDFs need decryption first. Scanned PDFs need OCR pre-processing before ingest.

---

## topical Probe Fail: Document Not Surfacing in Search

**Symptom:** `probe_verdicts.topical == "fail"`, verify detail contains:
```
topical FAIL: document_id=... NOT found in top-10 hybrid results for query='...'
```

**Cause:** The pending generation's Chunks did not rank in the top-10 for the auto-derived topical query (document title or first 10 words of first chunk). This can happen when:
- The topical query is too generic (e.g., the title is "Notes" with no distinctive terms)
- The Corpus is large and many other active Documents compete strongly for the query
- The embedding model scored the Document's content as low-similarity to its own title

This results in `index_suspect = true`. The pending batch is deleted; prior active generation (if any) stays served.

**Recovery (skill-level):** The skill may re-run ingest. If the same document consistently fails topical probes, the issue is likely a generic/uninformative title or first chunk. The skill should surface this to the user. There is no flag to override the probe derivation — the CLI auto-derives from stored chunk payloads.
