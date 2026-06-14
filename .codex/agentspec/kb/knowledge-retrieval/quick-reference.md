# Quick Reference — knowledge-retrieval

## CLI Commands per Mode

### hybrid (recall / synthesis_sub)

```bash
aineverforget search --json "<query>"
```

| Flag | Required | Notes |
|------|----------|-------|
| `--json` | Yes | Machine-readable output; must be present or output is prose |
| `"<query>"` | Yes | Positional; quoted string; no flag prefix |

No other flags. Top-k and collection are configured at the aineverforget installation level.

### lexscan (enumeration_content)

```bash
aineverforget lexscan --json "<term>"
```

| Flag | Required | Notes |
|------|----------|-------|
| `--json` | Yes | Machine-readable output |
| `"<term>"` | Yes | Single term or short phrase; no wildcards |

Exhaustive sweep — returns all chunks containing the term, not ranked by relevance.

### scroll (enumeration_metadata)

```bash
aineverforget scroll --json [--tag <tag>] [--since <iso-date>]
```

| Flag | Required | Notes |
|------|----------|-------|
| `--json` | Yes | Machine-readable output |
| `--tag <tag>` | No | Filter to documents with this tag |
| `--since <iso-date>` | No | Filter to documents ingested after this date (ISO 8601) |

Without `--tag` or `--since`, returns metadata for all documents in the corpus.

---

## Self-Report Count Sources

Counts in `self_report` must come from CLI JSON output verbatim. Never compute them from the `ranked_chunks` list length.

| self_report field | CLI source (search) | CLI source (lexscan) | CLI source (scroll) |
|-------------------|--------------------|--------------------|-------------------|
| `candidate_count` | `candidate_count` field | `chunk_count` field | `document_count` field |
| `dense_hits` | `dense_hits` field | **-1** (literal) | **-1** (literal) |
| `sparse_hits` | `sparse_hits` field | **-1** (literal) | **-1** (literal) |
| `citationable_count` | `citationable_count` field | count of docs where `document_path` non-empty | equals `candidate_count` (scroll always has paths) |

---

## Gate Evaluation Table

| Mode | Gate condition | gate_pass when | gate_pass = false means |
|------|---------------|----------------|------------------------|
| hybrid (recall) | `candidate_count >= 1 AND (dense_hits >= 1 OR sparse_hits >= 1) AND citationable_count >= 1` | All three clauses true | Zero retrievable candidates — try one reformulation |
| hybrid (synthesis_sub) | Same as recall | Same | Same — skill decides whether to synthesize partial |
| lexscan | Not evaluated | Always `true` | N/A — empty is valid |
| scroll | Not evaluated | Always `true` | N/A — empty is valid |

### Gate Clause Rationale

- **candidate_count >= 1:** At least one candidate returned post-fusion.
- **dense_hits >= 1 OR sparse_hits >= 1:** At least one modality contributed results before fusion. Prevents a case where the CLI returns a candidate count > 0 from a fusion artifact but no real hits from either retriever.
- **citationable_count >= 1:** At least one result has a `document_path` — can be cited by the synthesizer.

---

## ranked_chunks Population per Mode

| Mode | ranked_chunks content | score | text |
|------|----------------------|-------|------|
| hybrid | One entry per candidate from `candidates` array | From `score` field | From `text` field |
| lexscan | One entry per document from `documents` array | null | From first matching chunk if available; null otherwise |
| scroll | Empty list `[]` | null | null |

---

## Verdict Values

| verdict | Meaning |
|---------|---------|
| `pass` | Gate passed (hybrid) or mode is lexscan/scroll |
| `empty` | Gate failed after one reformulation (hybrid only) |
| `cli_error` | CLI exited non-zero |
| `parse_error` | CLI output not parseable as expected shape |
