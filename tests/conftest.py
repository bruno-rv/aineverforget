"""Test fixtures for aineverforget tests.

Provides sample Document and Chunk instances for use across test modules.
Only stdlib + pydantic required — no qdrant, FlagEmbedding, or other heavy deps.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aineverforget.models import Chunk, Document, IngestState


# ---------------------------------------------------------------------------
# Sample constants
# ---------------------------------------------------------------------------

SAMPLE_SOURCE_ID = "/Users/test/notes"
SAMPLE_DOCUMENT_PATH = "/Users/test/notes/2024-01-15-meeting.md"
SAMPLE_DOCUMENT_SHA256 = "a" * 64  # 64-char hex string (placeholder)
SAMPLE_DOCUMENT_ID = "test-doc-001"
SAMPLE_INGESTED_AT = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_document() -> Document:
    """A minimal Document representing a markdown note."""
    return Document(
        source_id=SAMPLE_SOURCE_ID,
        source_type="markdown",
        document_id=SAMPLE_DOCUMENT_ID,
        document_path=SAMPLE_DOCUMENT_PATH,
        document_sha256=SAMPLE_DOCUMENT_SHA256,
        title="2024-01-15 Meeting Notes",
        producer="user",
        raw_text="# Meeting Notes\n\nDiscussed the architecture of aineverforget.\n",
        meta={"loader_verdict": "ok"},
    )


@pytest.fixture
def sample_chunk(sample_document: Document) -> Chunk:
    """A minimal Chunk derived from sample_document."""
    return Chunk(
        source_id=sample_document.source_id,
        source_type=sample_document.source_type,
        document_id=sample_document.document_id,
        document_path=sample_document.document_path,
        document_sha256=sample_document.document_sha256,
        ingest_generation=1,
        ingest_state=IngestState.active,
        title=sample_document.title,
        chunk_index=0,
        chunk_start_word=0,
        chunk_end_word=12,
        heading_path="# Meeting Notes",
        pdf_page=None,
        tags=["meeting", "architecture"],
        producer=sample_document.producer,
        ingested_at=SAMPLE_INGESTED_AT,
        loader_version="text:1.0",
        chunker_version="chunker:1.0",
        embedding_model="BAAI/bge-m3",
        text="# Meeting Notes\n\nDiscussed the architecture of aineverforget.",
    )


@pytest.fixture
def sample_chunk_pdf() -> Chunk:
    """A minimal Chunk from a PDF source (pdf_page set, heading_path None)."""
    return Chunk(
        source_id="/Users/test/docs",
        source_type="pdf",
        document_id="test-pdf-001",
        document_path="/Users/test/docs/report.pdf",
        document_sha256="b" * 64,
        ingest_generation=2,
        ingest_state=IngestState.pending,
        title="Annual Report 2024",
        chunk_index=3,
        chunk_start_word=440,
        chunk_end_word=660,
        heading_path=None,
        pdf_page=5,
        tags=["report", "2024"],
        producer="producer-x",
        ingested_at=SAMPLE_INGESTED_AT,
        loader_version="pdf:1.0",
        chunker_version="chunker:1.0",
        embedding_model="BAAI/bge-m3",
        text="Revenue grew 12% year-over-year driven by enterprise subscriptions.",
    )
