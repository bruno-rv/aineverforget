# Quick Reference — note-summarization

Decision matrix for `note-summarizer`. Use this file to resolve ambiguous cases quickly without re-reading the full template or contract.

---

## Section Decision Matrix

| Source signal | Include section? | Notes |
|---------------|-----------------|-------|
| Always | `## TL;DR` | 2–3 sentences; always required |
| Always | `## Key Concepts` | Named concepts, terms, frameworks; always required |
| Explicit or implicit decisions ("we decided", "agreed to use", "chosen approach") | `## Key Decisions` | Conditional; omit if no decisions present |
| Action items ("TODO", "action:", "will do by", "owner:", dated tasks) | `## Action Items` | Conditional; numbered list; omit if none |
| Source references prior context or assumes background knowledge | `## Context & Background` | Optional; only if content is non-trivial |
| Unresolved questions raised explicitly or implicitly | `## Open Questions` | Optional; only if genuinely open |

**Rule:** When in doubt about optional/conditional sections, omit. A missing section is less harmful than an empty-stub section (which fails the structure gate).

---

## Routing Heuristic: Raw Note vs. Pre-Structured Doc

| Signal | Classification | Action |
|--------|---------------|--------|
| 0–2 `##` headings in source | Raw note / unstructured | Summarize normally |
| 3+ `##` headings in source | Pre-structured document | Stop; flag for knowledge-indexer |
| No headings, multi-paragraph prose | Raw note | Summarize normally |
| Mixed: some headings + long prose blocks | Ambiguous | Treat as unstructured; note in self_report |

---

## Compression Edge Cases

Normal operating range: source 600–6,000 words. Outside this range, the 300–600 body target may conflict with the compression bounds (`0.05 < ratio < 1.0`). Resolve as follows:

### Short Sources (< ~600 words)

Problem: a 400-word body for a 300-word source → ratio = 1.33, fails upper bound.

Resolution:
- Reduce body to strictly less than source word count.
- May produce a body under 300 words — this is acceptable.
- Minimum body: enough to cover TL;DR + Key Concepts meaningfully (typically 80–150 words for a very short source).
- Do not pad with filler to hit 300 words. Padding is a faithfulness violation.

Compression check: `body_words < source_word_count` must hold. A ratio just below 1.0 (e.g., 0.85) for a short source is acceptable if all content is genuine.

### Long Sources (> ~6,000 words)

Problem: a 300-word body for a 7,000-word source → ratio = 0.043, fails lower bound.

Resolution:
- Expand body above 300 words proportionally.
- For 6,000–10,000 word sources: target ~400–600 words.
- For 10,000+ word sources: up to 600 words (hard ceiling). Prioritize TL;DR completeness and entity coverage.
- Do not exceed 600 words; prefer entity density over prose density when space is tight.

If after expansion `ratio < 0.05` still holds, set `compression_in_bounds = false` and `verdict = "fail"`. The skill will decide how to handle.

### Compression Ratio Calculation

```
body_words       = word count of summary.md body (no frontmatter; summary.md has no frontmatter)
source_word_count = word count of full source file
compression_ratio = body_words / source_word_count
```

The example in self-report-contract.md: `word_count=450`, `source_word_count=2000` → `compression_ratio = 450/2000 = 0.225`. This is within bounds (0.05 < 0.225 < 1.0).

---

## Entity Extraction Quick Rules

- Extract: proper names (people, orgs, products), project/feature names, dates (ISO or prose), version numbers, specific numeric values with units.
- Do NOT extract: generic nouns ("meeting", "decision", "team"), common verbs, stop words.
- Verbatim match required: if source says "Project X" and summary says "project X" (lowercase), that is a miss. Case must match.
- When entity density is high and body limit is tight: list entities in Key Concepts as `**EntityName**: ...` bullets. This preserves entities efficiently.

---

## missing_sections vs. Omitted Sections

This distinction matters for the skill gate (`missing_sections == []`).

| Case | `missing_sections` | Correct? |
|------|-------------------|---------|
| Source has decisions; agent writes `## Key Decisions` | `[]` | Yes |
| Source has no decisions; agent omits `## Key Decisions` | `[]` | Yes — omission is correct |
| Source has decisions; agent fails to write `## Key Decisions` | `["## Key Decisions"]` | Fail — section was expected |
| Source has action items; agent omits `## Action Items` | `["## Action Items"]` | Fail — section was expected |

The agent is responsible for deciding which conditionals apply and then populating them. `missing_sections` only lists sections the agent determined should have been present but failed to write.
