"""aineverforget.cli — command-line interface.

Entry point: ``aineverforget`` (installed via pyproject.toml console_scripts).

Subcommands
-----------
ingest   <paths…> [--tag TAG] [--source-id ID] [--producer NAME]
search   <query> [--limit N] [--source-id ID] [--path PATH] [--since ISO] [--tag TAG]
lexscan  <term>  [--source-id ID] [--path PATH] [--since ISO] [--tag TAG] [--count]
scroll           [--source-id ID] [--source-type TYPE] [--path PATH] [--since ISO] [--tag TAG]
verify   <document-id> [--generation N]
gc
status

All subcommands accept ``--json`` (output as JSON to stdout).

Exit codes
----------
0   success
1   unexpected error
2   not implemented / usage error
3   ingest lock overlap (concurrent ingest running)
4   verify-fail / INDEX_SUSPECT
5   malformed state (lock file error, etc.)

No heavy imports at module level: qdrant-client, FlagEmbedding, pypdf, mistune
are NOT imported here.  They are imported lazily inside the command functions.
``aineverforget --help`` must work even when those packages are not installed.
"""

from __future__ import annotations

import argparse
import json
import sys


# ---------------------------------------------------------------------------
# JSON output helpers
# ---------------------------------------------------------------------------


def _json_out(data: object, *, pretty: bool = False) -> None:
    """Print *data* as JSON to stdout."""
    if pretty:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(json.dumps(data, default=str))


def _not_implemented(verb: str, *, json_mode: bool) -> int:
    """Emit a structured 'not implemented' response and return exit code 2."""
    msg = {
        "error": "not_implemented",
        "verb": verb,
        "message": (
            f"aineverforget {verb}: not yet implemented. "
            "Build Phase A is in progress."
        ),
    }
    if json_mode:
        _json_out(msg)
    else:
        print(f"[aineverforget] {verb}: not yet implemented.", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Subcommand: ingest
# ---------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest one or more source paths into the Corpus.

    Orchestrates: acquire lock → load → chunk → embed → upsert(pending)
    → verify → promote/retire or mark-failed.

    Delegates to ``aineverforget.ingest.ingest_paths()``.
    Exits 3 on lock overlap, 4 on INDEX_SUSPECT, 0 on success.
    """
    from aineverforget.ingest import IngestOutcome, ingest_paths
    from aineverforget.run_lock import IngestLockOverlapError
    from pathlib import Path

    try:
        report = ingest_paths(
            paths=[Path(p) for p in args.paths],
            tags=args.tag if args.tag else None,
            source_id=getattr(args, "source_id", None),
            producer=getattr(args, "producer", "user"),
            require_verify=not args.no_verify,
        )
    except NotImplementedError:
        return _not_implemented("ingest", json_mode=args.json)
    except IngestLockOverlapError as e:
        if args.json:
            _json_out({"error": "lock_overlap", "message": str(e)})
        else:
            print(f"[aineverforget] ingest blocked: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        if args.json:
            _json_out({"error": "unexpected", "message": str(e)})
        else:
            print(f"[aineverforget] error: {e}", file=sys.stderr)
        return 1

    if args.json:
        _json_out(
            {
                "total_paths": report.total_paths,
                "success_count": report.success_count,
                "no_op_count": report.no_op_count,
                "index_suspect_count": report.index_suspect_count,
                "error_count": report.error_count,
                "skipped_count": report.skipped_count,
                "results": [
                    {
                        "path": str(r.path),
                        "outcome": r.outcome.value,
                        "document_id": r.document_id,
                        "generation": r.generation,
                        "chunk_count": r.chunk_count,
                        "document_ids": r.document_ids,
                        "generations": r.generations,
                        "loader_verdict": r.loader_verdict,
                        "detail": r.detail,
                    }
                    for r in report.results
                ],
            }
        )
    else:
        print(
            f"Ingested {report.success_count}/{report.total_paths} paths; "
            f"{report.no_op_count} no-op; "
            f"{report.index_suspect_count} INDEX_SUSPECT; "
            f"{report.error_count} error; "
            f"{report.skipped_count} skipped."
        )

    if report.index_suspect_count > 0:
        return 4
    if report.error_count > 0:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Subcommand: search
# ---------------------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    """Hybrid (dense+sparse RRF) search over active Chunks.

    Delegates to ``store.search()`` after encoding the query with
    ``BGEM3Embedder.encode_query()``.

    Returns a ``SearchResult`` JSON envelope including dense_hits, sparse_hits,
    candidate_count, citationable_count for the retriever Quality Gate.
    """
    try:
        from aineverforget.config import load_settings
        from aineverforget.embedding import BGEM3Embedder
        from aineverforget.store import QdrantStore

        settings = load_settings()
        embedder = BGEM3Embedder(model_name=settings.embed_model)
        store = QdrantStore(url=settings.qdrant_url, collection_name=settings.collection)

        query_emb = embedder.encode_query(args.query)
        result = store.search(
            query_emb,
            limit=args.limit,
            source_id=getattr(args, "source_id", None),
            document_path=getattr(args, "path", None),
            since=getattr(args, "since", None),
            tags=args.tag if args.tag else None,
        )
    except NotImplementedError:
        return _not_implemented("search", json_mode=args.json)
    except Exception as e:
        if args.json:
            _json_out({"error": "unexpected", "message": str(e)})
        else:
            print(f"[aineverforget] search error: {e}", file=sys.stderr)
        return 1

    # Ensure query field reflects the CLI arg (store may leave it blank)
    result = result.model_copy(update={"query": args.query})

    if args.json:
        _json_out(result.model_dump())
    else:
        for i, c in enumerate(result.candidates, 1):
            print(f"{i}. [{c.score:.4f}] {c.document_path} (chunk {c.chunk_index})")
            print(f"   {c.text[:120]}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: lexscan
# ---------------------------------------------------------------------------


def cmd_lexscan(args: argparse.Namespace) -> int:
    """Exhaustive content enumeration via full-text payload index.

    Uses Qdrant MatchText scroll (paginated, not top-k) to count ALL Chunks
    containing *term*.  Answers "how many times did I mention Y?".
    """
    try:
        from aineverforget.config import load_settings
        from aineverforget.store import QdrantStore

        settings = load_settings()
        store = QdrantStore(url=settings.qdrant_url, collection_name=settings.collection)

        result = store.lexscan(
            args.term,
            source_id=getattr(args, "source_id", None),
            document_path=getattr(args, "path", None),
            since=getattr(args, "since", None),
            tags=args.tag if args.tag else None,
        )
    except NotImplementedError:
        return _not_implemented("lexscan", json_mode=args.json)
    except Exception as e:
        if args.json:
            _json_out({"error": "unexpected", "message": str(e)})
        else:
            print(f"[aineverforget] lexscan error: {e}", file=sys.stderr)
        return 1

    if args.json:
        if getattr(args, "count", False):
            _json_out(
                {
                    "term": result["term"],
                    "chunk_count": result["chunk_count"],
                    "document_count": result["document_count"],
                    "occurrence_count": result.get("occurrence_count", 0),
                }
            )
        else:
            _json_out(result)
    else:
        print(
            f"'{result['term']}': {result['chunk_count']} chunks "
            f"in {result['document_count']} documents."
        )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: scroll
# ---------------------------------------------------------------------------


def cmd_scroll(args: argparse.Namespace) -> int:
    """Metadata enumeration: payload-filter scroll over active Chunks.

    Answers metadata questions like "which Sources tagged X?" without semantic
    search.  Results deduped by document_id / max active generation.
    """
    try:
        from aineverforget.config import load_settings
        from aineverforget.store import QdrantStore

        settings = load_settings()
        store = QdrantStore(url=settings.qdrant_url, collection_name=settings.collection)

        result = store.scroll(
            source_id=getattr(args, "source_id", None),
            source_type=getattr(args, "source_type", None),
            document_path=getattr(args, "path", None),
            since=getattr(args, "since", None),
            tags=args.tag if args.tag else None,
        )
    except NotImplementedError:
        return _not_implemented("scroll", json_mode=args.json)
    except Exception as e:
        if args.json:
            _json_out({"error": "unexpected", "message": str(e)})
        else:
            print(f"[aineverforget] scroll error: {e}", file=sys.stderr)
        return 1

    if args.json:
        _json_out(result)
    else:
        print(f"Documents: {result['document_count']} | Chunks: {result['chunk_count']}")
        for doc in result.get("documents", []):
            print(f"  {doc.get('document_path')} (gen {doc.get('ingest_generation')})")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: verify
# ---------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-run verification probes against an existing active generation.

    Fix C: derive real probes from stored active chunks for this document
    rather than running an empty probe list that trivially passes.

    Probe strategy:
    - topical: query from the document title (or first 10 words of first chunk)
    - specific: query from the first distinctive noun phrase found in a chunk
      (first word ≥ 5 chars from a chunk that is not a stopword)
    - negative: fixed unrelated query; deferred if corpus has no other docs
    """
    try:
        from aineverforget.config import load_settings
        from aineverforget.embedding import BGEM3Embedder
        from aineverforget.store import QdrantStore
        from aineverforget.verify import Probe, ProbeType, run_probes

        settings = load_settings()
        embedder = BGEM3Embedder(model_name=settings.embed_model)
        store = QdrantStore(url=settings.qdrant_url, collection_name=settings.collection)

        generation = getattr(args, "generation", None)
        if generation is None:
            generation = store.max_active_generation(args.document_id)

        if generation is None:
            if args.json:
                _json_out({"error": "not_found", "message": f"No active generation for {args.document_id!r}"})
            else:
                print(f"[aineverforget] verify: no active generation for {args.document_id!r}", file=sys.stderr)
            return 1

        # Fetch real active chunk payloads (incl. text) for this document/generation.
        # Fix: scroll() returns document metadata (no "text" field), so specific_word
        # was never derivable and only topical+negative probes ran.  get_chunks()
        # returns full chunk payloads including "text", enabling all three probe types.
        doc_chunks = store.get_chunks(args.document_id, generation)

        if not doc_chunks:
            if args.json:
                _json_out({"error": "not_found", "message": f"No active chunks for {args.document_id!r} gen={generation}"})
            else:
                print(f"[aineverforget] verify: no active chunks for {args.document_id!r} gen={generation}", file=sys.stderr)
            return 1

        # Derive topical probe from title or first chunk text (text is now available)
        first_chunk = doc_chunks[0]
        title = first_chunk.get("title") or ""
        topical_query = title.strip() if title.strip() else " ".join(first_chunk.get("text", "").split()[:10])

        # Derive specific probe: first word ≥ 5 chars from any chunk text
        _STOPWORDS = {"about", "after", "again", "also", "another", "because", "before",
                      "between", "could", "every", "first", "from", "have", "here", "into",
                      "more", "most", "other", "over", "same", "should", "some", "such",
                      "than", "that", "their", "them", "then", "there", "these", "they",
                      "this", "those", "through", "under", "very", "what", "when", "where",
                      "which", "while", "with", "would", "your"}
        specific_word: str | None = None
        for chunk in doc_chunks:
            for word in chunk.get("text", "").split():
                w = word.strip(".,;:!?\"'()[]{}").lower()
                if len(w) >= 5 and w not in _STOPWORDS and w.isalpha():
                    specific_word = word.strip(".,;:!?\"'()[]{}]")
                    break
            if specific_word:
                break

        probes: list = [
            Probe(probe_type=ProbeType.topical, query=topical_query),
        ]
        if specific_word:
            probes.append(Probe(
                probe_type=ProbeType.specific,
                query=specific_word,
                expected_substring=specific_word,
            ))
        probes.append(Probe(
            probe_type=ProbeType.negative,
            query="xyzzy_nonexistent_term_for_negative_probe_aineverforget",
        ))

        verdict = run_probes(store, args.document_id, generation, probes=probes, embedder=embedder)
    except NotImplementedError:
        return _not_implemented("verify", json_mode=args.json)
    except Exception as e:
        if args.json:
            _json_out({"error": "unexpected", "message": str(e)})
        else:
            print(f"[aineverforget] verify error: {e}", file=sys.stderr)
        return 1

    probe_results_data = [
        {
            "probe_type": r.probe.probe_type.value,
            "query": r.probe.query,
            "expected_substring": r.probe.expected_substring,
            "passed": r.passed,
            "deferred": r.deferred,
            "matched_chunk_ids": r.matched_chunk_ids,
            "detail": r.detail,
        }
        for r in verdict.probe_results
    ]

    if args.json:
        _json_out(
            {
                "document_id": verdict.document_id,
                "generation": verdict.generation,
                "passed": verdict.passed,
                "negative_deferred": verdict.negative_deferred,
                "index_suspect": verdict.index_suspect,
                "probe_results": probe_results_data,
            }
        )
    else:
        status = "PASS" if verdict.passed else "FAIL (INDEX_SUSPECT)"
        print(f"verify {args.document_id} gen={verdict.generation}: {status}")
        for r in verdict.probe_results:
            mark = "✓" if r.passed else ("~" if r.deferred else "✗")
            print(f"  {mark} [{r.probe.probe_type.value}] {r.detail}")

    return 4 if verdict.index_suspect else 0


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Show collection health and Corpus statistics.

    Reports: collection name, Qdrant URL, point count, active chunk count,
    document count, source count, last ingested_at, Qdrant health.
    """
    try:
        from aineverforget.config import load_settings
        from aineverforget.store import QdrantStore

        settings = load_settings()
        store = QdrantStore(url=settings.qdrant_url, collection_name=settings.collection)
        result = store.status()
    except NotImplementedError:
        return _not_implemented("status", json_mode=args.json)
    except Exception as e:
        if args.json:
            _json_out({"error": "unexpected", "message": str(e)})
        else:
            print(f"[aineverforget] status error: {e}", file=sys.stderr)
        return 1

    try:
        from aineverforget.run_journal import recent_events, recent_runs
        journal_events = recent_events(5)
        journal_runs = recent_runs(3)
    except Exception:
        journal_events = []
        journal_runs = []

    if args.json:
        result["journal"] = {"recent_events": journal_events, "recent_runs": journal_runs}
        _json_out(result)
    else:
        print(f"Collection:    {result.get('collection')}")
        print(f"Qdrant URL:    {result.get('qdrant_url')}")
        print(f"Healthy:       {result.get('qdrant_healthy')}")
        print(f"Points total:  {result.get('point_count')}")
        print(f"Active chunks: {result.get('active_chunk_count')}")
        print(f"Documents:     {result.get('document_count')}")
        print(f"Sources:       {result.get('source_count')}")
        print(f"Last ingest:   {result.get('last_ingested_at')}")
        if journal_runs:
            print()
            print("Recent runs:")
            for r in journal_runs:
                run_type = "ask" if r["event"] == "ASK_START" else "ingest"
                ts = r["ts"][:19]
                print(f"  {ts}  {run_type:<7}  dispatches={r['dispatches']}  id={r['run_id'][:8]}…")
        if journal_events:
            print()
            print("Recent events (last 5):")
            for e in journal_events:
                ts = e["ts"][:19]
                agent = f"  agent={e['agent']}" if e.get("agent") else ""
                print(f"  {ts}  {e['event']:<20}{agent}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: gc
# ---------------------------------------------------------------------------


def cmd_gc(args: argparse.Namespace) -> int:
    """Retire superseded Chunks and orphaned pending/failed records.

    Safe to run at any time; never touches the current max-active generation.
    """
    try:
        from aineverforget.config import load_settings
        from aineverforget.store import QdrantStore

        settings = load_settings()
        store = QdrantStore(url=settings.qdrant_url, collection_name=settings.collection)
        result = store.gc()
    except NotImplementedError:
        return _not_implemented("gc", json_mode=args.json)
    except Exception as e:
        if args.json:
            _json_out({"error": "unexpected", "message": str(e)})
        else:
            print(f"[aineverforget] gc error: {e}", file=sys.stderr)
        return 1

    if args.json:
        _json_out(result)
    else:
        print(
            f"gc: deleted {result.get('superseded_deleted', 0)} superseded + "
            f"{result.get('orphan_deleted', 0)} orphan Chunks "
            f"across {result.get('documents_affected', 0)} documents."
        )
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aineverforget",
        description=(
            "aineverforget — local-first, eval-gated knowledge brain. "
            "Ingest heterogeneous text, search and recall with citations."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    sub = parser.add_subparsers(dest="subcommand", metavar="<command>")
    sub.required = True

    # ── ingest ──────────────────────────────────────────────────────────────
    p_ingest = sub.add_parser(
        "ingest",
        help="Ingest source files into the Corpus.",
        description=(
            "Ingest one or more source files (markdown, PDF) into the Corpus. "
            "Implements: load → chunk → embed → upsert(pending) → verify → "
            "promote/retire.  Single-writer enforced via run lock."
        ),
    )
    p_ingest.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="One or more source file paths to ingest.",
    )
    p_ingest.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="Tag to apply to all Chunks from this ingest. Repeatable.",
    )
    p_ingest.add_argument(
        "--source-id",
        metavar="ID",
        help="Stable source identifier (default: resolved file path).",
    )
    p_ingest.add_argument(
        "--producer",
        default="user",
        metavar="NAME",
        help="Producer name to embed in Chunk payloads (default: user).",
    )
    p_ingest.add_argument(
        "--no-verify",
        action="store_true",
        default=False,
        dest="no_verify",
        help=(
            "Skip verification probes and promote directly to active. "
            "Use for trusted/bulk ingest only. By default, verification is required."
        ),
    )
    p_ingest.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON to stdout.",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    # ── search ──────────────────────────────────────────────────────────────
    p_search = sub.add_parser(
        "search",
        help="Hybrid semantic+lexical search over active Chunks.",
        description=(
            "Hybrid (dense+sparse RRF) search over active Chunks. "
            "Returns a SearchResult JSON envelope with per-modality hit counts "
            "for the retriever Quality Gate."
        ),
    )
    p_search.add_argument(
        "query",
        help="Query string.",
    )
    p_search.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of RRF-fused results (default: 10).",
    )
    p_search.add_argument(
        "--source-id",
        metavar="ID",
        help="Filter to a specific source_id.",
    )
    p_search.add_argument(
        "--path",
        metavar="PATH",
        help="Filter to a specific document_path.",
    )
    p_search.add_argument(
        "--since",
        metavar="ISO",
        help="Filter to Chunks ingested at or after this ISO-8601 datetime.",
    )
    p_search.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="Filter to Chunks with this tag. Repeatable (match any).",
    )
    p_search.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON to stdout.",
    )
    p_search.set_defaults(func=cmd_search)

    # ── lexscan ─────────────────────────────────────────────────────────────
    p_lexscan = sub.add_parser(
        "lexscan",
        help="Exhaustive content enumeration via full-text payload index.",
        description=(
            "Exhaustive content search: paginated scroll over ALL active Chunks "
            "whose text contains *term* (MatchText via full-text payload index). "
            "Answers 'how many times did I mention Y?' — not top-k, all occurrences."
        ),
    )
    p_lexscan.add_argument(
        "term",
        help="Search term for full-text MatchText filter.",
    )
    p_lexscan.add_argument(
        "--source-id",
        metavar="ID",
        help="Filter to a specific source_id.",
    )
    p_lexscan.add_argument(
        "--path",
        metavar="PATH",
        help="Filter to a specific document_path.",
    )
    p_lexscan.add_argument(
        "--since",
        metavar="ISO",
        help="Filter to Chunks ingested at or after this ISO-8601 datetime.",
    )
    p_lexscan.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="Filter to Chunks with this tag. Repeatable (match any).",
    )
    p_lexscan.add_argument(
        "--count",
        action="store_true",
        default=False,
        help="Output only chunk_count and document_count (no full payload).",
    )
    p_lexscan.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON to stdout.",
    )
    p_lexscan.set_defaults(func=cmd_lexscan)

    # ── scroll ──────────────────────────────────────────────────────────────
    p_scroll = sub.add_parser(
        "scroll",
        help="Metadata enumeration via payload filter (active-only, deduped).",
        description=(
            "Metadata-only scroll over active Chunks.  Answers questions like "
            "'which Sources are tagged X?' without semantic search.  "
            "Results deduped by document_id / max active generation."
        ),
    )
    p_scroll.add_argument(
        "--source-id",
        metavar="ID",
        help="Filter to a specific source_id.",
    )
    p_scroll.add_argument(
        "--source-type",
        metavar="TYPE",
        help="Filter to a specific source_type (e.g. markdown, pdf).",
    )
    p_scroll.add_argument(
        "--path",
        metavar="PATH",
        help="Filter to a specific document_path.",
    )
    p_scroll.add_argument(
        "--since",
        metavar="ISO",
        help="Filter to Chunks ingested at or after this ISO-8601 datetime.",
    )
    p_scroll.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="Filter to Chunks with this tag. Repeatable (match any).",
    )
    p_scroll.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON to stdout.",
    )
    p_scroll.set_defaults(func=cmd_scroll)

    # ── verify ──────────────────────────────────────────────────────────────
    p_verify = sub.add_parser(
        "verify",
        help="Re-run verification probes for a document.",
        description=(
            "Run verification probes (topical / specific / negative) against "
            "an existing active generation.  Used by the knowledge-indexer agent."
        ),
    )
    p_verify.add_argument(
        "document_id",
        help="document_id to verify.",
    )
    p_verify.add_argument(
        "--generation",
        type=int,
        metavar="N",
        help="Specific generation to verify (default: max active).",
    )
    p_verify.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON to stdout.",
    )
    p_verify.set_defaults(func=cmd_verify)

    # ── status ──────────────────────────────────────────────────────────────
    p_status = sub.add_parser(
        "status",
        help="Show collection health and Corpus statistics.",
        description=(
            "Report collection size, document/chunk counts, last ingest time, "
            "and Qdrant health."
        ),
    )
    p_status.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON to stdout.",
    )
    p_status.set_defaults(func=cmd_status)

    # ── gc ──────────────────────────────────────────────────────────────────
    p_gc = sub.add_parser(
        "gc",
        help="Retire superseded Chunks and orphaned pending/failed records.",
        description=(
            "Garbage-collect non-max active generations and orphaned "
            "pending/failed Chunks.  Safe at any time; never touches "
            "the current max-active generation."
        ),
    )
    p_gc.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON to stdout.",
    )
    p_gc.set_defaults(func=cmd_gc)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the ``aineverforget`` CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
