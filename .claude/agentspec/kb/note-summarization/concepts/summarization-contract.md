# Summarization Contract — note-summarization

Defines the formal interface between `note-summarizer` and its upstream (the user / skill) and downstream (`knowledge-indexer`). All parties depend on this contract remaining stable.

---

## Position in the Pipeline

```text
User / Skill
     |
     | provides: source file path
     v
note-summarizer   ← this agent
     |
     | writes: summary.md
     | returns: {output, metadata, self_report} JSON
     v
Skill (quality gate evaluation)
     |
     | if gates pass: passes summary_path to
     v
knowledge-indexer
     |
     | chunks summary.md by ## section
     | indexes into Qdrant
     v
aineverforget search
```

---

## Upstream Contract — What the Agent Accepts

| Input | Required? | Type | Notes |
|-------|-----------|------|-------|
| `source_path` | Required | Absolute file path | Readable text file (markdown, txt, pdf-extracted text) |
| Explicit overwrite flag | Optional | Boolean signal | If `summary.md` exists and no flag given, agent stops |

**Source eligibility:**
- File must exist and be non-empty.
- File must be unstructured or lightly structured prose (fewer than 3 `##` headings).
- Pre-structured documents (3+ `##` headings) should be routed directly to knowledge-indexer by the skill. If the agent receives one, it flags it and returns `verdict="fail"`.

**What the agent does NOT need:**
- Metadata files, manifest files, or configuration.
- Information about the downstream collection or Qdrant parameters — that is knowledge-indexer's concern.

---

## Downstream Contract — What the Agent Guarantees

### summary.md

When `verdict="pass"`, the agent guarantees `summary.md` at `summary_path`:

- Plain Markdown, no frontmatter.
- Starts with `## TL;DR`.
- Contains `## TL;DR` and `## Key Concepts` (always).
- Contains `## Key Decisions` iff source had decisions; `## Action Items` iff source had action items.
- No empty-stub sections.
- Body word count: 300–600 words (adapted at compression edges; see quick-reference.md).
- All entities from `entities_in_source` present verbatim in body text.
- Each `##` section is a standalone embedding unit — no cross-section dependencies in prose.

### self_report JSON

The agent guarantees all fields in the frozen schema are present and computed:

```json
{
  "self_report": {
    "structure_present": <bool>,
    "required_sections_found": ["## TL;DR", "## Key Concepts", ...],
    "missing_sections": [],
    "compression_ratio": <float, 4 decimal places>,
    "compression_in_bounds": <bool>,
    "entities_in_source": [...],
    "entities_in_summary": [...],
    "missing_entities": [],
    "verdict": "pass" | "fail"
  }
}
```

No field is ever null or omitted. If a value cannot be computed, the agent returns `verdict="fail"` with a descriptive reason embedded in the JSON (convention: add a `"fail_reason"` string to `self_report`).

---

## What the Agent Does NOT Guarantee

- That `verdict="pass"` means the summary is factually accurate at the claim level. Claim-level faithfulness is a dev-time eval (ADR-0003), not a runtime guarantee.
- That all decisions or action items are captured — completeness is best-effort within the body word target.
- Retry or recovery behavior — the skill manages retries and escalation.

---

## Skill Gate Enforcement (for reference)

The skill enforces these gates on `self_report` after the agent returns. The agent does not run these:

| Gate | Condition |
|------|-----------|
| Structure | `structure_present == true` AND `missing_sections == []` |
| Compression | `0.05 < compression_ratio < 1.0` |
| Entity | `missing_entities == []` |
| Fail ladder | Gate fail → skill retries → skill escalates to opus → `needs_user` |

---

## Version and Stability

This contract is defined by `.claude/agentspec/shared/self-report-contract.md` (note-summarizer section). Changes to field names, types, or gate logic must be made there first and propagated to this file. The agent definition (`agents/dev/note-summarizer.md`) and the KB template (`patterns/summary-template.md`) are downstream of the contract, not the other way around.
