"""aineverforget.identity — deterministic IDs for the ingest identity model.

All ID functions are pure (stdlib-only) and produce stable outputs for the
same inputs across Python versions and platforms.

No heavy imports: importable with only stdlib.
"""

from __future__ import annotations

import hashlib
import uuid


# ---------------------------------------------------------------------------
# UUIDv5 namespace — hardcoded, never runtime-generated.
# ---------------------------------------------------------------------------

POINT_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
"""Namespace UUID for all aineverforget point IDs.

Hardcoded so IDs are stable across restarts.  This constant is mirrored in
``models._POINT_NAMESPACE`` so ``Chunk.point_id`` does not need to import
this module.
"""


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    """Return the SHA-256 hex digest of *text* (UTF-8 encoded).

    Used to compute ``document_sha256`` (content identity field).  The digest
    is deterministic for the same text regardless of ingest time or order.

    Parameters
    ----------
    text:
        The raw text content of a Document.

    Returns
    -------
    str
        64-character lowercase hex string, e.g.
        ``"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"``.

    Examples
    --------
    >>> sha256_text("hello") == sha256_text("hello")
    True
    >>> sha256_text("hello") != sha256_text("world")
    True
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Document identity
# ---------------------------------------------------------------------------


def make_document_id(source_id: str, document_path: str) -> str:
    """Return a deterministic identifier for a Document within a Source.

    Computed as UUIDv5(POINT_NAMESPACE, ``source_id|document_path``).
    Stable: the same (source_id, document_path) pair always yields the same
    ``document_id``, even across Python restarts.

    This is NOT content-dependent — the same logical document keeps its
    ``document_id`` across content updates (what changes is ``document_sha256``
    and ``ingest_generation``).

    Parameters
    ----------
    source_id:
        Stable identifier for the Source (path or Producer reference), as
        supplied by the caller of ``ingest_paths()``.
    document_path:
        Filesystem path or logical path within a Producer bundle for this
        Document.  Must be the canonical path (absolute or bundle-relative).

    Returns
    -------
    str
        UUID string, e.g. ``"550e8400-e29b-41d4-a716-446655440000"``.

    Examples
    --------
    >>> make_document_id("/notes", "2024-01-01.md") == make_document_id("/notes", "2024-01-01.md")
    True
    >>> make_document_id("/notes", "a.md") != make_document_id("/notes", "b.md")
    True
    """
    value = f"{source_id}|{document_path}"
    return str(uuid.uuid5(POINT_NAMESPACE, value))


# ---------------------------------------------------------------------------
# Point identity
# ---------------------------------------------------------------------------


def point_id(document_id: str, document_sha256: str, chunk_index: int) -> str:
    """Return the deterministic UUIDv5 point ID for a Chunk in Qdrant.

    Composed of exactly three components (per PLAN.md line 52):
        ``document_id | document_sha256 | chunk_index``

    Design constraints:
    - Excludes ``ingest_generation``: identical content at any generation
      yields the same point IDs, enabling the idempotency dedup in step 1
      of the ingest flow.
    - Excludes ``ingest_state``: promote_generation and gc operate by payload
      mutation (``set_payload`` on stable IDs filtered by document_id +
      generation + state), never by rewriting IDs.
    - ``chunk_index`` is the only differentiator between Chunks of the same
      Document+content, so uniqueness degrades gracefully to chunk order.

    Parameters
    ----------
    document_id:
        Deterministic Document identifier (from ``make_document_id``).
    document_sha256:
        SHA-256 hex digest of the Document's raw text (content identity).
    chunk_index:
        Zero-based index of this Chunk within its Document.

    Returns
    -------
    str
        UUID string, e.g. ``"550e8400-e29b-41d4-a716-446655440000"``.

    Examples
    --------
    Uniqueness across chunk_index::

        >>> point_id("doc1", "sha", 0) != point_id("doc1", "sha", 1)
        True

    Uniqueness across sha (content change)::

        >>> point_id("doc1", "sha_old", 0) != point_id("doc1", "sha_new", 0)
        True

    Determinism (same inputs → same output)::

        >>> point_id("doc1", "sha", 0) == point_id("doc1", "sha", 0)
        True
    """
    value = f"{document_id}|{document_sha256}|{chunk_index}"
    return str(uuid.uuid5(POINT_NAMESPACE, value))


# ---------------------------------------------------------------------------
# Generation allocation helper (signature; implementation defers to store)
# ---------------------------------------------------------------------------


def next_ingest_generation(max_active_generation: int | None) -> int:
    """Compute the next ingest_generation to allocate for a Document.

    Called after ``gc`` has retired stale pending/failed Chunks so that
    ``max_active_generation`` reflects the current state cleanly.

    The ingest flow (per PLAN.md § Identity model, step 2):
        1. ``gc`` any stale ``pending``/``failed`` Chunks for this document_id.
        2. Read max ``ingest_generation`` G for document_id (via
           ``store.max_active_generation(document_id)``).
        3. Allocate G+1 for the new pending batch.
        4. Upsert new Chunks at generation G+1 with ``ingest_state=pending``.

    Parameters
    ----------
    max_active_generation:
        Current maximum active generation for the document_id, or ``None``
        if no active generation exists (first ingest or all prior attempts
        failed / were gc'd).

    Returns
    -------
    int
        The generation number to assign to the new pending batch (≥ 1).

    Examples
    --------
    >>> next_ingest_generation(None)
    1
    >>> next_ingest_generation(0)
    1
    >>> next_ingest_generation(3)
    4
    """
    if max_active_generation is None:
        return 1
    return max_active_generation + 1
