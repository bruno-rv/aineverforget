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
