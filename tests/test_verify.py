"""Tests for aineverforget.verify.run_probes.

Uses QdrantClient(':memory:') via a real QdrantStore — no server required.

Retrieval strategy after the verify-gate fix:
- topical / negative: hybrid search (dense+sparse RRF) via store.search().
  MatchText is NOT used; similarity is vector-based.
- specific:            lexical MatchText scroll (single distinctive token).

Note on :memory: Qdrant + RRF:
  In :memory: mode, sparse vector filtering has no effect — every point gets a
  sparse-arm rank regardless of overlap.  This means RRF fusion returns ALL
  points for ANY query.  Dense similarity IS respected.

  Consequence for tests:
  - topical PASS / FAIL: test via dense similarity (word-frequency FakeEmbedder)
    → dense arm gives high score to semantically matching chunks → works.
  - specific PASS / FAIL: lexical MatchText → works reliably (single token).
  - negative PASS with cold-start deferral: no search issued → works.
  - negative PASS/FAIL for non-cold-start: uses monkeypatch on store.search
    to return controlled candidates → tests the verdict logic, not Qdrant behaviour.
    Real-server validation is in scripts/smoke_real.py.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from qdrant_client import QdrantClient

from aineverforget.embedding import EmbeddingVector, PassageEmbedding, QueryEmbedding
from aineverforget.models import Chunk, IngestState, RetrievedChunk, SearchResult
from aineverforget.store import QdrantStore
from aineverforget.verify import Probe, ProbeType, run_probes

# ---------------------------------------------------------------------------
# FakeEmbedder — word-frequency dense vectors
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Deterministic word-frequency embedder for tests.

    Dense vector: each word hashes to a specific dimension (MD5 mod 1024), set
    to 1.0.  Two texts sharing vocabulary → high cosine similarity; disjoint
    vocabularies → ~0 similarity.

    Sparse vector: same word→token_id mapping with weight 1.0.

    Note: in :memory: Qdrant the sparse arm of RRF is NOT filtered by overlap,
    so only the dense arm drives real similarity.  This embedder's dense vectors
    provide reliable topical pass/fail.
    """

    DIM = 1024

    def encode_passages(self, texts: list[str]) -> list[PassageEmbedding]:
        return [self._encode(t) for t in texts]

    def encode_query(self, text: str) -> QueryEmbedding:
        emb = self._encode(text)
        return QueryEmbedding(dense=emb.dense, sparse=emb.sparse)

    def _encode(self, text: str) -> PassageEmbedding:
        words = set(text.lower().split())
        raw = [0.0] * self.DIM
        lw: dict[int, float] = {}
        for word in words:
            d_idx = int(hashlib.md5(word.encode()).hexdigest()[:4], 16) % self.DIM
            raw[d_idx] = 1.0
            s_idx = int(hashlib.md5(word.encode()).hexdigest()[4:8], 16) % 30000
            lw[s_idx] = 1.0
        norm = sum(x * x for x in raw) ** 0.5
        dense = [x / norm if norm > 0 else 0.0 for x in raw]
        indices = sorted(lw.keys())
        values = [lw[i] for i in indices]
        sparse = EmbeddingVector(indices=indices, values=values)
        return PassageEmbedding(dense=dense, sparse=sparse)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COLLECTION = "test_ainf_verify_collection"

DOC_ID_PENDING = "doc-pending-verify"
DOC_ID_UNRELATED_A = "doc-unrelated-alpha"
DOC_ID_UNRELATED_B = "doc-unrelated-beta"

PENDING_GEN = 2

_fake_embedder = FakeEmbedder()


def _make_store() -> QdrantStore:
    client = QdrantClient(":memory:")
    store = QdrantStore(collection_name=COLLECTION, client=client)
    store.ensure_collection()
    return store


def _emb(text: str) -> PassageEmbedding:
    return _fake_embedder._encode(text)


def _make_chunk(
    document_id: str = DOC_ID_PENDING,
    document_sha256: str = "a" * 64,
    chunk_index: int = 0,
    ingest_generation: int = PENDING_GEN,
    ingest_state: IngestState = IngestState.pending,
    text: str = "Default test chunk text",
    source_id: str = "/test/source",
    source_type: str = "markdown",
    tags: list[str] | None = None,
    ingested_at: datetime | None = None,
) -> Chunk:
    if ingested_at is None:
        ingested_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return Chunk(
        source_id=source_id,
        source_type=source_type,
        document_id=document_id,
        document_path=f"/test/{document_id}.md",
        document_sha256=document_sha256,
        ingest_generation=ingest_generation,
        ingest_state=ingest_state,
        title=f"Title {document_id}",
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


def _make_retrieved(document_id: str, text: str = "") -> RetrievedChunk:
    """Helper: build a minimal RetrievedChunk for mock search results."""
    return RetrievedChunk(
        score=0.9,
        point_id="pt-1",
        source_id="/test/source",
        source_type="markdown",
        document_id=document_id,
        document_path="/test/doc.md",
        document_sha256="a" * 64,
        title="T",
        chunk_index=0,
        heading_path=None,
        pdf_page=None,
        tags=[],
        text=text,
        producer="user",
        ingested_at="2024-06-01T12:00:00+00:00",
        ingest_generation=PENDING_GEN,
    )


# ---------------------------------------------------------------------------
# Scenario 1: topical PASS + specific PASS
# ---------------------------------------------------------------------------


def test_verify_topical_specific_pass() -> None:
    """topical + specific probes pass when pending gen has the right vocabulary."""
    store = _make_store()

    # Pending generation chunks with aineverforget vocabulary
    pending_text_0 = "aineverforget knowledge indexer stores documents for retrieval"
    pending_text_1 = "aineverforget uses Qdrant vector database hybrid search BGE-M3"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="d" * 64,
                chunk_index=0,
                ingest_generation=PENDING_GEN,
                ingest_state=IngestState.pending,
                text=pending_text_0,
            ),
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="d" * 64,
                chunk_index=1,
                ingest_generation=PENDING_GEN,
                ingest_state=IngestState.pending,
                text=pending_text_1,
            ),
        ],
        [_emb(pending_text_0), _emb(pending_text_1)],
    )

    probes = [
        Probe(
            probe_type=ProbeType.topical,
            # Overlapping vocabulary → dense cosine similarity → pending chunk retrieved
            query="aineverforget knowledge indexer",
            limit=10,
        ),
        Probe(
            probe_type=ProbeType.specific,
            # Single token in pending text (lexical MatchText in :memory: mode)
            query="Qdrant",
            expected_substring="Qdrant",
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is True
    assert verdict.index_suspect is False
    assert len(verdict.probe_results) == 2

    topical_r, specific_r = verdict.probe_results
    assert topical_r.passed is True
    assert topical_r.deferred is False

    assert specific_r.passed is True
    assert specific_r.deferred is False


# ---------------------------------------------------------------------------
# Scenario 2: topical FAIL → verdict fail
# ---------------------------------------------------------------------------


def test_verify_unretrievable_pending_fails_topical() -> None:
    """Unrelated pending gen text → topical probe fails → verdict fail.

    Query vocabulary is completely disjoint from pending chunk vocabulary →
    FakeEmbedder dense vectors have ~0 cosine similarity → pending chunk is
    NOT at the top of the dense arm → topical fails.

    We only have 1 active doc (unrelated A) so the pending chunk has to compete
    purely on dense similarity.
    """
    store = _make_store()

    # Unrelated active doc with quantum vocabulary (drives dense arm top rank)
    unrelated_text = "quantum physics superposition entanglement wave particle"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_UNRELATED_A,
                document_sha256="h" * 64,
                ingest_state=IngestState.active,
                ingest_generation=1,
                text=unrelated_text,
            )
        ],
        [_emb(unrelated_text)],
    )

    # Pending gen with completely different vocabulary (no overlap with query)
    pending_text = "xyzzy foobar baz qux nonsense garbled flipflop zap wham blorp"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="i" * 64,
                chunk_index=0,
                ingest_generation=PENDING_GEN,
                ingest_state=IngestState.pending,
                text=pending_text,
            )
        ],
        [_emb(pending_text)],
    )

    probes = [
        Probe(
            probe_type=ProbeType.topical,
            # Query matches unrelated doc vocabulary, NOT pending gen
            # Dense arm: unrelated ranks #1, pending ranks lower (0 cosine)
            # With limit=1, only unrelated returned → pending NOT found → FAIL
            query="quantum physics superposition entanglement wave",
            limit=1,  # tight limit: only the top-scoring doc
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is False
    assert verdict.index_suspect is True

    topical_r = verdict.probe_results[0]
    assert topical_r.passed is False
    assert topical_r.deferred is False
    assert "FAIL" in topical_r.detail or "NOT found" in topical_r.detail


# ---------------------------------------------------------------------------
# Scenario 3: cold-start — negative probe deferred, topical+specific pass
# ---------------------------------------------------------------------------


def test_verify_cold_start_defers_negative() -> None:
    """No unrelated active docs → negative probe DEFERRED, verdict passes."""
    store = _make_store()

    pending_text = "aineverforget coldstart first document knowledge indexer"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="e" * 64,
                chunk_index=0,
                ingest_generation=PENDING_GEN,
                ingest_state=IngestState.pending,
                text=pending_text,
            )
        ],
        [_emb(pending_text)],
    )

    probes = [
        Probe(
            probe_type=ProbeType.topical,
            query="aineverforget knowledge indexer",
            limit=10,
        ),
        Probe(
            probe_type=ProbeType.specific,
            query="coldstart",
            expected_substring="coldstart",
            limit=10,
        ),
        Probe(
            probe_type=ProbeType.negative,
            query="quantum physics superposition entanglement",
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is True
    assert verdict.index_suspect is False
    assert verdict.negative_deferred is True
    assert len(verdict.probe_results) == 3

    topical_r, specific_r, negative_r = verdict.probe_results
    assert topical_r.passed is True
    assert topical_r.deferred is False

    assert specific_r.passed is True
    assert specific_r.deferred is False

    assert negative_r.passed is True
    assert negative_r.deferred is True
    assert "DEFERRED" in negative_r.detail or "cold" in negative_r.detail.lower()


def test_verify_cold_start_only_active_is_same_doc_defers_negative() -> None:
    """Active chunks only from SAME doc → cold-start → negative deferred."""
    store = _make_store()

    active_text = "aineverforget version one original content knowledge indexer"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="f" * 64,
                chunk_index=0,
                ingest_generation=1,
                ingest_state=IngestState.active,
                text=active_text,
            )
        ],
        [_emb(active_text)],
    )
    pending_text = "aineverforget version two updated content knowledge indexer"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="g" * 64,
                chunk_index=1,
                ingest_generation=PENDING_GEN,
                ingest_state=IngestState.pending,
                text=pending_text,
            )
        ],
        [_emb(pending_text)],
    )

    probes = [
        Probe(
            probe_type=ProbeType.negative,
            query="quantum physics superposition entanglement wave",
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.negative_deferred is True
    assert verdict.passed is True
    assert verdict.probe_results[0].deferred is True


def test_verify_near_empty_defers_negative() -> None:
    """Too few unrelated active docs → negative probe deferred as near-empty."""
    store = _make_store()

    for idx, (doc_id, text) in enumerate([
        (DOC_ID_UNRELATED_A, "quantum physics superposition entanglement"),
        (DOC_ID_UNRELATED_B, "gardening tomatoes compost seedlings"),
    ]):
        store.upsert_chunks(
            [
                _make_chunk(
                    document_id=doc_id,
                    document_sha256=str(idx) * 64,
                    ingest_state=IngestState.active,
                    ingest_generation=1,
                    text=text,
                )
            ],
            [_emb(text)],
        )

    probes = [
        Probe(
            probe_type=ProbeType.negative,
            query="cooking recipes pasta",
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is True
    assert verdict.index_suspect is False
    assert verdict.negative_deferred is True
    negative_r = verdict.probe_results[0]
    assert negative_r.passed is True
    assert negative_r.deferred is True
    assert "insufficient unrelated active Documents" in negative_r.detail


# ---------------------------------------------------------------------------
# Scenario 4: negative probe verdict logic (monkeypatched store.search)
# In :memory: mode, RRF returns all points regardless of sparse overlap.
# We mock store.search to test the negative probe's verdict logic directly.
# ---------------------------------------------------------------------------


def test_verify_negative_passes_when_pending_absent_from_results(monkeypatch) -> None:
    """Negative probe passes when pending doc NOT in hybrid search results."""
    store = _make_store()

    # Seed enough unrelated active docs so cold-start/near-empty deferral is NOT triggered.
    unrelated_text = "quantum physics superposition entanglement"
    unrelated_chunks = []
    unrelated_embeddings = []
    for idx, doc_id in enumerate([DOC_ID_UNRELATED_A, DOC_ID_UNRELATED_B, "doc-unrelated-gamma"]):
        text = f"{unrelated_text} {idx}"
        unrelated_chunks.append(
            _make_chunk(
                document_id=doc_id,
                document_sha256=str(idx) * 64,
                ingest_state=IngestState.active,
                ingest_generation=1,
                text=text,
            )
        )
        unrelated_embeddings.append(_emb(text))
    store.upsert_chunks(unrelated_chunks, unrelated_embeddings)

    # Mock store.search: negative query returns only the unrelated doc, NOT pending
    def mock_search(query, *, limit=10, view_filter=None, **kwargs):
        return SearchResult(
            query="quantum physics superposition entanglement",
            candidates=[_make_retrieved(DOC_ID_UNRELATED_A, text=unrelated_text)],
            dense_hits=1,
            sparse_hits=1,
            candidate_count=1,
            citationable_count=1,
        )

    monkeypatch.setattr(store, "search", mock_search)

    probes = [
        Probe(
            probe_type=ProbeType.negative,
            query="quantum physics superposition entanglement",
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is True
    assert verdict.index_suspect is False
    assert verdict.negative_deferred is False
    negative_r = verdict.probe_results[0]
    assert negative_r.passed is True
    assert "PASS" in negative_r.detail


def test_verify_negative_fails_when_pending_surfaces(monkeypatch) -> None:
    """Negative probe fails when pending doc appears in hybrid search results."""
    store = _make_store()

    # Seed enough unrelated active docs so cold-start/near-empty deferral is NOT triggered.
    unrelated_text = "quantum physics superposition entanglement"
    unrelated_chunks = []
    unrelated_embeddings = []
    for idx, doc_id in enumerate([DOC_ID_UNRELATED_A, DOC_ID_UNRELATED_B, "doc-unrelated-gamma"]):
        text = f"{unrelated_text} {idx}"
        unrelated_chunks.append(
            _make_chunk(
                document_id=doc_id,
                document_sha256=str(idx) * 64,
                ingest_state=IngestState.active,
                ingest_generation=1,
                text=text,
            )
        )
        unrelated_embeddings.append(_emb(text))
    store.upsert_chunks(unrelated_chunks, unrelated_embeddings)

    # Mock store.search: negative query returns BOTH unrelated AND pending doc
    def mock_search(query, *, limit=10, view_filter=None, **kwargs):
        return SearchResult(
            query="quantum physics superposition entanglement",
            candidates=[
                _make_retrieved(DOC_ID_UNRELATED_A, text=unrelated_text),
                _make_retrieved(DOC_ID_PENDING, text="quantum pending content"),  # bad!
            ],
            dense_hits=2,
            sparse_hits=2,
            candidate_count=2,
            citationable_count=2,
        )

    monkeypatch.setattr(store, "search", mock_search)

    probes = [
        Probe(
            probe_type=ProbeType.negative,
            query="quantum physics superposition entanglement",
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is False
    assert verdict.index_suspect is True
    assert verdict.negative_deferred is False
    negative_r = verdict.probe_results[0]
    assert negative_r.passed is False
    assert "FAIL" in negative_r.detail or "unexpectedly" in negative_r.detail


# ---------------------------------------------------------------------------
# Specific probe edge cases
# ---------------------------------------------------------------------------


def test_verify_specific_fails_when_substring_absent() -> None:
    """Pending gen text doesn't contain expected_substring → specific fails."""
    store = _make_store()

    pending_text = "aineverforget knowledge indexer document storage system"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="k" * 64,
                chunk_index=0,
                ingest_generation=PENDING_GEN,
                ingest_state=IngestState.pending,
                text=pending_text,
            )
        ],
        [_emb(pending_text)],
    )

    probes = [
        Probe(
            probe_type=ProbeType.specific,
            query="aineverforget",  # term IS in text → chunk found by MatchText
            expected_substring="Qdrant",  # NOT in the text → specific fails
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is False
    assert verdict.index_suspect is True

    specific_r = verdict.probe_results[0]
    assert specific_r.passed is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_verify_empty_probes_list_passes() -> None:
    """No probes → verdict passes trivially."""
    store = _make_store()
    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, [], _fake_embedder)

    assert verdict.passed is True
    assert verdict.index_suspect is False
    assert verdict.probe_results == []
    assert verdict.negative_deferred is False


def test_verify_verdict_document_id_and_generation_propagated() -> None:
    """VerifyVerdict carries document_id and generation from run_probes args."""
    store = _make_store()

    pending_text = "aineverforget test generation propagation metadata"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="n" * 64,
                chunk_index=0,
                ingest_generation=PENDING_GEN,
                ingest_state=IngestState.pending,
                text=pending_text,
            )
        ],
        [_emb(pending_text)],
    )

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, [], _fake_embedder)

    assert verdict.document_id == DOC_ID_PENDING
    assert verdict.generation == PENDING_GEN


def test_verify_specific_no_expected_substring_passes_on_presence() -> None:
    """Specific probe with no expected_substring passes if any pending-gen chunk found."""
    store = _make_store()

    pending_text = "aineverforget specific probe no substring required presence check"
    store.upsert_chunks(
        [
            _make_chunk(
                document_id=DOC_ID_PENDING,
                document_sha256="o" * 64,
                chunk_index=0,
                ingest_generation=PENDING_GEN,
                ingest_state=IngestState.pending,
                text=pending_text,
            )
        ],
        [_emb(pending_text)],
    )

    probes = [
        Probe(
            probe_type=ProbeType.specific,
            query="aineverforget",
            expected_substring=None,
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is True
    assert verdict.probe_results[0].passed is True


# ---------------------------------------------------------------------------
# Fix #2: topical probe must match document_id AND ingest_generation
# ---------------------------------------------------------------------------


def test_topical_probe_requires_correct_generation(monkeypatch) -> None:
    """Topical probe must check both document_id AND ingest_generation.

    A chunk with the right document_id but wrong (old) generation must NOT
    cause the probe to pass — the pending generation must be present.
    """
    store = _make_store()

    text = "aineverforget knowledge retrieval"

    # Simulate: search returns a chunk with matching doc_id but OLD generation (1, not PENDING_GEN=2)
    old_gen_chunk = RetrievedChunk(
        score=0.9,
        point_id="pt-old",
        source_id="/test/source",
        source_type="markdown",
        document_id=DOC_ID_PENDING,
        document_path="/test/doc.md",
        document_sha256="a" * 64,
        title="T",
        chunk_index=0,
        heading_path=None,
        pdf_page=None,
        tags=[],
        text=text,
        producer="user",
        ingested_at="2024-06-01T12:00:00+00:00",
        ingest_generation=1,  # OLD generation, not PENDING_GEN
    )

    from aineverforget.models import SearchResult

    def _fake_search(*args, **kwargs):
        return SearchResult(
            query="",
            candidates=[old_gen_chunk],
            dense_hits=1,
            sparse_hits=0,
            candidate_count=1,
            citationable_count=1,
        )

    monkeypatch.setattr(store, "search", _fake_search)

    probes = [
        Probe(
            probe_type=ProbeType.topical,
            query=text,
            expected_substring=None,
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    # Must FAIL: search returned a chunk from gen=1, not the pending gen=PENDING_GEN=2
    assert verdict.passed is False, (
        "topical probe must fail when only old-generation chunks are returned"
    )


def test_topical_probe_passes_with_correct_generation(monkeypatch) -> None:
    """Topical probe passes when document_id AND ingest_generation match."""
    store = _make_store()

    text = "aineverforget knowledge retrieval"

    correct_gen_chunk = RetrievedChunk(
        score=0.9,
        point_id="pt-correct",
        source_id="/test/source",
        source_type="markdown",
        document_id=DOC_ID_PENDING,
        document_path="/test/doc.md",
        document_sha256="a" * 64,
        title="T",
        chunk_index=0,
        heading_path=None,
        pdf_page=None,
        tags=[],
        text=text,
        producer="user",
        ingested_at="2024-06-01T12:00:00+00:00",
        ingest_generation=PENDING_GEN,  # Correct generation
    )

    from aineverforget.models import SearchResult

    def _fake_search(*args, **kwargs):
        return SearchResult(
            query="",
            candidates=[correct_gen_chunk],
            dense_hits=1,
            sparse_hits=0,
            candidate_count=1,
            citationable_count=1,
        )

    monkeypatch.setattr(store, "search", _fake_search)

    probes = [
        Probe(
            probe_type=ProbeType.topical,
            query=text,
            expected_substring=None,
            limit=10,
        ),
    ]

    verdict = run_probes(store, DOC_ID_PENDING, PENDING_GEN, probes, _fake_embedder)

    assert verdict.passed is True, (
        "topical probe must pass when document_id AND ingest_generation match"
    )
