# Answer Synthesis KB

> **Purpose:** Grounded answer generation from ranked chunks, with inline citations and coverage verdict.
> **Owner:** `answer-synthesizer` agent
> **Validated:** 2026-06-14 — initial authoring

---

## What answer-synthesizer Does

`answer-synthesizer` is the final judgment step in the `/ask` pipeline. It receives a question, one or more sets of ranked_chunks from `knowledge-retriever`, and a sub_query_ledger assembled by the skill. It produces an answer where every factual claim is grounded in a specific chunk's text, with an inline citation attached to each claim. It then computes four deterministic booleans and returns `{output, metadata, self_report}` only.

The agent never retries, never calls the CLI, never escalates. The skill runs all gates and owns the reiterate ladder (re-synthesize → opus → "can't confirm"). The agent's job is to synthesize once and report honestly.

---

## Three Modes

### Recall

One set of `ranked_chunks` for a single question. The simplest case: ground claims, cite, evaluate, return. `sub_query_ledger` has one key. `coverage_verdict` is "complete" when that key is "answered".

### Synthesis

N sub-queries, each with their own `ranked_chunks` set and a ledger entry ("answered" or "empty"). The agent performs map-reduce: synthesize each answered sub-query's contribution, merge into one answer, dedup citations across sub-queries. If any ledger entry is "empty", `coverage_verdict` is "partial" and the answer must contain an explicit qualification naming the unresolved sub-queries.

### Enumeration

`ranked_chunks` from a `lexscan` (content: "how many times did I mention X") or `scroll` (metadata: "which sources are tagged Y"). Content enumeration produces a list answer with one citation per document referenced. Metadata enumeration produces a list of sources — `groundedness_pass` is trivially true when the answer only lists documents from the input without adding claims.

---

## Refusal Contract

When no chunks are provided, or when all chunks are empty, or when no chunk text supports the question after attempting synthesis:

- `output.answer` states explicitly that the information cannot be confirmed from notes.
- `output.citations` is `[]`.
- `output.coverage_verdict` is "partial".
- `output.qualification` is non-null and describes what was missing.
- All four self_report booleans are `true` (refusal is gate-passing — silence or false implication is the failure mode, not explicit refusal).
- `verdict` is "pass".

Refusal is the correct and honest output when chunks are absent or unsupportive. It is not a failure state. The skill may escalate from there; the agent does not.

---

## Quick Navigation

### Concepts

| File | What it covers |
|------|---------------|
| `concepts/answer-contract.md` | Upstream/downstream boundaries; citation-grade requirements; what makes a claim grounded |

### Patterns

| File | What it covers |
|------|---------------|
| `patterns/citation-contract.md` | Inline citation format; dedup rules for synthesis; claim→chunk_id mapping |

### Reference

| File | What it covers |
|------|---------------|
| `reference/troubleshooting.md` | Groundedness failures; coverage inconsistency; hallucination guards; marginally relevant chunks |

---

## Key Concepts

| Concept | Definition |
|---------|-----------|
| Grounded claim | A statement in the answer that shares at least one key term with a specific chunk's text |
| Inline citation | `[source: Title, chunk N]` or `[source: Title, heading, chunk N]` appended to the claim in the answer text |
| citations entry | Structured object with `claim` (verbatim phrase), `chunk_id` (point_id uuid), and provenance fields |
| coverage_verdict | "complete" when all ledger entries are "answered"; "partial" when any is "empty" |
| qualification | Required non-null text when coverage_verdict is "partial"; names the unresolved sub-queries |
| sub_query_ledger | Assembled by the skill; passed into the agent; maps each sub-query to "answered" or "empty" |
| Refusal | Explicit answer stating claims can't be confirmed; all gates pass; not a failure state |
| FROZEN triple | {output, metadata, self_report} — the only return value; schema is immutable |

---

## Learning Path

| Step | Read | Goal |
|------|------|------|
| 1 | `concepts/answer-contract.md` | Understand what data flows in and out and what makes a claim citation-grade |
| 2 | `patterns/citation-contract.md` | Know the exact inline format and how to map claims to chunk_ids without drift |
| 3 | `quick-reference.md` | Gate checklist and coverage_verdict decision table at a glance |
| 4 | `reference/troubleshooting.md` | Known failure modes before synthesizing against edge-case chunks |

---

## Boundaries

**This domain owns:** answer synthesis, inline citation format, groundedness evaluation, coverage_verdict and qualification logic, refusal contract.

**This domain does NOT own:**
- Retrieval — owned by `knowledge-retriever` / `knowledge-retrieval` KB
- Sub-query decomposition and ledger assembly — owned by the `/ask` skill
- CLI invocation — this agent never calls the CLI
- Gate enforcement and reiteration — owned by the `/ask` skill
- Ingest and indexing — owned by `knowledge-indexer` / `knowledge-indexing` KB
