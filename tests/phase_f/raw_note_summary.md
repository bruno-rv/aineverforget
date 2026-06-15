## TL;DR

A review sync on 2026-06-14 between Bruno and Claude covered 15 findings from the Phase E (Run Journal) adversarial review, including 3 critical issues that were diagnosed and fixed. All P0+P1 fixes were committed as e580de1, with 374 tests passing, and Phase F (live dry-run on real Qdrant + bge-m3) identified as the next step.

## Key Concepts

- **base64 redaction bug**: The base64 pattern included `/` in its character class, causing absolute paths longer than 40 chars to be mangled to `[REDACTED].ext`, silently destroying `GATE_FAIL`, `DISPATCH_START`, and `INDEX_SUSPECT` event source fields. Fix: remove `/` from the charset.
- **shell injection**: Old `<question>` forward-ref markers in SKILL.md bash blocks became live bash substitutions once skills went live. Values containing `"` or `$(...)` execute arbitrary commands. Fix: env-var or temp-file indirection for all user-controlled values.
- **/tmp/ainf_run_id shared path**: Both `/ingest` and `/ask` skills wrote run UUIDs to the same static file, causing concurrent runs to overwrite each other and misattribute journal events. Fix: `mktemp` per session, stored in `_AINF_RUN_ID_FILE`.
- **PRAGMA busy_timeout**: Redundant Python `timeout=` on `_db_insert` was removed; PRAGMA alone governs the 5-second timeout to prevent future drift.
- **ASK_START timing bug**: The `ASK_START` journal event was emitted before routing, so `ask_type` was never recorded in `recent_runs()`. Fix: move event emission to after STEP 0 classification and add `--ask-type`.

## Key Decisions

- PRAGMA busy_timeout issue not a blocker — no functional difference today; Python `timeout=` removed from `_db_insert` to rely solely on PRAGMA.
- F13 (`--json` + ValueError returning empty stdout) deferred.
- F15 (`ask_id`/`ingest_id` columns always NULL) deferred; likely resolved by removing the columns entirely since neither skill uses them.

## Action Items

1. Execute Phase F: live dry-run on real Qdrant + bge-m3.
