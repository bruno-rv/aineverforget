"""aineverforget.models — canonical data contracts for the tool layer.

These types are the shared language between every module.  Parallel agents
implement against these definitions verbatim; do not change signatures.

No heavy imports: importable with only stdlib + pydantic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# IngestState — visibility gate for Chunks
# ---------------------------------------------------------------------------


class IngestState(str, Enum):
    """Visibility gate for Chunk records in Qdrant.

    Transitions (per PLAN.md § Identity model):
        pending → active   (on verify-pass: promote_generation)
        pending → failed   (on verify-fail or crash: mark_failed / gc)

    Only ``active`` Chunks are served by any read path.
    ``pending`` and ``failed`` are invisible to search/scroll/lexscan/status.
    A failed first-ingest leaves the Document absent from results (not half-served).
    """

    pending = "pending"
    active = "active"
    failed = "failed"


# ---------------------------------------------------------------------------
# Document — normalized output of a Loader
# ---------------------------------------------------------------------------


class Document(BaseModel):
    """Normalized content extracted by a Loader from a Source.

    One Source may yield many Documents (e.g. a Producer markdown bundle).
    A Document is the unit that gets chunked.  It carries all metadata needed
    to populate every Chunk payload field.

    Fields
    ------
    source_id:
        Stable identifier for the ingest Source (path or Producer reference).
        Provided by the caller of ingest_paths(); not derived from content.
    source_type:
        Source type string keyed to the Loader registry (e.g. ``"markdown"``,
        ``"pdf"``).  Determines which Loader was used.
    document_id:
        Deterministic id for this Document within its Source.
        Computed by ``identity.make_document_id(source_id, document_path)``.
    document_path:
        Filesystem path (or logical path within a bundle) of the Document.
        Canonical payload field name — ``source_path`` is NOT used.
    document_sha256:
        SHA-256 hex digest of the Document's raw_text (content identity, not
        version order).  Used for idempotency check in step 1 of the ingest
        flow and as part of the point_id determinism.
    title:
        Human-readable title for citation display.  May be extracted from
        heading, filename, or PDF metadata.
    producer:
        Name of the Producer that created this Source (or ``"user"`` for
        directly-supplied notes).  Propagated to Chunk payload.
    raw_text:
        Full extracted text handed to the chunker.
    meta:
        Arbitrary loader-specific metadata (PDF page count, detected language,
        loader verdict, etc.).  Not stored in Qdrant directly; used during
        chunking/embedding phases.
    """

    source_id: str
    source_type: str
    document_id: str
    document_path: str
    document_sha256: str
    title: str
    producer: str
    raw_text: str
    meta: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Chunk — atomic retrievable unit stored in Qdrant
# ---------------------------------------------------------------------------

# Namespace UUID for point_id generation — hardcoded (see identity.py).
# Defined here so to_payload / point_id tests can import without identity module.
_POINT_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


class Chunk(BaseModel):
    """Atomic retrievable slice of a Document, stored as a Qdrant point.

    This is the exact payload schema stored in Qdrant.  Every field maps
    1-to-1 to a Qdrant payload key.  Field names are canonical per PLAN.md —
    do not rename them.

    Point ID
    --------
    ``point_id`` is a deterministic UUIDv5 of
    ``document_id | document_sha256 | chunk_index`` (exactly three components,
    per PLAN.md line 52 — no embedding_model, no ingest_generation).  This
    makes identical content yield identical IDs (enabling the idempotency
    check) and allows promote/retire to operate by payload mutation
    (``set_payload`` on stable IDs) rather than ID rewriting.

    Payload serialization
    ---------------------
    ``to_payload()`` returns a dict ready for Qdrant ``payload`` with:
    - ``ingest_state`` as its ``.value`` string (``"pending"`` | …)
    - ``ingested_at`` as an ISO-8601 string (matching the datetime payload index)
    - ``heading_path`` / ``pdf_page`` as ``None`` when absent

    Fields
    ------
    source_id:         Stable Source identifier.
    source_type:       Source type (``"markdown"`` | ``"pdf"`` | …).
    document_id:       Deterministic Document identifier.
    document_path:     Filesystem / logical path of the originating Document.
    document_sha256:   SHA-256 hex digest of the Document's raw text (content
                       identity — not a version order).
    ingest_generation: Monotonic integer per ``document_id`` (version order).
                       Allocated as max-active-generation + 1 at write time.
                       Visibility is gated by ``ingest_state``, not this field.
    ingest_state:      Visibility gate: ``pending`` → ``active`` | ``failed``.
                       Only ``active`` Chunks are ever served.
    title:             Human-readable title for citation display.
    chunk_index:       Zero-based index of this Chunk within its Document.
    chunk_start_word:  Word offset (0-based) of the first word of this Chunk
                       in the Document's raw_text.
    chunk_end_word:    Word offset (exclusive) of the last word of this Chunk.
    heading_path:      For markdown Chunks: pipe-joined heading ancestry
                       (e.g. ``"## Architecture | ### Store"``).  ``None`` for
                       non-markdown Chunks.
    pdf_page:          For PDF Chunks: 0-based page index.  ``None`` for
                       non-PDF Chunks.
    tags:              List of free-form tags applied at ingest time (passed
                       via ``--tag``).  Stored as a keyword-indexed array.
    producer:          Name of the Producer that created the Source.
    ingested_at:       UTC datetime of ingest.  Stored as ISO-8601 string in
                       Qdrant (datetime payload index).
    loader_version:    Version string of the Loader that produced the Document.
    chunker_version:   Version string of the chunker that produced this Chunk.
    embedding_model:   Model checkpoint used to encode this Chunk's vectors.
    text:              The full text of this Chunk (full-text indexed in Qdrant
                       for ``lexscan``).
    """

    source_id: str
    source_type: str
    document_id: str
    document_path: str
    document_sha256: str
    ingest_generation: int
    ingest_state: IngestState
    title: str
    chunk_index: int
    chunk_start_word: int
    chunk_end_word: int
    heading_path: str | None = None
    pdf_page: int | None = None
    tags: list[str] = Field(default_factory=list)
    producer: str
    ingested_at: datetime
    loader_version: str
    chunker_version: str
    embedding_model: str
    text: str

    @field_validator("ingested_at", mode="before")
    @classmethod
    def _ensure_aware(cls, v: object) -> datetime:
        """Ensure ingested_at is a timezone-aware datetime."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v
        raise ValueError(f"Cannot parse ingested_at: {v!r}")

    @property
    def point_id(self) -> str:
        """Deterministic UUIDv5 for this Chunk's Qdrant point.

        Composed of exactly three components:
            ``document_id | document_sha256 | chunk_index``

        Intentionally excludes ``ingest_generation`` and ``ingest_state`` so
        that identical content yields identical IDs across ingest runs (the
        idempotency dedup) and promote/retire can mutate payload without
        rewriting IDs.

        Returns
        -------
        str
            UUID string (e.g. ``"550e8400-e29b-41d4-a716-446655440000"``).
        """
        value = f"{self.document_id}|{self.document_sha256}|{self.chunk_index}"
        return str(uuid.uuid5(_POINT_NAMESPACE, value))

    def to_payload(self) -> dict[str, Any]:
        """Serialize this Chunk to a Qdrant payload dict.

        All 20 schema fields are included.  Type coercions:
        - ``ingest_state`` → ``.value`` string (``"pending"`` | ``"active"`` | ``"failed"``)
        - ``ingested_at`` → ISO-8601 string with UTC offset (matching the
          Qdrant datetime payload index)
        - ``heading_path`` / ``pdf_page`` → ``None`` when absent
        - ``tags`` → list[str] (may be empty)

        The dict is safe to pass directly as Qdrant ``PointStruct.payload``.

        Returns
        -------
        dict[str, Any]
            Flat mapping of payload field name → value.
        """
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "document_id": self.document_id,
            "document_path": self.document_path,
            "document_sha256": self.document_sha256,
            "ingest_generation": self.ingest_generation,
            "ingest_state": self.ingest_state.value,
            "title": self.title,
            "chunk_index": self.chunk_index,
            "chunk_start_word": self.chunk_start_word,
            "chunk_end_word": self.chunk_end_word,
            "heading_path": self.heading_path,
            "pdf_page": self.pdf_page,
            "tags": list(self.tags),
            "producer": self.producer,
            "ingested_at": self.ingested_at.isoformat(),
            "loader_version": self.loader_version,
            "chunker_version": self.chunker_version,
            "embedding_model": self.embedding_model,
            "text": self.text,
        }


# ---------------------------------------------------------------------------
# Search result types — shared by store.search() and CLI --json schema
# ---------------------------------------------------------------------------


class RetrievedChunk(BaseModel):
    """One Chunk returned by a hybrid search query.

    Carries all citation-grade fields so the Agent can cite it without a
    second lookup.

    score:          RRF-fused relevance score from Qdrant Query API.
    point_id:       Qdrant point UUID string (stable, deterministic).
    source_id:      Source identifier.
    source_type:    Source type.
    document_id:    Document identifier.
    document_path:  Filesystem / logical path.
    document_sha256:Content identity hash.
    title:          Human-readable title.
    chunk_index:    Index within document.
    heading_path:   Markdown heading ancestry (None for non-markdown).
    pdf_page:       PDF page index (None for non-PDF).
    tags:           Tags applied at ingest.
    text:           Full chunk text (needed for groundedness gate).
    producer:       Producer name.
    ingested_at:    ISO-8601 ingest timestamp string.
    ingest_generation: Active generation that was served.
    """

    score: float
    point_id: str
    source_id: str
    source_type: str
    document_id: str
    document_path: str
    document_sha256: str
    title: str
    chunk_index: int
    heading_path: str | None
    pdf_page: int | None
    tags: list[str]
    text: str
    producer: str
    ingested_at: str
    ingest_generation: int


class SearchResult(BaseModel):
    """Result envelope returned by ``store.search()`` and ``aineverforget search --json``.

    Carries per-modality hit counts (pre-fusion) alongside the fused candidates
    so the Orchestrator can evaluate the retriever Quality Gate:

        candidate_count >= 1
        AND (dense_hits >= 1 OR sparse_hits >= 1)
        AND citationable_count >= 1

    Per ADR-0003 and PLAN.md § Quality Gates: gate uses pre-modality counts,
    NOT a fused-RRF score floor (RRF scores are rank-based and uncalibrated).

    Attributes
    ----------
    query:              The query string that produced this result.
    candidates:         RRF-fused chunks ordered by descending score.
    dense_hits:         Number of results returned by the dense prefetch
                        (before fusion).  Used for gate evaluation.
    sparse_hits:        Number of results returned by the sparse prefetch
                        (before fusion).  Used for gate evaluation.
    candidate_count:    Total fused candidates (== len(candidates)).
    citationable_count: Candidates with non-empty ``text`` AND non-empty
                        ``document_path`` — the minimum for a citation.
    """

    query: str
    candidates: list[RetrievedChunk]
    dense_hits: int
    sparse_hits: int
    candidate_count: int
    citationable_count: int
