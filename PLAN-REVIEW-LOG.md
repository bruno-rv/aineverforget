# Plan Review Log: aineverforget

Act 1 (grill-with-docs) complete — plan locked, CONTEXT.md + 3 ADRs written. MAX_ROUNDS=5.

Resolved in Act 1 (12 forks):
- Local-first Python CLI; reuse neverforget knowledge/ stack
- Normalize-to-text, single Qdrant collection, images deferred
- v1 loaders: md/txt + PDF (repo = external Producer markdown; email deferred)
- Synthesis + aggregation first-class (not deferred)
- Embedder: bge-m3-class, dense+sparse (ADR-0002)
- Producer-agnostic ingest
- Hybrid retrieval (dense+sparse, RRF), reranker v1.1
- Flat corpus + system filters + optional --tag
- Synthesis = agentic multi-hop + query routing (recall/synthesis/enumeration)
- AgentSpec agents over Python CLI; orchestrator-as-skill, one-level nesting (ADR-0001)
- Roster: note-summarizer, knowledge-indexer, knowledge-retriever, answer-synthesizer + /ingest + /ask
- Evals: full system v1 (runtime Gates + dev-time gold/RAGAS/cross-model judge); escalate sonnet→opus→needs_user; NO per-Ask cap (Cost Telemetry mitigation) (ADR-0003)
- Observability: Run Journal (JSONL+SQLite) + Cost Telemetry

---

## Round 1 — Codex (thread 019ec0aa-243b-7e63-bd76-8f5f35275f88)

VERDICT: REVISE — 22 findings (10 High, 9 Medium, 1 Low). Highlights:
- [H] RRF score-floor retriever Gate invalid (RRF fuses by rank, not score).
- [H] BGE-M3 dense+sparse needs FlagEmbedding (not FastEmbed/ST); no e5 prefix.
- [H] Content Enumeration ("mention Y") can't be payload-scroll; needs lexical scan.
- [H] Source→many-Documents: IDs/payload need document_id/path/sha256.
- [H] One-level nesting ambiguous (agent vs skill owns Gate/Reiterate).
- [H] Summarizer LLM-judge violates "Gate = deterministic" (it's an Eval).
- [H] Qdrant sparse schema (sparse_vectors_config, upsert, prefetch) unspecified.
- [H] .codex vs .claude storage; [H] no-cap lacks pre-runaway failsafe.
- [M] delete-then-upsert data-loss; markdown chunker naive (fenced code/tables);
  PDF failure verdicts; CLI not --json-contractible; synthesis lacks dedup+coverage;
  dev-time evals not contractible; journal taxonomy/redaction; citation-grade payload.
- [L] "records nothing" slogan vs Journal/stored text.

### Claude's response
Accepted 21/22; partial-rejected #1.
- REJECTED #1 (.codex): agents are Claude Code AgentSpec agents → `.claude/agentspec/`
  is canonical (matches neverforget + global CLAUDE.md); added explicit note in PLAN.
- ACCEPTED all others. PLAN → rev 2:
  - Identity model section (source_id/document_id/document_sha256; UUIDv5 on
    document_id|document_sha256|chunk_index); upsert-new-then-delete-stale.
  - Citation-grade payload (heading_path, pdf_page, loader_version, chunker_version).
  - Embedder = FlagEmbedding.BGEM3FlagModel, no e5 prefix (ADR-0002 patched).
  - Qdrant sparse schema + Query API RRF prefetch spelled out.
  - Retriever Gate = result-count + per-modality pre-fusion diagnostics + citation
    evidence (NOT fused-score floor).
  - Summarizer = deterministic Gate (structure/compression/entity-presence) +
    ingest-time Eval (LLM-judge faithfulness, off hot path).
  - Enumeration split: metadata→scroll, content→new `lexscan` exhaustive+dedup verb.
  - Nesting: agents return {output, metadata, self-report} only; skill owns Gates +
    ladder.
  - No-cap failsafe: preflight fan-out estimate + live telemetry flush + soft-warn
    journal events.
  - Markdown parser (fenced code/tables, heading_path); PDF loader verdicts
    (encrypted/scanned→needs_user, OCR out of scope).
  - CLI: all verbs --json + stable schemas + exit codes; added `lexscan`.
  - Synthesis: dedup by document_id/point-id + coverage ledger per sub-query.
  - Dev-time evals: frozen mini-Corpus, fixture schema, expected point-IDs, CI triggers.
  - Journal schema (ask_id/attempt_id/agent/verdict-enums/tokens/spend) + redaction
    allowlist.
  - Slogan reworded (CONTEXT + PLAN goal).

---

## Round 2 — Codex (same thread, resumed)

VERDICT: REVISE — Round-1 substance confirmed resolved; 6 new findings (cross-doc
drift + tightening the 3 new mechanisms):
- [H] ADR-0003 + CONTEXT still said retriever "score floor" / summarizer runtime-judge.
- [H] `lexscan` not implementable as "exhaustive sparse/exact" — sparse is top-k, `text` not indexed.
- [H] upsert-then-delete still has duplicate-visible / crashed-mid-update window.
- [M] CONTEXT "code repositories / one Document per file" drift.
- [M] payload field drift (`source_path` vs `document_path`); ADR-0002 too.
- [M] retriever Gate + coverage ledger lack concrete pass/fail + refusal contracts;
  BGE-M3 sparse→Qdrant adapter (lexical_weights→indices/values) unspecified.

### Claude's response
Accepted all 6 → rev 3:
- Propagated Rev-2 gate semantics to CONTEXT.md (Quality Gate def) + ADR-0003
  (retriever deterministic predicate; summarizer Gate=deterministic, faithfulness=Eval).
- `lexscan` redefined: Qdrant **full-text payload index on `text`** (MatchText) +
  paginated exhaustive scroll + dedup + pinned tokenizer + `--count`. Added full-text
  index to payload schema.
- Versioning: added `document_version`; flow = upsert→verify→delete-stale; retrieval
  dedups by document_id keeping highest version; added `gc` reconcile verb for crash
  recovery.
- CONTEXT drift fixed (codebase summaries / Producer bundle yields many Documents).
- Field name standardized to `document_path` (PLAN + ADR-0002; removed `source_path`).
- Retriever Gate predicate made concrete: `candidate_count≥1 AND (dense_hits≥1 OR
  sparse_hits≥1) AND citationable_count≥1`. Coverage ledger passed into
  answer-synthesizer + coverage verdict (complete|partial) + qualify/refuse rule.
- BGE-M3 sparse adapter contract: `return_sparse=True` → `lexical_weights`
  {token_id:weight} → `SparseVector(indices=sorted, values=...)`, pinned model/
  tokenizer + fixture test.

---

## Round 3 — Codex (same thread, resumed)

VERDICT: REVISE — all Round-2 fixes confirmed resolved; ONE blocking correctness bug:
- [H] `document_version = document_sha256` then "keep highest version" — SHA256 is
  unordered, so "highest" can pick stale content and `gc` can't determine latest.

### Claude's response
Accepted (real bug I introduced) → rev 4:
- Separated content identity from version order: `document_sha256` = content identity
  (dedup + point-ID input, NOT ordered); added monotonic **`ingest_generation`**
  (int per document_id) as the orderable version.
- Update flow: sha-unchanged→no-op; else upsert at generation G+1 → verify → delete
  `< G+1`. Retrieval/synthesis dedup by document_id keeping **max ingest_generation**.
  `gc` retires non-max generations (monotonic → latest unambiguous).
- Fixed payload schema + gc verb wording accordingly.

---

## Round 4 — Codex (same thread, resumed)

VERDICT: REVISE — hash-ordering bug confirmed fixed; ONE blocking failure-path issue:
- [H] Failed verification can make bad chunks visible: upsert G+1 → verify → delete,
  but G+1 is already max generation, so if verify fails retrieval surfaces unverified
  chunks.

### Claude's response
Accepted (real gap) → rev 5:
- Added `ingest_state` (pending|active|failed) visibility gate. New chunks upsert as
  `pending`; verify filtered by document_id+generation; promote to `active` only on
  pass (then retire older active); on fail delete/mark `failed` pending, prior active
  stays served + `INDEX_SUSPECT`.
- Retrieval (`search`/`lexscan`) filters `ingest_state=active` (added query_filter +
  keyword index). Dedup by max active generation. `gc` also clears pending/failed orphans.

---

## Round 5 — Codex (same thread, resumed) — MAX_ROUNDS cap

VERDICT: REVISE — verify-visibility fix confirmed for search/lexscan; ONE remaining:
- [H] Metadata `scroll` + `status` don't filter `ingest_state=active`, so they can
  count pending/failed/superseded chunks.

### Claude's response
Accepted (same correctness family, trivial) → rev 6:
- Extended `ingest_state=active` filter + max-active-generation dedup to **every read
  path** (search, lexscan, scroll, status), per Phase A.6 + the metadata-Enumeration
  + scroll-verb lines.

---

## Resolution — stopped at the cap (5 rounds), with one open design choice

The loop ran the full MAX_ROUNDS=5. Trajectory: **22 → 6 → 1 → 1 → 1**. Honest read:

- **Rounds 1–2 were genuine architecture wins** (keep these): embedder library fix
  (FlagEmbedding, no e5 prefix), RRF-score-floor correction, content-vs-metadata
  Enumeration split (`lexscan`), Source→Document identity model, citation-grade
  payload, CLI `--json` contracts, runtime-Gate-vs-dev-time-Eval split, journal
  schema, no-cap soft failsafe, doc-drift fixes.
- **Rounds 3–5 were whack-a-mole** in ONE subsystem: each fix introduced the next
  bug, all inside an ingest **versioning/visibility state machine**
  (`ingest_generation` → `pending/active` → read-path filters). That state machine
  is **loop-generated, not user-requested** — the user asked for eval-gated agents
  that reiterate, not transactional visibility semantics in the vector store. For a
  single-writer, single-user, local CLI it may be solving problems that do not exist
  (concurrent readers racing a re-ingest; crash-recovery mid-update). The bug class
  Codex keeps finding exists *because* of the verify-gate.

**Not "converged."** The broad architecture is hardened; the ingest-versioning
subsystem is neither demonstrably done nor demonstrably needed.

### Open design choice (for the user, before any more review)
Does aineverforget need **verify-gated visibility + `ingest_generation` + `gc`**, or
does a single-writer local tool get the same correctness from the **simple model**:
delete-by-`document_id` then insert, idempotent by content hash, verify probes that
**warn (`INDEX_SUSPECT`) rather than gate visibility**? The simple model retires the
entire round-3–5 bug class. This is the lever — resolve it first; THEN (if simplified)
one confirmation Codex round on the simpler design is worth running.

**Status: stopped at cap. Awaiting user decision on the ingest model, then sign-off.
No code during either act.**

---

## Post-cap — user decision + pre-hardening (rev 7)

User chose to **keep the transactional verify-gated ingest model** (informed: flagged
as unrequested complexity likely to seed more rounds). Honoring it.

To avoid future whack-a-mole, pre-hardened the advisor-predicted weak spots in the
state machine (rev 7), rather than waiting for Codex to surface them one per round:
- **Ingest lock** (reuse `run_lock.py`) enforces single-writer → `read max G → write
  G+1` is race-free; concurrent ingest rejected, not corrupting.
- **No-op only vs an `active` generation.** No active (first ingest / prior failed) →
  re-ingest proceeds even on hash match; gc stale pending/failed first. (Closes the
  "sha matches a failed attempt" hole.)
- **Verify targets the pending set explicitly** (`document_id + ingest_generation=G+1`).
- Failed first ingest → Document simply absent (no active), not half-served.
- **gc never touches the active-max set** → concurrent reads always correct; gc is
  pure reclamation.

Optional: a confirmation Codex round on rev 7 to check the pre-hardening closed the
predicted gaps.

---

## Round 6 — Codex confirmation on rev 7 (user-approved, beyond default cap)

VERDICT: REVISE — pre-hardening confirmed (lock, no-op-vs-active, failed-first-absent,
read-path filters all good). ONE genuine semantic bug (different class — real, not
whack-a-mole):
- [H] Verify scoped only to `document_id + ingest_generation=G+1` proves the chunks
  exist but NOT retrievability against the real corpus, and breaks negative-probe
  semantics (nothing to compete against).

### Claude's response
Accepted (real, in-scope: verify-probe design is part of the requested eval system)
→ rev 8:
- Verify now runs against a **verification view** = `active OR (document_id=X AND
  ingest_generation=G+1)`. topical/specific must retrieve the pending gen among all
  active Chunks; negative must NOT surface it (competing against unrelated active
  Chunks). Then promote on pass.

---

## Round 7 — Codex confirmation on rev 8

VERDICT: REVISE — verification-view fix confirmed. ONE real cold-start edge case:
- [H] First ingest fails the negative probe — empty active corpus means the unrelated
  query returns the only pending Chunk, blocking initial indexing.

### Claude's response
Accepted (real, in-scope) → rev 9:
- Cold-start rule: negative probe deferred (or run vs a frozen unrelated background
  fixture) until ≥1 unrelated active Document exists. Topical/specific still apply.

---

## Resolution — locking recommended (7 rounds run; 2 beyond default cap)

Trajectory: **22 → 6 → 1 → 1 → 1 → 1 → 1**. Two distinct phases:
- **Rounds 1–2:** broad architecture — real, high-value, fully resolved.
- **Rounds 3–5:** whack-a-mole in the (user-kept) ingest state machine — pre-hardened
  in rev 7 to stop the bleed.
- **Rounds 6–7:** genuine verify-probe design subtleties (verification view, cold-start)
  — real and in-scope, both fixed.

**Why stop here (not round 8):** rounds 6–7 are no longer architectural — they are
verify-probe *semantics* that a design doc can be mined for edge cases indefinitely
(threshold tuning, near-duplicate Documents, background-fixture composition…). The
correct place to settle those is **empirically, with real gold fixtures in Phase D**,
not in prose. The plan now captures the structural rules (verification view, cold-start,
deferral); the residual is calibration, which is build-time.

Codex never emitted a literal APPROVED, but there is **no open architectural
disagreement** — every finding was accepted and fixed except the `.codex`-vs-`.claude`
location (rejected with rationale Codex accepted). Recommendation: **lock rev 9**; treat
further verify-probe precision as a Phase-D empirical task.

**Status: recommend lock. Awaiting user sign-off. No code during either act.**

---

## Round 8 — Codex on rev 9 — APPROVED

VERDICT: **APPROVED**. No remaining concrete correctness or architectural holes.
Codex confirmed the ingest/versioning/visibility state machine covers all prior
failure cases (single-writer lock, active-only no-op, verification view, cold-start
negative deferral, active-only reads across search/lexscan/scroll/status, max-active
dedup, gc preserving active-max). Retrieval thresholds, probe calibration, and
background-fixture composition are correctly scoped as Phase-D empirical work.

## FINAL — converged & APPROVED at rev 9 (8 Codex rounds)

Trajectory: **22 → 6 → 1 → 1 → 1 → 1 → 1 → APPROVED**. Plan locked. Both acts complete:
Act 1 grill (12 forks → CONTEXT.md + 3 ADRs), Act 2 Codex adversarial review (8 read-only
rounds, one resumed thread). No open disagreements. Ready for build on user's go.
