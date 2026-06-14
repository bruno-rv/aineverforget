# Codex Fix List — Phase E (Run Journal)

Review date: 2026-06-14. 370 tests currently pass.
Fix everything in **P0** and **P1** then re-run the full test suite.
P2 is nice-to-have; leave for later if scope is tight.

---

## P0 — Critical (fix before anything else)

### F1 — Base64 regex mangles source file paths
**File:** `src/aineverforget/run_journal.py`, line 42

`/` is in the character class. Any absolute path with ≥40 contiguous
`[A-Za-z0-9/]` chars is redacted to `[REDACTED].ext`.
Empirically verified: `/Users/bruno/Dev/aineverforget/tests/fixtures/sample.md` → `/[REDACTED].md`.
Destroys the `source` field in DISPATCH_START, GATE_PASS, GATE_FAIL, INDEX_SUSPECT events.

**Fix:** Remove `/` from the base64 pattern:
```python
# before
re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
# after
re.compile(r"\b[A-Za-z0-9+]{40,}={0,2}\b"),
```

Add a regression test:
```python
def test_file_path_not_redacted(journal_dir):
    path = "/Users/bruno/Dev/aineverforget/tests/fixtures/sample.md"
    result = redact(path)
    assert "[REDACTED]" not in result
    assert result == path
```

---

### F2 — Shell injection in SKILL.md bash blocks
**Files:** `.claude/skills/ask/SKILL.md` line 48,
`.claude/skills/ingest/SKILL.md` lines 93, 120, 127, 162, 213

User-controlled values (`<question>`, `<path>`, `<sr.verdict>`) are interpolated
raw into double-quoted shell strings. A question or path containing `"` or `$(...)` can
inject arbitrary commands. Old forward-ref markers were inert prose; these are live bash.

**Fix:** Write user-controlled strings to a temp file, then pass the file path:

For `--question` in ask/SKILL.md (line 48):
```bash
# Before:
python3 scripts/run_journal.py ASK_START --run-id "$(cat /tmp/ainf_run_id)" --question "<question>"

# After:
python3 -c "import sys; open('/tmp/ainf_ask_q.txt','w').write(sys.argv[1])" "<question>"
python3 scripts/run_journal.py ASK_START --run-id "$(cat /tmp/ainf_run_id)" --question "$(cat /tmp/ainf_ask_q.txt)"
```

Simpler alternative — add `--question-env` support to `scripts/run_journal.py`
and emit `AINF_Q="<question>" python3 scripts/run_journal.py ASK_START ...`.

For `--source` and `--verdict` (paths and agent self-report strings):
Same pattern — write to a temp file or pass via env var.

**Minimum viable fix (least invasive):** Quote all user-controlled values through
`shlex.quote()` in the Bash tool call when Claude executes these skill blocks.
Since skills are Claude instructions (not standalone shell scripts), the safest
edit is to add an explicit note in both SKILL.md files:

```
> SECURITY: When substituting <question>, <path>, or <verdict>, Claude MUST pass
> the value via an env variable (AINF_VAL="<value>" python3 ...) or write it to
> /tmp/ainf_arg.txt and read it back, never inline in the command string.
```

---

### F3 — Shared `/tmp/ainf_run_id` causes cross-run event misattribution
**Files:** `.claude/skills/ask/SKILL.md` lines 47-48,
`.claude/skills/ingest/SKILL.md` lines 55-56

Both skills write to the same static path. Concurrent `/ask` or `/ask`+`/ingest` runs
overwrite each other's UUID. All subsequent journal calls in the first run emit under the
second run's `run_id`. `recent_runs()` dispatch counts are corrupted.

**Fix:** Use `mktemp` to generate a unique file per session:
```bash
# Before:
python3 -c "import uuid; print(uuid.uuid4())" > /tmp/ainf_run_id
# ...all subsequent calls: $(cat /tmp/ainf_run_id)

# After:
_AINF_RUN_ID_FILE=$(mktemp /tmp/ainf_run_id_XXXXXX)
python3 -c "import uuid; print(uuid.uuid4())" > "$_AINF_RUN_ID_FILE"
# ...all subsequent calls: $(cat "$_AINF_RUN_ID_FILE")
```

Apply consistently in both SKILL.md files. The `_AINF_RUN_ID_FILE` variable must be
defined at the top of the run and referenced in every subsequent bash block.

---

## P1 — Must fix before Phase F

### F4 — Reiterate dispatches emit no DISPATCH_START event
**Files:** `.claude/skills/ask/SKILL.md` lines 171-172, 285-286,
`.claude/skills/ingest/SKILL.md` lines 134-135

Two-Strike table rows increment `dispatches_used` but include no journal bash block.
The cost proxy (dispatch count) in the journal underreports on any gate failure.
`ASK_CLOSE --dispatches <dispatches_used>` reports N but SQLite counts M < N.

**Fix:** Add DISPATCH_START call immediately after `dispatches_used += 1` in each
reiterate row in both SKILL.md Two-Strike tables. For ask/SKILL.md example:

```bash
# Add after each "Re-dispatch knowledge-retriever ... dispatches_used += 1":
python3 scripts/run_journal.py DISPATCH_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" \
    --agent knowledge-retriever --sub-query-id "<sub_query_id>" \
    --dispatches-used <dispatches_used>
```

Same pattern for note-summarizer reiterate rows in ingest/SKILL.md.

---

### F5 — ASK_START emitted before routing; ask_type always absent
**File:** `.claude/skills/ask/SKILL.md` lines 47-48

`ask_type` is determined in STEP 0. Journal call is before STEP 0.
`_DETAIL_ALLOWLIST["ASK_START"]` includes `"ask_type"` but no call site ever
passes it. `recent_runs()` (which queries ASK_START rows) can never show ask type.

**Fix:** Move the ASK_START journal call to after STEP 0, add `--ask-type`:
```bash
# Move to after STEP 0 classification:
python3 scripts/run_journal.py ASK_START \
    --run-id "$(cat "$_AINF_RUN_ID_FILE")" \
    --question "<question>" \
    --ask-type "<ask_type>"
```

Note: `--ask-type` is already a supported flag in `scripts/run_journal.py`.

---

### F6 — Single-backend write failure is fully silent
**File:** `src/aineverforget/run_journal.py`, line 177

`if len(errors) == 2` only warns on total failure. A SQLite-only or JSONL-only
failure leaves JSONL and SQLite permanently diverged with no signal.

**Fix:**
```python
# Before:
if len(errors) == 2:
    print(
        f"[aineverforget] journal write failed: {'; '.join(errors)}",
        file=sys.stderr,
    )

# After:
if errors:
    print(
        f"[aineverforget] journal write{'d' if len(errors) == 1 else ''} "
        f"{'partial ' if len(errors) == 1 else ''}failure: {'; '.join(errors)}",
        file=sys.stderr,
    )
```

Update `test_both_writes_non_fatal` to verify single-backend failure also warns.

---

### F7 — Soft-warn check missing from first-pass fan-out
**File:** `.claude/skills/ask/SKILL.md` lines 175-181

Soft-warn check is inside the Two-Strike `failure_count_retriever` block and only
fires on reiterate dispatches. A Synthesis with N=13 sub-queries crosses
SOFT_WARN_THRESHOLD=12 during first-pass fan-out with no event emitted.

**Fix:** Move soft-warn check out of the Two-Strike block so it fires after every
`dispatches_used += 1` increment, regardless of dispatch type:

```
After each dispatches_used += 1 (in STEP 2, STEP 4, AND Two-Strike reiterate):
  if dispatches_used >= SOFT_WARN_THRESHOLD:
    python3 scripts/run_journal.py SOFT_WARN --run-id "$(cat "$_AINF_RUN_ID_FILE")" \
        --dispatches-used <dispatches_used>
    Report: "Dispatch count (<dispatches_used>) crossed soft-warn threshold."
```

Same change applies to ingest/SKILL.md (already only fires on reiterate there too).

---

### F8 — GATE_FAIL calls omit `--gate-score` in both skills
**Files:** `.claude/skills/ingest/SKILL.md` line 127,
`.claude/skills/ask/SKILL.md` line 164

`gate_score` is a first-class `REAL` column in the SQLite schema. No GATE_FAIL
call passes it; all rows have `gate_score = NULL`. Aggregate diagnostic queries
silently return NULL.

**Fix:** Add `--gate-score` to every GATE_FAIL bash block where a score is available
from the agent's `self_report`:

```bash
# ingest/SKILL.md note-summarizer GATE_FAIL (after line 127):
python3 scripts/run_journal.py GATE_FAIL \
    --run-id "$(cat "$_AINF_RUN_ID_FILE")" \
    --agent note-summarizer \
    --source "<path>" \
    --verdict "<sr.verdict>" \
    --gate-score "<sr.gate_score_or_0.0>"

# ask/SKILL.md retriever GATE_FAIL (line 164 area):
python3 scripts/run_journal.py GATE_FAIL \
    --run-id "$(cat "$_AINF_RUN_ID_FILE")" \
    --agent knowledge-retriever \
    --sub-query-id "<sub_query_id>" \
    --gate-score "<computed_score_or_0.0>"
```

If no numeric score is available, pass `--gate-score 0.0` rather than omitting.

---

### F9 — Dead allowlist entries mislead readers
**File:** `src/aineverforget/run_journal.py`, lines 24 and 30

`verdict`, `gate_score` are in both `_TOP_LEVEL_FIELDS` and `_DETAIL_ALLOWLIST["GATE_FAIL"]`.
`tokens`, `spend` are in both `_TOP_LEVEL_FIELDS` and `_DETAIL_ALLOWLIST["TELEMETRY"]`.
The `elif key in allowed_detail` branch is unreachable for these fields; they always
go top-level. A consumer reading `record["detail"]["gate_score"]` always gets `KeyError`.

Also remove `"detail"` from `_DETAIL_ALLOWLIST["GATE_FAIL"]` — this creates the
confusing `record["detail"]["detail"]` nesting (see F14).

**Fix:**
```python
# Before:
"GATE_FAIL": frozenset({"source", "document_id", "sub_query_id", "verdict", "gate_score", "detail"}),
"TELEMETRY": frozenset({"tokens", "spend"}),

# After:
"GATE_FAIL": frozenset({"source", "document_id", "sub_query_id"}),
"TELEMETRY": frozenset(),  # all TELEMETRY fields are top-level; keep key for schema completeness
```

---

### F10 — PRAGMA busy_timeout overrides Python timeout= silently
**File:** `src/aineverforget/run_journal.py`, lines 112 and 115

`sqlite3.connect(timeout=5.0)` installs Python-level `sqlite3_busy_handler()`.
`PRAGMA busy_timeout=5000` calls `sqlite3_busy_timeout()` which, per the SQLite C API
spec, clears any handler set by `sqlite3_busy_handler()`. Python timeout= is dead
after the PRAGMA. Both are 5s today so no functional difference, but changing one
constant without knowing about the other silently breaks the intent.

**Fix:** Remove the redundant Python `timeout=` argument; rely solely on PRAGMA:
```python
# Before:
con = sqlite3.connect(str(db_path), timeout=5.0)

# After:
con = sqlite3.connect(str(db_path))
```

---

### F11 — Broad except swallows real errors in recent_events/recent_runs
**File:** `src/aineverforget/run_journal.py`, lines 200-201 and 246-247

A schema bug, column rename, or permission error returns `[]` indistinguishable
from "journal is empty". `aineverforget status` silently shows no journal data
on any read failure.

**Fix:**
```python
# Before:
except Exception:
    return []

# After (both recent_events and recent_runs):
except Exception as exc:
    print(f"[aineverforget] journal read failed: {exc}", file=sys.stderr)
    return []
```

Also apply the same pattern to the `except Exception` in `cmd_status` in `cli.py`
(imports `recent_events`/`recent_runs`):
```python
except Exception as exc:
    print(f"[aineverforget] journal unavailable: {exc}", file=sys.stderr)
    journal_events = []
    journal_runs = []
```

---

### F12 — executescript DDL on every INSERT acquires schema write lock per write
**File:** `src/aineverforget/run_journal.py`, line 116

`con.executescript(_CREATE_DDL)` runs inside `_db_insert` on every call.
Under parallel agent fan-out (e.g. 10 parallel DISPATCH_START events), all 10
connections compete for the schema write lock before their INSERTs can proceed.

**Fix:** Add a module-level sentinel to run DDL only once per db_path per process:

```python
_schema_initialized: set[str] = set()

def _ensure_schema(db_path: Path, con: sqlite3.Connection) -> None:
    key = str(db_path)
    if key not in _schema_initialized:
        con.executescript(_CREATE_DDL)
        _schema_initialized.add(key)
```

In `_db_insert`, replace:
```python
con.executescript(_CREATE_DDL)
```
with:
```python
_ensure_schema(db_path, con)
```

Note: `_schema_initialized` is module-level so tests that use `tmp_path` (different
db_path per test) will still trigger DDL creation for each unique path. No test
isolation issue.

---

## P2 — Nice to Have

### F13 — `--json` + ValueError = empty stdout (JSONDecodeError for caller)
**File:** `scripts/run_journal.py`, lines 132-135

```python
# Before:
except ValueError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(0)

# After:
except ValueError as exc:
    if args.json_out:
        print(json.dumps({"error": "invalid_event", "message": str(exc)},
                         ensure_ascii=False))
    else:
        print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(0)
```

---

### F14 — `"detail"` key in GATE_FAIL allowlist (covered by F9 — remove it there)

Already resolved by F9. No separate action needed.

---

### F15 — ask_id/ingest_id columns always NULL
**File:** `src/aineverforget/run_journal.py` DDL and `scripts/run_journal.py`

No call site in either SKILL.md ever passes `--ask-id` or `--ingest-id`.
Option A (simpler): Remove `ask_id`/`ingest_id` columns from the DDL and the
`_TOP_LEVEL_FIELDS`/`_DEST_TO_FIELD` maps. Both skills use `run_id` as the
universal run key.
Option B: Wire `--ask-id <uuid>` in ask/SKILL.md alongside `--run-id` to enable
ask-vs-ingest discrimination in queries.

**Recommendation:** Option A unless cross-run-type queries are a planned use case.

---

## Test Checklist

After all P0+P1 fixes:

- [ ] `pytest tests/` — all 370+ tests pass
- [ ] New test: source path ≥40 chars NOT redacted (F1)
- [ ] New test: single-backend failure warns on stderr (F6)
- [ ] New test: `recent_events()` logs to stderr on DB error instead of silent `[]` (F11)
- [ ] Existing `test_both_writes_non_fatal` updated to test single-failure warning too (F6)
- [ ] No new tests broken by sentinel optimization (F12)
