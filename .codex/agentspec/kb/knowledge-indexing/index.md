# Knowledge Indexing KB

> **Purpose:** CLI-driven ingest of source paths into the aineverforget Corpus, with verify-based promotion to active state.
> **Owner:** `knowledge-indexer` agent
> **Validated:** 2026-06-14 — confirmed against src/aineverforget/cli.py, ingest.py, verify.py

---

## Quick Navigation

### Concepts

| File | What it covers |
|------|---------------|
| `concepts/ingest-contract.md` | Upstream/downstream contract: what knowledge-indexer consumes, produces, guarantees, and leaves untouched |

### Patterns

| File | What it covers |
|------|---------------|
| `patterns/probe-calibration.md` | How to read probe_results, what cold_start means, when negative_deferred is correct |

### Reference

| File | What it covers |
|------|---------------|
| `reference/troubleshooting.md` | index_suspect scenarios (MatchText multi-word failure, cold-start confusion, hash collision no_op), recovery steps |

---

## Key Concepts

| Concept | Definition |
|---------|-----------|
| Ingest | CLI-driven pipeline: load → chunk → embed → upsert(pending) → verify → promote(active) or delete(failed) |
| outcome | Per-path result from `aineverforget ingest --json`: `success`, `no_op`, `index_suspect`, `error`, `skipped` |
| ingest_state | Chunk lifecycle state. Only `active` chunks are served. `pending` is temporary during verify; `failed`/retired are deleted. |
| generation | Monotonically increasing integer. Each new ingest of a document gets G+1. The max active generation is authoritative. |
| no_op | Content hash unchanged and active generation exists; ingest skips. Returns chunk_count=0, generation=null. |
| index_suspect | Verify failed; pending batch deleted; prior active (if any) stays served. The skill handles recovery. |
| skipped | Loader returned encrypted or scanned PDF verdict; path not indexed. |
| cold_start | No other active Documents exist in the Corpus. The negative probe is deferred because it cannot discriminate. |
| negative_deferred | The negative probe was skipped due to cold_start. This is correct behavior, not a failure. |
| cli_result | Raw JSON string from `aineverforget ingest --json`. Stored as audit trail in self_report. |
| probe_verdicts | Derived from `aineverforget verify --json` after a success outcome. Maps probe_type → "pass"/"fail". |

---

## When Is knowledge-indexer Invoked?

The agent is the second stage of the `/ingest` skill pipeline:

```
/ingest <paths>
    │
    ├─ [if pre-structured source] knowledge-indexer directly
    │
    └─ [if unstructured note/transcript]
            note-summarizer → summary.md → knowledge-indexer
```

The agent is also invocable standalone for direct indexing of markdown, text files, or PDFs that do not need prior summarization.

---

## Learning Path

| Step | Read | Goal |
|------|------|------|
| 1 | `concepts/ingest-contract.md` | Understand upstream/downstream boundaries |
| 2 | `quick-reference.md` | CLI commands, flag spellings, outcome decision matrix |
| 3 | `patterns/probe-calibration.md` | Read probe_results and cold_start correctly |
| 4 | `reference/troubleshooting.md` | Known failure modes before improvising |

---

## Agent Usage

| Task | Agent | When |
|------|-------|------|
| Index summary.md after note-summarizer | `knowledge-indexer` | summary.md written to disk |
| Index pre-structured source directly | `knowledge-indexer` | Source is markdown or PDF |
| Retrieve indexed content | `knowledge-retriever` | After active generation exists |
| Answer questions with citations | `answer-synthesizer` | After knowledge-retriever returns chunks |

---

## Boundaries

**This domain owns:** CLI invocation, JSON result parsing, verify call, probe_verdicts mapping, self_report assembly.

**This domain does NOT own:**
- summary.md production — owned by `note-summarizer`
- Qdrant collection management — the CLI handles this transparently
- Chunk embedding model selection — controlled by `aineverforget` settings
- Retry logic, recovery from index_suspect — owned by the skill orchestrator
- Search and retrieval — downstream `knowledge-retriever`
