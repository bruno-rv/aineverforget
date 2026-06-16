---
name: ingest
description: |
  /ingest <paths> orchestrator. Classifies each source (raw note/transcript vs
  pre-structured), dispatches note-summarizer (if raw) then knowledge-indexer per
  source, runs Quality Gates deterministically, enforces the ingest lock via CLI
  exit codes, journals events via scripts/run_journal.py, and applies the Two-Strike
  rule. One level of nesting — skill only, per ADR-0001.
trigger: /ingest
metadata:
  version: "1.0"
  model: sonnet
  constraints:
    - "ADR-0001: one-level nesting only — skill dispatches agents, agents never dispatch"
    - "agents return {output, metadata, self_report} ONLY; skill owns all gates and routing"
    - "knowledge-indexer gate fail on index_suspect/error → no retry; report to user"
    - "Two-Strike rule: same failure retried twice → needs_user"
    - "journal events call scripts/run_journal.py; run_id written to $_AINF_RUN_ID_FILE (mktemp) at RUN_START"
    - "note-summarizer faithfulness is a dev-time Eval (Phase D), NOT a runtime gate"
---

## Constants

| Name | Value | Description |
|------|-------|-------------|
| `SUMMARY_TEMPLATE_SECTIONS` | `["## TL;DR","## Key Concepts","## Key Decisions","## Action Items"]` | All 4 must be present for pre-structured detection |
| `SOFT_WARN_THRESHOLD` | `8` | dispatches_used at which to emit soft-warn journal event |
| `NOTE_SUMMARIZER_AGENT` | `.claude/agentspec/agents/dev/note-summarizer.md` | Agent file |
| `KNOWLEDGE_INDEXER_AGENT` | `.claude/agentspec/agents/dev/knowledge-indexer.md` | Agent file |

## Run-scoped Counters

Initialize before processing the first source:

- `dispatches_used` = 0
- `sources_indexed` = 0
- `sources_failed` = 0

---

## STEP 0 — VALIDATE PATHS

For **each** path in the user-supplied `<paths>`:

```bash
ls -la "<path>"
```

- **File exists:** continue to classification.
- **File absent:** report `"Path not found: <path> — skipping."`, increment `sources_failed`, skip remaining steps for this path.

If **all** supplied paths are absent: report summary and stop.

```bash
_AINF_RUN_ID_FILE=$(mktemp /tmp/ainf_run_id_XXXXXX)
python3 -c "import uuid; print(uuid.uuid4())" > "$_AINF_RUN_ID_FILE"
python3 scripts/run_journal.py RUN_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --paths <count-of-valid-paths>
```

> **SECURITY:** When substituting `<path>` or `<verdict>` into bash blocks, pass values via env var or
> temp file — never inline in the command string.

---

## STEP 1 — CLASSIFY EACH SOURCE

For each validated path, determine dispatch mode before invoking any agent.

**Pre-structured → skip note-summarizer:**
- Path ends in `.pdf` → **direct** (knowledge-indexer only).
- Path ends in `.docx` → **direct** (knowledge-indexer only; the loader reconstructs markdown from the Word document).
- Path basename is `summary.md` → **direct**.
- Path ends in `.md` or `.txt` and contains both `## TL;DR` and `## Key Concepts` → **direct**.
- Path ends in `.md` or `.txt` → run:
  ```bash
  grep -cE "^## " "<path>"
  ```
  Count ≥ 3 → **direct**. Count < 3 → **raw note**.
- Path ends in any other extension → **direct**. The CLI byte-sniffs it: text-like content is indexed as markdown (flagged `low_confidence`); binary content is rejected (`IngestOutcome.error`). Pass `--source-type` to override.

**Raw note/transcript → needs summarization:**
- `.md` / `.txt` file where the section count < 3 and it is not a generated summary → dispatch note-summarizer first.

Record classification per path. Then process each path through its dispatch sequence (STEP 2–3).

---

## STEP 2 — NOTE-SUMMARIZER (raw notes only)

For each **raw** source path:

```
▶ DISPATCH note-summarizer
  Agent:    .claude/agentspec/agents/dev/note-summarizer.md
  Input:    source_path=<path>
  Model:    sonnet (first attempt), opus (second reiterate)
  dispatches_used += 1
```

```bash
python3 scripts/run_journal.py DISPATCH_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent note-summarizer --source "<path>" --dispatches-used <dispatches_used>
```

Check soft-warn after each dispatch:
```
if dispatches_used >= SOFT_WARN_THRESHOLD:
    ```bash
    python3 scripts/run_journal.py SOFT_WARN --run-id "$(cat "$_AINF_RUN_ID_FILE")" --dispatches-used <dispatches_used>
    ```
    Report: "Dispatch count (<dispatches_used>) crossed soft-warn threshold (SOFT_WARN_THRESHOLD)."
```

On return, capture `{output, metadata, self_report}`.

### STEP 2a — note-summarizer Gate

Save returned `self_report` JSON:
```bash
python3 -c "import json; print(json.dumps(<self_report>))" > /tmp/ainf_note_sum_sr.json
```

Run gate:
```bash
python3 -c "
import json, sys
sr = json.load(open('/tmp/ainf_note_sum_sr.json'))
ok = (sr.get('structure_present') is True
      and sr.get('missing_sections', ['x']) == []
      and sr.get('compression_in_bounds') is True
      and sr.get('missing_entities', ['x']) == [])
sys.exit(0 if ok else 1)
"
```

**Exit 0 — gate pass:**
```bash
python3 scripts/run_journal.py GATE_PASS --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent note-summarizer --source "<path>"
```

Proceed to STEP 3 with `output.summary_path`.

**Exit 1 — gate fail:**
```bash
python3 scripts/run_journal.py GATE_FAIL --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent note-summarizer --source "<path>" --verdict "<sr.verdict>" --gate-score 0.0
```

Apply Two-Strike ladder (track `failure_count_note_summarizer` — reset between sources):

| `failure_count_note_summarizer` | Action |
|---------------------------------|--------|
| 1 (first fail) | Re-dispatch note-summarizer, same source, model=sonnet. `dispatches_used += 1`. Emit DISPATCH_START + check soft-warn (see below). |
| 2 (second fail) | Re-dispatch note-summarizer, same source, model=**opus**. `dispatches_used += 1`. Emit DISPATCH_START + check soft-warn. |
| 3 (Two-Strike) | `needs_user` — report: "note-summarizer failed 3 times for `<path>`. Manual review required. Missing: `<missing_sections>` / entities: `<missing_entities>`." Increment `sources_failed`. Skip to next source. |

After each reiterate dispatch (`dispatches_used += 1` in rows 1 and 2):
```bash
python3 scripts/run_journal.py DISPATCH_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent note-summarizer --source "<path>" --dispatches-used <dispatches_used>
```

Check soft-warn after each dispatch (including reiterates):
```
if dispatches_used >= SOFT_WARN_THRESHOLD:
    ```bash
    python3 scripts/run_journal.py SOFT_WARN --run-id "$(cat "$_AINF_RUN_ID_FILE")" --dispatches-used <dispatches_used>
    ```
    Report: "Dispatch count (<dispatches_used>) crossed soft-warn threshold (SOFT_WARN_THRESHOLD)."
```

---

## STEP 3 — KNOWLEDGE-INDEXER

For each source (using `summary_path` from STEP 2 if raw, or the original path if pre-structured):

```
▶ DISPATCH knowledge-indexer
  Agent:    .claude/agentspec/agents/dev/knowledge-indexer.md
  Input:    source_path=<summary_path or original_path>, tags=<user-provided tags if any>
  Model:    sonnet
  dispatches_used += 1
```

```bash
python3 scripts/run_journal.py DISPATCH_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent knowledge-indexer --source "<path>" --dispatches-used <dispatches_used>
```

Check soft-warn after dispatch:
```
if dispatches_used >= SOFT_WARN_THRESHOLD:
    ```bash
    python3 scripts/run_journal.py SOFT_WARN --run-id "$(cat "$_AINF_RUN_ID_FILE")" --dispatches-used <dispatches_used>
    ```
    Report: "Dispatch count (<dispatches_used>) crossed soft-warn threshold (SOFT_WARN_THRESHOLD)."
```

On return, capture `{output, metadata, self_report}`.

### STEP 3a — knowledge-indexer Gate

Route on `self_report.verdict`:

| `verdict` | Action |
|-----------|--------|
| `"indexed"` | Proceed to probe gate (STEP 3b). |
| `"no_op"` | Content unchanged; already indexed. `sources_indexed += 1`. Report: "No-op: `<path>` already indexed (identical content)." |
| `"lock_overlap"` | Report: "Ingest lock held — another ingest is running. Wait and retry this source manually." `sources_failed += 1`. |
| `"index_suspect"` | → STEP 3c (INDEX_SUSPECT path). |
| `"error"` | → STEP 3c (error path). |
| `"skipped"` | Report loader reason (e.g., encrypted PDF). `sources_failed += 1`. |

### STEP 3b — probe gate (verdict = "indexed")

Save the full returned JSON:
```bash
python3 -c "import json; print(json.dumps(<full_agent_result>))" > /tmp/ainf_indexer_result.json
```

Run gate:
```bash
python3 -c "
import json, sys
r = json.load(open('/tmp/ainf_indexer_result.json'))
out = r['output']; sr = r['self_report']
ok = (out['ingest_state'] == 'active'
      and sr['probe_verdicts']['topical'] == 'pass'
      and sr['probe_verdicts']['specific'] == 'pass'
      and (sr['probe_verdicts']['negative'] == 'pass'
           or sr.get('negative_deferred') is True))
sys.exit(0 if ok else 1)
"
```

**Exit 0:** `sources_indexed += 1`. Report: "Indexed: `<path>` → document_id=`<output.document_id>`, chunks=`<output.chunk_count>`."

```bash
python3 scripts/run_journal.py GATE_PASS --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent knowledge-indexer --document-id "<output.document_id>"
```

**Exit 1:** Treat as `index_suspect` — proceed to STEP 3c.

### STEP 3c — INDEX_SUSPECT / error (no retry)

```bash
python3 scripts/run_journal.py INDEX_SUSPECT --run-id "$(cat "$_AINF_RUN_ID_FILE")" --source "<path>" --verdict "<sr.verdict>"
```

Report to user:
```
INDEX_SUSPECT: <path>
  verdict:      <sr.verdict>
  document_id:  <output.document_id or "unknown">
  probe fail:   <first failing probe or "see verdict">

No retry. Run `aineverforget gc` to clean up any pending chunks,
then re-check the source or re-ingest after fixing the issue.
```

`sources_failed += 1`.

**Do NOT retry knowledge-indexer.** The CLI's verify→promote cycle is the authority; a failed verify means the content did not pass indexing checks, and retrying without changing the source produces the same result.

---

## STEP 4 — CLOSE

After all sources processed:

```
Ingest complete.
  Indexed:         <sources_indexed>
  Failed/skipped:  <sources_failed>
  Dispatches used: <dispatches_used>
```

If `sources_failed > 0`: list each failed source and its failure reason.

```bash
python3 scripts/run_journal.py RUN_CLOSE --run-id "$(cat "$_AINF_RUN_ID_FILE")" --indexed <sources_indexed> --failed <sources_failed> --dispatches <dispatches_used>
```

---

## Failure Taxonomy

| Failure | Reiterate Path | Cap |
|---------|----------------|-----|
| note-summarizer gate fail | retry sonnet → opus → needs_user | Two-Strike (3 total) |
| knowledge-indexer: index_suspect | journal + report, **no retry** | — |
| knowledge-indexer: error | journal + report, **no retry** | — |
| knowledge-indexer: lock_overlap | report to user, **no retry** | — |
| knowledge-indexer: skipped | report loader reason, no retry | — |
| path not found | report + skip | — |
| probe gate fail (exit 1) | treated as index_suspect | — |

---

## Two-Strike Rule

Track per-agent, per-source. Reset `failure_count_<agent>` when moving to the next source.

- `failure_count_note_summarizer`: incremented each time the note-summarizer gate fails.
- When count reaches 3 (initial fail + 2 retries) → `needs_user`. Skip to next source.

Knowledge-indexer has **no Two-Strike**: its failures are non-retryable by design.

---

## Journal Events

All events call `scripts/run_journal.py`. Run ID is generated once at `RUN_START`
and read from `$_AINF_RUN_ID_FILE` for all subsequent events in the same run.

| Event | Trigger |
|-------|---------|
| `RUN_START` | After path validation, before first source processing |
| `DISPATCH_START` | Before each agent dispatch |
| `GATE_PASS` | After gate passes |
| `GATE_FAIL` | After gate fails |
| `INDEX_SUSPECT` | knowledge-indexer index_suspect or probe gate fail |
| `SOFT_WARN` | dispatches_used ≥ SOFT_WARN_THRESHOLD |
| `RUN_CLOSE` | After all sources processed |

---

## Agent Dispatch Summary

| Step | Agent | Input | On Success | On Fail |
|------|-------|-------|------------|---------|
| STEP 2 | note-summarizer | raw source path | `summary_path` → STEP 3 | retry → opus → needs_user (Two-Strike) |
| STEP 3 | knowledge-indexer | summary_path or direct path | sources_indexed++ | see failure taxonomy |
