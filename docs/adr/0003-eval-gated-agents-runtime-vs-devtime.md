# ADR-0003: Eval-gated Agents — runtime Gates vs dev-time Evals

- Status: Accepted
- Date: 2026-06-13
- Deciders: Bruno, Claude (grill-with-docs-codex)

## Context

Every Agent must meet a minimum quality standard or Reiterate until it does. The
naive design — "run an LLM judge after every Agent and retry on a low score" — is
a trap: it puts a slow, nondeterministic, gameable judge on the hot path, doubles
cost per Ask, and false-fails into retry loops. A second trap is measuring things
at runtime that can only be measured against a gold set ("recall@k"), which does
not exist for an arbitrary live user query.

## Decision

Split quality enforcement into **two systems**:

1. **Quality Gates (runtime).** Cheap, deterministic checks on every Agent call,
   able to trigger Reiterate:
   - `answer-synthesizer`: every claim cites a real Chunk; cited Chunk IDs exist;
     groundedness by lexical/entailment overlap against the cited Chunks. Fail →
     re-synthesize, else answer "can't confirm from your notes."
   - `knowledge-indexer`: verify probes — topical (doc retrievable above floor),
     specific (a known fact substring retrievable), negative (unrelated query does
     not surface it). Fail → re-chunk/re-embed, else `INDEX_SUSPECT`.
   - `knowledge-retriever`: deterministic candidate predicate — `candidate_count
     >= 1 AND (dense_hits >= 1 OR sparse_hits >= 1) AND citationable_count >= 1`,
     using per-modality (pre-fusion) hit counts, **never a fused-RRF score floor**
     (RRF is rank-based and uncalibrated). Fail → reformulate/broaden query.
   - `note-summarizer`: its runtime **Gate is deterministic** (required structure
     present, compression ratio in bounds, no entity absent from the source). Its
     LLM-judge faithfulness check is an **ingest-time Eval**, not a Gate (Gates stay
     judge-free) — affordable because summarization is off the hot path.

2. **Evals (dev-time).** Per-Agent gold sets run in CI when a prompt/model/chunking
   changes — recall@k / MRR for retrieval, RAGAS faithfulness / answer-relevance /
   context-precision for answering, gold summaries for summarization. A cross-model
   judge may be used here. **Reuse an existing harness (RAGAS / promptfoo /
   DeepEval); do not hand-roll RAG metrics.** Gold sets are versioned and never
   normalized to the live Corpus.

**Reiterate ladder** (orchestrator-owned, per ADR-0001): retry same Agent →
escalate the step to a stronger model (sonnet → opus) → `needs_user`. Bounded by
the **Two-Strike Rule**.

**Scope:** the full system — runtime Gates *and* the dev-time gold/RAGAS/cross-model
harness — is in v1 (user's explicit choice).

**No per-Ask budget cap in v1** (user's explicit choice). Mitigation: **Cost
Telemetry** — dispatches and tokens are logged per Ask so an expensive Ask is
visible. This is recorded as the top open risk (see PLAN.md).

## Alternatives considered

- **LLM judge on every call.** Rejected: cost, nondeterminism, loop risk on the
  hot path.
- **Runtime gates only, no gold harness.** Rejected by the user: no regression net
  when prompts/models change.
- **Cross-model redo as the escalation tier** (Codex / OpenRouter). Considered;
  the user chose model-tier escalation (sonnet → opus) instead. Cross-model is
  retained only in the dev-time judge, not the runtime ladder.
- **Hard per-Ask budget ceiling.** Considered (it is `neverforget`'s dispatch
  budget); the user chose no cap for v1, accepting the cost risk with telemetry as
  the safeguard.

## Consequences

- Positive: quality enforced without a judge on the hot path; rigorous regression
  coverage; bounded, auditable Reiterate.
- Negative: building the full eval system (runtime + gold + RAGAS + cross-model
  judge) is a large v1 surface. The Synthesis path (multi-hop × Reiterate × opus
  escalation × no cap) is the highest cost-blowup risk; Cost Telemetry makes it
  visible but does not stop it.
