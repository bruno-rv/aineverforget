# Ingest Contract

## Position in Pipeline

Knowledge indexing is the second (and sometimes first) stage of the aineverforget `/ingest` skill:

```
/ingest <paths>
    │
    ├─ [pre-structured source] ─────────────────────────────────────────→ knowledge-indexer
    │                                                                              │
    └─ [unstructured note / transcript]                                            │
            note-summarizer → summary.md ─────────────────────────────→ knowledge-indexer
                                                                                   │
                                                                     {output, metadata, self_report}
                                                                                   │
                                                                         Corpus (Qdrant, active)
                                                                                   │
                                                                         knowledge-retriever
```

`knowledge-indexer` receives a path from the skill (either `summary.md` from `note-summarizer` or the raw source file) and drives the full ingest-verify-promote cycle via the `aineverforget` CLI. It never runs concurrently with another ingest on the same document: the CLI enforces a single-writer lock.

---

## Consumes

- **Source path** — an absolute path to a markdown, text, or PDF file. Either:
  - `summary.md` produced by `note-summarizer` (structured for per-section embedding), or
  - A pre-structured source (a PDF, a markdown note, a meeting transcript) passed directly by the skill.
- **Tags** — an optional list of string tags to apply to all Chunks in the ingest. Passed as `--tag <value>` (one flag per tag) to the CLI.
- **Qdrant connection** — configured in the aineverforget settings file (URL + collection name). The CLI loads settings transparently; the agent does not manage connection params.

The agent does **not** consume:
- Chunk embeddings — the CLI handles BGE-M3 embedding internally
- Qdrant collection schema — the CLI calls `ensure_collection()` transparently
- Any Qdrant client object — the CLI owns the connection

---

## Produces

| Artifact | Location | Owner |
|----------|----------|-------|
| Active Chunks | Qdrant collection | `aineverforget` CLI (via store.promote_generation) |
| `{output, metadata, self_report}` | Returned to skill | `knowledge-indexer` (this domain) |

The agent writes **no files to disk**. Chunks land in Qdrant; the self_report is the structured return value for the skill.

---

## Guarantees

1. Source files are never modified, moved, or deleted. `Read` and `Bash` (read-only inspection) only.
2. `--no-verify` is never used. Every ingest goes through the CLI's lock → load → chunk → embed → upsert(pending) → verify → promote/delete cycle.
3. `cli_result` in `output` is the raw JSON string from `aineverforget ingest --json` stdout — never a parsed object. This is the complete audit record.
4. `ingest_state` is derived from the CLI outcome: `"active"` for `success`, absent/irrelevant for all other outcomes. It is never inferred from assumptions.
5. `probe_verdicts` are populated from `aineverforget verify --json <document_id>` — only called after a `success` outcome. For all other outcomes, probe_verdicts are omitted or null.
6. `cold_start` in self_report mirrors `negative_deferred` from the verify output. It is not computed independently.
7. For `no_op` outcomes: the document content was unchanged (same hash, active generation exists). No new Chunks are created. chunk_count=0, generation=null.

---

## Relationship to Downstream Consumer

Active Chunks produced by a successful ingest are consumed by `knowledge-retriever` via `aineverforget search --json` (hybrid dense+sparse RRF) and `aineverforget lexscan --json` (full-text scroll). The `knowledge-retriever` then returns `ranked_chunks` to `answer-synthesizer` for grounded answer generation.

The `tags` applied at ingest time are stored in each Chunk's payload. Downstream retrieval can filter by tag using `--tag <value>`.

The `generation` number enables the CLI's garbage collection (`aineverforget gc`) to retire superseded Chunks after re-ingests of updated documents.

---

## What This Domain Does Not Own

- Producing `summary.md` — owned by `note-summarizer`
- Embedding model configuration — controlled by aineverforget settings
- Qdrant collection creation and schema migration — the CLI owns this
- Retry and recovery from `index_suspect` or `error` — owned by the skill orchestrator
- Search, lexscan, scroll operations — downstream `knowledge-retriever`
- Answer generation with citations — downstream `answer-synthesizer`
