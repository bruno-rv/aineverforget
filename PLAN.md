# Plan: aineverforget — a local-first, eval-gated knowledge brain
_Locked via grill-with-docs — by Claude + Bruno. Terms per CONTEXT.md. ADRs in docs/adr/._
_Rev 9 — Codex Round 1–7 (+ verify-view + cold-start rule). See PLAN-REVIEW-LOG.md._

## Goal

Build **aineverforget**: a local-first personal knowledge base that ingests
heterogeneous text — notes, transcripts, summaries, PDFs, and codebase summaries
emitted by external Producers — into one hybrid vector space, and answers
questions grounded in the whole Corpus with Citations. It serves both **Recall**
(pinpoint lookup) and **Synthesis** (aggregation/summarization across many
Sources). It is built the way `neverforget` is: a deterministic Tool layer driven
by single-responsibility AgentSpec Agents, each with its own Knowledge Base and a
**Quality Gate** that makes a step Reiterate until it meets its Threshold, all
coordinated by an Orchestrator skill with one level of nesting. It does not capture
Sources itself — it indexes what you supply and journals what it does.

> Agent/KB artifacts live under `.claude/agentspec/` (Claude Code is the Agent
> runtime, matching `neverforget` and the user's global CLAUDE.md). This is
> canonical; `.codex/` mirrors these artifacts for Codex sessions and must stay
> in sync with `.claude/`.

## Architecture (two layers)

```
┌─ Agent layer (judgment, AgentSpec, Claude) ─────────────────────────────┐
│  Orchestrator skills:  /ingest    /ask                                   │
│    /ingest →  note-summarizer → knowledge-indexer                        │
│    /ask    →  (route) → knowledge-retriever ×N → answer-synthesizer      │
│  Agents return {output, metadata, self-report} ONLY — they never retry,  │
│  dispatch, or escalate. The skill runs every Quality Gate and owns the   │
│  Reiterate ladder (retry → sonnet→opus → needs_user; Two-Strike bound).  │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ Bash → CLI with --json contracts (stable)
┌─ Tool layer (deterministic, Python CLI `aineverforget`) ─────────────────┐
│  loaders(md/txt, pdf) → chunk → BGEM3FlagModel(dense+sparse)             │
│  → Qdrant hybrid upsert / query(RRF) / scroll / lexscan / verify / status│
│  Run Journal (JSONL + SQLite) · Cost Telemetry                           │
└──────────────────────────────┬──────────────────────────────────────────┘
                               ▼
                Qdrant @ 127.0.0.1:6333  (local, one collection)
                ainf_corpus_bgem3_v1 · dense(1024,cosine)+sparse · RRF
```

## Identity model (Source → Document → Chunk)

One Source may yield many Documents (a Producer markdown bundle, a multi-file
drop); one Document yields many Chunks. IDs and payload carry all three levels:

- `source_id` = stable id of the ingest Source (path or Producer ref).
- `document_id` = id of one Document within a Source; `document_path`,
  `document_sha256`.
- Point ID = deterministic UUIDv5 of `document_id | document_sha256 | chunk_index`.
- **`document_sha256` is content *identity*, not a version order** (a hash is
  unordered). Ordering uses a separate **monotonic `ingest_generation`** (integer
  per `document_id`). Visibility is gated by **`ingest_state`** (`pending` →
  `active` | `failed`) — only verified Chunks are ever served.
- **Idempotent, gap-free, duplicate-free, verify-gated update.** Single-writer is
  *enforced*, not assumed:
  0. **Acquire an ingest lock** (reuse `neverforget` `run_lock.py`: pid +
     heartbeat). This serializes ingest so the `read max G → write G+1` step is
     race-free; concurrent ingest is rejected, not corrupting.
  1. Compute `document_sha256`. **No-op only if** an `ingest_state=active`
     generation exists for `document_id` **and** its hash equals it (identical
     content, identical point IDs). If the Document has **no active generation**
     (first ingest, or a prior attempt left only `pending`/`failed`), proceed to
     re-ingest even on a hash match — first `gc` any stale `pending`/`failed` Chunks
     for this `document_id`.
  2. Read max `ingest_generation` `G` for `document_id`; **upsert** the new Chunks
     at generation `G+1` with **`ingest_state=pending`**.
  3. **Verify** the pending generation against a **verification view** —
     `Filter(should=[ingest_state="active", must([document_id=X,
     ingest_generation=G+1])])` — so probes compete against the **real active
     Corpus**, not just the Document's own Chunks (otherwise the negative probe is
     meaningless and topical recall is unproven):
     - **topical / specific** → the pending generation must be the retrieved /
       expected result *among all active Chunks*;
     - **negative** → an unrelated query must NOT surface the pending generation
       (meaningful only because it competes against unrelated active Chunks).
     - **Cold-start rule:** when no unrelated active Document exists yet (the first
       ingest, or a near-empty Corpus), the negative probe is **deferred** (or run
       against a frozen unrelated background fixture) — it cannot be meaningful when
       the pending Chunk is the only candidate. Topical/specific still apply.
     - **Pass** → promote those Chunks to **`ingest_state=active`**, then delete (or
       mark `failed`) Chunks of older generations.
     - **Fail** → delete / mark `failed` the pending `G+1` Chunks. The prior
       **active** generation (if any) stays served; emit `INDEX_SUSPECT`. (A failed
       *first* ingest leaves the Document with no active generation — it is simply
       absent from results, not half-served.)
  - **Every read path filters `ingest_state=active`** and dedups by `document_id`
    keeping the max **active** `ingest_generation`. Unverified, failed, or superseded
    Chunks are never served or counted — even mid-update.
  - `aineverforget gc` retires non-max active generations + orphaned
    `pending`/`failed` Chunks. **gc never touches the active-max set**, so a
    concurrent read is always correct; gc is pure reclamation, safe to run anytime.

## Payload schema (citation-grade)

`source_id`, `source_type`, `document_id`, `document_path`, `document_sha256`
(content identity), `ingest_generation` (monotonic int, version order),
`ingest_state` (`pending`|`active`|`failed`, visibility gate), `title`,
`chunk_index`, `chunk_start_word`, `chunk_end_word`,
`heading_path` (markdown), `pdf_page` (pdf), `tags[]`, `producer`, `ingested_at`,
`loader_version`, `chunker_version`, `embedding_model`, `text`.
Indexes: keyword on `source_type` / `document_path` / `tags` / `ingest_state`;
datetime on `ingested_at`; **full-text index on `text`** (for content Enumeration
via `lexscan`). Field names are canonical (no `source_path`). Version + state
fields make reindex-safety, verify-gating, and citation provenance explicit.

## Approach (build phases)

**Phase A — Tool layer (Python CLI, deterministic, no LLM).**
1. Package scaffold (`pyproject.toml`, `src/aineverforget/`), reusing
   `neverforget`'s `knowledge/` module shape (store, embedding, chunking, ids,
   verify, corpus).
2. Loaders (registry keyed by Source type):
   - `md/txt`: parse markdown with a real parser (mistune/markdown-it), preserve
     fenced-code/table blocks intact, attach `heading_path` to each block.
   - `pdf`: text-layer (pypdf/pdfplumber), page-aware. **Loader verdicts**: `ok`,
     `encrypted`→needs_user, `scanned/no-text`→needs_user, `low-confidence`→flag.
     OCR is out of scope v1.
3. Chunking: shared core; markdown = block/heading-aware (never split a fenced
   code block or table); prose = word-window (~220 words / 40 overlap, sized to
   the embedder window); pdf = page-aware → word-window. Every Chunk keeps
   `heading_path`/`pdf_page` + `chunker_version`.
4. Embedding: **`FlagEmbedding.BGEM3FlagModel`** with `return_dense=True,
   return_sparse=True` → dense(1024) + `lexical_weights`. **Sparse adapter contract:**
   BGE-M3's `lexical_weights` is a `{token_id: weight}` map; convert to a Qdrant
   `SparseVector(indices=sorted(token_ids), values=[weights in that order])`. Pin
   the model + tokenizer version; a fixture test asserts the indices/values mapping
   round-trips and a known query/passage pair scores as expected. No e5 prefix
   (per BGE-M3 card). Encoding contract pinned + tested.
5. Qdrant store: one collection `ainf_corpus_bgem3_v1` with **both**
   `vectors_config={"dense": VectorParams(1024, COSINE)}` **and**
   `sparse_vectors_config={"sparse": SparseVectorParams()}`. Upsert points carry
   `{"dense": [...], "sparse": SparseVector(indices, values)}`. Create payload
   indexes on first `ensure_collection()`.
6. Retrieval: hybrid via Qdrant **Query API** —
   `query_points(prefetch=[Prefetch(query=dense, using="dense", limit=N),
   Prefetch(query=sparse, using="sparse", limit=N)], query=FusionQuery(RRF),
   query_filter=Filter(must=[FieldCondition(ingest_state="active")]))`. **Every read
   path — `search`, `lexscan`, `scroll`, `status` — filters `ingest_state=active`
   and counts/dedups by max active `ingest_generation` per `document_id`**, so
   unverified / failed / superseded Chunks are never served or counted. Reranker
   (bge-reranker-v2-m3) is v1.1.
7. CLI verbs, **all with `--json`, stable schemas, and explicit exit codes** (the
   Agent↔Tool contract):
   - `ingest <paths…> [--tag]`
   - `search <q> [--source --path --since --tag]` — hybrid (dense+sparse RRF).
   - `lexscan <term> [filters]` — **exhaustive content Enumeration**: a Qdrant
     **full-text payload index** on `text` (`MatchText`) drives a **paginated
     scroll** over ALL matching points (not top-k), deduped by Document. Tokenizer
     config pinned (lowercase + ascii-folding + optional stemming); `--count`
     returns total occurrences/Documents. This — not sparse top-k, not payload
     scroll — is how "how many times did I mention Y" is answered.
   - `scroll <filter>` — **metadata Enumeration** (payload filter only, e.g. "which
     Sources tagged X"); **active-only, deduped by max active generation per
     Document**.
   - `gc` — retire superseded Chunks (non-max `ingest_generation` per
     `document_id`); crash-recovery for interrupted re-ingest.
   - `verify`, `status`.

**Phase B — Agents + Knowledge Bases (`.claude/agentspec/`).**
Agents (`agents/dev/*.md`, frontmatter `model: sonnet`, `threshold`); each returns
`{output, metadata, self-report}` only. KB under `kb/<domain>/`:
- `note-summarizer` — raw notes/transcripts → structured `summary.md`. KB: summary
  template (sectioned for retrieval).
- `knowledge-indexer` — drives `aineverforget ingest` + `verify`. KB: chunking
  policy, verify-probe calibration.
- `knowledge-retriever` — drives hybrid `search`/`lexscan`/`scroll`, judges
  relevance, returns ranked Chunks + Citations. KB: query reformulation, filters.
- `answer-synthesizer` — question + Chunks → grounded, cited answer; refuses when
  unsupported. KB: answer/citation contract, refusal rules.
- `.claude/rules/agent-registry.md` — every Agent, what it owns, its Threshold.

**Phase C — Orchestrator skills (one-level nesting, ADR-0001).**
- `/ingest <paths>` — per Source: (note-summarizer if raw) → knowledge-indexer.
- `/ask <question>` — route to **Recall / Synthesis / Enumeration**:
  - Recall: one `knowledge-retriever` → `answer-synthesizer`.
  - Synthesis: decompose → `knowledge-retriever` ×N → **dedup Chunks by
    `document_id`/point-id + a coverage ledger keyed by sub-query** (which
    sub-queries were answered, which returned nothing) → map-reduce →
    `answer-synthesizer`, **passing the ledger in**. The synthesizer must emit a
    **coverage verdict** (`complete` | `partial`) and, when sub-queries are
    unresolved, **explicitly qualify or refuse** rather than imply full coverage.
  - Enumeration: **metadata** ("which Sources tagged X") → `scroll` over payload
    filter (active-only, deduped by max active generation); **content** ("how many
    times did I mention Y") → `lexscan` (full-text
    index + exhaustive paginated scroll + dedup). Never answer content-enumeration
    from payload scroll alone.
  - Skill runs every Quality Gate, owns the Reiterate ladder, records Cost
    Telemetry, and (no hard cap) emits a **preflight fan-out estimate** before a
    Synthesis Ask and **soft-warn journal events** when running totals cross a
    configurable threshold; telemetry is flushed live, not only at the end.

**Phase D — Eval system (full, ADR-0003).**
- Runtime Quality Gates (deterministic) wired into the skill (table below).
- Dev-time Eval harness — **contractible**: per-Agent fixtures over a **frozen
  mini-Corpus** (checked-in, never the live Corpus) with **expected point/Document
  IDs** for retrieval, gold summaries, gold Q→A. Metrics via RAGAS/promptfoo
  (faithfulness, answer-relevance, context-precision/recall, recall@k, MRR).
  Fixture schema + CI triggers (on prompt/model/chunker/embedder change) defined
  before Agents are authored.

**Phase E — Observability.**
- **Run Journal schema** (JSONL + SQLite mirror, reuse `neverforget`
  `run_journal.py`): closed event taxonomy with `ask_id`/`ingest_id`,
  `attempt_id`, `agent`, `event` (enum), `gate`, `gate_score`, `verdict` (enum:
  `pass|reiterate|needs_user|index_suspect|...`), `model`, `escalated`,
  `tokens`, `spend`, `ts`. **Redaction allowlist** on free-text fields (reuse
  sanitize pattern).
- Cost Telemetry: dispatches + tokens per Ask, flushed live; `status` surfaces the
  most expensive recent Asks.
- `aineverforget status`: collection size, #Sources/#Documents/#Chunks, last
  ingest, Qdrant health.

**Phase F — Supervised validation.**
- End-to-end dry run on a real mixed batch (md + pdf + a Producer summary). Run
  Recall / Synthesis / Enumeration Asks; confirm Gates fire and Reiterate, the
  coverage ledger + dedup work, Journal + Telemetry populate, PDF/markdown edge
  verdicts behave. Then sign off.

## Quality Gates (runtime, skill-run, deterministic) — per Agent

| Agent | Runtime Gate (every call, deterministic) | Fail → Reiterate | Dev-time Eval |
|---|---|---|---|
| answer-synthesizer | every claim cites a real Chunk; cited IDs exist; groundedness (lexical/entailment overlap vs cited Chunks); **coverage verdict** consistent with the ledger (no implied full coverage when `partial`) | re-synthesize → opus → "can't confirm from your notes" / qualify | RAGAS faithfulness / answer-relevance |
| knowledge-indexer | verify probes: topical / specific / negative | re-chunk/re-embed → `INDEX_SUSPECT` | gold probe fixtures |
| knowledge-retriever | deterministic predicate: `candidate_count≥1 AND (dense_hits≥1 OR sparse_hits≥1) AND citationable_count≥1` (per-modality pre-fusion counts — **NOT a fused-RRF score floor**) | reformulate/broaden → opus | recall@k / MRR vs gold point-IDs |
| note-summarizer | deterministic: required structure present, compression ratio in bounds, no entities absent from source | retry → opus → needs_user | **ingest-time Eval** (LLM-judge faithfulness, off hot path) + gold summaries |

Escalation: model-tier (sonnet→opus); then `needs_user`. Two-Strike bounds all
loops. (note-summarizer's faithfulness is an ingest-time **Eval**, not a Gate —
Gates stay judge-free per CONTEXT.md.)

## Key decisions & tradeoffs

- **Orchestrator is a skill, one-level nesting; Agents return data only** → single
  audit trail, visible failures; agentic Ask runs inside Claude Code. (ADR-0001)
- **One hybrid collection, BGE-M3 dense+sparse via FlagEmbedding** → multilingual +
  lexical recall, local; re-index cost to change model, mitigated by versioned
  name. (ADR-0002)
- **Eval-gated Agents, runtime Gates vs dev-time Evals** → quality without a judge
  on the hot path; full system in v1; model-tier escalation; no per-Ask cap with
  Cost Telemetry + preflight fan-out estimate + soft-warn as safeguard. (ADR-0003)
- **Producer-agnostic, normalize-to-text, images deferred** → external tools feed
  markdown; no repo Loader in v1.

## Risks / open questions

1. **TOP RISK — no per-Ask budget cap.** Synthesis × Reiterate × opus × no ceiling
   can spawn dozens of LLM calls. Accepted; safeguards = live Cost Telemetry +
   preflight fan-out estimate + soft-warn journal events (no hard stop). Revisit if
   telemetry shows blowups.
2. **Full eval system in v1 is a large surface.** Phasing A→F front-loads a working
   Tool+Ask before the dev-time harness hardens.
3. **BGE-M3 via FlagEmbedding** adds a dependency + RAM/encode cost heavier than
   e5; encoding contract must be pinned and tested.
4. **Groundedness gate fidelity.** Deterministic overlap may mis-fire; a small
   local NLI is a v1.1 upgrade.
5. **Content Enumeration completeness** depends on `lexscan` exhaustiveness +
   correct sparse/exact matching; a missed inflection of "Y" undercounts.
6. **opus escalation vs global "Opus = planning only" rule** — accepted for answer
   quality on the escalation path only.

## Out of scope (v1)

Images / OCR / captioning / visual-similarity; email (.eml/.mbox); raw-code search
or repo Loader; auto-summarizing repos via LLM; watch-folder/inbox ingest;
reranker; local Ollama answer path; RAPTOR summary hierarchy; per-Ask hard budget
cap; web/URL loader; multi-user / hosted / web UI.
