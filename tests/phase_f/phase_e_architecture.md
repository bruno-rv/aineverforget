## TL;DR

Phase E adds an append-only dual-write Run Journal (JSONL + SQLite) to aineverforget. Every ingest and ask run emits structured events: RUN_START, DISPATCH_START, GATE_PASS/FAIL, SOFT_WARN, INDEX_SUSPECT, ASK_START, ASK_CLOSE, RUN_CLOSE, TELEMETRY. Journal is non-fatal: backend failures warn to stderr but never abort the run. Redaction strips JWTs, hex secrets, and base64 tokens from all event payloads before write.

## Key Concepts

- **append_event()** — primary write entry point in `src/aineverforget/run_journal.py`. Validates event name against VALID_EVENTS frozenset (10 types), routes kwargs through _TOP_LEVEL_FIELDS and _DETAIL_ALLOWLIST, redacts secrets, writes JSONL then SQLite independently.
- **_TOP_LEVEL_FIELDS** — frozenset of fields that go directly into the top-level record dict: run_id, ask_id, ingest_id, attempt_id, agent, gate, gate_score, verdict, model, escalated, tokens, spend.
- **_DETAIL_ALLOWLIST** — per-event frozenset of fields permitted in the `detail` JSON blob. Fields in _TOP_LEVEL_FIELDS shadow the allowlist (they're routed top-level first via if/elif; the elif for detail is unreachable for shadowed fields).
- **redact()** — recursive secret-scrubber. Three regex patterns: JWT (eyJ prefix), hex 32+ chars, base64 40+ chars (no `/` in charset to avoid mangling absolute paths). Key-name blocklist: password, secret, token, api_key, etc.
- **dual-write non-fatal** — both JSONL and SQLite writes are in independent try/except blocks. `errors` list collects failures; any non-empty errors list prints warning to stderr. Single-backend failure warns; total failure warns but still returns the record.
- **_schema_initialized sentinel** — module-level set of db_path strings. DDL runs once per db_path per process via _ensure_schema(). Prevents schema write lock contention under parallel agent fan-out.
- **PRAGMA busy_timeout** — SQLite concurrency handled via `PRAGMA busy_timeout=5000` (5s retry). Python `sqlite3.connect(timeout=)` is NOT used in _db_insert because PRAGMA busy_timeout overrides it (SQLite C API: sqlite3_busy_timeout() clears the handler set by sqlite3_busy_handler()).
- **scripts/run_journal.py** — CLI driver for journal events. Accepts event name + flags matching _TOP_LEVEL_FIELDS and _DETAIL_ALLOWLIST fields. Used by /ingest and /ask skills via bash blocks.
- **recent_events(n)** — reads n most recent events from SQLite, oldest first. Returns [] on missing DB. Logs to stderr on read error.
- **recent_runs(n)** — CTE query joining RUN_START/ASK_START rows with DISPATCH_START counts per run_id. Returns run summaries with dispatch count.

## Key Decisions

- Dual-write (JSONL + SQLite) rather than single backend: JSONL for human-readable audit trail and streaming tail; SQLite for structured queries (recent_runs, dispatch counts, gate_score aggregates). Either can fail without losing the other.
- Non-fatal writes: journal failures never abort ingests or asks. Observability must not create availability risk.
- Redaction before write: secrets stripped at append_event() boundary, not at query time. Source fields (absolute paths) preserved by excluding `/` from the base64 charset.
- ASK_START emitted after routing (STEP 0): ask_type is determined by routing; journaling before routing means ask_type is always absent. Moved to after STEP 0 with explicit --ask-type flag.
- SOFT_WARN fires on every dispatch ≥ threshold, not only on reiterate: first-pass fan-out of N=13 sub-queries crosses SOFT_WARN_THRESHOLD=12 silently if the check is gated on reiterate path only.

## Action Items

- [ ] F13 (P2): scripts/run_journal.py — emit JSON error object when --json + unknown event type, instead of empty stdout.
- [ ] F15 (P2): Remove ask_id/ingest_id columns from SQLite DDL and _TOP_LEVEL_FIELDS; neither skill uses them. Option A (remove) preferred over Option B (wire --ask-id in skills).
- [ ] Phase F: supervised live dry-run on real Qdrant + bge-m3. Ingest mixed batch, run Recall/Synthesis/Enumeration asks, confirm gates + journal populate correctly.
- [ ] RAGAS deferred: LLM-judge faithfulness/answer-relevance metrics not in Phase E. Add in Phase D v1.1 once dep + API key confirmed.
