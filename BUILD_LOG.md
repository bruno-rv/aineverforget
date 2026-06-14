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

## Phase C — Orchestrator Skills — COMPLETE

> Status: **3 files** — `/ingest` skill, `/ask` skill, `gate_synthesis.py` gate script (smoke-tested 4 cases).

Files:
- `.claude/skills/ingest/SKILL.md` — per-source dispatch (note-summarizer if raw → knowledge-indexer), probe gate, INDEX_SUSPECT routing, Two-Strike on note-summarizer, lock/skip/no-op handling
- `.claude/skills/ask/SKILL.md` — Recall/Synthesis/Enumeration routing, Synthesis decomposition + preflight fan-out estimate, retriever ×N + dedup by point_id + coverage ledger, synthesizer gate via gate script, Two-Strike on retriever + synthesizer
- `scripts/gate_synthesis.py` — deterministic CLI gate for answer-synthesizer: recomputes `all_cited_ids_in_input` (set join), `groundedness_pass` (lexical overlap, no LLM judge), `coverage_ledger_consistent` (partial verdict + qualification required when any sub-query is empty); exit 0/1 + JSON diagnostics

Key decisions:
- Gate execution form: simple boolean/integer fields checked via inline `python3 -c` one-liner; synthesizer judgment fields recomputed by `scripts/gate_synthesis.py` (exit-code-based, deterministic — per ADR-0003 and advisor)
- All CLI command signatures verified against `cli.py` before writing (ingest/verify/search/lexscan/scroll — all use positional args, not `--<verb>-id` flags)
- Journal and Cost Telemetry calls are Phase E forward-refs (explicit no-op markers in both skills); skills are authored but not end-to-end executable until E
- Opus on reiterate ladder is a documented exception (PLAN.md risk #6) — kept as written, not "corrected" by global model rules
- Retriever gate: lexscan/scroll modes always pass (empty is valid); hybrid modes use `candidate_count ≥ 1 AND (dense_hits ≥ 1 OR sparse_hits ≥ 1) AND citationable_count ≥ 1` (per contract)
- Synthesizer judgment fields annotation enforced: skill saves ranked_chunks to `/tmp/ainf_synth_chunks.json` before dispatch so gate script can join against it
- Coverage ledger built from retriever `candidate_count` (authoritative) not synthesizer self-report
- Groundedness gate: tokenize claim + chunk text, filter stopwords, require ≥1 shared non-stopword token; deterministic, no NLI model (v1.1 upgrade per PLAN.md risk #4)

## Phase D — Dev-time Eval Harness — COMPLETE + integration-validated

> Status: **13 files** — frozen mini-corpus (3 notes + README), 4 fixture YAMLs, 4 eval scripts, 1 CI workflow.
> Deterministic runners: **7/7 scorer self-tests pass**, **5/5 gate synthesis fixture cases pass**.
> Integration eval (live Qdrant + bge-m3): **5/5 retrieval cases pass** (3 hybrid recall@1/MRR=1.0, 2 lexscan min_hits met).
> 341 unit tests still pass after ingest.py fix.

Files:
- `tests/eval/corpus/` — 3 frozen .md notes (note_raw_transcript.md, note_prestructured.md, note_technical.md) + README
  - Topics: DataSync PostgreSQL migration, VectorCore Qdrant ADR, APIGateway rate limiting
  - Corpus is **byte-frozen** — content changes rotate document_sha256 → rotate point_ids → invalidate retrieval gold
- `tests/eval/fixtures/` — 4 YAML fixture files per agent
  - `note_summarizer.yaml` — structure checks, required sections, entity list, compression bounds
  - `knowledge_indexer.yaml` — stable document_ids (pre-computed UUIDv5), probe verdict expectations
  - `knowledge_retriever.yaml` — gold Q→document_id mappings for recall@1/3/5 + MRR; lexscan min_hits cases
  - `answer_synthesizer.yaml` — 5 gate_synthesis.py cases (cited IDs, groundedness, coverage ledger: 2 pass + 3 fail)
- `scripts/eval_scorers.py` — recall@k / MRR metrics; pure Python stdlib; 7 self-tests pass standalone
- `scripts/eval_gate_synthesis.py` — runs gate_synthesis.py against answer_synthesizer.yaml; 5/5 cases pass
- `scripts/eval_note_summarizer.py` — validates saved note-summarizer output JSON against fixture (structural + entity checks)
- `scripts/eval_retrieval.py` — integration eval (needs live Qdrant); `--ingest` flag uses `--source-id` for stable document_ids
- `.github/workflows/eval.yml` — CI: deterministic job (no Qdrant, runs on push); integration job (manual dispatch, docker Qdrant)

Key decisions:
- **document_id stability**: eval uses repo-relative `--source-id tests/eval/corpus/<file>.md` for stable cross-machine IDs
- **document_id vs point_id in gold**: recall gold keys on `document_id` (path-stable, survives chunker changes); point_id would rotate on content/chunker change (one of the CI triggers)
- **RAGAS deferred**: LLM-judge faithfulness/answer-relevance are Phase D v1.1 — not added until dep + API key confirmed with user; deterministic metrics (recall@k/MRR + gate_synthesis.py unit tests) ship as the runnable backbone
- **Integration eval is manual-dispatch only in CI**: `--ingest` flag + `eval_retrieval.py` documented but gated behind `workflow_dispatch` event (requires live Qdrant + FlagEmbedding model loaded)
- **CI triggers**: push/PR on `chunking.py`, `embedding.py`, `gate_synthesis.py`, agent dev files, fixtures, corpus — exactly the set of changes that can invalidate eval results

Bugs found and fixed during integration run:
- **`--source-id` document_id not portable** (`ingest.py`): `make_document_id(path_source_id, d.document_path)` used the abs path as document_path, making document_id machine-specific. Fixed: `make_document_id(path_source_id, path_source_id)` — source_id used for both, matching `UUIDv5(NS, f"{sid}|{sid}")` formula; 341 tests still pass.
- **eval_retrieval.py glob included README.md**: changed `*.md` → `note_*.md` to exclude the freeze-warning README from ingest.
- **eval_retrieval.py threshold keys mismatched**: fixture uses `recall_at_1`/`min_mrr`; code was looking for `recall@1`/`mrr_threshold` → thresholds never enforced. Fixed key lookups.
- **eval_retrieval.py lexscan routing**: `case.get("type")` but fixture uses `search_mode` → lexscan cases routed as hybrid. Fixed to check `search_mode` first.
- **eval_retrieval.py lexscan term key**: `case.get("term")` but fixture uses `query` → term always empty string. Fixed to fall back to `query`.

## Phase E — Run Journal + Observability — COMPLETE

> Status: **370 tests pass** (29 new journal tests). Smoke-validated: JSONL+SQLite dual-write,
> redaction, query functions, CLI driver, `aineverforget status` journal section.

Files:
- `src/aineverforget/run_journal.py` — core module: VALID_EVENTS frozenset, per-event
  detail allowlist, `redact()` (JWT/hex/base64 + key-name blocklist), fcntl-locked JSONL
  append, SQLite WAL mirror (busy_timeout=5000), `append_event()` (both writes non-fatal;
  stderr warn if both fail), `recent_events(n)`, `recent_runs(n)`.
- `scripts/run_journal.py` — CLI driver: all event types + top-level/detail fields as flags;
  `--list N`, `--runs N` query modes; always exits 0 (non-fatal).
- `tests/test_run_journal.py` — 29 tests: redact (JWT/hex/dict/nested), append_event
  (JSONL + SQLite write, allowlist, unknown-kwarg drop, dual-failure non-fatal), recent_events,
  recent_runs (dispatch count aggregation).
- `.claude/skills/ingest/SKILL.md` — 9 forward-ref markers replaced with real
  `scripts/run_journal.py` bash calls; run_id written to `/tmp/ainf_run_id` at RUN_START.
- `.claude/skills/ask/SKILL.md` — all JOURNAL + TELEMETRY forward-refs replaced;
  cost_tally_tokens removed; cost summary block cleaned; run_id from `/tmp/ainf_run_id`.
- `src/aineverforget/cli.py` — `cmd_status` enhanced: `recent_events(5)` + `recent_runs(3)`
  added to both JSON (`result["journal"]`) and non-JSON output.

Key decisions:
- **No TELEMETRY events emitted from skills**: token counts are not available from the
  orchestrator skill context (Agent tool returns text, not usage). TELEMETRY event type
  stays in VALID_EVENTS for future use; dispatch count is the v1 cost proxy.
- **run_id via /tmp/ainf_run_id**: Bash state doesn't persist across Claude Code tool calls;
  run_id written to `/tmp/ainf_run_id` at RUN_START/ASK_START and read with `$(cat ...)`.
- **Journal failures always non-fatal**: each of JSONL + SQLite wrapped in try/except
  independently; stderr warning only when BOTH fail simultaneously.
- **Journal dir**: `$AINF_JOURNAL_DIR` env var or `<project_root>/runs/` (already in .gitignore).
- **`aineverforget status` adds journal section**: recent runs + recent events only when DB
  exists (empty journal = no section shown, not an error).

## Next phases (per PLAN.md)
- F: supervised live dry-run on real Qdrant + sign-off.
