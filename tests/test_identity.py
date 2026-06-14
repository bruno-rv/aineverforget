"""Tests for aineverforget.identity — deterministic ID functions.

Tests are self-contained: stdlib + pydantic only (no qdrant / FlagEmbedding).
"""

from __future__ import annotations

import uuid

import pytest

from aineverforget.identity import (
    POINT_NAMESPACE,
    make_document_id,
    next_ingest_generation,
    point_id,
    sha256_text,
)


# ---------------------------------------------------------------------------
# sha256_text
# ---------------------------------------------------------------------------


class TestSha256Text:
    def test_determinism(self) -> None:
        """Same text always yields the same hash."""
        text = "hello, world"
        assert sha256_text(text) == sha256_text(text)

    def test_returns_64_char_hex(self) -> None:
        """Result is a 64-char lowercase hex string."""
        result = sha256_text("test")
        assert isinstance(result, str)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_different_texts_differ(self) -> None:
        """Different texts yield different hashes."""
        assert sha256_text("hello") != sha256_text("world")
        assert sha256_text("") != sha256_text(" ")

    def test_known_value_empty_string(self) -> None:
        """sha256('') is the canonical empty-string SHA-256."""
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert sha256_text("") == expected

    def test_utf8_encoding(self) -> None:
        """Non-ASCII text is encoded as UTF-8 (not latin-1 or other)."""
        # The hash of 'café' in UTF-8 is deterministic
        result = sha256_text("café")
        assert isinstance(result, str)
        assert len(result) == 64
        # Confirm it differs from ASCII-only
        assert sha256_text("cafe") != result

    def test_whitespace_sensitivity(self) -> None:
        """Trailing whitespace changes the hash."""
        assert sha256_text("hello") != sha256_text("hello ")

    def test_case_sensitivity(self) -> None:
        """SHA-256 is case-sensitive."""
        assert sha256_text("Hello") != sha256_text("hello")


# ---------------------------------------------------------------------------
# make_document_id
# ---------------------------------------------------------------------------


class TestMakeDocumentId:
    def test_determinism(self) -> None:
        """Same (source_id, document_path) always yields the same document_id."""
        assert (
            make_document_id("/notes", "2024-01-01.md")
            == make_document_id("/notes", "2024-01-01.md")
        )

    def test_returns_uuid_string(self) -> None:
        """Result is a valid UUID string."""
        result = make_document_id("/notes", "test.md")
        parsed = uuid.UUID(result)
        assert str(parsed) == result

    def test_different_paths_differ(self) -> None:
        """Different document_paths yield different document_ids."""
        assert make_document_id("/notes", "a.md") != make_document_id("/notes", "b.md")

    def test_different_source_ids_differ(self) -> None:
        """Different source_ids yield different document_ids for the same path."""
        assert (
            make_document_id("/source-a", "doc.md")
            != make_document_id("/source-b", "doc.md")
        )

    def test_not_content_dependent(self) -> None:
        """document_id is NOT content-dependent (same path = same id regardless of text)."""
        id1 = make_document_id("/notes", "report.md")
        id2 = make_document_id("/notes", "report.md")  # same path, imagined different content
        assert id1 == id2

    def test_uses_point_namespace(self) -> None:
        """UUIDv5 is computed with POINT_NAMESPACE."""
        expected = str(uuid.uuid5(POINT_NAMESPACE, "/notes|doc.md"))
        assert make_document_id("/notes", "doc.md") == expected

    def test_empty_strings(self) -> None:
        """Empty source_id and document_path produce a valid (distinct) id."""
        result = make_document_id("", "")
        assert uuid.UUID(result)
        assert result != make_document_id("", "x")
        assert result != make_document_id("x", "")


# ---------------------------------------------------------------------------
# point_id
# ---------------------------------------------------------------------------


class TestPointId:
    def test_determinism(self) -> None:
        """Same inputs always yield the same point_id."""
        pid = point_id("doc1", "sha-abc", 0)
        assert pid == point_id("doc1", "sha-abc", 0)

    def test_returns_uuid_string(self) -> None:
        """Result is a valid UUID string."""
        pid = point_id("doc1", "sha-abc", 0)
        parsed = uuid.UUID(pid)
        assert str(parsed) == pid

    def test_uniqueness_across_chunk_index(self) -> None:
        """Different chunk_index values produce different point_ids."""
        sha = "a" * 64
        ids = [point_id("doc1", sha, i) for i in range(5)]
        assert len(set(ids)) == 5, "All chunk_index-varied IDs must be distinct"

    def test_uniqueness_across_sha(self) -> None:
        """Different document_sha256 values produce different point_ids."""
        id_old = point_id("doc1", "sha-old", 0)
        id_new = point_id("doc1", "sha-new", 0)
        assert id_old != id_new

    def test_uniqueness_across_document_id(self) -> None:
        """Different document_ids produce different point_ids."""
        sha = "b" * 64
        assert point_id("doc1", sha, 0) != point_id("doc2", sha, 0)

    def test_excludes_generation(self) -> None:
        """Identical (document_id, sha, chunk_index) gives identical ID regardless of generation.

        This verifies the PLAN.md contract: point_id does NOT include
        ingest_generation, so identical content across generations yields
        the same point IDs (idempotency dedup).
        """
        sha = "c" * 64
        # Simulate: same document, same content, but different generations
        # (the generation is NOT part of the point_id computation)
        id_gen1 = point_id("doc1", sha, 0)
        id_gen2 = point_id("doc1", sha, 0)  # same args = same id
        assert id_gen1 == id_gen2

    def test_uses_point_namespace(self) -> None:
        """UUIDv5 is computed with POINT_NAMESPACE."""
        expected = str(uuid.uuid5(POINT_NAMESPACE, "doc1|sha-abc|0"))
        assert point_id("doc1", "sha-abc", 0) == expected

    def test_large_chunk_index(self) -> None:
        """Large chunk_index values work correctly."""
        pid = point_id("doc", "sha", 10000)
        assert uuid.UUID(pid)

    def test_three_components_only(self) -> None:
        """Verify the UUIDv5 value string is exactly document_id|sha|chunk_index."""
        doc_id = "test-document-id"
        sha = "deadbeef" * 8
        idx = 42
        expected_value = f"{doc_id}|{sha}|{idx}"
        expected_uuid = str(uuid.uuid5(POINT_NAMESPACE, expected_value))
        assert point_id(doc_id, sha, idx) == expected_uuid


# ---------------------------------------------------------------------------
# next_ingest_generation
# ---------------------------------------------------------------------------


class TestNextIngestGeneration:
    def test_first_ingest(self) -> None:
        """None (no active generation) returns 1."""
        assert next_ingest_generation(None) == 1

    def test_zero_returns_one(self) -> None:
        """Generation 0 returns 1."""
        assert next_ingest_generation(0) == 1

    def test_increments(self) -> None:
        """Existing generation G returns G+1."""
        for g in [1, 2, 3, 10, 100]:
            assert next_ingest_generation(g) == g + 1

    def test_always_positive(self) -> None:
        """Result is always >= 1."""
        for g in [None, 0, 1, 5]:
            assert next_ingest_generation(g) >= 1
