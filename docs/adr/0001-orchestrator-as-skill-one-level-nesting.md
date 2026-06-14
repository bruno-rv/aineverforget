# ADR-0001: Orchestrator is a skill, agents nest one level deep

- Status: Accepted
- Date: 2026-06-13
- Deciders: Bruno, Claude (grill-with-docs-codex)

## Context

aineverforget answers questions agentically: a Synthesis Ask decomposes into
sub-queries, dispatches the retrieval Agent several times, then the answer Agent,
and each step is Quality-Gated and may Reiterate (retry → escalate model →
needs_user). Something has to own that control flow. Two shapes were on the table:

1. An **Orchestrator Agent** that other Agents could call, allowing arbitrary
   nesting (retriever calls a reformulator calls a judge…).
2. An **Orchestrator skill** in the main Claude session that is the *only*
   dispatcher; stage Agents do one job and return; they never dispatch other
   Agents.

`neverforget` faced the identical question and chose option 2 (its ADR-0002),
having proven it across ~80 recorded lessons. aineverforget inherits the same
constraint set: a single audit trail, visible failures, bounded fan-out.

## Decision

The Orchestrator is a **skill** (`/ingest`, `/ask`) run by the main Claude
session. It is the only thing that dispatches Agents and routes on their results.
Stage Agents (note-summarizer, knowledge-indexer, knowledge-retriever,
answer-synthesizer) never dispatch other Agents.

Consequences of this for the agentic features:

- **Multi-hop Synthesis** is a loop *in the `/ask` skill*: the skill decomposes
  the question, dispatches `knowledge-retriever` per sub-query, collects Chunks,
  then dispatches `answer-synthesizer` once. The retriever does not fan out.
- **The Reiterate ladder is orchestrator-owned.** An Agent returns its output plus
  a self-assessment; the skill runs the Quality Gate and, on failure, re-dispatches
  (retry → escalate to opus → needs_user). An Agent never escalates itself. This
  keeps one-level nesting intact even with eval-gating.
- **External/escalation support** (a stronger model) is requested *up* to the
  skill, which performs the re-dispatch — not sideways between Agents.

## Alternatives considered

- **Orchestrator Agent with nested dispatch.** Rejected: a second routing brain,
  sub-sub-agent failures leave no trace in the main context, and the audit trail
  fragments — the same reasons `neverforget` rejected it.

## Consequences

- Positive: one Run Journal captures everything; failures are visible in the main
  session; fan-out is bounded by what the skill chooses to dispatch.
- Negative: the agentic Ask runs **inside Claude Code** (interactive, headless
  `claude -p`, or `/loop` for scheduling). The deterministic `aineverforget` CLI
  (load/chunk/embed/search/scroll) remains standalone and cron-able; only the
  judgment layer requires a Claude session — accepted, matching `neverforget`.
