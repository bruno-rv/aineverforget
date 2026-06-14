"""aineverforget.verify — verify probes for the ingest Quality Gate.

Signatures and docstrings are final; implement the body.

The verify step (per PLAN.md § Identity model, step 3) is the knowledge-indexer
Quality Gate.  It runs after upsert of the pending G+1 Chunks and before
promote_generation().  Probes compete against the verification view — a Qdrant
filter that includes BOTH the active Corpus AND the pending generation (so
probes test against realistic competition, not just the Document's own Chunks).

Probe types (per PLAN.md § Quality Gates):
- topical:   A topic-level query must surface the pending generation among
             active results (proves the Document is generally retrievable).
- specific:  A query for a specific known fact substring must return a Chunk
             containing that substring (proves a key detail is findable).
- negative:  An unrelated query must NOT surface the pending generation
             (proves the Document doesn't over-retrieve for unrelated topics).

Cold-start rule (per PLAN.md § Identity model):
- If no unrelated active Document exists yet (first ingest or near-empty
  Corpus), the negative probe is DEFERRED — it cannot be meaningful when
  the pending Chunk is the only candidate.  Topical/specific still apply.
- Implementor: detect the cold-start condition by checking whether any
  active Chunks exist with ``document_id != document_id`` (i.e. at least one
  unrelated active Document).  If none, skip the negative probe and set
  ``verdict.negative_deferred = True``.

No heavy imports at module level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aineverforget.embedding import BGEM3Embedder
    from aineverforget.store import QdrantStore


# ---------------------------------------------------------------------------
# Probe input types
# ---------------------------------------------------------------------------


class ProbeType(str, Enum):
    """Type of a verification probe."""

    topical = "topical"
    specific = "specific"
    negative = "negative"


@dataclass
class Probe:
    """A single verification probe specification.

    Attributes
    ----------
    probe_type:
        Which probe type this is.
    query:
        The query string to issue.
    expected_substring:
        For ``specific`` probes: a substring that must appear in a returned
        Chunk's ``text`` for the probe to pass.  ``None`` for topical/negative.
    limit:
        Number of top results to retrieve for this probe.
    """

    probe_type: ProbeType
    query: str
    expected_substring: str | None = None
    limit: int = 10


# ---------------------------------------------------------------------------
# Probe result types
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Result of a single probe execution.

    Attributes
    ----------
    probe:
        The probe that was run.
    passed:
        Whether the probe passed.
    deferred:
        True only for negative probes deferred due to cold-start (no unrelated
        active Documents exist yet).  A deferred probe is NOT a failure.
    matched_chunk_ids:
        Point IDs of matching Chunks found in the verification view.
    detail:
        Human-readable explanation of the pass/fail/defer verdict.
    """

    probe: Probe
    passed: bool
    deferred: bool = False
    matched_chunk_ids: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class VerifyVerdict:
    """Aggregate verdict for all probes run on a pending generation.

    Attributes
    ----------
    document_id:
        The document being verified.
    generation:
        The pending generation that was tested (G+1).
    passed:
        True if ALL non-deferred probes passed.
    probe_results:
        Individual probe outcomes.
    negative_deferred:
        True if the negative probe was deferred due to cold-start.
    index_suspect:
        Set to True when passed=False.  The caller (ingest.py) emits
        INDEX_SUSPECT and marks the pending generation as failed.
    """

    document_id: str
    generation: int
    passed: bool
    probe_results: list[ProbeResult] = field(default_factory=list)
    negative_deferred: bool = False
    index_suspect: bool = False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_probes(
    store: "QdrantStore",
    document_id: str,
    generation: int,
    probes: list[Probe],
    embedder: "BGEM3Embedder",
) -> VerifyVerdict:
    """Run verification probes against the pending generation in the verify view.

    Orchestrates probe execution using the verification view filter from
    ``store.verification_view_filter(document_id, generation)``, which scopes
    queries to:
        EITHER ingest_state=active   (the live Corpus)
        OR     (document_id=X AND ingest_generation=G+1)  (the pending batch)

    This ensures:
    - Topical/specific probes prove the pending generation is findable AMONG
      active Chunks — not just when it's the only candidate.
    - Negative probes prove unrelated queries do NOT surface the pending
      generation despite it being in the view.

    Probe execution order: topical → specific → negative.

    Retrieval strategy:
    - topical:  hybrid search (dense+sparse RRF) via ``store.search()`` with
                the verification view filter.  Multi-word queries work correctly
                because RRF fuses dense semantic similarity with sparse lexical
                weights — no AND-within-chunk constraint.
    - specific: lexical MatchText scroll within the verification view.
                Single distinctive terms are reliable with MatchText; the term
                must appear verbatim in a chunk so lexical is the right gate.
    - negative: hybrid search (dense+sparse RRF) via ``store.search()`` with
                the verification view filter.  The pending document_id must
                NOT appear in the top-k results for an unrelated query.

    Cold-start detection:
    Before running the negative probe, check whether any active Chunks exist
    with ``document_id != document_id`` (using ``store.scroll()`` with no
    filters and checking for distinct document_ids).  If none, set
    ``probe_result.deferred = True`` and skip the negative probe.

    Pass / fail rules:
    - topical probe passes:   the pending generation's ``document_id`` appears
                              in the top-``probe.limit`` results.
    - specific probe passes:  at least one result contains
                              ``probe.expected_substring`` in its ``text``.
    - negative probe passes:  no result has ``document_id == document_id``
                              (the pending Document does NOT surface for an
                              unrelated query).
    - negative probe deferred: cold-start condition; counted as pass.

    Overall: ``passed = all(r.passed or r.deferred for r in probe_results)``.
    If not passed: ``verdict.index_suspect = True``.

    Parameters
    ----------
    store:
        ``QdrantStore`` instance to issue probe queries against.
    document_id:
        The document_id whose pending generation is being verified.
    generation:
        The pending generation number (G+1) to verify.
    probes:
        List of probes to run.  Typically supplied by the knowledge-indexer
        agent (one topical, one specific, one negative — see PLAN.md § Quality
        Gates / knowledge-indexer).
    embedder:
        ``BGEM3Embedder`` instance used to embed topical/negative probe queries
        for hybrid retrieval.

    Returns
    -------
    VerifyVerdict
        Aggregate verdict with individual probe results.
    """
    m = store._models()
    client = store._get_client()
    collection = store.collection_name

    # Build the verification view filter once: active OR (doc_id=X, gen=G+1)
    view_filter = store.verification_view_filter(document_id, generation)

    # Cold-start detection: does any active Document other than ours exist?
    scroll_result = store.scroll()
    unrelated_active_docs = [
        d for d in scroll_result["documents"]
        if d["document_id"] != document_id
    ]
    has_unrelated_active = len(unrelated_active_docs) > 0

    probe_results: list[ProbeResult] = []
    negative_deferred = False

    for probe in probes:
        if probe.probe_type == ProbeType.topical:
            result = _run_topical_hybrid(store, embedder, probe, document_id, generation, view_filter)
            probe_results.append(result)

        elif probe.probe_type == ProbeType.specific:
            result = _run_specific(
                m, client, collection, probe, document_id, generation, view_filter
            )
            probe_results.append(result)

        elif probe.probe_type == ProbeType.negative:
            if not has_unrelated_active:
                # Cold-start: defer the negative probe
                negative_deferred = True
                deferred_result = ProbeResult(
                    probe=probe,
                    passed=True,
                    deferred=True,
                    matched_chunk_ids=[],
                    detail=(
                        "negative probe DEFERRED: no unrelated active Documents "
                        "exist yet (cold-start). Topical/specific still apply."
                    ),
                )
                probe_results.append(deferred_result)
            else:
                result = _run_negative_hybrid(store, embedder, probe, document_id, generation, view_filter)
                probe_results.append(result)

    overall_passed = all(r.passed or r.deferred for r in probe_results)

    return VerifyVerdict(
        document_id=document_id,
        generation=generation,
        passed=overall_passed,
        probe_results=probe_results,
        negative_deferred=negative_deferred,
        index_suspect=not overall_passed,
    )


# ---------------------------------------------------------------------------
# Private probe runners
# ---------------------------------------------------------------------------


def _scroll_view(
    m: object,
    client: object,
    collection: str,
    query_filter: object,
    limit: int,
) -> list[object]:
    """Scroll the verification view for up to *limit* points matching *query_filter*."""
    points: list[object] = []
    offset = None

    while len(points) < limit:
        page_size = min(limit - len(points), 100)
        result, next_offset = client.scroll(
            collection_name=collection,
            scroll_filter=query_filter,
            limit=page_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(result)
        if next_offset is None:
            break
        offset = next_offset

    return points[:limit]


def _run_topical_hybrid(
    store: "QdrantStore",
    embedder: "BGEM3Embedder",
    probe: Probe,
    document_id: str,
    generation: int,
    view_filter: object,
) -> ProbeResult:
    """Topical probe via hybrid search: pending document_id+generation must appear in top-k.

    Uses dense+sparse RRF fusion (``store.search()`` with ``view_filter``).
    Multi-word queries work correctly because RRF merges dense semantic similarity
    with sparse weights — not subject to AND-within-chunk MatchText limitations.
    """
    query_emb = embedder.encode_query(probe.query)
    search_result = store.search(query_emb, limit=probe.limit, view_filter=view_filter)
    candidates = getattr(search_result, "candidates", [])

    matched_ids = [c.point_id for c in candidates]
    found_doc = any(
        c.document_id == document_id and c.ingest_generation == generation
        for c in candidates
    )

    if found_doc:
        detail = (
            f"topical PASS: document_id={document_id!r} gen={generation} found in top-{probe.limit} "
            f"hybrid results for query={probe.query!r}"
        )
    else:
        detail = (
            f"topical FAIL: document_id={document_id!r} gen={generation} NOT found in top-{probe.limit} "
            f"hybrid results for query={probe.query!r} (got {len(candidates)} candidates)"
        )

    return ProbeResult(
        probe=probe,
        passed=found_doc,
        deferred=False,
        matched_chunk_ids=matched_ids,
        detail=detail,
    )


def _run_specific(
    m: object,
    client: object,
    collection: str,
    probe: Probe,
    document_id: str,
    generation: int,
    view_filter: object,
) -> ProbeResult:
    """Specific probe: a chunk of the pending generation must contain expected_substring.

    Uses a MatchText query scoped to the verification view. The specific check
    (substring match) is done in Python on the returned chunk text — MatchText
    tokenizes which may differ from pure substring. We check BOTH:
    1. The chunk belongs to the pending generation's document_id AND generation.
    2. Its text contains probe.expected_substring (case-insensitive).

    If expected_substring is None, presence of any pending-gen chunk is sufficient.
    """
    query_filter = m.Filter(
        must=[
            m.FieldCondition(
                key="text",
                match=m.MatchText(text=probe.query),
            ),
            view_filter,
        ]
    )

    points = _scroll_view(m, client, collection, query_filter, probe.limit)

    matched_ids = [str(pt.id) for pt in points]

    # Filter to pending generation chunks of this document
    pending_gen_points = [
        pt for pt in points
        if (pt.payload or {}).get("document_id") == document_id
        and (pt.payload or {}).get("ingest_generation") == generation
    ]

    if probe.expected_substring is None:
        passed = len(pending_gen_points) > 0
        if passed:
            detail = (
                f"specific PASS: {len(pending_gen_points)} pending-gen chunk(s) "
                f"found for query={probe.query!r} (no expected_substring required)"
            )
        else:
            detail = (
                f"specific FAIL: no pending-gen chunks found for "
                f"document_id={document_id!r} gen={generation} "
                f"query={probe.query!r}"
            )
    else:
        sub = probe.expected_substring.lower()
        matching = [
            pt for pt in pending_gen_points
            if sub in ((pt.payload or {}).get("text", "")).lower()
        ]
        passed = len(matching) > 0
        if passed:
            detail = (
                f"specific PASS: expected_substring={probe.expected_substring!r} "
                f"found in {len(matching)} pending-gen chunk(s) "
                f"for query={probe.query!r}"
            )
        else:
            detail = (
                f"specific FAIL: expected_substring={probe.expected_substring!r} "
                f"not found in any of {len(pending_gen_points)} pending-gen chunk(s) "
                f"for document_id={document_id!r} gen={generation} "
                f"query={probe.query!r}"
            )

    return ProbeResult(
        probe=probe,
        passed=passed,
        deferred=False,
        matched_chunk_ids=matched_ids,
        detail=detail,
    )


def _run_negative_hybrid(
    store: "QdrantStore",
    embedder: "BGEM3Embedder",
    probe: Probe,
    document_id: str,
    generation: int,
    view_filter: object,
) -> ProbeResult:
    """Negative probe via hybrid search: pending document_id+generation must NOT appear in top-k.

    Uses dense+sparse RRF fusion (``store.search()`` with ``view_filter``).
    The probe passes if none of the returned chunks belong to the pending document_id
    at the current generation.

    This is meaningful only when unrelated active Documents exist (cold-start
    is handled by the caller — see run_probes).
    """
    query_emb = embedder.encode_query(probe.query)
    search_result = store.search(query_emb, limit=probe.limit, view_filter=view_filter)
    candidates = getattr(search_result, "candidates", [])

    matched_ids = [c.point_id for c in candidates]
    pending_found = any(
        c.document_id == document_id and c.ingest_generation == generation
        for c in candidates
    )
    passed = not pending_found

    if passed:
        detail = (
            f"negative PASS: document_id={document_id!r} correctly absent from "
            f"top-{probe.limit} hybrid results for unrelated query={probe.query!r}"
        )
    else:
        detail = (
            f"negative FAIL: document_id={document_id!r} unexpectedly appeared in "
            f"top-{probe.limit} hybrid results for unrelated query={probe.query!r}"
        )

    return ProbeResult(
        probe=probe,
        passed=passed,
        deferred=False,
        matched_chunk_ids=matched_ids,
        detail=detail,
    )
