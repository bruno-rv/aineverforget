"""Tests for aineverforget.models — data contracts.

Tests are self-contained: stdlib + pydantic only (no qdrant / FlagEmbedding).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from aineverforget.identity import POINT_NAMESPACE
from aineverforget.models import (
    Chunk,
    Document,
    IngestState,
    RetrievedChunk,
    SearchResult,
)


# ---------------------------------------------------------------------------
# IngestState enum
# ---------------------------------------------------------------------------


class TestIngestState:
    def test_values(self) -> None:
        """All three state values are present."""
        assert IngestState.pending.value == "pending"
        assert IngestState.active.value == "active"
        assert IngestState.failed.value == "failed"

    def test_is_str_enum(self) -> None:
        """IngestState is a str enum (coercible from string)."""
        assert IngestState("pending") is IngestState.pending
        assert IngestState("active") is IngestState.active
        assert IngestState("failed") is IngestState.failed

    def test_invalid_raises(self) -> None:
        """Unknown state strings raise ValueError."""
        with pytest.raises(ValueError):
            IngestState("unknown")

    def test_string_comparison(self) -> None:
        """str(IngestState.active) behaves as expected for payload serialization."""
        assert IngestState.active == "active"
        assert IngestState.pending != "active"


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


class TestDocument:
    def test_construction(self, sample_document: Document) -> None:
        """Document is constructed with all fields."""
        assert sample_document.source_id == "/Users/test/notes"
        assert sample_document.source_type == "markdown"
        assert sample_document.document_sha256 == "a" * 64

    def test_meta_default_empty(self) -> None:
        """meta defaults to an empty dict."""
        doc = Document(
            source_id="src",
            source_type="markdown",
            document_id="doc1",
            document_path="/path/to/doc.md",
            document_sha256="a" * 64,
            title="Test",
            producer="user",
            raw_text="hello",
        )
        assert doc.meta == {}

    def test_meta_preserved(self, sample_document: Document) -> None:
        """meta dict is preserved as-is."""
        assert sample_document.meta["loader_verdict"] == "ok"


# ---------------------------------------------------------------------------
# Chunk.point_id property
# ---------------------------------------------------------------------------


class TestChunkPointId:
    def test_point_id_is_uuid_string(self, sample_chunk: Chunk) -> None:
        """point_id returns a valid UUID string."""
        pid = sample_chunk.point_id
        parsed = uuid.UUID(pid)
        assert str(parsed) == pid

    def test_point_id_deterministic(self, sample_chunk: Chunk) -> None:
        """Same Chunk always yields the same point_id."""
        assert sample_chunk.point_id == sample_chunk.point_id

    def test_point_id_uses_three_components(self, sample_chunk: Chunk) -> None:
        """point_id is UUIDv5(NAMESPACE, document_id|sha256|chunk_index)."""
        expected = str(
            uuid.uuid5(
                POINT_NAMESPACE,
                f"{sample_chunk.document_id}|{sample_chunk.document_sha256}|{sample_chunk.chunk_index}",
            )
        )
        assert sample_chunk.point_id == expected

    def test_point_id_varies_with_chunk_index(self, sample_chunk: Chunk) -> None:
        """Different chunk_index → different point_id."""
        chunk2 = sample_chunk.model_copy(update={"chunk_index": 1})
        assert sample_chunk.point_id != chunk2.point_id

    def test_point_id_varies_with_sha(self, sample_chunk: Chunk) -> None:
        """Different document_sha256 → different point_id."""
        chunk2 = sample_chunk.model_copy(update={"document_sha256": "b" * 64})
        assert sample_chunk.point_id != chunk2.point_id

    def test_point_id_stable_across_generations(self, sample_chunk: Chunk) -> None:
        """Changing ingest_generation does NOT change point_id (generation excluded)."""
        chunk_gen2 = sample_chunk.model_copy(update={"ingest_generation": 2})
        assert sample_chunk.point_id == chunk_gen2.point_id

    def test_point_id_stable_across_states(self, sample_chunk: Chunk) -> None:
        """Changing ingest_state does NOT change point_id."""
        chunk_pending = sample_chunk.model_copy(update={"ingest_state": IngestState.pending})
        chunk_failed = sample_chunk.model_copy(update={"ingest_state": IngestState.failed})
        assert sample_chunk.point_id == chunk_pending.point_id
        assert sample_chunk.point_id == chunk_failed.point_id

    def test_point_id_pdf_chunk(self, sample_chunk_pdf: Chunk) -> None:
        """PDF chunk point_id is also deterministic and valid."""
        pid = sample_chunk_pdf.point_id
        assert uuid.UUID(pid)


# ---------------------------------------------------------------------------
# Chunk.to_payload — round-trip and field presence
# ---------------------------------------------------------------------------


class TestChunkToPayload:
    EXPECTED_KEYS = {
        "source_id",
        "source_type",
        "document_id",
        "document_path",
        "document_sha256",
        "ingest_generation",
        "ingest_state",
        "title",
        "chunk_index",
        "chunk_start_word",
        "chunk_end_word",
        "heading_path",
        "pdf_page",
        "tags",
        "producer",
        "ingested_at",
        "loader_version",
        "chunker_version",
        "embedding_model",
        "text",
    }

    def test_all_20_fields_present(self, sample_chunk: Chunk) -> None:
        """to_payload() returns all 20 canonical payload fields."""
        payload = sample_chunk.to_payload()
        assert set(payload.keys()) == self.EXPECTED_KEYS

    def test_ingest_state_is_string(self, sample_chunk: Chunk) -> None:
        """ingest_state is serialized as its .value string, not the enum."""
        payload = sample_chunk.to_payload()
        assert isinstance(payload["ingest_state"], str)
        assert payload["ingest_state"] == "active"

    def test_pending_state_serialized(self, sample_chunk: Chunk) -> None:
        """pending state serialized as 'pending'."""
        chunk = sample_chunk.model_copy(update={"ingest_state": IngestState.pending})
        payload = chunk.to_payload()
        assert payload["ingest_state"] == "pending"

    def test_failed_state_serialized(self, sample_chunk: Chunk) -> None:
        """failed state serialized as 'failed'."""
        chunk = sample_chunk.model_copy(update={"ingest_state": IngestState.failed})
        payload = chunk.to_payload()
        assert payload["ingest_state"] == "failed"

    def test_ingested_at_is_iso_string(self, sample_chunk: Chunk) -> None:
        """ingested_at is an ISO-8601 string (for Qdrant datetime payload index)."""
        payload = sample_chunk.to_payload()
        ingested_at = payload["ingested_at"]
        assert isinstance(ingested_at, str)
        # Must be parseable as datetime
        parsed = datetime.fromisoformat(ingested_at)
        assert parsed.tzinfo is not None, "ingested_at must include timezone info"

    def test_heading_path_preserved(self, sample_chunk: Chunk) -> None:
        """heading_path is included when set."""
        payload = sample_chunk.to_payload()
        assert payload["heading_path"] == "# Meeting Notes"

    def test_heading_path_none_for_prose(self, sample_chunk: Chunk) -> None:
        """heading_path is None when not set."""
        chunk = sample_chunk.model_copy(update={"heading_path": None})
        payload = chunk.to_payload()
        assert payload["heading_path"] is None

    def test_pdf_page_preserved(self, sample_chunk_pdf: Chunk) -> None:
        """pdf_page is included for PDF chunks."""
        payload = sample_chunk_pdf.to_payload()
        assert payload["pdf_page"] == 5

    def test_pdf_page_none_for_markdown(self, sample_chunk: Chunk) -> None:
        """pdf_page is None for markdown chunks."""
        payload = sample_chunk.to_payload()
        assert payload["pdf_page"] is None

    def test_tags_is_list(self, sample_chunk: Chunk) -> None:
        """tags is serialized as a list (not a set or tuple)."""
        payload = sample_chunk.to_payload()
        assert isinstance(payload["tags"], list)
        assert set(payload["tags"]) == {"meeting", "architecture"}

    def test_empty_tags(self, sample_chunk: Chunk) -> None:
        """Empty tags serialized as empty list."""
        chunk = sample_chunk.model_copy(update={"tags": []})
        payload = chunk.to_payload()
        assert payload["tags"] == []

    def test_field_values_match(self, sample_chunk: Chunk) -> None:
        """All non-transformed field values match the Chunk attributes."""
        payload = sample_chunk.to_payload()
        assert payload["source_id"] == sample_chunk.source_id
        assert payload["source_type"] == sample_chunk.source_type
        assert payload["document_id"] == sample_chunk.document_id
        assert payload["document_path"] == sample_chunk.document_path
        assert payload["document_sha256"] == sample_chunk.document_sha256
        assert payload["ingest_generation"] == sample_chunk.ingest_generation
        assert payload["title"] == sample_chunk.title
        assert payload["chunk_index"] == sample_chunk.chunk_index
        assert payload["chunk_start_word"] == sample_chunk.chunk_start_word
        assert payload["chunk_end_word"] == sample_chunk.chunk_end_word
        assert payload["producer"] == sample_chunk.producer
        assert payload["loader_version"] == sample_chunk.loader_version
        assert payload["chunker_version"] == sample_chunk.chunker_version
        assert payload["embedding_model"] == sample_chunk.embedding_model
        assert payload["text"] == sample_chunk.text

    def test_round_trip_via_from_payload(self, sample_chunk: Chunk) -> None:
        """Payload dict can reconstruct a Chunk with identical field values.

        Note: Chunk(ingest_state=payload["ingest_state"], ingested_at=payload["ingested_at"])
        requires coercion — validates that the serialized forms are parseable.
        """
        payload = sample_chunk.to_payload()
        # Reconstruct — this tests that the serialized values are valid inputs
        rebuilt = Chunk(**payload)
        assert rebuilt.ingest_state == sample_chunk.ingest_state
        assert rebuilt.ingested_at == sample_chunk.ingested_at
        assert rebuilt.point_id == sample_chunk.point_id

    def test_no_extra_fields(self, sample_chunk: Chunk) -> None:
        """to_payload() returns exactly the 20 schema fields, no extras."""
        payload = sample_chunk.to_payload()
        extra = set(payload.keys()) - self.EXPECTED_KEYS
        assert extra == set(), f"Unexpected extra fields: {extra}"


# ---------------------------------------------------------------------------
# Chunk ingested_at validation
# ---------------------------------------------------------------------------


class TestChunkIngestedAt:
    def test_accepts_aware_datetime(self) -> None:
        """Accepts an aware datetime."""
        dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        chunk = Chunk(
            source_id="s",
            source_type="markdown",
            document_id="d",
            document_path="/p",
            document_sha256="a" * 64,
            ingest_generation=1,
            ingest_state=IngestState.active,
            title="T",
            chunk_index=0,
            chunk_start_word=0,
            chunk_end_word=10,
            producer="user",
            ingested_at=dt,
            loader_version="text:1.0",
            chunker_version="chunker:1.0",
            embedding_model="BAAI/bge-m3",
            text="Hello world",
        )
        assert chunk.ingested_at.tzinfo is not None

    def test_naive_datetime_gets_utc(self) -> None:
        """A naive datetime is coerced to UTC-aware."""
        dt_naive = datetime(2024, 1, 1, 12, 0, 0)
        chunk = Chunk(
            source_id="s",
            source_type="markdown",
            document_id="d",
            document_path="/p",
            document_sha256="a" * 64,
            ingest_generation=1,
            ingest_state=IngestState.active,
            title="T",
            chunk_index=0,
            chunk_start_word=0,
            chunk_end_word=10,
            producer="user",
            ingested_at=dt_naive,
            loader_version="text:1.0",
            chunker_version="chunker:1.0",
            embedding_model="BAAI/bge-m3",
            text="Hello world",
        )
        assert chunk.ingested_at.tzinfo is not None

    def test_iso_string_accepted(self) -> None:
        """ISO-8601 string is accepted and parsed."""
        chunk = Chunk(
            source_id="s",
            source_type="markdown",
            document_id="d",
            document_path="/p",
            document_sha256="a" * 64,
            ingest_generation=1,
            ingest_state=IngestState.active,
            title="T",
            chunk_index=0,
            chunk_start_word=0,
            chunk_end_word=10,
            producer="user",
            ingested_at="2024-01-15T10:30:00+00:00",
            loader_version="text:1.0",
            chunker_version="chunker:1.0",
            embedding_model="BAAI/bge-m3",
            text="Hello world",
        )
        assert isinstance(chunk.ingested_at, datetime)
        assert chunk.ingested_at.year == 2024


# ---------------------------------------------------------------------------
# SearchResult envelope
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_construction(self) -> None:
        """SearchResult can be constructed with all fields."""
        result = SearchResult(
            query="What is the architecture?",
            candidates=[],
            dense_hits=0,
            sparse_hits=0,
            candidate_count=0,
            citationable_count=0,
        )
        assert result.query == "What is the architecture?"
        assert result.candidates == []
        assert result.dense_hits == 0

    def test_retriever_gate_fields_present(self) -> None:
        """SearchResult carries the four gate fields needed by the Orchestrator."""
        result = SearchResult(
            query="test",
            candidates=[],
            dense_hits=5,
            sparse_hits=3,
            candidate_count=8,
            citationable_count=7,
        )
        # Gate: candidate_count >= 1 AND (dense_hits >= 1 OR sparse_hits >= 1)
        #        AND citationable_count >= 1
        assert result.candidate_count == 8
        assert result.dense_hits == 5
        assert result.sparse_hits == 3
        assert result.citationable_count == 7

    def test_model_dump_is_json_serializable(self) -> None:
        """SearchResult.model_dump() produces a JSON-serializable dict."""
        import json

        result = SearchResult(
            query="test query",
            candidates=[],
            dense_hits=2,
            sparse_hits=1,
            candidate_count=3,
            citationable_count=3,
        )
        dumped = result.model_dump()
        # Should not raise
        json.dumps(dumped)
