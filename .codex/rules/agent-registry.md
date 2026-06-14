# Agent Registry — aineverforget (always active)

**Contract:** an agent not registered here does not exist. Creating or renaming an agent and updating this table happen in the SAME session.

The `model` column is **CLAUDE-ONLY METADATA** (other runtimes ignore it).

---

## Registry Table

| Agent / Skill | Stage | Triggers | Owns | Never Does | model (Claude-only) |
|---------------|-------|----------|------|------------|---------------------|
| `/ingest` (SKILL, Orchestrator) | ingest | user runs `/ingest <paths>` | dispatch note-summarizer + knowledge-indexer per Source; run Quality Gates; journal; lock enforcement | be an agent; nest >1 deep | sonnet |
| `/ask` (SKILL, Orchestrator) | ask | user runs `/ask <question>` | route to Recall/Synthesis/Enumeration; dispatch knowledge-retriever ×N; manage sub-query ledger + dedup; dispatch answer-synthesizer; run Quality Gates; journal; Cost Telemetry | be an agent; nest >1 deep | sonnet |
| `note-summarizer` | ingest/summarize | `/ingest` when Source is a raw note/transcript (unstructured prose) | transform raw note → structured `summary.md`; extract entities; compute compression ratio; populate `self_report` | retry on gate fail; dispatch; escalate; modify source file | sonnet |
| `knowledge-indexer` | ingest/index | `/ingest` after note-summarizer (if ran) or directly for pre-structured Sources | drive `aineverforget ingest --json`; read CLI JSON result; extract probe_verdicts from verify; populate `self_report` | retry on gate fail; dispatch; escalate; modify source file; run verify CLI itself (CLI does it) | sonnet |
| `knowledge-retriever` | ask/retrieval | `/ask` per sub-query (one per Recall; N in parallel for Synthesis) | drive `aineverforget search/lexscan/scroll --json`; judge citationability; try one reformulation if zero results; populate `self_report` with pre-fusion counts | retry after reformulation; dispatch; escalate; merge results across sub-queries (skill does that) | sonnet |
| `answer-synthesizer` | ask/synthesis | `/ask` after all knowledge-retriever(s) complete | ground claims in chunks; cite every claim; emit coverage_verdict consistent with sub-query ledger; qualify when partial; refuse when unsupported | hallucinate beyond provided chunks; imply full coverage when partial; dispatch; escalate; retry | sonnet |

---

## Routing Rules

### Orchestrator-only dispatch

`/ingest` and `/ask` skills are the **sole dispatchers**. Stage agents never dispatch other agents.

### /ingest — stage sequence

```text
/ingest <paths>
  For each Source path:
    IF raw note/transcript → note-summarizer → knowledge-indexer
    IF pre-structured doc  → knowledge-indexer (direct)
  Quality Gate per agent (skill-run, deterministic)
  On gate fail: retry (sonnet) → escalate (opus) → needs_user
```

### /ask — stage sequence

```text
/ask <question>
  Route: Recall | Synthesis | Enumeration

  Recall:
    knowledge-retriever (hybrid search, one query)
    → answer-synthesizer

  Synthesis:
    Decompose question into N sub-queries
    Emit preflight fan-out estimate
    knowledge-retriever ×N (parallel, each one sub-query)
    Dedup chunks by document_id/point_id + coverage ledger
    → answer-synthesizer (with deduped chunks + ledger)

  Enumeration (metadata):
    knowledge-retriever (scroll, payload filter)
    → answer-synthesizer (tabular list format)

  Enumeration (content):
    knowledge-retriever (lexscan, exhaustive full-text)
    → answer-synthesizer (count + occurrences format)

  Quality Gate per agent (skill-run, deterministic)
  Cost Telemetry flushed live
  Soft-warn journal event when running total crosses threshold
```

### Failure routing (skill-owned, per agent)

| Gate fail | First reiterate | Second reiterate | Then |
|-----------|----------------|------------------|------|
| note-summarizer | retry sonnet | escalate opus | needs_user |
| knowledge-indexer | INDEX_SUSPECT journal + no retry | — | report to user |
| knowledge-retriever | reformulate + retry sonnet | escalate opus → empty result | pass empty to synthesizer |
| answer-synthesizer | re-synthesize sonnet | escalate opus | "can't confirm from your notes" |

Two-Strike rule: the same failure retried twice → needs_user, regardless of agent.

---

## Same-Session Registration Contract

Creating or renaming an agent file under `.claude/agentspec/agents/dev/` requires updating this table in the same session. An unregistered agent is a process gap.

Each entry must carry:
- **Stage** — which pipeline stage the agent operates in
- **Triggers** — what causes the Orchestrator (or user) to dispatch it
- **Owns** — the outputs and decisions the agent is authoritative for
- **Never Does** — hard boundaries preventing scope creep
- **model** — always `sonnet` for stage agents; `sonnet` for orchestrator skills (Claude-only metadata)

---

## Agent File Locations

| Agent | File |
|-------|------|
| `note-summarizer` | `.claude/agentspec/agents/dev/note-summarizer.md` |
| `knowledge-indexer` | `.claude/agentspec/agents/dev/knowledge-indexer.md` |
| `knowledge-retriever` | `.claude/agentspec/agents/dev/knowledge-retriever.md` |
| `answer-synthesizer` | `.claude/agentspec/agents/dev/answer-synthesizer.md` |

Orchestrator skills live under `.claude/skills/` (Phase C).

---

## KB Domain Cross-Reference

| Agent | Primary KB domain | Path |
|-------|-------------------|------|
| `note-summarizer` | `note-summarization` | `.claude/agentspec/kb/note-summarization/` |
| `knowledge-indexer` | `knowledge-indexing` | `.claude/agentspec/kb/knowledge-indexing/` |
| `knowledge-retriever` | `knowledge-retrieval` | `.claude/agentspec/kb/knowledge-retrieval/` |
| `answer-synthesizer` | `answer-synthesis` | `.claude/agentspec/kb/answer-synthesis/` |

KB domain details (paths, concepts, patterns, specs) indexed in `.claude/agentspec/kb/_index.yaml`.

---

## Global Constraints (all agents)

Per ADR-0001:
- Agents return `{output, metadata, self_report}` ONLY
- Agents never retry, dispatch, or escalate — the Orchestrator skill owns all of that
- Gates are deterministic (no LLM judge on the hot path) — per ADR-0003
- note-summarizer faithfulness is a dev-time Eval, NOT a runtime gate

Per PLAN.md:
- All read paths filter `ingest_state=active` (CLI enforces)
- Synthesis answers must include a `coverage_verdict` and qualify when partial
- Enumeration content uses `lexscan` (exhaustive), never sparse top-k alone
