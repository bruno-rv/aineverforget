"""aineverforget.ingest — high-level ingest orchestration.

The ingest flow (per PLAN.md § Identity model, rev 9)
------------------------------------------------------
Called by the CLI ``aineverforget ingest <paths> [--tag]`` and by the
``knowledge-indexer`` agent via that CLI.

For each path in *paths*:

    0. Acquire the ingest run lock (``run_lock.IngestLock``) — single-writer
       enforcement.  If a live concurrent ingest is running, abort with
       ``IngestLockOverlapError`` (exit code 3 from CLI).

    1. **No-op check**: compute ``document_sha256 = identity.sha256_text(raw_text)``
       for this path.  No-op ONLY if:
         - An ``ingest_state=active`` generation exists for ``document_id``
         - AND its ``document_sha256`` matches (identical content, identical
           point IDs → skip).
       If the Document has NO active generation (first ingest, or all prior
       attempts failed/were gc'd), proceed to re-ingest EVEN on a hash match.
       Before proceeding, gc any stale ``pending``/``failed`` Chunks for this
       ``document_id`` (``store.delete_generations(document_id, states=[pending,failed])``).

    2. **Load**: call ``loaders.get_loader(source_type).load(path)`` to get
       Documents.  For encrypted/scanned PDF verdicts, surface the verdict to
       the caller and skip ingest (these are not errors — the CLI emits a
       structured warning and continues to the next path).

    3. **Chunk**: for each Document, call
       ``chunking.chunk_document(document, settings, ingest_generation=G+1, ...)``.

    4. **Embed**: call ``embedder.encode_passages([c.text for c in chunks])``
       to get dense+sparse embeddings for all Chunks.

    5. **Upsert (pending)**: call ``store.upsert_chunks(chunks, embeddings)``
       — all Chunks land with ``ingest_state=pending``.

    6. **Verify**: call ``verify.run_probes(store, document_id, G+1, probes, embedder)``
       using hybrid retrieval + the verification view (``store.verification_view_filter``).

    7a. **Pass** → call ``store.promote_generation(document_id, G+1)`` to
        promote pending → active.  Then call
        ``store.delete_generations(document_id, states=[active],
        max_generation_to_delete=G)`` to retire the old active generation.

    7b. **Fail** → call ``store.delete_generations(document_id,
        states=[pending], generation=G+1)`` to purge the failed pending batch.
        Emit ``INDEX_SUSPECT`` in the result.  The prior active generation (if
        any) remains served.  A failed first ingest leaves the Document absent
        from results.

    8. Release the ingest lock (on exit of the ``IngestLock`` context manager
       — always released, even on exception).

NOTE: generation allocation is INSIDE the lock:
    G = store.max_active_generation(document_id)  (read after gc)
    G1 = identity.next_ingest_generation(G)       (G+1)

No heavy imports at module level.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aineverforget.config import Settings
    from aineverforget.embedding import BGEM3Embedder
    from aineverforget.store import QdrantStore
    from aineverforget.verify import Probe


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class IngestOutcome(str, Enum):
    """Outcome of a single path's ingest attempt.

    Values
    ------
    no_op:
        Content is identical to the current active generation; skipped.
    success:
        Verify passed; new generation is active.
    index_suspect:
        Verify failed; pending batch deleted; prior active (if any) stays served.
        Emit INDEX_SUSPECT signal.
    error:
        Unexpected exception; lock released; path skipped.
    skipped:
        Path skipped due to loader verdict (encrypted / scanned PDF, etc.).
    """

    no_op = "no_op"
    success = "success"
    index_suspect = "index_suspect"
    error = "error"
    skipped = "skipped"


@dataclass
class PathIngestResult:
    """Ingest result for a single path.

    Attributes
    ----------
    path:
        The source path that was processed.
    outcome:
        Ingest outcome (see ``IngestOutcome``).
    document_id:
        Deterministic document_id (None if load failed before ID computation).
        For multi-doc paths this is the first/primary document_id; see
        ``document_ids`` for the full list.
    generation:
        The ingest_generation that was upserted (None for no_op/skipped/error).
        For multi-doc paths this is the primary document's generation; see
        ``generations`` for the full list.
    chunk_count:
        Number of Chunks upserted (0 for no_op/skipped/error).
        For multi-doc paths this is the sum across all documents.
    document_ids:
        All document_ids produced by this path (single-element list for
        single-doc paths; populated for multi-doc paths).
    generations:
        All ingest_generations produced by this path (mirrors document_ids).
    loader_verdict:
        Loader verdict string (from ``Document.meta["loader_verdict"]``).
    detail:
        Human-readable detail for errors/warnings.
    """

    path: Path
    outcome: IngestOutcome
    document_id: str | None = None
    generation: int | None = None
    chunk_count: int = 0
    document_ids: list[str] = field(default_factory=list)
    generations: list[int] = field(default_factory=list)
    loader_verdict: str | None = None
    detail: str = ""


@dataclass
class IngestReport:
    """Aggregate report for an ``ingest_paths()`` call.

    Attributes
    ----------
    results:
        Per-path results.
    total_paths:
        Total paths attempted.
    success_count:
        Paths with outcome ``success``.
    no_op_count:
        Paths with outcome ``no_op``.
    index_suspect_count:
        Paths with outcome ``index_suspect``.
    error_count:
        Paths with outcome ``error``.
    skipped_count:
        Paths with outcome ``skipped``.
    """

    results: list[PathIngestResult] = field(default_factory=list)
    total_paths: int = 0
    success_count: int = 0
    no_op_count: int = 0
    index_suspect_count: int = 0
    error_count: int = 0
    skipped_count: int = 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def ingest_paths(
    paths: list[Path],
    *,
    tags: list[str] | None = None,
    source_id: str | None = None,
    producer: str = "user",
    settings: "Settings | None" = None,
    store: "QdrantStore | None" = None,
    embedder: "BGEM3Embedder | None" = None,
    probes: "list[Probe] | Callable[[object], list[Probe]] | None" = None,
    require_verify: bool = True,
    run_dir: Path | None = None,
    session_id: str | None = None,
) -> IngestReport:
    """Ingest one or more source paths into the aineverforget Corpus.

    This is the main ingest entry point, called by ``aineverforget ingest``
    and by the knowledge-indexer agent via that CLI verb.

    Implements the full ingest flow documented in this module's docstring
    (steps 0–8) for each path.  The ingest run lock wraps ALL paths in a
    single lock acquisition — ingest is atomic from the perspective of
    concurrent processes.

    Parameters
    ----------
    paths:
        Source file paths to ingest.  May be a mix of types (.md, .txt, .pdf).
    tags:
        Free-form tags to apply to all Chunks from this ingest call.
        Stored in the ``tags`` payload field.
    source_id:
        Stable source identifier to use for all paths.  If ``None``,
        defaults to ``str(path.resolve())`` per path.
    producer:
        Producer name to embed in all Chunk payloads (default ``"user"``).
    settings:
        Runtime settings.  If ``None``, loads via ``config.load_settings()``.
    store:
        Qdrant store.  If ``None``, creates a ``QdrantStore`` from *settings*.
    embedder:
        BGE-M3 embedder.  If ``None``, creates a ``BGEM3Embedder`` from *settings*.
    probes:
        Verification probes.  If ``None`` AND ``require_verify=True`` (the
        default), the call raises ``ValueError`` — the CLI fail-closed contract
        prevents accidental unverified promotion.  Pass ``require_verify=False``
        (via ``--no-verify`` CLI flag) for explicit trusted/bulk ingest without
        probes.  The CLI supplies a per-document probe factory for the
        knowledge-indexer agent workflow.
    require_verify:
        If ``True`` (default) AND ``probes is None``, raise ``ValueError`` to
        prevent silent unverified promotion.  Set to ``False`` only when the
        caller explicitly opts out of verification (e.g. ``--no-verify`` flag
        or programmatic bulk migration).
    run_dir:
        Directory for the ingest lock file (default ``Path("runs")``).
    session_id:
        Session identifier for the lock (default: UUID4 generated at call time).

    Returns
    -------
    IngestReport
        Aggregate report with per-path outcomes and counts.

    Raises
    ------
    ValueError
        If ``probes is None`` and ``require_verify=True`` (fail-closed contract).
    IngestLockOverlapError
        If a live concurrent ingest is running (from ``run_lock``).  The CLI
        converts this to exit code 3.
    """
    # When require_verify=True and no probes supplied, auto-derive per-document
    # from chunk content (same algorithm as cmd_verify). This is the normal CLI
    # path; explicit probes override auto-derivation for programmatic callers.
    # Lazy imports — keep module-level clean
    from aineverforget import chunking, identity
    from aineverforget.config import load_settings
    from aineverforget.loaders import LoaderVerdict, get_loader, infer_source_type
    from aineverforget.models import IngestState
    from aineverforget.run_lock import IngestLock
    from aineverforget import verify as verify_mod

    # Trigger loader self-registration by importing the loader submodules.
    # Each module registers itself into the global registry on import.
    import aineverforget.loaders.text  # noqa: F401
    import aineverforget.loaders.pdf   # noqa: F401

    # Resolve defaults
    if settings is None:
        settings = load_settings()

    if store is None:
        from aineverforget.store import QdrantStore
        store = QdrantStore(
            url=settings.qdrant_url,
            collection_name=settings.collection,
            dense_dim=settings.embed_dim,
        )

    if embedder is None:
        from aineverforget.embedding import BGEM3Embedder
        embedder = BGEM3Embedder(model_name=settings.embed_model)

    if session_id is None:
        session_id = str(uuid.uuid4())

    # Ensure collection exists
    store.ensure_collection()

    _tags = tags or []
    report = IngestReport(total_paths=len(paths))
    source_path_components = (
        _source_override_path_components(paths)
        if source_id is not None and len(paths) > 1
        else {}
    )

    # Step 0: Acquire single-writer lock around ALL paths
    with IngestLock(session_id=session_id, run_dir=run_dir):

        for path in paths:
            result = _ingest_one_path(
                path=path,
                path_count=len(paths),
                path_component=source_path_components.get(path.resolve()),
                source_id=source_id,
                producer=producer,
                settings=settings,
                store=store,
                embedder=embedder,
                probes=probes,
                require_verify=require_verify,
                tags=_tags,
                identity_mod=identity,
                chunking_mod=chunking,
                verify_mod=verify_mod,
                loaders_get_loader=get_loader,
                loaders_infer_source_type=infer_source_type,
                LoaderVerdict=LoaderVerdict,
                IngestState=IngestState,
            )
            report.results.append(result)
            if result.outcome == IngestOutcome.success:
                report.success_count += 1
            elif result.outcome == IngestOutcome.no_op:
                report.no_op_count += 1
            elif result.outcome == IngestOutcome.index_suspect:
                report.index_suspect_count += 1
            elif result.outcome == IngestOutcome.error:
                report.error_count += 1
            elif result.outcome == IngestOutcome.skipped:
                report.skipped_count += 1

    return report


# ---------------------------------------------------------------------------
# Per-path ingest implementation
# ---------------------------------------------------------------------------


def _ingest_one_path(
    *,
    path: Path,
    path_count: int,
    path_component: str | None,
    source_id: str | None,
    producer: str,
    settings: object,
    store: object,
    embedder: object,
    probes: object,
    require_verify: bool,
    tags: list[str],
    identity_mod: object,
    chunking_mod: object,
    verify_mod: object,
    loaders_get_loader: object,
    loaders_infer_source_type: object,
    LoaderVerdict: object,
    IngestState: object,
) -> PathIngestResult:
    """Process a single path through the full rev-9 ingest flow."""
    try:
        # Infer source type and get loader
        try:
            source_type = loaders_infer_source_type(path)
        except ValueError as exc:
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.error,
                detail=str(exc),
            )

        loader = loaders_get_loader(source_type)

        # Resolve per-path source_id
        path_source_id = source_id if source_id is not None else str(path.resolve())

        # Step 2: Load — get Documents from this path
        try:
            documents = list(loader.load(path))
        except Exception as exc:
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.error,
                detail=f"Loader error: {exc}",
            )

        if not documents:
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.error,
                detail="Loader returned no documents.",
            )

        # Apply caller overrides to ALL loaded documents.
        # A single file with --source-id keeps the original cross-machine-stable
        # identity: document_id = UUIDv5(NS, f"{source_id}|{source_id}").
        # When one source_id covers multiple paths or one loader returns multiple
        # Documents, add a logical path component so documents do not collapse into
        # successive generations of the same document_id.
        rewritten = []
        document_count = len(documents)
        for index, d in enumerate(documents):
            updates: dict = {"producer": producer}
            if source_id is not None:
                document_path = _source_override_document_path(
                    path_source_id,
                    path,
                    d,
                    document_index=index,
                    document_count=document_count,
                    path_count=path_count,
                    path_component=path_component,
                )
                updates["source_id"] = path_source_id
                updates["document_path"] = document_path
                updates["document_id"] = identity_mod.make_document_id(
                    path_source_id, document_path
                )
            rewritten.append(d.model_copy(update=updates))
        documents = rewritten

        # Use the first document for loader verdict (it applies to the whole path).
        doc = documents[0]

        # Check loader verdict — skip encrypted/scanned
        loader_verdict_val = doc.meta.get("loader_verdict")
        loader_verdict_str = (
            loader_verdict_val.value
            if hasattr(loader_verdict_val, "value")
            else str(loader_verdict_val) if loader_verdict_val else None
        )

        if loader_verdict_str in ("encrypted", "scanned"):
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.skipped,
                document_id=doc.document_id,
                loader_verdict=loader_verdict_str,
                detail=f"Skipped: loader verdict={loader_verdict_str!r}",
            )

        from aineverforget.models import IngestState as IS

        # Fix A: Process EACH document independently through its own
        # G/G1/verify/promote/retire cycle.  A multi-doc loader (e.g. PDF
        # with per-page Documents, or a ZIP loader) may return several
        # Documents per path; each has its own document_id and generation
        # counter.  All were previously keyed to documents[0] only.
        doc_results: list[PathIngestResult] = []

        for d in documents:
            doc_result = _ingest_one_document(
                path=path,
                document=d,
                loader_verdict_str=loader_verdict_str,
                store=store,
                embedder=embedder,
                probes=probes,
                require_verify=require_verify,
                tags=tags,
                settings=settings,
                identity_mod=identity_mod,
                chunking_mod=chunking_mod,
                verify_mod=verify_mod,
                IS=IS,
                producer=producer,
            )
            doc_results.append(doc_result)

        # Aggregate: return the "worst" outcome across all documents.
        # Priority: error > index_suspect > skipped > success > no_op.
        _OUTCOME_RANK = {
            IngestOutcome.error: 5,
            IngestOutcome.index_suspect: 4,
            IngestOutcome.skipped: 3,
            IngestOutcome.success: 2,
            IngestOutcome.no_op: 1,
        }
        primary = doc_results[0]
        if len(doc_results) == 1:
            # Single-doc: populate document_ids/generations from the single result.
            return PathIngestResult(
                path=primary.path,
                outcome=primary.outcome,
                document_id=primary.document_id,
                generation=primary.generation,
                chunk_count=primary.chunk_count,
                document_ids=(
                    [primary.document_id] if primary.document_id is not None else []
                ),
                generations=(
                    [primary.generation] if primary.generation is not None else []
                ),
                loader_verdict=primary.loader_verdict,
                detail=primary.detail,
            )
        # Multi-doc: always aggregate across all Documents, even if all outcomes
        # are the same (otherwise the report collapses to first doc only and
        # loses chunk_count/document_ids for the remaining documents).
        worst = max(doc_results, key=lambda r: _OUTCOME_RANK.get(r.outcome, 0))
        total_chunks = sum(r.chunk_count for r in doc_results)
        all_doc_ids = [r.document_id for r in doc_results if r.document_id is not None]
        all_generations = [r.generation for r in doc_results if r.generation is not None]
        return PathIngestResult(
            path=path,
            outcome=worst.outcome,
            document_id=primary.document_id,
            generation=primary.generation,
            chunk_count=total_chunks,
            document_ids=all_doc_ids,
            generations=all_generations,
            loader_verdict=loader_verdict_str,
            detail=(
                f"Multi-doc path ({len(doc_results)} docs): worst={worst.outcome.value} "
                f"doc_ids={all_doc_ids} | chunks={total_chunks}"
            ),
        )

    except Exception as exc:
        return PathIngestResult(
            path=path,
            outcome=IngestOutcome.error,
            detail=f"Unexpected error: {type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


_STOPWORDS = frozenset({
    "about", "after", "again", "also", "another", "because", "before",
    "between", "could", "every", "first", "from", "have", "here", "into",
    "more", "most", "other", "over", "same", "should", "some", "such",
    "than", "that", "their", "them", "then", "there", "these", "they",
    "this", "those", "through", "under", "very", "what", "when", "where",
    "which", "while", "with", "would", "your",
})


def _derive_probes_from_chunks(chunks: list, verify_mod: object) -> list:
    """Auto-derive verification probes from pending chunks.

    Mirrors the probe derivation in cmd_verify so that `aineverforget ingest`
    (without explicit probes) produces the same probe set as a subsequent
    `aineverforget verify` call.
    """
    Probe = verify_mod.Probe  # type: ignore[attr-defined]
    ProbeType = verify_mod.ProbeType  # type: ignore[attr-defined]

    first_chunk = chunks[0] if chunks else None
    title = (first_chunk.title if first_chunk and hasattr(first_chunk, "title") else "") or ""
    text0 = (first_chunk.text if first_chunk and hasattr(first_chunk, "text") else "") or ""
    topical_query = title.strip() if title.strip() else " ".join(text0.split()[:10])

    specific_word: str | None = None
    for chunk in chunks:
        text = (chunk.text if hasattr(chunk, "text") else "") or ""
        for word in text.split():
            w = word.strip(".,;:!?\"'()[]{}").lower()
            if len(w) >= 5 and w not in _STOPWORDS and w.isalpha():
                specific_word = word.strip(".,;:!?\"'()[]{}]")
                break
        if specific_word:
            break

    probes: list = [Probe(probe_type=ProbeType.topical, query=topical_query)]
    if specific_word:
        probes.append(Probe(
            probe_type=ProbeType.specific,
            query=specific_word,
            expected_substring=specific_word,
        ))
    probes.append(Probe(
        probe_type=ProbeType.negative,
        query="zxqjvf_wbhkm_plxnq_gjrtv_dfwck",
    ))
    return probes


def _ingest_one_document(
    *,
    path: "Path",
    document: object,
    loader_verdict_str: str | None,
    store: object,
    embedder: object,
    probes: object,
    require_verify: bool,
    tags: list[str],
    settings: object,
    identity_mod: object,
    chunking_mod: object,
    verify_mod: object,
    IS: object,
    producer: str,
) -> "PathIngestResult":
    """Process a single Document through the full G/G1/verify/promote/retire cycle.

    Fix A: called once per Document in a multi-doc Source so that EVERY
    document (not just documents[0]) gets verified and promoted independently.
    Fix D: promote_generation() is wrapped in try/except; any exception
    triggers rollback of G1 (pending+active) without touching the prior
    active generation G.
    """
    document_id: str = document.document_id  # type: ignore[attr-defined]
    document_sha256: str | None = document.document_sha256  # type: ignore[attr-defined]

    # Step 1: No-op check
    G = store.max_active_generation(document_id)

    if G is not None:
        active_sha = _get_active_sha(store, document_id)
        if active_sha is not None and active_sha == document_sha256:
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.no_op,
                document_id=document_id,
                loader_verdict=loader_verdict_str,
                detail="Content unchanged; skipped (no-op).",
            )

    # GC stale pending/failed for this document
    store.delete_generations(
        document_id,
        states=[IS.pending, IS.failed],
    )

    # Re-read G after gc
    G = store.max_active_generation(document_id)
    G1 = identity_mod.next_ingest_generation(G)

    # Step 3: Chunk this document
    chunks: list = chunking_mod.chunk_document(
        document,
        settings,
        ingest_generation=G1,
        embedding_model=settings.embed_model,
        producer=producer,
    )

    if not chunks:
        return PathIngestResult(
            path=path,
            outcome=IngestOutcome.skipped,
            document_id=document_id,
            loader_verdict=loader_verdict_str,
            detail="Document produced no chunks (empty content).",
        )

    # Apply tags
    if tags:
        chunks = [c.model_copy(update={"tags": tags}) for c in chunks]

    # Step 4: Embed
    texts = [c.text for c in chunks]
    embeddings = embedder.encode_passages(texts)

    # Step 5: Upsert pending
    store.upsert_chunks(chunks, embeddings)

    # Step 6: Verify — use explicit probes, auto-derive from chunks, or skip
    if probes is not None:
        _probes = probes(document) if callable(probes) else probes
    elif require_verify:
        _probes = _derive_probes_from_chunks(chunks, verify_mod)
    else:
        _probes = None  # --no-verify path

    if _probes is not None:
        verdict = verify_mod.run_probes(store, document_id, G1, _probes, embedder)
        verify_passed = verdict.passed
    else:
        verify_passed = True
        verdict = None

    if verify_passed:
        # Step 7a: Promote pending → active.
        expected_promoted = len(chunks)
        try:
            promoted_count = store.promote_generation(document_id, G1)
        except Exception as exc:
            # Fix D: promote raised — roll back G1 entirely, preserve prior G.
            try:
                store.delete_generations(
                    document_id,
                    states=[IS.pending, IS.active],
                    generation=G1,
                )
            except Exception:
                pass  # best-effort rollback
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.index_suspect,
                document_id=document_id,
                generation=G1,
                chunk_count=0,
                loader_verdict=loader_verdict_str,
                detail=(
                    f"INDEX_SUSPECT: promote_generation raised "
                    f"{type(exc).__name__}: {exc} for generation={G1}"
                ),
            )

        if promoted_count != expected_promoted:
            # Partial/failed promote → roll back G1.
            store.delete_generations(
                document_id,
                states=[IS.pending, IS.active],
                generation=G1,
            )
            return PathIngestResult(
                path=path,
                outcome=IngestOutcome.index_suspect,
                document_id=document_id,
                generation=G1,
                chunk_count=0,
                loader_verdict=loader_verdict_str,
                detail=(
                    f"INDEX_SUSPECT: promote_generation promoted "
                    f"{promoted_count}/{expected_promoted} chunks for generation={G1}"
                ),
            )

        # Retire old active generation (if any) — only after a full promote.
        if G is not None:
            store.delete_generations(
                document_id,
                states=[IS.active],
                max_generation_to_delete=G,
            )

        return PathIngestResult(
            path=path,
            outcome=IngestOutcome.success,
            document_id=document_id,
            generation=G1,
            chunk_count=len(chunks),
            loader_verdict=loader_verdict_str,
            detail=(
                f"Ingested generation={G1}, chunks={len(chunks)}"
                + (f", verify=deferred_negative={verdict.negative_deferred}" if verdict else "")
            ),
        )
    else:
        # Step 7b: Verify failed — delete pending batch, signal INDEX_SUSPECT
        store.delete_generations(
            document_id,
            states=[IS.pending],
            generation=G1,
        )
        detail_parts = [f"INDEX_SUSPECT: verify failed for generation={G1}"]
        if verdict:
            failed_probes = [
                r.detail for r in verdict.probe_results if not r.passed and not r.deferred
            ]
            if failed_probes:
                detail_parts.append("; ".join(failed_probes))
        return PathIngestResult(
            path=path,
            outcome=IngestOutcome.index_suspect,
            document_id=document_id,
            generation=G1,
            chunk_count=0,
            loader_verdict=loader_verdict_str,
            detail=" | ".join(detail_parts),
        )


def _source_override_document_path(
    source_id: str,
    path: Path,
    document: object,
    *,
    document_index: int,
    document_count: int,
    path_count: int,
    path_component: str | None,
) -> str:
    """Return the logical document_path to pair with an explicit source_id."""
    if path_count == 1 and document_count == 1:
        return source_id

    component = path_component
    if component is None:
        component = Path(str(getattr(document, "document_path", "") or path.name)).name
    if not component:
        component = path.name
    if document_count > 1:
        component = f"{component}#document-{document_index + 1}"
    return f"{source_id.rstrip('/')}/{component}"


def _source_override_path_components(paths: list[Path]) -> dict[Path, str]:
    """Return stable relative components for multi-path source_id overrides."""
    resolved_paths = [path.resolve() for path in paths]
    try:
        common_parent = Path(
            os.path.commonpath([str(path.parent) for path in resolved_paths])
        )
    except ValueError:
        common_parent = None

    components: dict[Path, str] = {}
    for resolved in resolved_paths:
        if common_parent is not None:
            try:
                component = resolved.relative_to(common_parent).as_posix()
            except ValueError:
                component = resolved.name
        else:
            component = resolved.as_posix().lstrip("/")
        components[resolved] = component or resolved.name
    return components


def _get_active_sha(store: object, document_id: str) -> str | None:
    """Return the document_sha256 of the max-generation active batch for document_id.

    Scrolls ALL active chunks for this document_id, identifies the highest
    ``ingest_generation``, and returns the ``document_sha256`` from that generation.
    This is max-gen aware: if multiple generations exist, only the newest matters.
    Returns None if no active chunks exist.
    """
    m = store._models()
    client = store._get_client()

    scroll_filter = m.Filter(
        must=[
            m.FieldCondition(
                key="ingest_state",
                match=m.MatchValue(value="active"),
            ),
            m.FieldCondition(
                key="document_id",
                match=m.MatchValue(value=document_id),
            ),
        ]
    )

    all_points = []
    offset = None
    while True:
        result, next_offset = client.scroll(
            collection_name=store.collection_name,
            scroll_filter=scroll_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_points.extend(result)
        if next_offset is None or not result:
            break
        offset = next_offset

    if not all_points:
        return None

    # Find max generation
    max_gen = -1
    max_sha = None
    for pt in all_points:
        payload = pt.payload or {}
        gen = payload.get("ingest_generation", -1)
        if gen > max_gen:
            max_gen = gen
            max_sha = payload.get("document_sha256")

    return max_sha
