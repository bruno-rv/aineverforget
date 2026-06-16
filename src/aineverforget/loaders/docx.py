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
