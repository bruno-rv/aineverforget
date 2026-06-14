# Build Log: aineverforget

## Phase A — Tool layer (Python CLI) — COMPLETE + real-server validated + Codex-APPROVED

> Status: **341 unit tests pass**, **real-server smoke 17/17** (live bge-m3 + live Qdrant),
> Codex code review **APPROVED** (6 rounds, 12→6→3→1→APPROVED — see CODE-REVIEW-LOG.md).

Built via parallel sonnet agents against frozen contracts (foundation → wave1 → wave2).
**307 tests pass** (`.venv` py3.14). Console script `aineverforget` installed; `--help`
+ `status --json` work; a live Qdrant is reachable at 127.0.0.1:6333.

Modules (`src/aineverforget/`):
- `models.py` / `identity.py` / `config.py` — Document/Chunk/IngestState; sha256 + UUIDv5
  point IDs; settings. (foundation)
- `run_lock.py` — single-writer ingest lock. **Fixed a double-close bug** (overlap path
  unlocked+closed the sidecar fd then the `finally` closed it again → OSError(EBADF)
  masked IngestLockOverlapError). Now raises cleanly.
- `loaders/` — md/txt (header/title) + pdf (page-aware; verdicts ok/encrypted/scanned/
  low_confidence). **low_confidence is unicode-aware** (letter/mark/number/punct ratio +
  U+FFFD/control mojibake signal) — Portuguese/CJK pass as ok (pt+en corpus, ADR-0002).
- `chunking.py` — markdown via mistune v3 AST (never splits fenced code/tables, heading_path),
  prose word-window (220/40), pdf page-aware.
- `embedding.py` — BGEM3FlagModel dense+sparse; **sparse adapter coerces FlagEmbedding's
  string token-ids to int** before sorting (lexicographic sort would corrupt Qdrant index
  order). Model lazy-loaded + mocked in tests.
- `store.py` — one collection (dense 1024 cosine + sparse), hybrid RRF via Query API,
  lexscan (full-text MatchText + paginated scroll + count), scroll (metadata, active-only),
  versioning (ingest_generation + ingest_state), verification_view_filter, gc, status.
  Tested via real `QdrantClient(":memory:")`.
- `verify.py` — topical/specific/negative probes against the verification view; cold-start
  defers the negative probe.
- `ingest.py` — full rev-9 flow: lock → load → no-op-vs-active → upsert pending (G+1) →
  verify → promote active + retire old / fail → index_suspect. e2e tested.
- `cli.py` — 7 verbs (ingest/search/lexscan/scroll/verify/status/gc), all `--json` with
  stable schemas + exit codes.

### Real smoke test (live bge-m3 + live Qdrant server) — `scripts/smoke_real.py`
- **H1 embedder VALIDATED against the real model**: dense dim 1024; sparse indices
  ascending + **int** (the FlagEmbedding string-token-id int-coercion belief is now an
  observed fact, n=14 sample `[7,8,28,71,168…]`); query dense 1024. ✓
- **BUG FOUND (verify gate)**: real ingest returned `index_suspect` → a legitimate doc
  was rejected + deleted. Root cause (debug_verify.py): verify probes use lexical
  `MatchText`; a **multi-word** topical query returns 0 because MatchText is AND-within-a-
  single-chunk, and the query's words are split across chunks. Single-word probes pass.
  → Confirms the advisor-predicted fidelity hole. **FIXED**: `store.search()` gained
  `view_filter: Any | None = None` param; `verify.run_probes()` now requires `embedder`
  and uses hybrid search (dense+sparse RRF) for topical/negative probes; `ingest.py`
  passes the embedder at the call site. Specific probe stays lexical (single distinctive
  term). Re-ran smoke: H3/H4 both PASS — `document INDEXED (verify passed, promoted active)`.
  309 unit tests pass (2 new: `test_search_with_view_filter_includes_pending_gen` + monkeypatched
  negative PASS/FAIL tests in test_verify.py).

### Known gaps / deferred (track for later phases)
1. ~~`:memory:` lexscan unvalidated~~ **lexscan CONFIRMED working on the real server**
   (`Marmota`→1, `Curseduca`→1, lowercase→1, absent→0). The earlier smoke "H2 fail" was a
   harness bug (read a dict return via `getattr` → always 0); fixed. **Smoke now 12/12.**
   (`:memory:` still doesn't enforce payload indexes — keep validating new store features on
   the real server, but the full-text path is proven.)
2. ~~**verify uses MatchText (lexical), not hybrid retrieval**~~ **FIXED + real-server verified.**
3. **FlagEmbedding + pdfplumber not installed** in env → real embedding + pdf fallback
   not exercised live (mocked in tests). Validate in Phase F.
4. Dev-time eval harness (RAGAS/gold), Run Journal + Cost Telemetry observability, and the
   AgentSpec agents/skills are Phases B–E — not yet built.

## Phase B — AgentSpec agents + Knowledge Bases — COMPLETE

> Status: **27 files** written across 4 agents, 4 KB domains, shared contract, registry, KB index.

Built via parallel sonnet agents; schema frozen before build to prevent drift; verified post-hoc.

Files:
- `.claude/agentspec/agents/dev/` — `note-summarizer.md`, `knowledge-indexer.md`,
  `knowledge-retriever.md`, `answer-synthesizer.md`
- `.claude/agentspec/kb/` — 4 domains × 5 files each (index, quick-reference, 1 concept,
  1 pattern, 1 troubleshooting) = 20 KB files
- `.claude/agentspec/shared/self-report-contract.md` — frozen schema + gate table for all 4 agents
- `.claude/agentspec/kb/_index.yaml` — KB domain index
- `.claude/rules/agent-registry.md` — routing rules, failure table, Two-Strike rule

Key decisions:
- `verify` CLI arg is positional (`aineverforget verify <document_id> --json`), not `--document-id`
- knowledge-retriever gate applies only to recall/synthesis_sub; lexscan/scroll always `gate_pass=true`
- answer-synthesizer gates are declarative booleans in `self_report` (no Bash sys.exit); skill recomputes
- Judgment fields (`groundedness_pass`, `all_claims_cited` etc.) annotated: skill must recompute from
  citations join — must NOT gate blindly on agent's booleans (Phase C note in contract)
- Contract typo fixed: `"failed"` → `"error"` in knowledge-indexer gate (real outcomes: success/no_op/
  index_suspect/error/skipped)

## Next phases (per PLAN.md)
- C: orchestrator skills `/ingest` `/ask`.
- D: dev-time eval system (gold fixtures + RAGAS + cross-model judge in CI).
- E: observability (Run Journal JSONL+SQLite, Cost Telemetry).
- F: supervised live dry-run on real Qdrant + sign-off.
