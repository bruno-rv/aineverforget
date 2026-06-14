# KB Domain: note-summarization

**Owner:** note-summarizer agent
**Downstream consumer:** knowledge-indexer agent
**Pipeline position:** Stage 1 of /ingest (unstructured sources only)

---

## What This Domain Covers

The `note-summarization` KB domain defines how `note-summarizer` transforms raw notes, meeting transcripts, and unstructured prose into `summary.md` files optimized for Qdrant/RAG indexing.

This domain does NOT cover:
- Pre-structured documents (already organized with `##` headings) — those bypass summarization entirely and go directly to knowledge-indexer.
- Vector storage or chunk indexing — that is the `knowledge-indexer` domain.
- Claim-level faithfulness evaluation — that is a dev-time eval per ADR-0003, not a runtime concern.

---

## Routing: When This Domain Applies

```text
/ingest <source>
    |
    ├── source has 3+ ## headings?
    |       YES → skip note-summarizer → knowledge-indexer directly
    |
    └── NO (raw note / transcript / unstructured prose)
            → note-summarizer (this domain)
            → produces summary.md
            → knowledge-indexer
```

The routing decision is made by the skill (orchestrator). The agent itself checks for pre-structured sources as a stop condition and flags accordingly in `self_report`.

---

## Domain Files

| File | Purpose |
|------|---------|
| `index.md` (this file) | Overview, routing logic, domain scope |
| `quick-reference.md` | Decision matrix — which sections to include, compression edge cases |
| `concepts/summarization-contract.md` | Upstream/downstream contract — inputs accepted, outputs guaranteed |
| `patterns/summary-template.md` | Canonical template — section spec, body targets, entity rules |
| `reference/troubleshooting.md` | Known failure modes and remediation paths |

---

## Key Invariants

These hold across all files in this domain. If any file contradicts them, the contract wins.

1. **Sections:** Always write `## TL;DR` and `## Key Concepts`. Write `## Key Decisions` iff decisions are present in source. Write `## Action Items` iff action items are present. Omit optional sections entirely when empty.
2. **Body target:** 300–600 words. Compression ratio = body words / source word count; bounds `0.05 < ratio < 1.0`. Adapt body length when source is outside the comfortable 600–6,000 word range.
3. **Entity preservation:** Every entity listed in `entities_in_source` must appear verbatim in summary text. `missing_entities == []` is a skill gate.
4. **Self-report schema:** Frozen in `.claude/agentspec/shared/self-report-contract.md`. Agent returns `{output, metadata, self_report}` only.
5. **No gates by the agent:** The agent populates `self_report` fields honestly. The skill enforces gates, retries, and escalation.
6. **Source is untouched:** `summary.md` is the only file the agent writes.

---

## Self-Report Gate Summary (for reference — enforced by skill)

| Field | Gate |
|-------|------|
| `structure_present == true` AND `missing_sections == []` | Structure gate |
| `0.05 < compression_ratio < 1.0` | Compression gate |
| `missing_entities == []` | Entity gate |
| Any gate fail | Skill retries → opus → needs_user |
