# .docx Loader + Unknown-Extension Sniff — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users drop `.docx` meeting summaries and arbitrary text-format notes into `aineverforget ingest`, while binary files fail closed with a clear error.

**Architecture:** A new pure-Python `DocxLoader` mirrors `PDFLoader`, reconstructs the document to markdown, and registers as `source_type="docx"`. A byte-sniff (`_looks_like_text` + `resolve_source`) replaces the hard `ValueError` for unknown extensions: text-like bytes ingest as `markdown` flagged `low_confidence`; binary bytes fail closed. The chunker routes `docx` through the existing markdown strategy.

**Tech Stack:** Python 3.12, pydantic v2, python-docx (new dep, depends on already-locked lxml), pytest, uv (lockfile), Qdrant (only for the final manual smoke).

**Branch:** `feat/docx-loader-sniff` (stacks on `chore/reproducible-clone` / PR #7). Spec: `docs/superpowers/specs/2026-06-16-docx-loader-unknown-ext-design.md`.

**Venv interpreter:** `.venv/bin/python` (do NOT assume `python` is on PATH).

---

## File map

| File | Action | Responsibility |
|------|--------|----------------|
| `pyproject.toml` | Modify | add `python-docx` runtime dep |
| `requirements-dev.lock` | Regenerate | pin python-docx + transitive |
| `src/aineverforget/loaders/__init__.py` | Modify | `.docx`→`"docx"` in `infer_source_type`; add `_looks_like_text`, `resolve_source` |
| `src/aineverforget/loaders/docx.py` | Create | `DocxLoader` — markdown reconstruction, verdicts, registration |
| `src/aineverforget/chunking.py` | Modify | route `"docx"` through markdown strategy |
| `src/aineverforget/ingest.py` | Modify | register docx module; use `resolve_source`; downgrade sniffed verdict |
| `.claude/skills/ingest/SKILL.md` | Modify | classify `.docx` as direct |
| `README.md` | Modify | document `.docx` + sniff behavior |
| `tests/test_loaders.py` | Modify | `infer_source_type`/sniff/`resolve_source`/`DocxLoader` tests |
| `tests/test_chunking_docx.py` | Create | docx→markdown chunk routing test |

---

## Task 0: Add python-docx dependency

**Files:**
- Modify: `pyproject.toml:10-17` (the `dependencies` array)
- Regenerate: `requirements-dev.lock`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, the `dependencies` array currently ends:

```toml
dependencies = [
    "pydantic>=2,<3",
    "qdrant-client>=1.15,<2",
    "FlagEmbedding",
    "pypdf",
    "pdfplumber",
    "mistune>=3,<4",
]
```

Change it to add `python-docx` (mirror the pinned-floor style used by `mistune`):

```toml
dependencies = [
    "pydantic>=2,<3",
    "qdrant-client>=1.15,<2",
    "FlagEmbedding",
    "pypdf",
    "pdfplumber",
    "mistune>=3,<4",
    "python-docx>=1,<2",
]
```

- [ ] **Step 2: Install into the venv (unblocks tests immediately)**

Run: `.venv/bin/python -m pip install "python-docx>=1,<2"`
Expected: `Successfully installed python-docx-1.x.x lxml-...` (lxml already present).

- [ ] **Step 3: Verify the import works**

Run: `.venv/bin/python -c "import docx; print(docx.__version__)"`
Expected: prints a version like `1.1.2` (no `ModuleNotFoundError`).

- [ ] **Step 4: Regenerate the lockfile**

Run: `uv pip compile pyproject.toml --extra dev --python-version 3.12 -o requirements-dev.lock`
Expected: `requirements-dev.lock` now contains a `python-docx==…` line.
(If `uv` is not installed: `.venv/bin/python -m pip install uv` first, then re-run. Confirm with `grep -n python-docx requirements-dev.lock`.)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements-dev.lock
git commit -m "build: add python-docx runtime dependency

Pure-Python (depends on already-locked lxml). Required for the .docx
loader. Lockfile regenerated for Python 3.12 / macOS arm64.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 1: `infer_source_type` recognizes `.docx`

**Files:**
- Modify: `src/aineverforget/loaders/__init__.py:202-210`
- Test: `tests/test_loaders.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_loaders.py` (inside the existing `class TestRegistry` or a new `class TestSourceResolution`):

```python
from aineverforget.loaders import infer_source_type


class TestInferSourceType:
    def test_docx_extension_maps_to_docx(self, tmp_path: Path):
        p = tmp_path / "summary.docx"
        p.write_bytes(b"PK\x03\x04stub")  # not read by infer_source_type
        assert infer_source_type(p) == "docx"

    def test_markdown_extension_unchanged(self, tmp_path: Path):
        assert infer_source_type(tmp_path / "n.md") == "markdown"

    def test_unknown_extension_still_raises(self, tmp_path: Path):
        with pytest.raises(ValueError):
            infer_source_type(tmp_path / "n.weirdext")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestInferSourceType -v`
Expected: `test_docx_extension_maps_to_docx` FAILS (raises `ValueError`).

- [ ] **Step 3: Implement**

In `src/aineverforget/loaders/__init__.py`, `infer_source_type` currently reads:

```python
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".md", ".txt", ".markdown", ".rst", ".text"}:
        return "markdown"
    raise ValueError(
        f"Cannot infer source_type for extension {suffix!r} (path={path}). "
        "Pass --source-type explicitly."
    )
```

Change to add the `.docx` branch:

```python
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix in {".md", ".txt", ".markdown", ".rst", ".text"}:
        return "markdown"
    raise ValueError(
        f"Cannot infer source_type for extension {suffix!r} (path={path}). "
        "Pass --source-type explicitly."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestInferSourceType -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/aineverforget/loaders/__init__.py tests/test_loaders.py
git commit -m "feat: infer_source_type maps .docx to docx

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `_looks_like_text` byte sniff

**Files:**
- Modify: `src/aineverforget/loaders/__init__.py` (add helper after `infer_source_type`)
- Test: `tests/test_loaders.py`

- [ ] **Step 1: Write the failing test**

```python
from aineverforget.loaders import _looks_like_text


class TestLooksLikeText:
    def test_plain_utf8_is_text(self):
        assert _looks_like_text("# Notes\n\nhello world\n".encode("utf-8")) is True

    def test_empty_is_text(self):
        assert _looks_like_text(b"") is True

    def test_nul_byte_is_binary(self):
        assert _looks_like_text(b"PK\x03\x04\x00\x00rubbish") is False

    def test_high_control_ratio_is_binary(self):
        assert _looks_like_text(bytes(range(1, 9)) * 20) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestLooksLikeText -v`
Expected: FAILS at import (`cannot import name '_looks_like_text'`).

- [ ] **Step 3: Implement**

In `src/aineverforget/loaders/__init__.py`, add directly after the `infer_source_type` function:

```python
# Bytes that are legitimate in text files even though they are control codes.
_ALLOWED_CONTROL_BYTES: frozenset[int] = frozenset({0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1B})
_BINARY_CONTROL_RATIO: float = 0.30


def _looks_like_text(data: bytes) -> bool:
    """Heuristic: does *data* (a byte prefix) look like a text file?

    Returns False if a NUL byte is present or the ratio of non-text control
    bytes exceeds ``_BINARY_CONTROL_RATIO``.  Used to decide whether an
    unknown-extension Source is safe to ingest as text.
    """
    if not data:
        return True
    if b"\x00" in data:
        return False
    nontext = sum(
        1 for b in data if b < 0x20 and b not in _ALLOWED_CONTROL_BYTES
    )
    return (nontext / len(data)) <= _BINARY_CONTROL_RATIO
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestLooksLikeText -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/aineverforget/loaders/__init__.py tests/test_loaders.py
git commit -m "feat: add _looks_like_text byte-sniff helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `resolve_source` (sniff dispatcher)

**Files:**
- Modify: `src/aineverforget/loaders/__init__.py` (add `resolve_source` after `_looks_like_text`)
- Test: `tests/test_loaders.py`

- [ ] **Step 1: Write the failing test**

```python
from aineverforget.loaders import resolve_source


class TestResolveSource:
    def test_known_docx(self, tmp_path: Path):
        p = tmp_path / "s.docx"
        p.write_bytes(b"PK\x03\x04stub")
        assert resolve_source(p) == ("docx", False)

    def test_known_markdown(self, tmp_path: Path):
        p = tmp_path / "s.md"
        p.write_text("# hi", encoding="utf-8")
        assert resolve_source(p) == ("markdown", False)

    def test_unknown_text_sniffs_to_markdown(self, tmp_path: Path):
        p = tmp_path / "notes.org"
        p.write_text("* Org heading\nplain notes\n", encoding="utf-8")
        assert resolve_source(p) == ("markdown", True)

    def test_unknown_binary_raises(self, tmp_path: Path):
        p = tmp_path / "blob.bin"
        p.write_bytes(b"\x00\x01\x02\x03binarygarbage")
        with pytest.raises(ValueError, match="binary"):
            resolve_source(p)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestResolveSource -v`
Expected: FAILS at import (`cannot import name 'resolve_source'`).

- [ ] **Step 3: Implement**

In `src/aineverforget/loaders/__init__.py`, add after `_looks_like_text`:

```python
_SNIFF_PREFIX_BYTES: int = 8192


def resolve_source(path: Path) -> tuple[str, bool]:
    """Resolve a Source path to ``(source_type, sniffed_unknown)``.

    Known extensions resolve via :func:`infer_source_type` with
    ``sniffed_unknown=False``.  Unknown extensions are byte-sniffed: text-like
    content resolves to ``("markdown", True)`` so it ingests as text (the
    caller downgrades the loader verdict to ``low_confidence``); binary content
    raises ``ValueError`` so the ingest fails closed rather than storing garbage.
    """
    try:
        return infer_source_type(path), False
    except ValueError:
        pass
    prefix = path.read_bytes()[:_SNIFF_PREFIX_BYTES]
    if _looks_like_text(prefix):
        return "markdown", True
    raise ValueError(
        f"Cannot ingest {path}: unrecognized extension and the content looks "
        "binary. Pass --source-type explicitly to override."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestResolveSource -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/aineverforget/loaders/__init__.py tests/test_loaders.py
git commit -m "feat: add resolve_source sniff dispatcher

Unknown extensions: text-like -> markdown(sniffed), binary -> fail closed.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `DocxLoader` — happy path

**Files:**
- Create: `src/aineverforget/loaders/docx.py`
- Test: `tests/test_loaders.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_loaders.py`:

```python
class TestDocxLoader:
    @pytest.fixture
    def loader(self):
        from aineverforget.loaders.docx import DocxLoader
        return DocxLoader()

    @pytest.fixture
    def docx_file(self, tmp_path: Path) -> Path:
        from docx import Document as DocxFile
        d = DocxFile()
        d.add_heading("Weekly Sync", level=1)
        d.add_paragraph("We discussed the roadmap.")
        d.add_heading("Decisions", level=2)
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text = "Item"
        t.cell(0, 1).text = "Owner"
        t.cell(1, 0).text = "Ship docx loader"
        t.cell(1, 1).text = "Bruno"
        p = tmp_path / "sync.docx"
        d.save(str(p))
        return p

    def test_returns_one_document(self, loader, docx_file: Path):
        docs = list(loader.load(docx_file))
        assert len(docs) == 1

    def test_source_type_is_docx(self, loader, docx_file: Path):
        doc = list(loader.load(docx_file))[0]
        assert doc.source_type == "docx"

    def test_headings_become_markdown(self, loader, docx_file: Path):
        doc = list(loader.load(docx_file))[0]
        assert "# Weekly Sync" in doc.raw_text
        assert "## Decisions" in doc.raw_text

    def test_table_becomes_pipe_table(self, loader, docx_file: Path):
        doc = list(loader.load(docx_file))[0]
        assert "| Item | Owner |" in doc.raw_text
        assert "| --- | --- |" in doc.raw_text
        assert "| Ship docx loader | Bruno |" in doc.raw_text

    def test_verdict_ok(self, loader, docx_file: Path):
        doc = list(loader.load(docx_file))[0]
        assert doc.meta["loader_verdict"] == "ok"

    def test_identity_fields(self, loader, docx_file: Path):
        doc = list(loader.load(docx_file))[0]
        assert doc.source_id == str(docx_file.resolve())
        assert doc.document_path == doc.source_id
        assert doc.document_id == make_document_id(doc.source_id, doc.document_path)
        assert doc.document_sha256 == sha256_text(doc.raw_text)

    def test_registered_in_registry(self, docx_file: Path):
        import aineverforget.loaders.docx  # noqa: F401
        assert "docx" in registered_source_types()
        assert get_loader("docx") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestDocxLoader -v`
Expected: FAILS at fixture import (`ModuleNotFoundError: aineverforget.loaders.docx`).

- [ ] **Step 3: Implement the loader**

Create `src/aineverforget/loaders/docx.py`:

```python
"""aineverforget.loaders.docx — DOCX Loader (rich markdown reconstruction).

Source types handled: ``"docx"`` (``.docx``).

python-docx is pure-Python (depends on lxml, already locked).  The loader walks
the document body in order, reconstructing markdown: heading styles -> ``#``×level,
tables -> pipe tables, paragraphs -> prose.  The result is chunked by the
markdown strategy (see ``chunking.chunk_document``).  Direct-indexed like PDF;
no summarizer.  Embedded-image / OCR text is out of scope.

No heavy imports at module level; python-docx is imported lazily in ``load()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from aineverforget.identity import make_document_id, sha256_text
from aineverforget.loaders import LoaderVerdict, register_loader
from aineverforget.models import Document

LOADER_VERSION = "docx:1.0"

LOW_TEXT_THRESHOLD: int = 20
"""Minimum non-whitespace chars before flagging a docx ``low_confidence``."""

# OLE compound-file magic.  A password-protected/encrypted .docx is an OLE
# container, not a zip; python-docx raises the same PackageNotFoundError for
# encrypted and corrupt files, so encryption is detected by this signature.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _para_to_markdown(style_name: str, text: str) -> str:
    """Convert one paragraph (style name + text) to a markdown line."""
    if not text:
        return ""
    name = (style_name or "").strip()
    if name == "Title":
        return f"# {text}"
    if name.startswith("Heading"):
        try:
            level = int(name.split()[-1])
        except (ValueError, IndexError):
            level = 2
        level = max(1, min(level, 6))
        return f"{'#' * level} {text}"
    return text


def _table_to_markdown(rows: list[list[str]]) -> str:
    """Convert a list of row cell-texts to a markdown pipe table."""
    if not rows:
        return ""
    header = rows[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


class DocxLoader:
    """Loader for ``.docx`` Source files. Registered as ``"docx"``."""

    def load(self, path: Path) -> Iterable[Document]:
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        source_id = str(path.resolve())
        document_path = source_id
        document_id = make_document_id(source_id, document_path)

        # Encrypted detection BEFORE python-docx (which cannot distinguish
        # encrypted from corrupt — both raise PackageNotFoundError).
        if path.read_bytes()[:8].startswith(_OLE_MAGIC):
            yield Document(
                source_id=source_id,
                source_type="docx",
                document_id=document_id,
                document_path=document_path,
                document_sha256=sha256_text(""),
                title=path.stem,
                producer="user",
                raw_text="",
                meta={
                    "loader_verdict": LoaderVerdict.encrypted.value,
                    "loader_version": LOADER_VERSION,
                },
            )
            return

        from docx import Document as DocxFile
        from docx.opc.exceptions import PackageNotFoundError
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            docx_doc = DocxFile(str(path))
        except PackageNotFoundError as exc:
            raise ValueError(
                f"Could not read .docx file {path}: not a valid Office Open XML "
                f"document ({exc})."
            ) from exc

        blocks: list[str] = []
        for child in docx_doc.element.body.iterchildren():
            tag = child.tag
            if tag.endswith("}p"):
                para = Paragraph(child, docx_doc)
                style_name = para.style.name if para.style is not None else ""
                md = _para_to_markdown(style_name, para.text.strip())
                if md:
                    blocks.append(md)
            elif tag.endswith("}tbl"):
                table = Table(child, docx_doc)
                rows = [
                    [cell.text.strip() for cell in row.cells] for row in table.rows
                ]
                md = _table_to_markdown(rows)
                if md:
                    blocks.append(md)

        raw_text = "\n\n".join(blocks)

        non_ws_chars = len("".join(raw_text.split()))
        verdict = (
            LoaderVerdict.low_confidence
            if non_ws_chars < LOW_TEXT_THRESHOLD
            else LoaderVerdict.ok
        )

        title = path.stem
        for block in blocks:
            if block.startswith("#"):
                title = block.lstrip("#").strip() or title
                break

        yield Document(
            source_id=source_id,
            source_type="docx",
            document_id=document_id,
            document_path=document_path,
            document_sha256=sha256_text(raw_text),
            title=title,
            producer="user",
            raw_text=raw_text,
            meta={
                "loader_verdict": verdict.value,
                "loader_version": LOADER_VERSION,
            },
        )


# ---------------------------------------------------------------------------
# Register in the global registry (runs once on import).
# ---------------------------------------------------------------------------

register_loader("docx", DocxLoader())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestDocxLoader -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/aineverforget/loaders/docx.py tests/test_loaders.py
git commit -m "feat: add DocxLoader with markdown reconstruction

Headings -> '#'xlevel, tables -> pipe tables, paragraphs -> prose.
source_type='docx'; registered in the loader registry.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `DocxLoader` verdicts (empty / encrypted / corrupt)

**Files:**
- Modify: `src/aineverforget/loaders/docx.py` (already handles all three from Task 4)
- Test: `tests/test_loaders.py`

This task is test-only — Task 4's implementation already covers these paths; here we lock them with explicit tests.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_loaders.py`:

```python
class TestDocxLoaderVerdicts:
    @pytest.fixture
    def loader(self):
        from aineverforget.loaders.docx import DocxLoader
        return DocxLoader()

    def test_near_empty_is_low_confidence(self, loader, tmp_path: Path):
        from docx import Document as DocxFile
        d = DocxFile()
        d.add_paragraph("hi")
        p = tmp_path / "empty.docx"
        d.save(str(p))
        doc = list(loader.load(p))[0]
        assert doc.meta["loader_verdict"] == "low_confidence"

    def test_ole_magic_is_encrypted(self, loader, tmp_path: Path):
        p = tmp_path / "locked.docx"
        p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
        doc = list(loader.load(p))[0]
        assert doc.meta["loader_verdict"] == "encrypted"
        assert doc.raw_text == ""

    def test_corrupt_not_zip_raises(self, loader, tmp_path: Path):
        p = tmp_path / "broken.docx"
        p.write_bytes(b"this is not a zip or an ole file")
        with pytest.raises(ValueError, match="not a valid Office Open XML"):
            list(loader.load(p))
```

- [ ] **Step 2: Run tests to verify they pass (impl already present)**

Run: `.venv/bin/python -m pytest tests/test_loaders.py::TestDocxLoaderVerdicts -v`
Expected: PASS (3 passed). If any fail, fix `docx.py` until green — do not change the tests.

- [ ] **Step 3: Commit**

```bash
git add tests/test_loaders.py
git commit -m "test: lock DocxLoader empty/encrypted/corrupt verdicts

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Chunker routes `docx` through markdown strategy

**Files:**
- Modify: `src/aineverforget/chunking.py:100-106`
- Test: `tests/test_chunking_docx.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_chunking_docx.py`:

```python
from __future__ import annotations

from aineverforget.chunking import chunk_document
from aineverforget.config import load_settings
from aineverforget.identity import make_document_id, sha256_text
from aineverforget.models import Document


def _docx_document(raw_text: str) -> Document:
    source_id = "test://doc.docx"
    return Document(
        source_id=source_id,
        source_type="docx",
        document_id=make_document_id(source_id, source_id),
        document_path=source_id,
        document_sha256=sha256_text(raw_text),
        title="Doc",
        producer="user",
        raw_text=raw_text,
        meta={"loader_verdict": "ok"},
    )


def test_docx_routes_through_markdown_strategy():
    settings = load_settings()
    raw = "# Heading\n\nFirst para.\n\n## Sub\n\nSecond para.\n"
    docx_doc = _docx_document(raw)
    md_doc = docx_doc.model_copy(update={"source_type": "markdown"})

    docx_chunks = chunk_document(
        docx_doc, settings, ingest_generation=1, embedding_model="BAAI/bge-m3"
    )
    md_chunks = chunk_document(
        md_doc, settings, ingest_generation=1, embedding_model="BAAI/bge-m3"
    )

    assert len(docx_chunks) > 0
    # Same strategy => same number of chunks and same chunk text as markdown.
    assert len(docx_chunks) == len(md_chunks)
    assert [c.text for c in docx_chunks] == [c.text for c in md_chunks]
    # Provenance preserved.
    assert all(c.source_type == "docx" for c in docx_chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_chunking_docx.py -v`
Expected: FAILS — `docx` currently falls through to `_chunk_prose`, so chunk text/count differ from the markdown strategy.

- [ ] **Step 3: Implement**

In `src/aineverforget/chunking.py`, the dispatch currently reads:

```python
    if document.source_type == "markdown":
        return _chunk_markdown(
            document,
            settings,
            ingest_generation=ingest_generation,
            embedding_model=embedding_model,
            producer=resolved_producer,
        )
    elif document.source_type == "pdf":
```

Change the first condition to include `docx`:

```python
    if document.source_type in ("markdown", "docx"):
        return _chunk_markdown(
            document,
            settings,
            ingest_generation=ingest_generation,
            embedding_model=embedding_model,
            producer=resolved_producer,
        )
    elif document.source_type == "pdf":
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_chunking_docx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aineverforget/chunking.py tests/test_chunking_docx.py
git commit -m "feat: route docx source_type through the markdown chunker

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Ingest wire-in (register module, resolve_source, sniff downgrade)

**Files:**
- Modify: `src/aineverforget/ingest.py` — loader imports (~263-275), call-site (~327-328), `_ingest_one` signature (~368-369), dispatch (~374-385), verdict handling (~435-453)
- Test: `tests/test_loaders.py` (an ingest-free unit test on the verdict-downgrade logic is covered by Task 3; this task's behavior is verified by the manual smoke in Task 9, plus the existing ingest test suite must stay green)

> Note: ingest_paths talks to Qdrant, so it is exercised by the real-server smoke (Task 9), not a unit test. The edits below must keep `.venv/bin/python -m pytest` green (no Qdrant required for the non-live tests).

- [ ] **Step 1: Register the docx loader module**

In `src/aineverforget/ingest.py`, the loader self-registration block reads:

```python
    # Trigger loader self-registration by importing the loader submodules.
    # Each module registers itself into the global registry on import.
    import aineverforget.loaders.text  # noqa: F401
    import aineverforget.loaders.pdf   # noqa: F401
```

Add the docx import:

```python
    # Trigger loader self-registration by importing the loader submodules.
    # Each module registers itself into the global registry on import.
    import aineverforget.loaders.text  # noqa: F401
    import aineverforget.loaders.pdf   # noqa: F401
    import aineverforget.loaders.docx  # noqa: F401
```

- [ ] **Step 2: Import `resolve_source` instead of `infer_source_type`**

In the same import block, change:

```python
    from aineverforget.loaders import LoaderVerdict, get_loader, infer_source_type
```

to:

```python
    from aineverforget.loaders import LoaderVerdict, get_loader, resolve_source
```

- [ ] **Step 3: Update the `_ingest_one` call site**

Find where `_ingest_one` is invoked (it passes the loader callables). The arguments currently include:

```python
                loaders_get_loader=get_loader,
                loaders_infer_source_type=infer_source_type,
```

Change to:

```python
                loaders_get_loader=get_loader,
                loaders_resolve_source=resolve_source,
```

- [ ] **Step 4: Update the `_ingest_one` signature**

The parameter list currently includes:

```python
    loaders_get_loader: object,
    loaders_infer_source_type: object,
```

Change to:

```python
    loaders_get_loader: object,
    loaders_resolve_source: object,
```

- [ ] **Step 5: Update the dispatch body**

The dispatch currently reads:

```python
        try:
            source_type = loaders_infer_source_type(path)
        except ValueError as exc:
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.error,
                detail=str(exc),
            )

        loader = loaders_get_loader(source_type)
```

Change to capture the sniff flag:

```python
        try:
            source_type, sniffed_unknown = loaders_resolve_source(path)
        except ValueError as exc:
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.error,
                detail=str(exc),
            )

        loader = loaders_get_loader(source_type)
```

- [ ] **Step 6: Downgrade verdict for sniffed-unknown text**

The verdict-handling block currently reads:

```python
        loader_verdict_str = (
            loader_verdict_val.value
            if hasattr(loader_verdict_val, "value")
            else str(loader_verdict_val) if loader_verdict_val else None
        )

        if loader_verdict_str in ("encrypted", "scanned"):
```

Insert the downgrade between the two statements:

```python
        loader_verdict_str = (
            loader_verdict_val.value
            if hasattr(loader_verdict_val, "value")
            else str(loader_verdict_val) if loader_verdict_val else None
        )

        # An unknown-extension file that was force-read as text is flagged so
        # the CLI surfaces that it was ingested on a best-effort basis.
        if sniffed_unknown and loader_verdict_str == "ok":
            loader_verdict_str = "low_confidence"

        if loader_verdict_str in ("encrypted", "scanned"):
```

- [ ] **Step 7: Run the full non-live test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass (same count as before plus the new loader/chunking tests). No Qdrant needed.

- [ ] **Step 8: Commit**

```bash
git add src/aineverforget/ingest.py
git commit -m "feat: ingest uses resolve_source (docx + unknown-ext sniff)

Registers the docx loader; unknown extensions sniff text-in/binary-out;
sniffed-unknown text is flagged low_confidence.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Docs — SKILL.md classification + README

**Files:**
- Modify: `.claude/skills/ingest/SKILL.md` (STEP 1 classification)
- Modify: `README.md:3-5` and the formats note

- [ ] **Step 1: Classify `.docx` as direct in the ingest skill**

Open `.claude/skills/ingest/SKILL.md`, find STEP 1 where `.pdf` is classified as `direct` (pre-structured → knowledge-indexer, bypassing note-summarizer). Add `.docx` to that same direct branch so a `.docx` is indexed directly. Match the file's existing wording/format (e.g. wherever it lists `.pdf` as direct, add `.docx`).

- [ ] **Step 2: Update README supported formats**

In `README.md`, the intro currently says:

```markdown
aineverforget is a local-first, eval-gated personal knowledge base. It ingests
Markdown, text, and PDF sources into Qdrant, then searches the indexed corpus
with BGE-M3 dense and sparse embeddings.
```

Change the first sentence to include Word documents:

```markdown
aineverforget is a local-first, eval-gated personal knowledge base. It ingests
Markdown, text, PDF, and Word (.docx) sources into Qdrant, then searches the
indexed corpus with BGE-M3 dense and sparse embeddings.
```

- [ ] **Step 3: Document the sniff behavior**

In `README.md`, under the Basic CLI usage / ingest section, add a short note:

```markdown
Accepted file types: `.md`, `.txt`, `.markdown`, `.rst`, `.text`, `.pdf`, and
`.docx`. A file with any other extension is byte-sniffed: text-like content is
ingested as markdown (flagged `low_confidence`), while binary content is
rejected with a clear error. Use `--source-type` to force a type.
```

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/ingest/SKILL.md README.md
git commit -m "docs: document .docx ingest + unknown-extension sniff

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Real end-to-end verification (manual smoke)

**Files:** none (verification only). Requires Qdrant running (`docker compose up -d qdrant`) and a one-time BGE-M3 model load.

- [ ] **Step 1: Author three test files**

Run:

```bash
.venv/bin/python - <<'PY'
from docx import Document as DocxFile
from pathlib import Path
out = Path("/tmp/anf_smoke"); out.mkdir(exist_ok=True)
d = DocxFile(); d.add_heading("Q3 Planning", 1)
d.add_paragraph("We will ship the docx loader."); d.add_heading("Owners", 2)
t = d.add_table(rows=2, cols=2)
t.cell(0,0).text="Task"; t.cell(0,1).text="Owner"
t.cell(1,0).text="Docx loader"; t.cell(1,1).text="Bruno"
d.save(str(out/"plan.docx"))
(out/"notes.org").write_text("* Heading\nplain org notes about Qdrant\n", encoding="utf-8")
(out/"blob.bin").write_bytes(b"\x00\x01\x02binary garbage"*50)
print("wrote", list(out.iterdir()))
PY
```

- [ ] **Step 2: Ingest the `.docx`**

Run: `.venv/bin/aineverforget ingest /tmp/anf_smoke/plan.docx --tag smoke`
(the console script created by `pip install -e .`; there is no `__main__.py`, so `python -m aineverforget` will not work)
Expected: outcome `success`; chunk count > 0; verdict `ok`.

- [ ] **Step 3: Ingest the unknown-extension text file**

Run: `.venv/bin/aineverforget ingest /tmp/anf_smoke/notes.org --tag smoke`
Expected: outcome `success`; loader verdict surfaced as `low_confidence`.

- [ ] **Step 4: Ingest the binary file (must fail closed)**

Run: `.venv/bin/aineverforget ingest /tmp/anf_smoke/blob.bin --tag smoke`
Expected: outcome `error`; detail mentions "looks binary"; nothing indexed.

- [ ] **Step 5: Search confirms the docx content is retrievable**

Run: `.venv/bin/aineverforget search "who owns the docx loader" --limit 3`
Expected: a chunk from `plan.docx` (the Owners table / Bruno) appears, with `source_type=docx` in the citation.

- [ ] **Step 6: Record the result**

No commit (verification only). If any step deviates, open a follow-up rather than editing tests to pass.

---

## Self-review

**Spec coverage:**
- `.docx` loader (rich markdown) → Tasks 4, 5. ✓
- own `source_type="docx"` + provenance → Tasks 4, 6 (chunk asserts `source_type=="docx"`). ✓
- unknown-ext sniff (text→markdown flagged / binary→fail-closed, default-on) → Tasks 2, 3, 7. ✓
- chunk dispatch one-liner → Task 6. ✓
- ingest wire-in + low_confidence downgrade → Task 7. ✓
- python-docx dep + relock → Task 0. ✓
- SKILL.md direct classification + README → Task 8. ✓
- TDD with in-test docx fixtures, no binary blobs in repo → Tasks 4-6. ✓
- error table (ok/low_confidence/encrypted/error) → Tasks 4, 5, 9. ✓
- OCR + code-block guard explicitly deferred → not in plan (correct). ✓

**Placeholder scan:** none — every code/test step has complete code and exact commands.

**Type consistency:** `resolve_source(path) -> (source_type, sniffed_unknown)` defined Task 3, consumed Task 7 (`source_type, sniffed_unknown = loaders_resolve_source(path)`). `_looks_like_text(bytes) -> bool` defined Task 2, used Task 3. `DocxLoader` defined Task 4, imported in tests Tasks 4/5. `LoaderVerdict` values (`ok`/`low_confidence`/`encrypted`) match `loaders/__init__.py` enum. `make_document_id`/`sha256_text` signatures match identity.py. Chunk routing uses `source_type in ("markdown","docx")` consistently.
