---
name: ask
description: |
  /ask <question> orchestrator. Routes to Recall, Synthesis, or Enumeration;
  decomposes Synthesis into sub-queries with preflight fan-out estimate; dispatches
  knowledge-retriever ×N (one per sub-query); deduplicates chunks and builds the
  coverage ledger; dispatches answer-synthesizer; runs all Quality Gates
  deterministically (including recomputing judgment fields via scripts/gate_synthesis.py
  rather than trusting agent booleans); flushes Cost Telemetry live; emits soft-warn
  journal events; Two-Strike bounds all retry loops. One level of nesting — skill only.
trigger: /ask
metadata:
  version: "1.0"
  model: sonnet
  constraints:
    - "ADR-0001: one-level nesting only"
    - "agents return {output, metadata, self_report} ONLY; skill owns all gates"
    - "answer-synthesizer judgment fields MUST be recomputed via gate_synthesis.py, not trusted from agent"
    - "Enumeration (content) uses lexscan only — never scroll alone for content enumeration"
    - "Enumeration (metadata) uses scroll only — never content-enum from scroll"
    - "coverage_verdict must be 'partial' + non-null qualification when any sub-query is empty"
    - "Two-Strike rule: same failure retried twice → needs_user"
    - "no per-Ask hard cap; safeguards: preflight estimate + soft-warn + live telemetry"
    - "journal events call scripts/run_journal.py; run_id written to $_AINF_RUN_ID_FILE (mktemp) before STEP 0; ASK_START emitted after STEP 0 with ask_type"
    - "opus escalation on the reiterate path is a documented exception to global model rules (PLAN.md risk #6)"
---

## Constants

| Name | Value | Description |
|------|-------|-------------|
| `SOFT_WARN_THRESHOLD` | `12` | dispatches_used at which to emit soft-warn journal event |
| `RETRIEVER_AGENT` | `.Codex/agentspec/agents/dev/knowledge-retriever.md` | Agent file |
| `SYNTHESIZER_AGENT` | `.Codex/agentspec/agents/dev/answer-synthesizer.md` | Agent file |
| `GATE_SYNTHESIS_SCRIPT` | `scripts/gate_synthesis.py` | Deterministic gate for synthesizer output |
| `SYNTH_OUTPUT_TMP` | `/tmp/ainf_synth_output.json` | Temp file: full synthesizer result |
| `SYNTH_CHUNKS_TMP` | `/tmp/ainf_synth_chunks.json` | Temp file: ranked_chunks passed to synthesizer |

## Run-scoped Counters

Initialize at Ask start:

- `dispatches_used` = 0
- `failure_count_retriever` = 0 (reset per sub-query)
- `failure_count_synthesizer` = 0
```bash
_AINF_RUN_ID_FILE=$(mktemp /tmp/ainf_run_id_XXXXXX)
python3 -c "import uuid; print(uuid.uuid4())" > "$_AINF_RUN_ID_FILE"
```

> **SECURITY:** When substituting `<question>` into bash blocks, pass the value via env var or
> temp file — never inline in the command string.

---

## STEP 0 — ROUTE

Classify the question into one of four types. This is the **only step in this skill
where LLM judgment is applied** — routing is orchestration-level classification,
not a quality gate.

**Routing rubric (apply in order; stop at first match):**

| Pattern | Type | CLI path |
|---------|------|----------|
| "which sources", "list all documents", "what tags", "how many sources", "what do I have tagged" | Enumeration (metadata) | `scroll` |
| "how many times", "where do I mention", "find all occurrences", "every time I", "count of" | Enumeration (content) | `lexscan` |
| "across my notes", "summarize all", "compare", "what do all my notes say", "aggregate", multiple aspects or "overview of everything" | Synthesis | hybrid × N |
| everything else | Recall | hybrid × 1 |

**Hard rule:** Never use `scroll` to answer content questions ("how many times did I write X"). Never use `lexscan` to answer metadata questions ("which sources are tagged Y").

Record `ask_type` as one of: `recall` | `synthesis` | `enumeration_metadata` | `enumeration_content`.

```bash
python3 scripts/run_journal.py ASK_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --question "<question>" --ask-type "<ask_type>"
```

---

## STEP 1 — DECOMPOSE (Synthesis only)

Skip this step for Recall and Enumeration.

Decompose the question into N specific, non-overlapping sub-queries. Each sub-query
must be answerable independently from a single retrieval pass.

**Preflight fan-out estimate:**
```
Synthesis plan: N sub-queries → N knowledge-retriever dispatches + 1 answer-synthesizer.
Estimated dispatches: N+1 (plus up to 2×N reiterate slots if retrieval fails).
```

Report this estimate to the user before dispatching any agents.

Record `sub_queries` list (each with a `sub_query_id` = integer index 0..N-1).

---

## STEP 2 — DISPATCH KNOWLEDGE-RETRIEVER(S)

Dispatch one retriever per sub-query (or one for Recall/Enumeration).

For each sub-query (or the single query for Recall/Enumeration):

```
▶ DISPATCH knowledge-retriever
  Agent:         .Codex/agentspec/agents/dev/knowledge-retriever.md
  Input:
    query:          <sub-query text or original question>
    query_type:     <see table below>
    search_mode:    <see table below>
    sub_query_id:   <integer index or null for recall/enumeration>
  Model:         sonnet
  dispatches_used += 1
```

| `ask_type` | `query_type` | `search_mode` |
|------------|-------------|---------------|
| recall | `recall` | `hybrid` |
| synthesis sub-query | `synthesis_sub` | `hybrid` |
| enumeration_metadata | `enumeration_metadata` | `scroll` |
| enumeration_content | `enumeration_content` | `lexscan` |

```bash
python3 scripts/run_journal.py DISPATCH_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent knowledge-retriever --sub-query-id "<sub_query_id>" --dispatches-used <dispatches_used>
```

Check soft-warn after each dispatch:
```
if dispatches_used >= SOFT_WARN_THRESHOLD:
    ```bash
    python3 scripts/run_journal.py SOFT_WARN --run-id "$(cat "$_AINF_RUN_ID_FILE")" --dispatches-used <dispatches_used>
    ```
    Report: "Dispatch count (<dispatches_used>) crossed soft-warn threshold. Synthesis may be expensive."
```

For Synthesis: dispatch all N retrievers in **one message** as parallel Agent calls.
Collect all results before proceeding to STEP 3.

---

## STEP 2a — KNOWLEDGE-RETRIEVER GATE

Apply per retriever result. Reset `failure_count_retriever` for each sub-query.

Save returned result (use `recall` as sub_query_id for Recall/Enumeration):
```bash
python3 -c "import json; print(json.dumps(<full_agent_result>))" > /tmp/ainf_retriever_<sub_query_id>.json
```

**For `recall` and `synthesis_sub` modes — gate is active:**

```bash
python3 -c "
import json, sys
r = json.load(open('/tmp/ainf_retriever_<sub_query_id>.json'))
sr = r['self_report']
ok = (sr['candidate_count'] >= 1
      and (sr['dense_hits'] >= 1 or sr['sparse_hits'] >= 1)
      and sr['citationable_count'] >= 1)
sys.exit(0 if ok else 1)
"
```

**For `lexscan` and `scroll` modes — gate always passes** (empty result is valid):
```bash
# No gate check needed. candidate_count >= 0 is acceptable.
```

**On gate pass (exit 0 or enumeration mode):**
```bash
python3 scripts/run_journal.py GATE_PASS --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent knowledge-retriever --sub-query-id "<sub_query_id>"
```

Collect `output.ranked_chunks` for this sub-query.

**On gate fail (exit 1):**
```bash
python3 scripts/run_journal.py GATE_FAIL --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent knowledge-retriever --sub-query-id "<sub_query_id>" --gate-score 0.0
```

Apply Two-Strike ladder (`failure_count_retriever` is per sub-query):

| `failure_count_retriever` | Action |
|--------------------------|--------|
| 1 (first fail) | Re-dispatch knowledge-retriever with reformulated/broadened query, model=sonnet. `dispatches_used += 1`. Emit DISPATCH_START + check soft-warn (see below). |
| 2 (second fail) | Re-dispatch knowledge-retriever, model=**opus**. `dispatches_used += 1`. Emit DISPATCH_START + check soft-warn. |
| 3 (Two-Strike) | Record sub-query as empty in coverage ledger. Pass empty ranked_chunks for this sub-query to STEP 3. |

After each reiterate dispatch (`dispatches_used += 1` in rows 1 and 2):
```bash
python3 scripts/run_journal.py DISPATCH_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent knowledge-retriever --sub-query-id "<sub_query_id>" --dispatches-used <dispatches_used>
```

Check soft-warn after each reiterate dispatch:
```
if dispatches_used >= SOFT_WARN_THRESHOLD:
    ```bash
    python3 scripts/run_journal.py SOFT_WARN --run-id "$(cat "$_AINF_RUN_ID_FILE")" --dispatches-used <dispatches_used>
    ```
    Report: "Dispatch count (<dispatches_used>) crossed soft-warn threshold. Synthesis may be expensive."
```

---

## STEP 3 — DEDUP + COVERAGE LEDGER (Synthesis only; skip for Recall/Enumeration)

After all N retrievers complete, merge and deduplicate chunks.

**Dedup by `point_id`:** Iterate retriever results in sub-query order. Add each chunk
to `deduped_chunks` only if its `point_id` has not been seen yet. This preserves the
highest-ranked occurrence of each physical chunk.

```python
seen_point_ids = set()
deduped_chunks = []
for retriever_result in retriever_results_in_order:
    for chunk in retriever_result["output"]["ranked_chunks"]:
        if chunk["point_id"] not in seen_point_ids:
            seen_point_ids.add(chunk["point_id"])
            deduped_chunks.append(chunk)
```

**Build coverage ledger** from retriever self_reports:
```python
coverage_ledger = {}
for sub_query_id, result in enumerate(retriever_results):
    sr = result["self_report"]
    coverage_ledger[sub_queries[sub_query_id]] = (
        "answered" if sr["candidate_count"] >= 1 else "empty"
    )
```

This is the **skill's authoritative ledger** — built from observed candidate counts, not from the synthesizer's self-assessment.

For Recall and Enumeration: `deduped_chunks` = the single retriever's `ranked_chunks`. Coverage ledger is not applicable.

---

## STEP 4 — DISPATCH ANSWER-SYNTHESIZER

```
▶ DISPATCH answer-synthesizer
  Agent:    .Codex/agentspec/agents/dev/answer-synthesizer.md
  Input:
    question:          <original question>
    ask_type:          <recall|synthesis|enumeration_metadata|enumeration_content>
    ranked_chunks:     <deduped_chunks list>
    sub_query_ledger:  <coverage_ledger dict or null>
  Model:    sonnet
  dispatches_used += 1
```

```bash
python3 scripts/run_journal.py DISPATCH_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent answer-synthesizer --dispatches-used <dispatches_used>
```

Check soft-warn after dispatch:
```
if dispatches_used >= SOFT_WARN_THRESHOLD:
    ```bash
    python3 scripts/run_journal.py SOFT_WARN --run-id "$(cat "$_AINF_RUN_ID_FILE")" --dispatches-used <dispatches_used>
    ```
    Report: "Dispatch count (<dispatches_used>) crossed soft-warn threshold."
```

Save deduped_chunks to SYNTH_CHUNKS_TMP before dispatch (gate script reads this):
```bash
python3 -c "import json; print(json.dumps(<deduped_chunks>))" > /tmp/ainf_synth_chunks.json
```

On return, save the full synthesizer result to SYNTH_OUTPUT_TMP:
```bash
python3 -c "import json; print(json.dumps(<full_synth_result>))" > /tmp/ainf_synth_output.json
```

---

## STEP 4a — ANSWER-SYNTHESIZER GATE

**Critical:** Do NOT trust the agent's self_report booleans directly. The skill
must recompute `all_cited_ids_in_input` and `groundedness_pass` via the gate
script, and verify `coverage_ledger_consistent` independently.
(See Phase C note in `.Codex/agentspec/shared/self-report-contract.md`.)

Run the gate script:
```bash
python3 scripts/gate_synthesis.py /tmp/ainf_synth_output.json /tmp/ainf_synth_chunks.json
```

The script:
1. Joins `citations[*].chunk_id` against the skill-retained `ranked_chunks[*].point_id` → `all_cited_ids_in_input`
2. Checks lexical overlap between each claim and its cited chunk text → `groundedness_pass`
3. Verifies that any "empty" sub-query in `sub_query_ledger` forces `coverage_verdict="partial"` and non-null `qualification` → `coverage_ledger_consistent`

Outputs JSON diagnostic to stdout. Routes on exit code:

**Exit 0 — all gates pass:**
```bash
python3 scripts/run_journal.py GATE_PASS --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent answer-synthesizer
```

Proceed to STEP 5 with the synthesizer's `output.answer`.

**Exit 1 — one or more gates fail:**
```bash
python3 scripts/run_journal.py GATE_FAIL --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent answer-synthesizer --gate-score 0.0
```

Apply Two-Strike ladder (`failure_count_synthesizer`):

| `failure_count_synthesizer` | Action |
|-----------------------------|--------|
| 1 (first fail) | Re-dispatch answer-synthesizer, same inputs, model=sonnet. `dispatches_used += 1`. Emit DISPATCH_START + check soft-warn (see below). |
| 2 (second fail) | Re-dispatch answer-synthesizer, model=**opus**. `dispatches_used += 1`. Emit DISPATCH_START + check soft-warn. |
| 3 (Two-Strike) | Return: "Can't confirm an answer from your notes for this question. Gate failures: `<gate script output>`." |

After each reiterate dispatch (`dispatches_used += 1` in rows 1 and 2):
```bash
python3 scripts/run_journal.py DISPATCH_START --run-id "$(cat "$_AINF_RUN_ID_FILE")" --agent answer-synthesizer --dispatches-used <dispatches_used>
```

Check soft-warn after each reiterate dispatch:
```
if dispatches_used >= SOFT_WARN_THRESHOLD:
    ```bash
    python3 scripts/run_journal.py SOFT_WARN --run-id "$(cat "$_AINF_RUN_ID_FILE")" --dispatches-used <dispatches_used>
    ```
    Report: "Dispatch count (<dispatches_used>) crossed soft-warn threshold."
```

After reiterate dispatch: re-save SYNTH_OUTPUT_TMP and re-run gate script.

---

## STEP 5 — CLOSE

**On success:** Present the answer with inline citations from `output.answer`.

If `output.coverage_verdict == "partial"`: prepend the qualification:
```
Note: <output.qualification>

<answer text with citations>
```

**Cost summary:**
```
Ask complete.
  Ask type:        <ask_type>
  Dispatches used: <dispatches_used>
```

```bash
python3 scripts/run_journal.py ASK_CLOSE --run-id "$(cat "$_AINF_RUN_ID_FILE")" --ask-type "<ask_type>" --dispatches <dispatches_used> --coverage "<coverage_verdict>"
```

---

## Failure Taxonomy

| Failure | Reiterate Path | Cap |
|---------|----------------|-----|
| knowledge-retriever gate fail | reformulate/broaden → opus → empty (pass to synthesizer) | Two-Strike per sub-query |
| answer-synthesizer gate fail | re-synthesize sonnet → opus → "can't confirm" | Two-Strike |
| synthesizer: all_cited_ids_in_input fail | re-synthesize (same inputs) | Two-Strike |
| synthesizer: groundedness_pass fail | re-synthesize (same inputs) | Two-Strike |
| synthesizer: coverage_ledger_consistent fail | re-synthesize with explicit ledger reminder | Two-Strike |

---

## Two-Strike Rule

- `failure_count_retriever`: per sub-query; reset before each retriever dispatch.
- `failure_count_synthesizer`: per Ask run; incremented each time the synthesizer gate fails.
- At count = 3 (initial + 2 retries): `needs_user` / "can't confirm" response.

---

## Enumeration Notes

**Content enumeration (lexscan):**
- The knowledge-retriever drives `aineverforget lexscan --json <term>` (CLI: positional arg).
- Zero hits is a complete answer (exhaustive scan found nothing), not a gate failure.
- The answer-synthesizer presents count + occurrence list, not a paragraph answer.

**Metadata enumeration (scroll):**
- The knowledge-retriever drives `aineverforget scroll --json [filters]` (CLI: no positional arg; use `--tag`, `--source-type`, `--path`, `--since` as filters).
- Results are active-only, deduped by max active generation — enforced by CLI.
- The answer-synthesizer presents a structured list.

**Never** answer "how many times did I mention X" using `scroll` alone — `scroll` is metadata only. Always use `lexscan` for content enumeration.

---

## Journal Events

All events call `scripts/run_journal.py`. Run ID is generated via `mktemp` at session
start, stored in `$_AINF_RUN_ID_FILE`, and read from that file for all subsequent
events. Token telemetry is not available from skill context; dispatch count is the cost proxy.

| Event | Trigger |
|-------|---------|
| `ASK_START` | Before route step |
| `DISPATCH_START` | Before each agent dispatch |
| `GATE_PASS` | After gate passes |
| `GATE_FAIL` | After gate fails |
| `SOFT_WARN` | dispatches_used ≥ SOFT_WARN_THRESHOLD |
| `ASK_CLOSE` | After answer presented |

---

## Agent Dispatch Summary

| Step | Agent | Input | On Success | On Fail |
|------|-------|-------|------------|---------|
| STEP 2 | knowledge-retriever | query, query_type, search_mode | ranked_chunks collected | reformulate → opus → empty |
| STEP 4 | answer-synthesizer | deduped_chunks, question, ask_type, coverage_ledger | answer presented | re-synthesize → opus → "can't confirm" |
