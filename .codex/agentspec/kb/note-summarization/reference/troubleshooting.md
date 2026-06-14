# Troubleshooting — note-summarization

Known failure modes for `note-summarizer`, with diagnosis and remediation. The agent never remediates itself — it returns `verdict="fail"` with a descriptive self_report. The skill (or operator) applies the remediation.

---

## Failure Mode 1: Source Too Short

**Symptom:** `compression_in_bounds = false`, `compression_ratio >= 1.0`

**Cause:** Source word count is low (< ~600 words). A minimal faithful summary exceeds the source length.

**Diagnosis:**
```
source_word_count < 600
body_words >= source_word_count
compression_ratio >= 1.0
```

**Remediation:**
- Reduce body to strictly fewer words than source. Even 80–150 words is valid for a very short source.
- Do not pad to hit the 300-word floor. Padding violates faithfulness.
- If source is under ~100 words (a stub note), the skill should route it directly to knowledge-indexer as a raw chunk rather than summarize.

**Agent behavior:** Agent writes the shortest faithful summary possible, reports `compression_ratio`, sets `compression_in_bounds = false` if ratio >= 1.0, sets `verdict = "fail"`.

---

## Failure Mode 2: Source Too Long (Ratio Floor Breach)

**Symptom:** `compression_in_bounds = false`, `compression_ratio <= 0.05`

**Cause:** Source word count is very high (> ~6,000 words). A 300-word body represents less than 5% of source.

**Diagnosis:**
```
source_word_count > 6000
body_words <= 300
compression_ratio <= 0.05
```

**Remediation:**
- Expand body proportionally. For 6,000–10,000 word sources, target 400–600 words. For 10,000+ word sources, stay at the 600-word ceiling.
- Prioritize entity coverage and TL;DR completeness when space is constrained.
- If after expansion the ratio still falls below 0.05 (source > ~12,000 words at 600-word body), the skill should consider pre-chunking the source before summarization.

**Agent behavior:** Agent writes a 600-word body, reports `compression_ratio`, sets `compression_in_bounds = false` if ratio <= 0.05, sets `verdict = "fail"`.

---

## Failure Mode 3: Missing Entities

**Symptom:** `missing_entities` is non-empty; `verdict = "fail"` (or "pass" with skill gate fail)

**Cause:** One or more entities identified in source (proper nouns, names, dates, numbers) did not appear verbatim in the summary.

**Common causes:**
- Entity was paraphrased ("Q2 2026" → "second quarter") — not a verbatim match.
- Entity was dropped to meet word target.
- Entity extraction missed the entity in source (under-extraction).

**Remediation:**
- Agent should include entities even at the cost of reducing prose. Entities are non-negotiable.
- Check entity extraction: if `entities_in_source` is suspiciously short relative to source length, extraction likely under-fired. The agent flags this in self_report.
- Verbatim match is case-sensitive. "Project X" and "project x" are different strings.

**Agent behavior:** Agent populates `entities_in_source` and `missing_entities` accurately. Skill gates on `missing_entities == []`. Skill retries.

---

## Failure Mode 4: Pre-Structured Source Routed to Summarizer

**Symptom:** `verdict = "fail"`, self_report contains routing note; `summary.md` not written.

**Cause:** Source already has 3+ `##` headings. Skill routing check failed to catch it, or user explicitly invoked note-summarizer on a structured doc.

**Remediation:**
- Route source directly to knowledge-indexer, bypassing note-summarizer.
- Skill should update its routing heuristic if this happens repeatedly.

**Agent behavior:** Stops without writing `summary.md`. Returns JSON with `verdict = "fail"` and a `fail_reason` describing the pre-structured source detection.

---

## Failure Mode 5: Empty Section Stub in Output

**Symptom:** `structure_present = false` or `missing_sections` non-empty; skill structure gate fails.

**Cause:** Agent wrote a section header (`## Key Decisions`) with no body content, or omitted a section that should have been present.

**Common causes:**
- Agent wrote conditional section header but found no content to fill it.
- Agent omitted a required section (TL;DR or Key Concepts) — always a bug.

**Remediation:**
- For conditional sections with no content: omit the header entirely.
- For missing required sections: regenerate.
- Skill retries on structure gate failure.

**Agent behavior:** On retry, agent is more conservative — only write conditional sections when source contains clear explicit signals, not borderline cases.

---

## Failure Mode 6: Language Unrecognizable

**Symptom:** `verdict = "fail"`, `fail_reason` describes unrecognizable language.

**Cause:** Source is binary, heavily encoded, corrupted, or in a script the agent cannot parse for entity extraction or summarization.

**Remediation:**
- Skill escalates to user with `needs_user` flag.
- User must pre-process source (OCR, decode, translate) before re-ingesting.

**Agent behavior:** Returns `verdict = "fail"` with descriptive `fail_reason`. Does not write `summary.md`.

---

## Failure Mode 7: summary.md Already Exists

**Symptom:** Agent stops without generating; returns `verdict = "fail"` with `fail_reason = "summary already exists"`.

**Cause:** Target `summary.md` is present and no explicit overwrite was requested.

**Remediation:**
- To overwrite: re-invoke with explicit overwrite signal.
- To preserve existing: route existing `summary.md` directly to knowledge-indexer for re-indexing.

**Agent behavior:** Hard stop. Does not read or modify the existing `summary.md`.
