# Code Review Log: aineverforget Phase A

Codex read-only code review (thread 019ec249-6bbe-7b72-86af-6772d011b70b).

## Round 1 — Codex (VERDICT: REVISE, 12 findings: 6 High / 5 Med / 1 Low)

**High**

- [ingest.py:447](/Users/bruno/Dev/aineverforget/src/aineverforget/ingest.py:447) + [cli.py:86](/Users/bruno/Dev/aineverforget/src/aineverforget/cli.py:86): `aineverforget ingest` never supplies probes, and `ingest_paths()` treats `probes=None` as verified, so real CLI ingests promote unverified chunks active. Fix: make probes mandatory for production ingest, or fail closed unless an explicit unsafe/test flag is set.

- [verify.py:329](/Users/bruno/Dev/aineverforget/src/aineverforget/verify.py:329) + [verify.py:454](/Users/bruno/Dev/aineverforget/src/aineverforget/verify.py:454): topical/negative probes check only `document_id`, not the pending `ingest_generation`; an old active generation can pass topical or fail negative for the new pending generation. Fix: require `c.document_id == document_id and c.ingest_generation == generation`.

- [store.py:354](/Users/bruno/Dev/aineverforget/src/aineverforget/store.py:354) + [store.py:456](/Users/bruno/Dev/aineverforget/src/aineverforget/store.py:456) + [store.py:1080](/Users/bruno/Dev/aineverforget/src/aineverforget/store.py:1080): `search`, `lexscan`, and `status` filter active but do not drop non-max active generations, so a crash after promote but before old-gen delete can surface/count superseded chunks. Fix: post-filter by per-`document_id` max active `ingest_generation` on every read path.

- [run_lock.py:486](/Users/bruno/Dev/aineverforget/src/aineverforget/run_lock.py:486): the Python `IngestLock` writes one heartbeat but never refreshes it, so any ingest lasting past `grace_hours` can be reclaimed by a second live writer. Fix: start an owner heartbeat thread/process in `IngestLock`, or never reclaim a live PID solely for stale heartbeat.

- [ingest.py:458](/Users/bruno/Dev/aineverforget/src/aineverforget/ingest.py:458): `promote_generation()` return count is ignored; if zero/partial promotion happens, old active chunks are still retired and the report says success. Fix: require promoted count to equal the pending chunk count before deleting old generations.

- [cli.py:177](/Users/bruno/Dev/aineverforget/src/aineverforget/cli.py:177) + [store.py:1209](/Users/bruno/Dev/aineverforget/src/aineverforget/store.py:1209): CLI passes `tags=[]` when no tag filter is requested, and the store turns that into `MatchAny([])`, which returns no results. Fix: pass `None` for absent tags or change `_optional_filters` to `if tags:`.

**Medium**

- [ingest.py:365](/Users/bruno/Dev/aineverforget/src/aineverforget/ingest.py:365): multi-document loaders are silently truncated to `documents[0]`, losing every other Document in a Source. Fix: iterate all loaded Documents and account/report per Document or aggregate per path.

- [ingest.py:346](/Users/bruno/Dev/aineverforget/src/aineverforget/ingest.py:346) + [chunking.py:98](/Users/bruno/Dev/aineverforget/src/aineverforget/chunking.py:98): `source_id` override is computed but unused, and `producer` is resolved but not propagated into chunks; `--source-id`/`--producer` do not affect stored identity/provenance. Fix: rewrite loaded Documents with caller source/producer before chunking, or pass those fields through loader/chunker APIs.

- [ingest.py:395](/Users/bruno/Dev/aineverforget/src/aineverforget/ingest.py:395) + [ingest.py:540](/Users/bruno/Dev/aineverforget/src/aineverforget/ingest.py:540): no-op hash check reads an arbitrary active chunk, not the max active generation, so lingering superseded active chunks can cause a false no-op. Fix: fetch `document_sha256` from active generation `G` specifically.

- [cli.py:323](/Users/bruno/Dev/aineverforget/src/aineverforget/cli.py:323): `verify` CLI calls the new `run_probes()` signature without `embedder`, so it always errors; even if fixed, it passes an empty probe list and verifies nothing. Fix: instantiate/pass `BGEM3Embedder` and add a real probe input contract or reject empty verification.

- [store.py:480](/Users/bruno/Dev/aineverforget/src/aineverforget/store.py:480) + [cli.py:235](/Users/bruno/Dev/aineverforget/src/aineverforget/cli.py:235): `lexscan --count` returns matching chunk count, not total term occurrences as specified for “how many times did I mention Y?”. Fix: compute and return `occurrence_count` separately from `chunk_count`.

**Low**

- [chunking.py:498](/Users/bruno/Dev/aineverforget/src/aineverforget/chunking.py:498): markdown headings update `heading_path` but do not advance word offsets, so subsequent `chunk_start_word`/`chunk_end_word` are not offsets into `Document.raw_text`. Fix: account for heading token word counts or include heading text in emitted chunk text/offset math.

VERDICT: REVISE

### Claude (arbiter): accepted all 12 → dispatching coordinated fix agent. Re-validate (unit + real smoke) then re-review.


---

## Round 2 — Codex re-review (fix pass interrupted by spend limit)

VERDICT: REVISE. Per-finding status:
- #1 PARTIAL (ingest_paths fail-closes, but CLI has no --no-verify flag / no probe path)
- #2–#12 UNADDRESSED
- NEW BREAKAGE: CLI `ingest` raises ValueError (require_verify default) but cmd_ingest passes
  neither probes nor require_verify=False; the error tells users to use `--no-verify` which
  doesn't exist; tests calling ingest without probes not migrated → 7 unit-test failures.

State: codebase BROKEN (302 pass / 7 fail), 11 of 12 findings still open. Fix agent died on
a monthly, account-wide spend limit (~76 tool-uses in). Re-dispatching sonnet will hit the
same wall until the cap is raised.

Remaining work (Codex punch list, priority order):
1. Finish verification contract: CLI probe input or explicit `--no-verify`; pass require_verify
   deliberately; migrate tests that bypass verification.
2. Verify must prove the PENDING generation (#2); add re-ingest-with-old-active tests.
3. Validate promote count before retiring old gens (#5); index_suspect/cleanup on exceptions.
4. Generation-aware read paths: search/lexscan/status + no-op SHA (#3, #9).
5. Empty-tag handling [] == no filter (#6).
6. Metadata: multi-document ingest (#7), source_id override + producer propagation (#8).
7. cli verify repair (#10), lexscan occurrence count (#11), lock heartbeat (#4), md offsets (#12).


---

## Round 3 — Codex on completed fixes (326 tests, smoke 17/17 verified)

VERDICT: REVISE. 6 RESOLVED (#1,#2,#5,#6,#8,#9,#11), 6 PARTIAL + new breakage:
- #7/NEW-HIGH: multi-doc Sources half-ingested — docs after documents[0] upserted pending,
  never verified/promoted/reported (flow keyed to first document_id).
- #3/NEW-HIGH: max-gen dedup is result-local not corpus-local — a query matching only old-gen
  content can return the stale active gen even when a newer active gen exists.
- #10 PARTIAL: cmd_verify passes embedder but runs probes=[] → trivially passes, verifies nothing.
- NEW-MED: promote_generation exception (not just count-mismatch) returns error without deleting
  pending G1.
- #4 PARTIAL: live-PID now beats stale heartbeat (good) but IngestLock never refreshes heartbeat.
- #12 PARTIAL: heading offsets advance by heading text words, not markdown marker tokens.
- NEW-LOW: chunk_document(producer=) ignores its own arg (ingest masks it).

### Claude (arbiter): all legit → round-3 fix agent (per-doc loop, corpus-local dedup, real
verify probes, promote-exception rollback, heartbeat refresher, offset/producer cleanup), then
final Codex round.


---

## Round 4 — Fix agent results (338 tests, smoke 17/17 verified)

All 7 Round 3 findings resolved:

- **Fix A (HIGH) RESOLVED**: Multi-doc Sources — restructured `_ingest_one_path()` to call
  new `_ingest_one_document()` helper for EACH Document; per-doc G/G1/verify/promote/retire
  cycle; path-level outcome aggregated by rank (error>index_suspect>skipped>success>no_op).

- **Fix B (HIGH) RESOLVED**: Corpus-local max-gen dedup — `search()` and `lexscan()` both
  now call `self.max_active_generation(doc_id)` per unique doc_id in results, filtering
  stale-gen chunks even when query only matches old-gen content.

- **Fix C (MEDIUM) RESOLVED**: `cmd_verify` derives real probes from stored chunks via
  `store.scroll()`; returns error if no active generation or no chunks; probe list is
  non-empty (topical + optional specific + negative).
  Also fixed: `scroll_result.get("chunks")` → `scroll_result.get("documents")` (API key
  mismatch that would have silently emptied probe list on real server).

- **Fix D (MEDIUM) RESOLVED**: `promote_generation()` call wrapped in try/except; on
  exception: delete pending G1 (best-effort), preserve prior active G, return index_suspect.

- **Fix E (MEDIUM) RESOLVED**: `IngestLock` context manager starts daemon heartbeat thread
  (`ainf-heartbeat-{lock_id[:8]}`); interval = `max(60.0, grace_hours*3600/4)`;
  stopped via threading.Event on __exit__.

- **Fix F (LOW) RESOLVED**: Markdown heading word-offset now counts marker tokens:
  `("#" * level + " " + text).split()` instead of `text.split()`.

- **Fix G (LOW) RESOLVED**: `producer` param plumbed through `chunk_document()` →
  `_chunk_prose()` / `_chunk_pdf()` / `_chunk_markdown()` → `_make_chunk()`.

Test count: 326 → 338 (+12 new tests covering all 7 fixes).
Smoke: 17/17 PASS (real Qdrant server + real bge-m3 embedder).


---

## Round 4 — Codex (338 tests, smoke 17/17 verified) — converging

VERDICT: REVISE. 9/12 RESOLVED; 3 remain:
- MED: scroll() still result-local (search/lexscan fixed, scroll missed) — with --tag/--since an
  older active gen can be max among filtered rows + chunk_count counts stale. Apply
  max_active_generation() per doc_id before doc_map/chunk_count (store.py ~617,637).
- MED: cmd_verify derives probes from scroll() (doc metadata, not chunk text) → specific_word
  rarely derived → only topical+negative run. Fetch real active chunk payloads (cli.py ~337,365).
- LOW: multi-doc path reporting collapses to first document (storage correct, report lossy)
  (ingest.py ~458).

### Claude (arbiter): all legit → round-4 fix (scroll corpus-local dedup, cmd_verify real chunk
derivation, multi-doc report aggregation), then final Codex round.


---

## Round 5 — Codex (340 tests, smoke 17/17 verified) — one trivial finding left

VERDICT: REVISE. Confirmed RESOLVED: scroll corpus-local dedup, store.get_chunks, cmd_verify
real-chunk probes, multi-doc per-doc loop + PathIngestResult aggregation.
Remaining (MEDIUM, follow-through): CLI ingest JSON drops the new `document_ids`/`generations`
fields (present in-memory on PathIngestResult but not serialized) → CLI caller can't see secondary
doc ids/gens. Add both to each JSON result item (ingest.py ~144,493; cli.py ~118).


---

## Round 6 — Codex — APPROVED

VERDICT: **APPROVED**. `ingest --json` now serializes document_ids/generations (single + multi-doc
tested). No remaining concrete correctness issue in the reviewed paths.

## FINAL — Phase A code review converged & APPROVED

Trajectory: **12 -> 6 -> 3 -> 1 -> APPROVED** (6 Codex read-only rounds, thread 019ec249).
All 12 original findings + follow-ups resolved. Verified: **341 unit tests pass, real-server
smoke 17/17** (live bge-m3 + live Qdrant). Bugs caught & fixed this review that mocks/:memory:
had hidden: verify-via-MatchText false-negative (good docs deleted), unverified CLI promotion,
generation-blind probes, result-local vs corpus-local max-gen dedup (search/lexscan/scroll),
promote-count/exception data-loss, lock heartbeat reclaim, multi-doc half-ingest, empty-tag
MatchAny([]) , and more.

---

## Phase E — Run Journal — 2026-06-14

9-angle adversarial review (5 parallel finders + 1 sweep) of Phase E:
`src/aineverforget/run_journal.py`, `scripts/run_journal.py`,
`.claude/skills/ask/SKILL.md`, `.claude/skills/ingest/SKILL.md`, `src/aineverforget/cli.py`.
**15 findings. Status: NEEDS FIXES. Fix handoff → CODEX-FIXLIST-PHASE-E.md.**

**Critical:**
1. Base64 regex `[A-Za-z0-9+/]{40,}` mangles source file paths (`/` in charset).
   Every GATE_FAIL/INDEX_SUSPECT source field silently becomes `[REDACTED].ext`.
2. Shell injection in SKILL.md bash blocks: user question/path/verdict interpolated raw
   into double-quoted shell strings. Old forward-ref markers were inert; live bash is exploitable.

**Error:**
3. Shared `/tmp/ainf_run_id` causes cross-run event misattribution under concurrency.
4. Two-Strike reiterate dispatches emit no DISPATCH_START event; DB underreports vs counter.
5. ASK_START emitted before routing: ask_type always absent from record and recent_runs().
6. Single-backend write failure silent (`len(errors)==2`); JSONL/SQLite diverge invisibly.

**Warning:**
7. Soft-warn check only on reiterate path; first-pass N-sub-query fan-out crosses threshold silently.
8. GATE_FAIL calls in both skills omit `--gate-score`; gate_score column always NULL.
9. Dead allowlist entries: verdict/gate_score in GATE_FAIL, tokens/spend in TELEMETRY (shadowed
   by _TOP_LEVEL_FIELDS; elif branches unreachable).
10. PRAGMA busy_timeout overrides Python timeout= (SQLite C API contract; same value now, trap later).
11. Broad `except Exception` in recent_events()/recent_runs() swallows schema errors silently.
12. executescript DDL on every INSERT; schema write lock per journal write; contends under parallel
    agent fan-out.

**Nice-to-have:**
13. `--json` + ValueError = empty stdout; caller gets JSONDecodeError.
14. `"detail"` key in GATE_FAIL allowlist creates `record["detail"]["detail"]` nesting.
15. ask_id/ingest_id schema columns always NULL (no call site wires them).
