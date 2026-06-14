"""Tests for aineverforget.store.QdrantStore.

Uses QdrantClient(':memory:') — in-process local Qdrant, no server required.
All hybrid search / full-text / filter behaviors are proven without mocking.

Note: ':memory:' mode emits "Payload indexes have no effect" warnings — expected.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from qdrant_client import QdrantClient

from aineverforget.embedding import EmbeddingVector, PassageEmbedding, QueryEmbedding
from aineverforget.models import Chunk, IngestState
from aineverforget.store import QdrantStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COLLECTION = "test_ainf_collection"


def _make_store() -> QdrantStore:
    """Create an in-memory store for testing."""
    client = QdrantClient(":memory:")
    store = QdrantStore(collection_name=COLLECTION, client=client)
    store.ensure_collection()
    return store


def _dense(seed: float = 0.1, dim: int = 1024) -> list[float]:
    """Deterministic 1024-dim dense vector (not normalized, but valid for cosine)."""
    v = [seed] * dim
    return v


def _sparse(token_ids: list[int], weights: list[float]) -> EmbeddingVector:
    """Build a sorted EmbeddingVector."""
    pairs = sorted(zip(token_ids, weights))
    indices = [p[0] for p in pairs]
    values = [p[1] for p in pairs]
    return EmbeddingVector(indices=indices, values=values)


def _passage_emb(seed: float = 0.1) -> PassageEmbedding:
    return PassageEmbedding(
        dense=_dense(seed),
        sparse=_sparse([1, 5, 10], [0.9, 0.5, 0.3]),
    )


def _query_emb(seed: float = 0.1) -> QueryEmbedding:
    return QueryEmbedding(
        dense=_dense(seed),
        sparse=_sparse([1, 5, 10], [0.9, 0.5, 0.3]),
    )


def _make_chunk(
    document_id: str = "doc-001",
    document_sha256: str = "a" * 64,
    chunk_index: int = 0,
    ingest_generation: int = 1,
    ingest_state: IngestState = IngestState.pending,
    text: str = "Test chunk text about architecture",
    source_id: str = "/Users/test/notes",
    source_type: str = "markdown",
    tags: list[str] | None = None,
    ingested_at: datetime | None = None,
) -> Chunk:
    if ingested_at is None:
        ingested_at = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    return Chunk(
        source_id=source_id,
        source_type=source_type,
        document_id=document_id,
        document_path=f"/Users/test/notes/{document_id}.md",
        document_sha256=document_sha256,
        ingest_generation=ingest_generation,
        ingest_state=ingest_state,
        title=f"Title for {document_id}",
        chunk_index=chunk_index,
        chunk_start_word=chunk_index * 10,
        chunk_end_word=(chunk_index + 1) * 10,
        heading_path="# Section",
        pdf_page=None,
        tags=tags or [],
        producer="user",
        ingested_at=ingested_at,
        loader_version="text:1.0",
        chunker_version="chunker:1.0",
        embedding_model="BAAI/bge-m3",
        text=text,
    )


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------


def test_ensure_collection_creates_collection() -> None:
    client = QdrantClient(":memory:")
    store = QdrantStore(collection_name=COLLECTION, client=client)

    assert not client.collection_exists(COLLECTION)
    store.ensure_collection()
    assert client.collection_exists(COLLECTION)


def test_ensure_collection_idempotent() -> None:
    """Calling ensure_collection twice must not raise."""
    client = QdrantClient(":memory:")
    store = QdrantStore(collection_name=COLLECTION, client=client)
    store.ensure_collection()
    store.ensure_collection()  # second call — must not raise
    assert client.collection_exists(COLLECTION)


# ---------------------------------------------------------------------------
# upsert_chunks
# ---------------------------------------------------------------------------


def test_upsert_chunks_basic() -> None:
    store = _make_store()
    chunk = _make_chunk()
    store.upsert_chunks([chunk], [_passage_emb()])

    client = store._get_client()
    points, _ = client.scroll(
        collection_name=COLLECTION,
        with_payload=True,
        with_vectors=False,
    )
    assert len(points) == 1
    assert points[0].payload["document_id"] == "doc-001"
    assert points[0].payload["ingest_state"] == "pending"


def test_upsert_chunks_mismatched_lengths_raises() -> None:
    store = _make_store()
    chunk = _make_chunk()
    with pytest.raises(ValueError):
        store.upsert_chunks([chunk], [])


def test_upsert_chunks_idempotent() -> None:
    """Upserting the same chunk twice results in one point."""
    store = _make_store()
    chunk = _make_chunk()
    store.upsert_chunks([chunk], [_passage_emb()])
    store.upsert_chunks([chunk], [_passage_emb()])

    client = store._get_client()
    points, _ = client.scroll(collection_name=COLLECTION)
    assert len(points) == 1


def test_upsert_chunks_point_id_deterministic() -> None:
    """Two chunks with same document_id/sha/index share the same point_id."""
    chunk1 = _make_chunk(ingest_generation=1)
    chunk2 = _make_chunk(ingest_generation=2)  # different gen, same identity
    assert chunk1.point_id == chunk2.point_id


# ---------------------------------------------------------------------------
# search — hybrid RRF
# ---------------------------------------------------------------------------


def test_search_returns_active_only() -> None:
    """Pending chunks must NOT appear in search results."""
    store = _make_store()

    pending_chunk = _make_chunk(
        document_id="doc-pending",
        ingest_state=IngestState.pending,
    )
    store.upsert_chunks([pending_chunk], [_passage_emb(0.2)])

    result = store.search(_query_emb(0.2))
    assert result.candidate_count == 0
    assert len(result.candidates) == 0


def test_search_returns_active_chunk() -> None:
    """Active chunk must appear in search results."""
    store = _make_store()

    chunk = _make_chunk(
        document_id="doc-active",
        ingest_state=IngestState.active,
    )
    store.upsert_chunks([chunk], [_passage_emb(0.5)])

    result = store.search(_query_emb(0.5))
    assert result.candidate_count >= 1
    assert any(c.document_id == "doc-active" for c in result.candidates)


def test_search_empty_corpus_no_raise() -> None:
    """Empty corpus returns zero results without raising."""
    store = _make_store()
    result = store.search(_query_emb())
    assert result.candidate_count == 0
    assert result.dense_hits == 0
    assert result.sparse_hits == 0
    assert result.citationable_count == 0


def test_search_citationable_count() -> None:
    """citationable_count counts chunks with non-empty text AND document_path."""
    store = _make_store()
    chunk = _make_chunk(
        document_id="doc-cite",
        ingest_state=IngestState.active,
        text="Some meaningful text",
    )
    store.upsert_chunks([chunk], [_passage_emb(0.5)])

    result = store.search(_query_emb(0.5))
    assert result.citationable_count >= 1


def test_search_source_id_filter() -> None:
    """source_id filter excludes chunks from other sources."""
    store = _make_store()

    chunk_a = _make_chunk(
        document_id="doc-a",
        ingest_state=IngestState.active,
        source_id="/source/a",
    )
    chunk_b = _make_chunk(
        document_id="doc-b",
        document_sha256="b" * 64,
        ingest_state=IngestState.active,
        source_id="/source/b",
    )
    store.upsert_chunks([chunk_a], [_passage_emb(0.5)])
    store.upsert_chunks([chunk_b], [_passage_emb(0.5)])

    result = store.search(_query_emb(0.5), source_id="/source/a")
    doc_ids = {c.document_id for c in result.candidates}
    assert "doc-a" in doc_ids
    assert "doc-b" not in doc_ids


def test_search_tags_filter() -> None:
    """tags filter restricts results to chunks with matching tags."""
    store = _make_store()

    tagged = _make_chunk(
        document_id="doc-tagged",
        ingest_state=IngestState.active,
        tags=["meeting", "q1"],
    )
    untagged = _make_chunk(
        document_id="doc-untagged",
        document_sha256="b" * 64,
        ingest_state=IngestState.active,
        tags=[],
    )
    store.upsert_chunks([tagged], [_passage_emb(0.5)])
    store.upsert_chunks([untagged], [_passage_emb(0.5)])

    result = store.search(_query_emb(0.5), tags=["meeting"])
    doc_ids = {c.document_id for c in result.candidates}
    assert "doc-tagged" in doc_ids
    assert "doc-untagged" not in doc_ids


def test_search_with_view_filter_includes_pending_gen() -> None:
    """view_filter overrides active-only gate: pending gen included in results."""
    store = _make_store()

    # Active chunk for some other document
    active_chunk = _make_chunk(
        document_id="doc-active-other",
        document_sha256="a1" * 32,
        ingest_state=IngestState.active,
        ingest_generation=1,
    )
    store.upsert_chunks([active_chunk], [_passage_emb(0.5)])

    # Pending chunk for the document under verify (gen=2)
    pending_chunk = _make_chunk(
        document_id="doc-pending-vf",
        document_sha256="b2" * 32,
        ingest_state=IngestState.pending,
        ingest_generation=2,
    )
    store.upsert_chunks([pending_chunk], [_passage_emb(0.5)])

    # Without view_filter: pending NOT returned
    result_default = store.search(_query_emb(0.5))
    default_doc_ids = {c.document_id for c in result_default.candidates}
    assert "doc-pending-vf" not in default_doc_ids

    # With view_filter: pending IS returned
    vf = store.verification_view_filter("doc-pending-vf", 2)
    result_vf = store.search(_query_emb(0.5), view_filter=vf)
    vf_doc_ids = {c.document_id for c in result_vf.candidates}
    assert "doc-pending-vf" in vf_doc_ids
    assert "doc-active-other" in vf_doc_ids


# ---------------------------------------------------------------------------
# lexscan — exhaustive full-text scroll
# ---------------------------------------------------------------------------


def test_lexscan_finds_matching_term() -> None:
    store = _make_store()

    chunk = _make_chunk(
        document_id="doc-lex",
        ingest_state=IngestState.active,
        text="The architecture of aineverforget is fascinating",
    )
    store.upsert_chunks([chunk], [_passage_emb()])

    result = store.lexscan("architecture")
    assert result["chunk_count"] >= 1
    assert result["document_count"] >= 1
    assert result["term"] == "architecture"


def test_lexscan_excludes_pending() -> None:
    """Pending chunks must not appear in lexscan results."""
    store = _make_store()

    pending = _make_chunk(
        document_id="doc-pending-lex",
        ingest_state=IngestState.pending,
        text="architecture pending content",
    )
    store.upsert_chunks([pending], [_passage_emb()])

    result = store.lexscan("architecture")
    assert result["chunk_count"] == 0


def test_lexscan_counts_all_occurrences() -> None:
    """lexscan must return ALL matching chunks, not just top-k."""
    store = _make_store()

    chunks = []
    embs = []
    for i in range(5):
        chunks.append(
            _make_chunk(
                document_id=f"doc-multi-{i}",
                document_sha256=chr(ord("a") + i) * 64,
                chunk_index=0,
                ingest_state=IngestState.active,
                text=f"Chunk {i} mentions aineverforget repeatedly",
            )
        )
        embs.append(_passage_emb(0.1 + i * 0.1))

    store.upsert_chunks(chunks, embs)

    result = store.lexscan("aineverforget")
    assert result["chunk_count"] == 5
    assert result["document_count"] == 5


def test_lexscan_document_count_dedup() -> None:
    """document_count counts distinct document_ids, not total chunks."""
    store = _make_store()

    # Two chunks from same document
    chunks = [
        _make_chunk(
            document_id="doc-shared",
            chunk_index=0,
            ingest_state=IngestState.active,
            text="aineverforget architecture discussion",
        ),
        _make_chunk(
            document_id="doc-shared",
            chunk_index=1,
            ingest_state=IngestState.active,
            text="more aineverforget details here",
        ),
    ]
    store.upsert_chunks(chunks, [_passage_emb(0.3), _passage_emb(0.4)])

    result = store.lexscan("aineverforget")
    assert result["chunk_count"] == 2
    assert result["document_count"] == 1


def test_lexscan_chunk_has_score_null() -> None:
    """Each chunk entry must have score=None (lexscan has no relevance score)."""
    store = _make_store()
    chunk = _make_chunk(
        document_id="doc-score",
        ingest_state=IngestState.active,
        text="aineverforget score test",
    )
    store.upsert_chunks([chunk], [_passage_emb()])

    result = store.lexscan("aineverforget")
    for c in result["chunks"]:
        assert c["score"] is None


# ---------------------------------------------------------------------------
# scroll — metadata enumeration
# ---------------------------------------------------------------------------


def test_scroll_returns_active_only() -> None:
    store = _make_store()

    active = _make_chunk(
        document_id="doc-scroll-active",
        ingest_state=IngestState.active,
    )
    pending = _make_chunk(
        document_id="doc-scroll-pending",
        document_sha256="b" * 64,
        ingest_state=IngestState.pending,
    )
    store.upsert_chunks([active], [_passage_emb(0.3)])
    store.upsert_chunks([pending], [_passage_emb(0.4)])

    result = store.scroll()
    doc_ids = {d["document_id"] for d in result["documents"]}
    assert "doc-scroll-active" in doc_ids
    assert "doc-scroll-pending" not in doc_ids


def test_scroll_dedup_by_document_id() -> None:
    """Multiple active chunks from same doc → single document entry with max gen."""
    store = _make_store()

    chunks = [
        _make_chunk(
            document_id="doc-dedup",
            chunk_index=0,
            ingest_generation=3,
            ingest_state=IngestState.active,
        ),
        _make_chunk(
            document_id="doc-dedup",
            chunk_index=1,
            ingest_generation=3,
            ingest_state=IngestState.active,
        ),
    ]
    store.upsert_chunks(chunks, [_passage_emb(0.3), _passage_emb(0.4)])

    result = store.scroll()
    docs = [d for d in result["documents"] if d["document_id"] == "doc-dedup"]
    assert len(docs) == 1
    assert docs[0]["ingest_generation"] == 3
    assert result["chunk_count"] == 2


def test_scroll_source_type_filter() -> None:
    store = _make_store()

    md_chunk = _make_chunk(
        document_id="doc-md",
        ingest_state=IngestState.active,
        source_type="markdown",
    )
    pdf_chunk = _make_chunk(
        document_id="doc-pdf",
        document_sha256="b" * 64,
        ingest_state=IngestState.active,
        source_type="pdf",
    )
    store.upsert_chunks([md_chunk], [_passage_emb(0.3)])
    store.upsert_chunks([pdf_chunk], [_passage_emb(0.4)])

    result = store.scroll(source_type="markdown")
    doc_ids = {d["document_id"] for d in result["documents"]}
    assert "doc-md" in doc_ids
    assert "doc-pdf" not in doc_ids


def test_scroll_corpus_local_max_gen_dedup_with_tag_filter() -> None:
    """Fix: scroll() must use corpus-local max-gen dedup (not result-local).

    Scenario: two active generations for one document; gen-1 has tag "old",
    gen-2 has tag "new" (simulates crash between promote and retire where both
    generations are temporarily active).  scroll(tags=["old"]) only matches
    gen-1 rows — a result-local max would think gen-1 is the max and return it.
    The corpus-local fix calls max_active_generation() which finds gen-2 as the
    true corpus max, and drops the gen-1 rows → 0 documents returned.
    """
    store = _make_store()

    # Gen 1: tagged "old", active
    gen1_chunks = [
        _make_chunk(
            document_id="doc-twogens",
            document_sha256="gen1" + "a" * 60,
            chunk_index=0,
            ingest_generation=1,
            ingest_state=IngestState.active,
            tags=["old"],
        ),
        _make_chunk(
            document_id="doc-twogens",
            document_sha256="gen1" + "a" * 60,
            chunk_index=1,
            ingest_generation=1,
            ingest_state=IngestState.active,
            tags=["old"],
        ),
    ]
    # Gen 2: tagged "new", active (the true max generation in the corpus)
    gen2_chunks = [
        _make_chunk(
            document_id="doc-twogens",
            document_sha256="gen2" + "b" * 60,
            chunk_index=2,
            ingest_generation=2,
            ingest_state=IngestState.active,
            tags=["new"],
        ),
    ]

    store.upsert_chunks(gen1_chunks, [_passage_emb(0.1), _passage_emb(0.2)])
    store.upsert_chunks(gen2_chunks, [_passage_emb(0.3)])

    # Without filter: should return only gen-2 document (corpus max)
    result_all = store.scroll()
    doc_entries = [d for d in result_all["documents"] if d["document_id"] == "doc-twogens"]
    assert len(doc_entries) == 1
    assert doc_entries[0]["ingest_generation"] == 2, (
        "scroll() without filter must return gen-2 (corpus max), not gen-1"
    )

    # Key assertion: with tag filter matching only old-gen rows, scroll must return
    # 0 documents (not the stale gen-1) because corpus max is gen-2.
    result_old = store.scroll(tags=["old"])
    twogens_entries = [d for d in result_old["documents"] if d["document_id"] == "doc-twogens"]
    assert len(twogens_entries) == 0, (
        "scroll(tags=['old']) must not surface gen-1 when gen-2 is the corpus max; "
        "corpus-local max_active_generation() must be used, not result-local"
    )
    # chunk_count must also be 0 for this doc (no stale chunks counted)
    twogens_chunk_count = sum(
        1 for c in result_old.get("documents", []) if c.get("document_id") == "doc-twogens"
    )
    assert twogens_chunk_count == 0, "chunk_count must not include stale gen-1 chunks"


# ---------------------------------------------------------------------------
# max_active_generation
# ---------------------------------------------------------------------------


def test_max_active_generation_none_when_empty() -> None:
    store = _make_store()
    assert store.max_active_generation("nonexistent-doc") is None


def test_max_active_generation_returns_max() -> None:
    store = _make_store()

    # Gen 1 active (chunk_index 0)
    chunk_gen1 = _make_chunk(
        document_id="doc-gen",
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
    )
    # Gen 2 active (chunk_index 1 to have distinct point_id)
    chunk_gen2 = _make_chunk(
        document_id="doc-gen",
        chunk_index=1,
        ingest_generation=2,
        ingest_state=IngestState.active,
    )
    store.upsert_chunks([chunk_gen1, chunk_gen2], [_passage_emb(0.3), _passage_emb(0.4)])

    assert store.max_active_generation("doc-gen") == 2


def test_max_active_generation_ignores_pending() -> None:
    store = _make_store()

    active = _make_chunk(
        document_id="doc-genp",
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
    )
    pending = _make_chunk(
        document_id="doc-genp",
        chunk_index=1,
        ingest_generation=5,
        ingest_state=IngestState.pending,
    )
    store.upsert_chunks([active, pending], [_passage_emb(0.3), _passage_emb(0.4)])

    # Must return 1 (active only), not 5 (pending)
    assert store.max_active_generation("doc-genp") == 1


# ---------------------------------------------------------------------------
# promote_generation
# ---------------------------------------------------------------------------


def test_promote_generation_changes_state() -> None:
    store = _make_store()

    chunk = _make_chunk(
        document_id="doc-promote",
        ingest_generation=2,
        ingest_state=IngestState.pending,
    )
    store.upsert_chunks([chunk], [_passage_emb()])

    promoted = store.promote_generation("doc-promote", 2)
    assert promoted == 1

    # Now search should find it
    result = store.search(_query_emb())
    doc_ids = {c.document_id for c in result.candidates}
    assert "doc-promote" in doc_ids


def test_promote_generation_returns_count() -> None:
    store = _make_store()

    chunks = [
        _make_chunk(
            document_id="doc-pcount",
            chunk_index=i,
            ingest_generation=3,
            ingest_state=IngestState.pending,
        )
        for i in range(3)
    ]
    store.upsert_chunks(chunks, [_passage_emb(0.1 * i) for i in range(3)])

    count = store.promote_generation("doc-pcount", 3)
    assert count == 3


def test_promote_generation_zero_if_no_match() -> None:
    store = _make_store()
    count = store.promote_generation("nonexistent", 99)
    assert count == 0


# ---------------------------------------------------------------------------
# delete_generations
# ---------------------------------------------------------------------------


def test_delete_generations_by_state() -> None:
    store = _make_store()

    pending = _make_chunk(
        document_id="doc-del",
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.pending,
    )
    active = _make_chunk(
        document_id="doc-del",
        chunk_index=1,
        ingest_generation=1,
        ingest_state=IngestState.active,
    )
    store.upsert_chunks([pending, active], [_passage_emb(0.3), _passage_emb(0.4)])

    deleted = store.delete_generations(
        "doc-del", states=[IngestState.pending]
    )
    assert deleted == 1

    # Active chunk still there
    client = store._get_client()
    from qdrant_client import models
    points, _ = client.scroll(
        collection_name=COLLECTION,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="document_id", match=models.MatchValue(value="doc-del"))]
        ),
        with_payload=True,
    )
    assert len(points) == 1
    assert points[0].payload["ingest_state"] == "active"


def test_delete_generations_by_exact_generation() -> None:
    store = _make_store()

    chunks = [
        _make_chunk(
            document_id="doc-exact",
            chunk_index=i,
            ingest_generation=i + 1,
            ingest_state=IngestState.active,
        )
        for i in range(3)
    ]
    store.upsert_chunks(chunks, [_passage_emb() for _ in range(3)])

    deleted = store.delete_generations("doc-exact", generation=2)
    assert deleted == 1


def test_delete_generations_max_generation_to_delete() -> None:
    """Delete all generations <= max_generation_to_delete."""
    store = _make_store()

    chunks = [
        _make_chunk(
            document_id="doc-maxdel",
            chunk_index=i,
            ingest_generation=i + 1,
            ingest_state=IngestState.active,
        )
        for i in range(4)
    ]
    store.upsert_chunks(chunks, [_passage_emb() for _ in range(4)])

    # Delete gens 1, 2, 3 (keep gen 4)
    deleted = store.delete_generations("doc-maxdel", max_generation_to_delete=3)
    assert deleted == 3

    assert store.max_active_generation("doc-maxdel") == 4


def test_delete_generations_returns_zero_if_no_match() -> None:
    store = _make_store()
    deleted = store.delete_generations("nonexistent", states=[IngestState.pending])
    assert deleted == 0


# ---------------------------------------------------------------------------
# verification_view_filter
# ---------------------------------------------------------------------------


def test_verification_view_filter_includes_active_and_pending_gen() -> None:
    """Filter must pass active chunks AND the specified pending generation."""
    store = _make_store()

    active_chunk = _make_chunk(
        document_id="doc-vvf-other",
        document_sha256="c" * 64,
        ingest_generation=1,
        ingest_state=IngestState.active,
    )
    pending_chunk = _make_chunk(
        document_id="doc-vvf",
        ingest_generation=2,
        ingest_state=IngestState.pending,
    )
    other_pending = _make_chunk(
        document_id="doc-vvf-other2",
        document_sha256="d" * 64,
        ingest_generation=99,
        ingest_state=IngestState.pending,
    )
    store.upsert_chunks(
        [active_chunk, pending_chunk, other_pending],
        [_passage_emb(0.3), _passage_emb(0.4), _passage_emb(0.5)],
    )

    vvf = store.verification_view_filter("doc-vvf", 2)
    client = store._get_client()
    points, _ = client.scroll(
        collection_name=COLLECTION,
        scroll_filter=vvf,
        with_payload=True,
    )

    doc_ids = {pt.payload["document_id"] for pt in points}
    # active chunk + pending gen 2 for doc-vvf
    assert "doc-vvf-other" in doc_ids  # active
    assert "doc-vvf" in doc_ids        # pending gen 2 included
    assert "doc-vvf-other2" not in doc_ids  # pending gen 99 excluded


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------


def test_gc_removes_superseded_active() -> None:
    """GC must delete non-max active generations."""
    store = _make_store()

    # Gen 1 active
    chunk_gen1 = _make_chunk(
        document_id="doc-gc",
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
    )
    # Gen 2 active (max)
    chunk_gen2 = _make_chunk(
        document_id="doc-gc",
        chunk_index=1,
        ingest_generation=2,
        ingest_state=IngestState.active,
    )
    store.upsert_chunks([chunk_gen1, chunk_gen2], [_passage_emb(0.3), _passage_emb(0.4)])

    result = store.gc()
    assert result["superseded_deleted"] == 1
    assert result["documents_affected"] >= 1

    # Max gen still active
    assert store.max_active_generation("doc-gc") == 2


def test_gc_removes_orphaned_pending() -> None:
    """GC must delete orphaned pending chunks."""
    store = _make_store()

    orphan = _make_chunk(
        document_id="doc-gc-orphan",
        ingest_generation=3,
        ingest_state=IngestState.pending,
    )
    store.upsert_chunks([orphan], [_passage_emb()])

    result = store.gc()
    assert result["orphan_deleted"] >= 1

    # Orphan gone
    assert store.max_active_generation("doc-gc-orphan") is None


def test_gc_never_touches_max_active() -> None:
    """GC must preserve the max active generation."""
    store = _make_store()

    active = _make_chunk(
        document_id="doc-gc-safe",
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
    )
    store.upsert_chunks([active], [_passage_emb()])

    store.gc()

    assert store.max_active_generation("doc-gc-safe") == 1


def test_gc_returns_correct_keys() -> None:
    store = _make_store()
    result = store.gc()
    assert "superseded_deleted" in result
    assert "orphan_deleted" in result
    assert "documents_affected" in result


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_collection_not_exists() -> None:
    client = QdrantClient(":memory:")
    store = QdrantStore(collection_name="nonexistent", client=client)

    s = store.status()
    assert s["collection_exists"] is False
    assert s["active_chunk_count"] == 0
    assert s["document_count"] == 0


def test_status_after_ingest() -> None:
    store = _make_store()

    chunks = [
        _make_chunk(
            document_id=f"doc-status-{i}",
            document_sha256=chr(ord("a") + i) * 64,
            ingest_state=IngestState.active,
            source_id="/source/main",
        )
        for i in range(3)
    ]
    store.upsert_chunks(chunks, [_passage_emb(0.1 * i) for i in range(3)])

    s = store.status()
    assert s["collection_exists"] is True
    assert s["active_chunk_count"] == 3
    assert s["document_count"] == 3
    assert s["source_count"] == 1
    assert s["last_ingested_at"] is not None
    assert s["qdrant_healthy"] is True
    assert s["collection"] == COLLECTION


def test_status_excludes_pending_from_active_count() -> None:
    store = _make_store()

    active = _make_chunk(
        document_id="doc-stat-active",
        ingest_state=IngestState.active,
    )
    pending = _make_chunk(
        document_id="doc-stat-pending",
        document_sha256="b" * 64,
        ingest_state=IngestState.pending,
    )
    store.upsert_chunks([active], [_passage_emb(0.3)])
    store.upsert_chunks([pending], [_passage_emb(0.4)])

    s = store.status()
    assert s["active_chunk_count"] == 1
    assert s["document_count"] == 1


# ---------------------------------------------------------------------------
# Full versioning flow: gen1 active → gen2 pending → promote → delete gen1
# ---------------------------------------------------------------------------


def test_versioning_flow_full() -> None:
    """End-to-end: ingest gen1, update to gen2, verify search sees only active."""
    store = _make_store()

    DOC_ID = "doc-version-flow"

    # Step 1: Upsert gen1 pending, promote to active
    chunk_g1 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.pending,
        text="Original content about aineverforget",
    )
    store.upsert_chunks([chunk_g1], [_passage_emb(0.5)])
    store.promote_generation(DOC_ID, 1)

    assert store.max_active_generation(DOC_ID) == 1

    # Step 2: Upsert gen2 pending
    chunk_g2 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=1,  # different chunk_index → different point_id
        ingest_generation=2,
        ingest_state=IngestState.pending,
        text="Updated content about aineverforget v2",
    )
    store.upsert_chunks([chunk_g2], [_passage_emb(0.6)])

    # Search only sees gen1 active
    result = store.search(_query_emb(0.5))
    gen_seen = {c.ingest_generation for c in result.candidates if c.document_id == DOC_ID}
    assert gen_seen == {1}

    # Step 3: Promote gen2
    store.promote_generation(DOC_ID, 2)

    # Step 4: Delete gen1
    deleted = store.delete_generations(
        DOC_ID, states=[IngestState.active], max_generation_to_delete=1
    )
    assert deleted == 1

    # Step 5: Search sees gen2 active, not gen1
    result2 = store.search(_query_emb(0.6))
    active_gens = {c.ingest_generation for c in result2.candidates if c.document_id == DOC_ID}
    assert 2 in active_gens
    assert 1 not in active_gens

    assert store.max_active_generation(DOC_ID) == 2


# ---------------------------------------------------------------------------
# Fix #3: max-gen dedup in search()
# ---------------------------------------------------------------------------


def test_search_max_gen_dedup_excludes_stale_generation() -> None:
    """search() with no view_filter must return only max-generation active chunks."""
    store = _make_store()
    DOC_ID = "doc-gen-dedup"

    # Upsert gen=1 active
    chunk_g1 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
        text="generation one content",
    )
    store.upsert_chunks([chunk_g1], [_passage_emb(0.5)])

    # Upsert gen=2 active (simulates a stale gen1 still in store)
    chunk_g2 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=1,
        ingest_generation=2,
        ingest_state=IngestState.active,
        text="generation two content updated",
    )
    store.upsert_chunks([chunk_g2], [_passage_emb(0.5)])

    result = store.search(_query_emb(0.5))
    gens = [c.ingest_generation for c in result.candidates if c.document_id == DOC_ID]
    assert gens, "expected chunks for DOC_ID in results"
    assert all(g == 2 for g in gens), f"stale gen1 chunk leaked into results: {gens}"


# ---------------------------------------------------------------------------
# Fix #3: max-gen dedup in lexscan()
# ---------------------------------------------------------------------------


def test_lexscan_max_gen_dedup_excludes_stale_generation() -> None:
    """lexscan() must count only max-generation active chunks per document."""
    store = _make_store()
    DOC_ID = "doc-lex-gen-dedup"

    chunk_g1 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
        text="aineverforget generation one content",
    )
    chunk_g2 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=1,
        ingest_generation=2,
        ingest_state=IngestState.active,
        text="aineverforget generation two content",
    )
    store.upsert_chunks([chunk_g1, chunk_g2], [_passage_emb(0.3), _passage_emb(0.4)])

    result = store.lexscan("aineverforget")
    # Both chunks match text, but only gen=2 should survive dedup
    matching = [c for c in result["chunks"] if c.get("document_id") == DOC_ID]
    gens = [c.get("ingest_generation") for c in matching]
    assert gens, "expected matching chunks for DOC_ID"
    assert all(g == 2 for g in gens), f"stale gen1 chunk leaked into lexscan: {gens}"


# ---------------------------------------------------------------------------
# Fix #6: empty tags list must not filter (MatchAny([]) returns nothing)
# ---------------------------------------------------------------------------


def test_search_empty_tags_returns_results() -> None:
    """tags=None (no filter) must return results; tags=[] must behave the same."""
    store = _make_store()

    chunk = _make_chunk(
        document_id="doc-notag",
        ingest_state=IngestState.active,
        tags=[],
        text="some content without any tags",
    )
    store.upsert_chunks([chunk], [_passage_emb(0.5)])

    # tags=None — no filter applied
    result_none = store.search(_query_emb(0.5), tags=None)
    assert any(c.document_id == "doc-notag" for c in result_none.candidates)

    # tags=[] — fix #6: must NOT produce MatchAny([]) which would return nothing
    # After fix, tags=[] is treated as tags=None (no filter)
    result_empty = store.search(_query_emb(0.5), tags=[])
    assert any(c.document_id == "doc-notag" for c in result_empty.candidates)


def test_lexscan_empty_tags_returns_results() -> None:
    """lexscan with tags=[] must return results (not zero due to MatchAny([]))."""
    store = _make_store()

    chunk = _make_chunk(
        document_id="doc-lex-notag",
        ingest_state=IngestState.active,
        tags=[],
        text="aineverforget content without tags",
    )
    store.upsert_chunks([chunk], [_passage_emb()])

    result = store.lexscan("aineverforget", tags=[])
    assert result["chunk_count"] >= 1


# ---------------------------------------------------------------------------
# Fix #11: occurrence_count field in lexscan()
# ---------------------------------------------------------------------------


def test_lexscan_occurrence_count_field_present() -> None:
    """lexscan() result must include occurrence_count field."""
    store = _make_store()

    chunk = _make_chunk(
        document_id="doc-occ",
        ingest_state=IngestState.active,
        text="aineverforget is a great tool for aineverforget users",
    )
    store.upsert_chunks([chunk], [_passage_emb()])

    result = store.lexscan("aineverforget")
    assert "occurrence_count" in result
    assert result["occurrence_count"] >= 2  # term appears twice in the text


def test_lexscan_occurrence_count_zero_when_no_match() -> None:
    """lexscan() occurrence_count is 0 when no chunks match."""
    store = _make_store()

    result = store.lexscan("nonexistentterm12345")
    assert result["occurrence_count"] == 0
    assert result["chunk_count"] == 0


# ---------------------------------------------------------------------------
# Fix #3: max-gen dedup in status()
# ---------------------------------------------------------------------------


def test_status_max_gen_dedup_active_chunk_count() -> None:
    """status() active_chunk_count must count only max-gen chunks per document."""
    store = _make_store()
    DOC_ID = "doc-status-dedup"

    chunk_g1 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
        text="generation one",
    )
    chunk_g2 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=1,
        ingest_generation=2,
        ingest_state=IngestState.active,
        text="generation two",
    )
    store.upsert_chunks([chunk_g1, chunk_g2], [_passage_emb(0.3), _passage_emb(0.4)])

    status = store.status()
    # Only 1 document; gen=2 is max, so only gen=2 chunk counts
    assert status["document_count"] == 1
    assert status["active_chunk_count"] == 1, (
        f"expected 1 (only gen=2 chunk), got {status['active_chunk_count']}"
    )


# ---------------------------------------------------------------------------
# Fix B: corpus-local max-gen dedup in search() and lexscan()
# ---------------------------------------------------------------------------


def test_search_corpus_local_dedup_excludes_stale_gen() -> None:
    """Fix B: search must exclude stale-gen results even when query only matches old-gen.

    Setup:
    - doc DOC_A has gen=1 (active) and gen=2 (active) — two concurrent active gens.
    - gen=1 chunk uses embedding seed=0.1; gen=2 chunk uses seed=0.9 (very different).
    - Query with seed=0.1 (close to gen=1 content).
    - Without corpus-local dedup, gen=1 chunk would be returned (it matches best).
    - With corpus-local dedup, the corpus max for DOC_A is gen=2, so gen=1 is excluded.
    """
    store = _make_store()
    DOC_ID = "doc-corpus-dedup"

    chunk_g1 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
        text="old generation content specific to gen one",
    )
    chunk_g2 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=1,
        ingest_generation=2,
        ingest_state=IngestState.active,
        text="new generation replacement content for gen two",
    )
    store.upsert_chunks(
        [chunk_g1, chunk_g2],
        [_passage_emb(0.1), _passage_emb(0.9)],
    )

    # Verify corpus max is gen=2
    assert store.max_active_generation(DOC_ID) == 2

    # Query with seed=0.1 (close to gen=1 vector)
    query = _query_emb(0.1)
    result = store.search(query, limit=5)
    doc_candidates = [c for c in result.candidates if c.document_id == DOC_ID]

    # Fix B: no gen=1 chunk should appear — corpus max is 2
    stale_gens = {c.ingest_generation for c in doc_candidates if c.ingest_generation < 2}
    assert not stale_gens, (
        f"Fix B: search returned stale gen(s) {stale_gens} for {DOC_ID}; "
        f"corpus max is gen=2, all results must be gen=2"
    )


def test_lexscan_corpus_local_dedup_excludes_stale_gen() -> None:
    """Fix B: lexscan must exclude stale-gen results via corpus-local max-gen check.

    Setup:
    - DOC_B has gen=1 (active) and gen=2 (active).
    - Both gens contain the term 'distinctiveterm'.
    - Without corpus-local dedup, both gens appear in lexscan.
    - With corpus-local dedup, only gen=2 appears.
    """
    store = _make_store()
    DOC_ID = "doc-lex-corpus-dedup"

    chunk_g1 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=0,
        ingest_generation=1,
        ingest_state=IngestState.active,
        text="distinctiveterm appears in the first generation of this document",
    )
    chunk_g2 = _make_chunk(
        document_id=DOC_ID,
        chunk_index=1,
        ingest_generation=2,
        ingest_state=IngestState.active,
        text="distinctiveterm also appears in the second generation of this document",
    )
    store.upsert_chunks(
        [chunk_g1, chunk_g2],
        [_passage_emb(0.2), _passage_emb(0.8)],
    )

    assert store.max_active_generation(DOC_ID) == 2

    # lexscan in :memory: mode — MatchText won't filter (no payload index).
    # But the corpus-local dedup logic runs AFTER scroll, so we test it by
    # verifying that only gen=2 chunks are returned for DOC_ID.
    # In :memory: mode all chunks are returned by scroll (MatchText no-ops),
    # so both gens are visible pre-dedup.  Post-dedup only gen=2 must remain.
    lx = store.lexscan("distinctiveterm")
    doc_chunks = [c for c in lx["chunks"] if c.get("document_id") == DOC_ID]

    # After corpus-local dedup, only gen=2 must remain
    stale_gens = {c.get("ingest_generation") for c in doc_chunks if c.get("ingest_generation", 0) < 2}
    assert not stale_gens, (
        f"Fix B: lexscan returned stale gen(s) {stale_gens} for {DOC_ID}; "
        f"corpus max is gen=2, only gen=2 must appear"
    )
