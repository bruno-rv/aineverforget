Phase E Review Sync — aineverforget
Date: 2026-06-14
Participants: Bruno, Claude

Quick sync to walk through the 9-angle adversarial review results for Phase E (Run Journal). 15 findings came out, 3 critical, 12 P1 or lower.

Bruno opened by asking whether the base64 redaction bug was actually a production risk or theoretical. Claude walked through it: the base64 pattern included `/` in the character class, which means any absolute path longer than 40 chars gets mangled to `[REDACTED].ext`. Source fields in GATE_FAIL, DISPATCH_START, and INDEX_SUSPECT events are all destroyed silently. Since those fields are the primary human-readable audit trail for failed ingests, this is a real data loss issue, not theoretical. Fix: remove `/` from the charset.

Second critical issue: shell injection in the SKILL.md bash blocks. Old forward-ref markers like `<question>` were inert prose. Once the skills went live, those markers became actual bash substitutions. A question or path containing `"` or `$(...)` will execute arbitrary commands in the Claude Code context. Fix applied: added SECURITY note requiring env-var or temp-file indirection for all user-controlled values. Not a perfect fix since it's a prose instruction rather than code, but acceptable for Claude-native execution context.

Third critical: the `/tmp/ainf_run_id` shared path. Both `/ingest` and `/ask` skills write the run UUID to the same static file. Concurrent runs overwrite each other. Journal events from the first run start being attributed to the second run's ID. Fix: mktemp per session, store in shell variable `_AINF_RUN_ID_FILE`.

Bruno asked whether the PRAGMA busy_timeout issue was worth blocking on. Decision: no. Both PRAGMA and Python timeout= are 5 seconds today, so no functional difference in behavior. The risk is future drift if someone changes one without knowing about the other. Fix applied: remove redundant Python timeout= from _db_insert; rely solely on PRAGMA.

The ASK_START timing bug was non-obvious: journal event was emitted before routing, so `ask_type` was never recorded. `recent_runs()` therefore can never show ask type in its output. Fixed by moving ASK_START to after STEP 0 classification and adding `--ask-type` to the call.

P2 deferred items: F13 (--json + ValueError returns empty stdout, not JSON error) and F15 (ask_id/ingest_id columns always NULL). Bruno decided to defer both. F15 likely resolved by removing the columns entirely since neither skill uses them.

Wrap-up: all P0+P1 fixes committed as e580de1. 374 tests pass. Phase F (live dry-run on real Qdrant + bge-m3) is next.
