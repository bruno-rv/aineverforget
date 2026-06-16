# Design — `.docx` loader + unknown-extension sniff

- **Date:** 2026-06-16
- **Status:** Approved (brainstorm), pending implementation plan
- **Branch:** `feat/docx-loader-sniff` (stacks on `chore/reproducible-clone` / PR #7)
- **Scope owner:** ingest / loaders subsystem

## Context

`aineverforget` ingests sources via a Loader registry: extension → `source_type`
→ `Loader` (`src/aineverforget/loaders/__init__.py`). Today only two loaders are
registered:

- `MarkdownLoader` — `.md .txt .markdown .rst .text` → `source_type="markdown"`
- `PDFLoader` — `.pdf` → `source_type="pdf"`

`infer_source_type(path)` (`loaders/__init__.py:183-210`) raises `ValueError` for
any other extension; `ingest.py:377-383` turns that into `IngestOutcome.error`.
Chunk-strategy dispatch keys on `Document.source_type` (`chunking.py:100,108`).
The CLI `aineverforget ingest` runs pure load → chunk → embed → index → verify;
note-summarization is an agent-layer concern in `.claude/skills/ingest/SKILL.md`,
not in the CLI.

## Goal

Move toward the product goal "drop ANY meeting summary, loose notes, draft, PDF,
or codebase summary." This round closes two gaps:

1. **`.docx` is unsupported** — the most common real meeting-summary format
   (Word / Google-Docs export) hits the hard `ValueError`.
2. **Unknown extensions are always rejected** — many plain-text formats
   (`.org .adoc .typ .log` …) are text but not in the allowlist.

## Decisions (locked in brainstorm)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Scope this round | `.docx` loader + unknown-ext policy. OCR and code-block size guard deferred to their own specs. |
| 2 | Unknown extension behavior | **Sniff** the bytes: text-like → ingest as `markdown` flagged `low_confidence`; binary → fail-closed `ValueError` with a clear message. **Default-on.** |
| 3 | `.docx` extraction depth | **Rich → markdown**: heading styles → `#`×level, tables → markdown pipe tables, paragraphs → prose. |
| 4 | `.docx` routing | **Direct-index like PDF** — chunk the reconstructed markdown as-is, no summarizer. |

Rationale for fail-closed-on-binary: preserves the existing deliberate contract
that the pipeline never silently ingests garbage chunks. Rationale for default-on
text sniff: matches the "drop anything" goal for the common case (text), while the
binary guard keeps the safety property.

Rationale for own `source_type="docx"` (not aliasing to `"markdown"`): chunk
payloads stay traceable to their true origin for provenance; the chunker simply
routes docx content through the markdown strategy.

## Architecture

No change to the Loader Protocol. One new loader, one new `source_type`, one new
byte-sniff helper, and a one-line chunk-dispatch extension. python-docx is
pure-Python (depends on `lxml`, already locked), so the no-system-C-deps and
no-secrets reproducibility properties are preserved.

```
path
 └─ resolve_source(path) ─────────────► (source_type, sniffed_unknown)
       ├ .docx                          ("docx",     False)
       ├ allowlisted text ext           ("markdown", False)
       ├ .pdf                           ("pdf",      False)
       ├ unknown + _looks_like_text     ("markdown", True)   → verdict downgraded to low_confidence
       └ unknown + binary               raise ValueError     → IngestOutcome.error (clear msg)
 └─ get_loader(source_type).load(path) ─► Document(source_type, raw_text, meta[loader_verdict])
 └─ chunk_document(doc) ──────────────► markdown strategy for source_type in {markdown, docx}
```

## Components

### 1. `src/aineverforget/loaders/docx.py` (new) — `DocxLoader`

- Implements the `Loader` Protocol. Lazy `import docx` inside `load()`.
- Walk the document body **in document order** (paragraphs and tables
  interleaved — iterate `document.element.body` children, not the separate
  `paragraphs`/`tables` collections, so order is preserved).
- Reconstruct **markdown**:
  - Paragraph with a `Heading N` style → `#`×N + text.
  - Table → markdown pipe table (header row = first table row; `---` separator).
  - Other paragraphs → plain text lines.
- Emit `Document(source_type="docx", raw_text=<markdown>, ...)` with all identity
  fields populated (mirror `PDFLoader`), `meta["loader_verdict"]` set.
- Verdicts (reuse `LoaderVerdict`):
  - clean extraction → `ok`
  - near-empty text (below a small char threshold; reuse the PDF
    `LOW_TEXT_THRESHOLD` pattern) → `low_confidence`
  - encrypted `.docx`: detect the OLE compound-file magic (`\xD0\xCF\x11\xE0` in
    the first bytes — a password-protected docx is an OLE container, not a zip)
    **before** calling python-docx → emit `encrypted` (flows into the existing
    skip path at `ingest.py:446`). This explicit check is necessary because
    python-docx raises the same `PackageNotFoundError` for encrypted and corrupt
    files, so a bare `except` cannot distinguish them.
  - not-a-zip / corrupt `.docx` (python-docx raises, no OLE magic) → loader
    raises → `IngestOutcome.error` with a clear message (handled by the existing
    exception path in the ingest dispatch)

### 2. `src/aineverforget/loaders/__init__.py` — registry + sniff

- `infer_source_type`: add `.docx → "docx"`.
- New helper `_looks_like_text(data: bytes) -> bool`: inspect the first ~8 KB —
  return `False` if a NUL byte is present or the ratio of non-text control bytes
  exceeds a threshold (e.g. > 0.30); else `True`. Pure, unit-testable.
- New helper `resolve_source(path) -> tuple[str, bool]` returning
  `(source_type, sniffed_unknown)`:
  - known extension → delegate to `infer_source_type`, `sniffed_unknown=False`
  - unknown extension → read a byte prefix; text-like → `("markdown", True)`;
    binary → raise `ValueError` ("…looks binary; pass --source-type to override").
- `infer_source_type` stays pure (name→type, still raises on unknown) so existing
  callers and tests are unaffected; the sniff lives only in `resolve_source`.

### 3. `src/aineverforget/chunking.py` — dispatch

- `chunk_document`: change the markdown branch to
  `if document.source_type in ("markdown", "docx"):` so docx-reconstructed
  markdown uses the existing heading-aware / table-atomic chunker. Single-line
  change; no new strategy.

### 4. `src/aineverforget/ingest.py` — wire-in

- Register `DocxLoader` alongside the existing loader imports (the block near
  `loaders` import at lines ~273-274 that triggers `register_loader` side effects).
- In the per-path dispatch (`_ingest_one` around line 377), replace the
  `infer_source_type(path)` call with `resolve_source(path)`. The binary
  `ValueError` path is unchanged (already → `IngestOutcome.error`). When
  `sniffed_unknown` is `True` and the loader returns verdict `ok`, downgrade the
  recorded `loader_verdict` to `low_confidence` (so the CLI surfaces that an
  unknown-extension file was force-read as text).

### 5. `pyproject.toml` + `requirements-dev.lock`

- Add `python-docx` to runtime dependencies (pin a range consistent with the
  project's existing style).
- Regenerate `requirements-dev.lock` on macOS arm64 / Python 3.12 per the
  documented `uv pip compile` command in the README.

### 6. `.claude/skills/ingest/SKILL.md`

- STEP 1 classification: add `.docx` to the **direct** branch (same as `.pdf`),
  so the `/ingest` agent indexes it directly without note-summarization.

### 7. `README.md`

- Add `.docx` to the supported-formats sentence and the accepted-extensions note.
- Document the unknown-extension sniff behavior (text-in → ingested as markdown,
  flagged; binary-in → rejected with a clear message; `--source-type` override).

## Error handling summary

| Situation | Behavior |
|-----------|----------|
| valid `.docx` with text | `ok`, indexed |
| `.docx` heading/table heavy | reconstructed markdown, chunked by markdown strategy |
| near-empty `.docx` | `low_confidence`, indexed (chunker may yield few/no chunks) |
| password-protected `.docx` (OLE magic) | `encrypted` verdict → skipped (existing path) |
| corrupt / non-zip `.docx` (no OLE magic) | loader raises → `IngestOutcome.error`, clear detail |
| unknown ext, text bytes | `("markdown", sniffed)` → indexed, `low_confidence` |
| unknown ext, binary bytes | `ValueError` → `IngestOutcome.error`, "looks binary…" |

## Testing (TDD)

python-docx can **author** `.docx` fixtures in-test, so no binary blobs enter the
repo.

- `DocxLoader`: build a docx with headings + a table + paragraphs → assert the
  reconstructed markdown (heading levels, pipe table, order) and `ok` verdict.
- `DocxLoader`: near-empty docx → `low_confidence`; bytes starting with the OLE
  magic `\xD0\xCF\x11\xE0` and `.docx` ext → `encrypted` verdict; other corrupt
  bytes with `.docx` ext → raises.
- `_looks_like_text`: UTF-8 text → `True`; bytes with NUL / high control ratio →
  `False`.
- `resolve_source`: `.docx` → `("docx", False)`; `.org` text file →
  `("markdown", True)`; `.bin` NUL blob → `ValueError`.
- `chunk_document`: a `source_type="docx"` Document routes through the markdown
  strategy and produces the same chunk shape as the equivalent markdown.
- Ingest-level: sniffed-unknown text file ends with `loader_verdict="low_confidence"`.

## Out of scope (separate specs)

- **OCR** for scanned / image-only PDFs (conflicts with pure-pip / no-secrets
  reproducibility — needs its own dependency-tradeoff design).
- **Code-block size guard** — atomic markdown code blocks that exceed the BGE-M3
  token window are silently truncated (`chunking.py:528-549`); a sub-splitting
  guard is a separate change.

## Acceptance criteria

1. `aineverforget ingest meeting.docx` indexes a Word meeting summary with
   headings and tables preserved as markdown structure, no summarizer call.
2. Dropping a `.org`/`.adoc` plain-text note ingests it as markdown, flagged
   `low_confidence`.
3. Dropping a `.pptx`/image file (binary) yields a clear fail-closed error, never
   garbage chunks.
4. All new unit tests pass; `requirements-dev.lock` regenerated and the
   deterministic setup checks still pass.
5. README + `/ingest` SKILL.md reflect `.docx` support and the sniff behavior.
