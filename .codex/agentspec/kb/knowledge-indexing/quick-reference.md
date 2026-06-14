# Knowledge Indexing Quick Reference

> Fast lookup for driving `aineverforget ingest` and assembling self_report.

---

## CLI Flag Reference

| Flag | Form | Notes |
|------|------|-------|
| `--json` | `aineverforget ingest --json <path>` | Output JSON to stdout. Required. |
| `--tag` | `--tag <value>` (repeatable) | Singular flag, repeatable. NOT `--tags`. One `--tag` per tag value. |
| `--source-id` | `--source-id <id>` | Optional. Defaults to resolved file path. |
| `--producer` | `--producer <name>` | Optional. Defaults to `"user"`. |
| `--no-verify` | `--no-verify` | NEVER USE. The knowledge-indexer must always use verification. |

```bash
# Single-tag ingest
aineverforget ingest --json /path/to/file.md --tag meeting

# Multi-tag ingest (repeat --tag, not comma-separated)
aineverforget ingest --json /path/to/file.md --tag meeting --tag q2 --tag action-item

# Verify a document after success
aineverforget verify --json <document_id>
```

---

## Ingest JSON Output Fields (per result entry)

The `--json` output wraps results in an envelope. Extract the first element of `results[]`.

| Field | Type | Notes |
|-------|------|-------|
| `outcome` | string | `success`, `no_op`, `index_suspect`, `error`, `skipped` |
| `document_id` | string or null | UUID. Null on early load failure. |
| `generation` | int or null | The G+1 generation upserted. Null for no_op/skipped/error. |
| `chunk_count` | int | Chunks upserted. 0 for no_op/skipped/error/index_suspect. |
| `document_ids` | list | All document_ids for multi-doc sources. Single-element for single-doc. |
| `generations` | list | Mirrors document_ids. |
| `loader_verdict` | string or null | `"encrypted"`, `"scanned"`, null for normal. |
| `detail` | string | Human-readable explanation; useful for index_suspect and error. |

---

## Verify JSON Output Fields

```bash
aineverforget verify --json <document_id>
```

| Field | Type | Notes |
|-------|------|-------|
| `document_id` | string | The document verified. |
| `generation` | int | The max active generation verified. |
| `passed` | bool | True if all non-deferred probes passed. |
| `negative_deferred` | bool | True if negative probe was deferred (cold_start). |
| `index_suspect` | bool | True if `passed == false`. |
| `probe_results[]` | list | Per-probe entries (see below). |

**`probe_results[]` entry fields:**

| Field | Notes |
|-------|-------|
| `probe_type` | `"topical"`, `"specific"`, `"negative"` |
| `query` | The query that was issued. |
| `passed` | bool |
| `deferred` | bool — true only for negative probe in cold_start |
| `detail` | Human-readable verdict with PASS/FAIL/DEFERRED and reason |

---

## Outcome Decision Matrix

| CLI outcome | `aineverforget verify` call needed? | `ingest_state` | `verdict` in self_report |
|-------------|-------------------------------------|----------------|--------------------------|
| `success` | YES — call `verify --json <document_id>` | `"active"` | `"indexed"` (if probes pass) or `"index_suspect"` (if probes fail post-promote) |
| `no_op` | NO | `"active"` (unchanged) | `"no_op"` |
| `index_suspect` | NO | n/a (pending batch deleted) | `"index_suspect"` |
| `error` | NO | n/a | `"error"` |
| `skipped` | NO | n/a | `"skipped"` |
| Exit code 3 | NO | n/a | `"lock_overlap"` |

---

## Required Processing Sequence

| Step | Action | Pass criteria |
|------|--------|---------------|
| 1 | Confirm source path exists | File readable on disk |
| 2 | `aineverforget ingest --json <path> [--tag ...]` | Exit code 0 or 4 (exit 4 = index_suspect; still parse JSON) |
| 3 | Parse JSON; check outcome field | outcome in known set |
| 4 | If outcome != `success`: assemble self_report, return | No verify needed |
| 5 | If outcome == `success`: `aineverforget verify --json <document_id>` | Parse probe_results |
| 6 | Map probe_results → probe_verdicts | Each probe_type → "pass"/"fail" |
| 7 | Set cold_start = negative_deferred from verify output | |
| 8 | Set verdict = "indexed" | All required probes pass or deferred |
| 9 | Assemble {output, metadata, self_report} and return | Schema matches frozen contract |

---

## Exit Code Reference

| Exit code | Meaning | Action |
|-----------|---------|--------|
| 0 | success or no_op | Parse JSON; check outcome |
| 1 | unexpected error | Parse JSON error field; return verdict "error" |
| 3 | lock overlap (concurrent ingest) | Return verdict "lock_overlap"; do not retry |
| 4 | index_suspect | Parse JSON; return verdict "index_suspect" |

---

## Common Pitfalls

| Don't | Do |
|-------|----|
| Use `--tags` (plural) | Use `--tag` once per tag value |
| Use `--no-verify` | Always let the CLI run verification |
| Call verify when outcome is not `success` | Skip verify for no_op / index_suspect / error / skipped |
| Store parsed JSON object as `cli_result` | Store the raw JSON string as `cli_result` |
| Set `ingest_state` based on assumptions | Derive it from CLI outcome (success → "active") |
| Retry on index_suspect | Return as-is; skill handles recovery |
| Use `--document-id` flag for verify | `verify` takes `document_id` as positional arg: `aineverforget verify --json <document_id>` |

---

## Related Documentation

| Topic | Path |
|-------|------|
| Ingest contract | `concepts/ingest-contract.md` |
| Probe calibration | `patterns/probe-calibration.md` |
| Troubleshooting | `reference/troubleshooting.md` |
| Frozen self-report schema | `.claude/agentspec/shared/self-report-contract.md` |
| CLI source | `src/aineverforget/cli.py` |
