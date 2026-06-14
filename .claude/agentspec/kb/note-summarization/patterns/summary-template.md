# Summary Template — note-summarization

Canonical template for `summary.md` produced by `note-summarizer`. Every file the agent writes must conform to this spec. The skill gates on the `self_report` fields that measure conformance — the agent never enforces gates itself.

---

## Section Inventory

| Section | Header | Required? | Rule |
|---------|--------|-----------|------|
| TL;DR | `## TL;DR` | Always | 2–3 sentence distillation of the source |
| Key Concepts | `## Key Concepts` | Always | Bullet list of named concepts, terms, frameworks |
| Key Decisions | `## Key Decisions` | Conditional — include iff source contains decisions | Bullet list of decisions made |
| Action Items | `## Action Items` | Conditional — include iff source contains action items | Numbered list |
| Context & Background | `## Context & Background` | Optional | Referenced context, background info |
| Open Questions | `## Open Questions` | Optional | Unresolved questions raised in source |

**Omit rule:** optional sections (Context & Background, Open Questions) and conditional sections (Key Decisions, Action Items) are omitted entirely when empty — no header, no "N/A", no placeholder. This is load-bearing: a present-but-empty section is a structure failure.

**Conditional vs. optional distinction:** Key Decisions and Action Items are conditional, meaning their omission is a deliberate signal ("this source had no decisions/action items"). The agent records which required-for-this-note sections it expected vs. found. `missing_sections` in self_report is only non-empty when a section that *should* be present is absent.

---

## Body Target

- **300–600 words total** across all body sections (frontmatter excluded from count)
- Each `##` section becomes one Qdrant embedding chunk — do not merge sections
- Compression ratio = body word count / source word count; must be `0.05 < ratio < 1.0`

**Compression edge cases** (handled at generation time, documented in quick-reference.md):
- Source < ~600 words: body must be reduced below the 300-word floor to keep ratio < 1.0. Minimum: faithfully distill without padding.
- Source > ~6,000 words: body must be kept above 300 words to keep ratio > 0.05. Expand coverage proportionally; do not truncate.

---

## Entity Preservation Rule

Entities (proper nouns, named projects, dates, numeric values, version numbers) found in the source **must** appear verbatim in the summary. This is enforced through `entities_in_source`, `entities_in_summary`, and `missing_entities` fields in `self_report`. When entity density and the 600-word ceiling conflict, entities win — omit prose, not entities.

Entity preservation is a runtime gate. Claim-level faithfulness (whether the summary misrepresents facts) is a dev-time eval (see ADR-0003) and is not a runtime gate.

---

## Frontmatter

`summary.md` carries no frontmatter. It is plain Markdown starting with `## TL;DR`. Source metadata lives in the `self_report.metadata` block returned to the skill.

---

## Canonical Example Structure

```
## TL;DR

<2–3 sentences that capture the essence. Reference key entities by name.>

## Key Concepts

- **<Named Concept>**: <1-sentence definition or role>
- **<Framework or Term>**: <role in source>
- <additional bullets as warranted>

## Key Decisions

- <Decision verbatim or closely paraphrased>
- <Decision 2>

## Action Items

1. <Action item — who, what, by when if stated>
2. <Action item 2>

## Context & Background

<Optional. Referenced prior context or background the source assumes.>

## Open Questions

<Optional. Unresolved questions raised explicitly or implicitly in the source.>
```

---

## Hard Rules

1. Never include content not present in the source (faithfulness over completeness).
2. Entities must survive verbatim — names, dates, numbers, project names.
3. No empty section stubs. Omit the header entirely if the section has no content.
4. Body word target is 300–600 words; adapt when compression bounds require it.
5. Each `##` section is a distinct embedding unit — never combine two sections under one header.
6. The agent returns `{output, metadata, self_report}` JSON only. It never writes a narrative report.
