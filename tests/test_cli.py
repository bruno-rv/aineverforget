"""Tests for aineverforget.cli — all verbs, --json output, exit codes.

No Qdrant server or embedding model required: QdrantStore and ingest_paths
are patched at the import boundary.

Exit-code contract (stable):
    0   success
    1   unexpected error
    2   not-implemented / usage error
    3   ingest lock overlap
    4   verify-fail / INDEX_SUSPECT
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aineverforget.cli import _derive_document_probes, main
from aineverforget.ingest import IngestOutcome, IngestReport, PathIngestResult
from aineverforget.models import SearchResult, RetrievedChunk
from aineverforget.verify import (
    Probe,
    ProbeResult,
    ProbeType,
    VerifyVerdict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides):
    """Return a minimal Settings-like MagicMock."""
    s = MagicMock()
    s.qdrant_url = "http://localhost:6333"
    s.collection = "test_collection"
    s.embed_model = "BAAI/bge-m3"
    s.embed_dim = 1024
    s.verify_topical_limit = 10
    s.verify_specific_limit = 5
    s.verify_negative_limit = 5
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_search_result(query: str = "") -> SearchResult:
    chunk = RetrievedChunk(
        score=0.95,
        point_id="550e8400-e29b-41d4-a716-446655440000",
        source_id="/notes",
        source_type="markdown",
        document_id="doc-001",
        document_path="/notes/test.md",
        document_sha256="a" * 64,
        title="Test Note",
        chunk_index=0,
        heading_path="# Test",
        pdf_page=None,
        tags=["test"],
        text="This is a test chunk.",
        producer="user",
        ingested_at="2024-01-15T10:30:00+00:00",
        ingest_generation=1,
    )
    return SearchResult(
        query=query,
        candidates=[chunk],
        dense_hits=1,
        sparse_hits=1,
        candidate_count=1,
        citationable_count=1,
    )


def _make_ingest_report(
    success: int = 1,
    no_op: int = 0,
    index_suspect: int = 0,
    error: int = 0,
    skipped: int = 0,
    paths: list[str] | None = None,
) -> IngestReport:
    if paths is None:
        paths = ["/tmp/test.md"] * (success + no_op + index_suspect + error + skipped)
    results = []
    for i, p in enumerate(paths):
        if i < success:
            outcome = IngestOutcome.success
        elif i < success + no_op:
            outcome = IngestOutcome.no_op
        elif i < success + no_op + index_suspect:
            outcome = IngestOutcome.index_suspect
        elif i < success + no_op + index_suspect + error:
            outcome = IngestOutcome.error
        else:
            outcome = IngestOutcome.skipped
        results.append(
            PathIngestResult(
                path=Path(p),
                outcome=outcome,
                document_id="doc-001" if outcome != IngestOutcome.error else None,
                generation=1 if outcome == IngestOutcome.success else None,
                chunk_count=3 if outcome == IngestOutcome.success else 0,
                loader_verdict="ok",
                detail="",
            )
        )
    total = success + no_op + index_suspect + error + skipped
    return IngestReport(
        results=results,
        total_paths=total,
        success_count=success,
        no_op_count=no_op,
        index_suspect_count=index_suspect,
        error_count=error,
        skipped_count=skipped,
    )


def _make_verdict(passed: bool = True, index_suspect: bool = False) -> VerifyVerdict:
    probe = Probe(probe_type=ProbeType.topical, query="test query", limit=10)
    probe_result = ProbeResult(
        probe=probe,
        passed=passed,
        deferred=False,
        matched_chunk_ids=["id-1"],
        detail="topical PASS: doc found" if passed else "topical FAIL: doc not found",
    )
    return VerifyVerdict(
        document_id="doc-001",
        generation=1,
        passed=passed,
        probe_results=[probe_result],
        negative_deferred=False,
        index_suspect=index_suspect,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_settings():
    with patch("aineverforget.cli.load_settings", side_effect=ImportError), \
         patch("aineverforget.config.load_settings", return_value=_make_settings()):
        yield _make_settings()


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_help_output_contains_subcommands(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "ingest" in out
    assert "search" in out
    assert "status" in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# No subcommand → SystemExit (argparse exits with code 2)
# ---------------------------------------------------------------------------


def test_no_subcommand_exits():
    with pytest.raises(SystemExit) as exc:
        main([])
    # argparse exits 2 for missing required subcommand
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

_INGEST_PATH = "aineverforget.ingest.ingest_paths"
# cli.py uses lazy imports so we patch at the source module level
_LOAD_SETTINGS = "aineverforget.config.load_settings"
_QDRANT_STORE = "aineverforget.store.QdrantStore"
_EMBEDDER = "aineverforget.embedding.BGEM3Embedder"
_RUN_PROBES_SRC = "aineverforget.verify.run_probes"


@pytest.fixture()
def patch_ingest():
    """Patch ingest_paths to return a successful single-file report."""
    report = _make_ingest_report(success=1)
    with patch(_INGEST_PATH, return_value=report) as mock:
        yield mock


def test_ingest_success_exit_code(patch_ingest, tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# Hello")
    code = main(["ingest", str(f)])
    assert code == 0


def test_ingest_success_json_schema(patch_ingest, tmp_path, capsys):
    f = tmp_path / "note.md"
    f.write_text("# Hello")
    code = main(["ingest", "--json", str(f)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    # Stable JSON schema keys
    assert "total_paths" in data
    assert "success_count" in data
    assert "no_op_count" in data
    assert "index_suspect_count" in data
    assert "error_count" in data
    assert "skipped_count" in data
    assert "results" in data
    assert isinstance(data["results"], list)


def test_ingest_json_result_item_schema(patch_ingest, tmp_path, capsys):
    f = tmp_path / "note.md"
    f.write_text("# Hello")
    main(["ingest", "--json", str(f)])
    data = json.loads(capsys.readouterr().out)
    item = data["results"][0]
    assert "path" in item
    assert "outcome" in item
    assert "document_id" in item
    assert "generation" in item
    assert "chunk_count" in item
    assert "loader_verdict" in item
    assert "detail" in item


def test_ingest_json_result_item_includes_document_ids_and_generations(tmp_path, capsys):
    """ingest --json per-result items must include document_ids and generations fields.

    These fields are needed so CLI/agent callers can see secondary document ids
    and generations for multi-doc paths.  Single-doc paths return single-element
    lists; multi-doc paths return one entry per document.
    """
    # Single-doc success: document_ids=[document_id], generations=[generation]
    report = _make_ingest_report(success=1)
    # The helper sets document_id="doc-001", generation=1 — verify lists match.
    with patch(_INGEST_PATH, return_value=report):
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        main(["ingest", "--json", str(f)])
    data = json.loads(capsys.readouterr().out)
    item = data["results"][0]
    assert "document_ids" in item, "document_ids must be present in per-result JSON"
    assert "generations" in item, "generations must be present in per-result JSON"
    assert isinstance(item["document_ids"], list), "document_ids must be a list"
    assert isinstance(item["generations"], list), "generations must be a list"

    # Multi-doc: build a report with two results merged into one PathIngestResult.
    multi_result = PathIngestResult(
        path=Path("/tmp/multi.pdf"),
        outcome=IngestOutcome.success,
        document_id="doc-page-1",
        generation=2,
        chunk_count=6,
        document_ids=["doc-page-1", "doc-page-2"],
        generations=[2, 2],
        loader_verdict=None,
        detail="Multi-doc path (2 docs): worst=success",
    )
    multi_report = IngestReport(
        results=[multi_result],
        total_paths=1,
        success_count=1,
    )
    with patch(_INGEST_PATH, return_value=multi_report):
        g = tmp_path / "multi.pdf"
        g.write_bytes(b"%PDF-1.4")
        main(["ingest", "--json", str(g)])
    data2 = json.loads(capsys.readouterr().out)
    item2 = data2["results"][0]
    assert item2["document_ids"] == ["doc-page-1", "doc-page-2"], (
        f"document_ids mismatch: {item2['document_ids']!r}"
    )
    assert item2["generations"] == [2, 2], (
        f"generations mismatch: {item2['generations']!r}"
    )


def test_ingest_index_suspect_exit_code(tmp_path):
    report = _make_ingest_report(index_suspect=1)
    with patch(_INGEST_PATH, return_value=report):
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        code = main(["ingest", str(f)])
    assert code == 4


def test_ingest_no_op_exit_code(tmp_path):
    report = _make_ingest_report(no_op=1)
    with patch(_INGEST_PATH, return_value=report):
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        code = main(["ingest", str(f)])
    assert code == 0


def test_ingest_error_exit_code(tmp_path):
    report = _make_ingest_report(error=1)
    with patch(_INGEST_PATH, return_value=report):
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        code = main(["ingest", str(f)])
    assert code == 1


def test_ingest_not_implemented_exit_code(tmp_path):
    with patch(_INGEST_PATH, side_effect=NotImplementedError("stub")):
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        code = main(["ingest", "--json", str(f)])
    assert code == 2


def test_ingest_lock_overlap_exit_code(tmp_path):
    from aineverforget.run_lock import IngestLockOverlapError
    with patch(_INGEST_PATH, side_effect=IngestLockOverlapError("concurrent ingest")):
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        code = main(["ingest", "--json", str(f)])
    assert code == 3


def test_ingest_lock_overlap_json_error_key(tmp_path, capsys):
    from aineverforget.run_lock import IngestLockOverlapError
    with patch(_INGEST_PATH, side_effect=IngestLockOverlapError("concurrent ingest")):
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        main(["ingest", "--json", str(f)])
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "lock_overlap"
    assert "message" in data


def test_ingest_not_implemented_json_schema(tmp_path, capsys):
    with patch(_INGEST_PATH, side_effect=NotImplementedError):
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        main(["ingest", "--json", str(f)])
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "not_implemented"
    assert data["verb"] == "ingest"


def test_ingest_passes_tags(tmp_path):
    report = _make_ingest_report(success=1)
    with patch(_INGEST_PATH, return_value=report) as mock:
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        main(["ingest", "--tag", "work", "--tag", "2024", str(f)])
    call_kwargs = mock.call_args.kwargs
    assert "work" in call_kwargs["tags"]
    assert "2024" in call_kwargs["tags"]


def test_ingest_passes_producer(tmp_path):
    report = _make_ingest_report(success=1)
    with patch(_INGEST_PATH, return_value=report) as mock:
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        main(["ingest", "--producer", "agent-x", str(f)])
    call_kwargs = mock.call_args.kwargs
    assert call_kwargs["producer"] == "agent-x"


def test_ingest_passes_probe_factory_by_default(tmp_path):
    report = _make_ingest_report(success=1)
    with patch(_INGEST_PATH, return_value=report) as mock:
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        main(["ingest", str(f)])
    call_kwargs = mock.call_args.kwargs
    assert call_kwargs["require_verify"] is True
    assert callable(call_kwargs["probes"])


def test_ingest_no_verify_disables_probe_factory(tmp_path):
    report = _make_ingest_report(success=1)
    with patch(_INGEST_PATH, return_value=report) as mock:
        f = tmp_path / "note.md"
        f.write_text("# Hello")
        main(["ingest", "--no-verify", str(f)])
    call_kwargs = mock.call_args.kwargs
    assert call_kwargs["require_verify"] is False
    assert call_kwargs["probes"] is None


def test_ingest_probe_factory_prefers_body_for_specific_probe():
    document = SimpleNamespace(
        title="Agent Simulation DataSync",
        raw_text=(
            "# Agent Simulation DataSync\n\n"
            "Alice Chen chose blue-green deployment for DataSync."
        ),
    )

    probes = _derive_document_probes(document, _make_settings())
    specific = next(p for p in probes if p.probe_type is ProbeType.specific)

    assert specific.query == "Alice"
    assert specific.expected_substring == "Alice"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _patch_search(query: str = "test query"):
    settings_mock = _make_settings()
    store_mock = MagicMock()
    embedder_mock = MagicMock()
    embedder_mock.encode_query.return_value = MagicMock()
    store_mock.search.return_value = _make_search_result(query="")  # store may leave blank

    return (
        patch(_LOAD_SETTINGS, return_value=settings_mock),
        patch(_QDRANT_STORE, return_value=store_mock),
        patch(_EMBEDDER, return_value=embedder_mock),
    )


def test_search_success_exit_code():
    p1, p2, p3 = _patch_search()
    with p1, p2, p3:
        code = main(["search", "test query"])
    assert code == 0


def test_search_json_schema(capsys):
    p1, p2, p3 = _patch_search()
    with p1, p2, p3:
        code = main(["search", "--json", "test query"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    # Stable schema keys from SearchResult
    assert "query" in data
    assert "candidates" in data
    assert "dense_hits" in data
    assert "sparse_hits" in data
    assert "candidate_count" in data
    assert "citationable_count" in data
    assert isinstance(data["candidates"], list)


def test_search_json_query_echoed(capsys):
    """CLI must populate query in the JSON output (not leave it blank)."""
    p1, p2, p3 = _patch_search()
    with p1, p2, p3:
        main(["search", "--json", "my search query"])
    data = json.loads(capsys.readouterr().out)
    assert data["query"] == "my search query"


def test_search_json_candidate_schema(capsys):
    p1, p2, p3 = _patch_search()
    with p1, p2, p3:
        main(["search", "--json", "test query"])
    data = json.loads(capsys.readouterr().out)
    assert len(data["candidates"]) == 1
    c = data["candidates"][0]
    for key in ("score", "point_id", "source_id", "document_id", "document_path",
                "title", "chunk_index", "text", "producer", "ingested_at",
                "ingest_generation", "tags"):
        assert key in c, f"missing key in candidate: {key}"


def test_search_unexpected_error_exit_code():
    settings_mock = _make_settings()
    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, side_effect=RuntimeError("Qdrant down")):
        code = main(["search", "--json", "query"])
    assert code == 1


def test_search_not_implemented_json():
    settings_mock = _make_settings()
    store_mock = MagicMock()
    embedder_mock = MagicMock()
    embedder_mock.encode_query.return_value = MagicMock()
    store_mock.search.side_effect = NotImplementedError("stub")
    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock), \
         patch(_EMBEDDER, return_value=embedder_mock):
        code = main(["search", "--json", "query"])
    assert code == 2


# ---------------------------------------------------------------------------
# lexscan
# ---------------------------------------------------------------------------


def _patch_lexscan(result: dict | None = None):
    if result is None:
        result = {
            "term": "architecture",
            "chunk_count": 5,
            "document_count": 2,
            "chunks": [
                {"document_id": "doc-001", "chunk_index": 0, "text": "... architecture ..."}
            ],
        }
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.lexscan.return_value = result
    return (
        patch(_LOAD_SETTINGS, return_value=settings_mock),
        patch(_QDRANT_STORE, return_value=store_mock),
        result,
    )


def test_lexscan_success_exit_code():
    p1, p2, result = _patch_lexscan()
    with p1, p2:
        code = main(["lexscan", "architecture"])
    assert code == 0


def test_lexscan_json_schema(capsys):
    p1, p2, result = _patch_lexscan()
    with p1, p2:
        code = main(["lexscan", "--json", "architecture"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert "term" in data
    assert "chunk_count" in data
    assert "document_count" in data
    assert "chunks" in data


def test_lexscan_count_flag_schema(capsys):
    """--count emits only term/chunk_count/document_count (no chunks list)."""
    p1, p2, result = _patch_lexscan()
    with p1, p2:
        code = main(["lexscan", "--json", "--count", "architecture"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert "term" in data
    assert "chunk_count" in data
    assert "document_count" in data
    assert "chunks" not in data


def test_lexscan_unexpected_error_exit_code():
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.lexscan.side_effect = RuntimeError("fail")
    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock):
        code = main(["lexscan", "--json", "term"])
    assert code == 1


# ---------------------------------------------------------------------------
# scroll
# ---------------------------------------------------------------------------


def _patch_scroll(result: dict | None = None):
    if result is None:
        result = {
            "document_count": 3,
            "chunk_count": 15,
            "documents": [
                {
                    "document_id": "doc-001",
                    "document_path": "/notes/test.md",
                    "ingest_generation": 1,
                    "source_type": "markdown",
                }
            ],
        }
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.scroll.return_value = result
    return (
        patch(_LOAD_SETTINGS, return_value=settings_mock),
        patch(_QDRANT_STORE, return_value=store_mock),
        result,
    )


def test_scroll_success_exit_code():
    p1, p2, result = _patch_scroll()
    with p1, p2:
        code = main(["scroll"])
    assert code == 0


def test_scroll_json_schema(capsys):
    p1, p2, result = _patch_scroll()
    with p1, p2:
        code = main(["scroll", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert "document_count" in data
    assert "chunk_count" in data
    assert "documents" in data
    assert isinstance(data["documents"], list)


def test_scroll_unexpected_error_exit_code():
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.scroll.side_effect = RuntimeError("fail")
    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock):
        code = main(["scroll", "--json"])
    assert code == 1


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

_RUN_PROBES = "aineverforget.verify.run_probes"


def _make_verify_chunk(document_id: str = "doc-001", generation: int = 1) -> dict:
    """Return a minimal active chunk dict for cmd_verify scroll mock."""
    return {
        "document_id": document_id,
        "ingest_generation": generation,
        "title": "Test Document Title",
        "text": "This document contains specific information about testing procedures.",
        "source_id": "src-001",
        "source_type": "markdown",
        "document_path": "/tmp/test.md",
        "document_sha256": "abc123",
        "chunk_index": 0,
        "heading_path": None,
        "pdf_page": None,
        "tags": [],
        "producer": "user",
        "ingested_at": "2024-01-15T10:00:00+00:00",
        "point_id": "point-001",
        "score": None,
    }


def _patch_verify(verdict: VerifyVerdict | None = None, document_id: str = "doc-001", generation: int = 1):
    if verdict is None:
        verdict = _make_verdict(passed=True, index_suspect=False)
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.max_active_generation.return_value = generation
    # Fix: cmd_verify now calls store.get_chunks() (returns full chunk payloads with
    # "text") instead of store.scroll() (returns doc metadata without "text").
    store_mock.get_chunks.return_value = [_make_verify_chunk(document_id, generation)]
    return (
        patch(_LOAD_SETTINGS, return_value=settings_mock),
        patch(_QDRANT_STORE, return_value=store_mock),
        patch(_RUN_PROBES, return_value=verdict),
        verdict,
    )


def test_verify_pass_exit_code():
    p1, p2, p3, verdict = _patch_verify()
    with p1, p2, p3:
        code = main(["verify", "doc-001"])
    assert code == 0


def test_verify_fail_exit_code():
    verdict = _make_verdict(passed=False, index_suspect=True)
    p1, p2, p3, _ = _patch_verify(verdict)
    with p1, p2, p3:
        code = main(["verify", "doc-001"])
    assert code == 4


def test_verify_json_schema(capsys):
    p1, p2, p3, verdict = _patch_verify()
    with p1, p2, p3:
        code = main(["verify", "--json", "doc-001"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    # Stable schema keys
    assert "document_id" in data
    assert "generation" in data
    assert "passed" in data
    assert "negative_deferred" in data
    assert "index_suspect" in data
    assert "probe_results" in data
    assert isinstance(data["probe_results"], list)


def test_verify_json_probe_result_schema(capsys):
    p1, p2, p3, verdict = _patch_verify()
    with p1, p2, p3:
        main(["verify", "--json", "doc-001"])
    data = json.loads(capsys.readouterr().out)
    assert len(data["probe_results"]) == 1
    pr = data["probe_results"][0]
    for key in ("probe_type", "query", "expected_substring", "passed",
                "deferred", "matched_chunk_ids", "detail"):
        assert key in pr, f"missing key in probe_result: {key}"


def test_verify_json_fail_index_suspect_true(capsys):
    verdict = _make_verdict(passed=False, index_suspect=True)
    p1, p2, p3, _ = _patch_verify(verdict)
    with p1, p2, p3:
        main(["verify", "--json", "doc-001"])
    data = json.loads(capsys.readouterr().out)
    assert data["index_suspect"] is True
    assert data["passed"] is False


def test_verify_document_id_passed_to_run_probes():
    p1, p2, p3, _ = _patch_verify(document_id="my-doc-id")
    with p1, p2, p3 as mock_run:
        main(["verify", "my-doc-id"])
    call_args = mock_run.call_args
    assert call_args.args[1] == "my-doc-id"


def test_verify_generation_arg():
    p1, p2, p3, _ = _patch_verify(document_id="my-doc-id", generation=5)
    with p1, p2, p3 as mock_run:
        main(["verify", "--generation", "5", "my-doc-id"])
    call_args = mock_run.call_args
    assert call_args.args[2] == 5  # generation


def test_verify_unexpected_error_exit_code():
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.max_active_generation.return_value = 1
    store_mock.get_chunks.return_value = [_make_verify_chunk("doc-001", 1)]
    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock), \
         patch(_RUN_PROBES, side_effect=RuntimeError("boom")):
        code = main(["verify", "--json", "doc-001"])
    assert code == 1


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

_STATUS_DATA = {
    "collection": "ainf_corpus_bgem3_v1",
    "qdrant_url": "http://localhost:6333",
    "collection_exists": True,
    "point_count": 100,
    "active_chunk_count": 80,
    "document_count": 10,
    "source_count": 3,
    "last_ingested_at": "2024-01-15T10:30:00+00:00",
    "qdrant_healthy": True,
}


def _patch_status(data: dict | None = None):
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.status.return_value = data or _STATUS_DATA
    return (
        patch(_LOAD_SETTINGS, return_value=settings_mock),
        patch(_QDRANT_STORE, return_value=store_mock),
    )


def test_status_success_exit_code():
    p1, p2 = _patch_status()
    with p1, p2:
        code = main(["status"])
    assert code == 0


def test_status_json_schema(capsys):
    p1, p2 = _patch_status()
    with p1, p2:
        code = main(["status", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    for key in ("collection", "qdrant_url", "collection_exists", "point_count",
                "active_chunk_count", "document_count", "source_count",
                "last_ingested_at", "qdrant_healthy"):
        assert key in data, f"missing key in status JSON: {key}"


def test_status_unexpected_error_exit_code():
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.status.side_effect = RuntimeError("Qdrant unreachable")
    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock):
        code = main(["status", "--json"])
    assert code == 1


def test_status_error_json_schema(capsys):
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.status.side_effect = RuntimeError("down")
    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock):
        main(["status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert "error" in data
    assert "message" in data


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------

_GC_DATA = {
    "superseded_deleted": 5,
    "orphan_deleted": 2,
    "documents_affected": 3,
}


def _patch_gc(data: dict | None = None):
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.gc.return_value = data or _GC_DATA
    return (
        patch(_LOAD_SETTINGS, return_value=settings_mock),
        patch(_QDRANT_STORE, return_value=store_mock),
    )


def test_gc_success_exit_code():
    p1, p2 = _patch_gc()
    with p1, p2:
        code = main(["gc"])
    assert code == 0


def test_gc_json_schema(capsys):
    p1, p2 = _patch_gc()
    with p1, p2:
        code = main(["gc", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert "superseded_deleted" in data
    assert "orphan_deleted" in data
    assert "documents_affected" in data


def test_gc_unexpected_error_exit_code():
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.gc.side_effect = RuntimeError("fail")
    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock):
        code = main(["gc", "--json"])
    assert code == 1


# ---------------------------------------------------------------------------
# main() returns int (not None / does not call sys.exit)
# ---------------------------------------------------------------------------


def test_main_returns_int_not_none():
    """main() must return int so callers can relay the exit code."""
    report = _make_ingest_report(success=1)
    with patch(_INGEST_PATH, return_value=report):
        result = main(["ingest", "/tmp/fake.md"])
    assert isinstance(result, int)


def test_main_does_not_call_sys_exit(monkeypatch):
    """main() must NOT call sys.exit internally."""
    exit_called = []
    monkeypatch.setattr(sys, "exit", lambda code=0: exit_called.append(code))
    report = _make_ingest_report(success=1)
    with patch(_INGEST_PATH, return_value=report):
        main(["ingest", "/tmp/fake.md"])
    assert exit_called == [], "main() must not call sys.exit()"


# ---------------------------------------------------------------------------
# JSON always valid for all verbs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb,args,patch_target,return_value", [
    (
        "status",
        ["status", "--json"],
        [(_LOAD_SETTINGS, _make_settings()), (_QDRANT_STORE, MagicMock(**{"status.return_value": _STATUS_DATA}))],
        None,
    ),
    (
        "gc",
        ["gc", "--json"],
        [(_LOAD_SETTINGS, _make_settings()), (_QDRANT_STORE, MagicMock(**{"gc.return_value": _GC_DATA}))],
        None,
    ),
])
def test_json_is_valid_for_verb(capsys, verb, args, patch_target, return_value):
    """Each verb with --json must emit valid, parseable JSON."""
    patches = [patch(target, return_value=val) for target, val in patch_target]
    with patches[0], patches[1]:
        code = main(args)
    out = capsys.readouterr().out.strip()
    assert out, f"{verb} --json produced no output"
    parsed = json.loads(out)  # raises if invalid
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Test: cmd_verify passes non-None embedder to run_probes (finding #10)
# ---------------------------------------------------------------------------


def test_verify_passes_non_none_embedder_to_run_probes():
    """cmd_verify must instantiate an embedder and pass it (non-None) to run_probes.

    Pre-fix code called ``run_probes(..., probes=[])`` with no embedder kwarg,
    so this assertion would fail against that behavior.
    """
    captured = {}

    def capture_run_probes(store_arg, document_id, generation, probes, embedder=None):
        captured["embedder"] = embedder
        return _make_verdict(passed=True, index_suspect=False)

    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.max_active_generation.return_value = 1
    store_mock.scroll.return_value = {"documents": [_make_verify_chunk("doc-001", 1)]}
    embedder_mock = MagicMock()

    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock), \
         patch(_EMBEDDER, return_value=embedder_mock), \
         patch(_RUN_PROBES_SRC, side_effect=capture_run_probes):
        code = main(["verify", "doc-001"])

    assert code == 0
    assert "embedder" in captured, "run_probes was not called at all"
    assert captured["embedder"] is not None, (
        "cmd_verify must pass a non-None embedder to run_probes"
    )


# ---------------------------------------------------------------------------
# Fix C: cmd_verify derives real probes from stored chunks (finding #10)
# ---------------------------------------------------------------------------


def test_verify_derives_nonempty_probes_from_chunks():
    """Fix C: cmd_verify must pass a non-empty probe list to run_probes.

    Prior to Fix C, cmd_verify always called run_probes(probes=[]) which
    trivially passed (no probe to fail).  After the fix, probes are derived
    from stored chunk content — at minimum a topical probe and a negative probe.
    """
    captured_probes = {}

    def capture_run_probes(store_arg, document_id, generation, probes, embedder=None):
        captured_probes["probes"] = probes
        return _make_verdict(passed=True, index_suspect=False)

    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.max_active_generation.return_value = 1
    store_mock.scroll.return_value = {"documents": [_make_verify_chunk("doc-001", 1)]}
    embedder_mock = MagicMock()

    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock), \
         patch(_EMBEDDER, return_value=embedder_mock), \
         patch(_RUN_PROBES_SRC, side_effect=capture_run_probes):
        code = main(["verify", "doc-001"])

    assert code == 0
    assert "probes" in captured_probes, "run_probes was not called"
    probes = captured_probes["probes"]
    assert len(probes) > 0, (
        "Fix C: cmd_verify must pass a non-empty probe list to run_probes; "
        f"got {probes!r}"
    )
    # At minimum one topical and one negative probe
    from aineverforget.verify import ProbeType
    probe_types = {p.probe_type for p in probes}
    assert ProbeType.topical in probe_types, (
        f"Fix C: must include a topical probe; got types={probe_types}"
    )
    assert ProbeType.negative in probe_types, (
        f"Fix C: must include a negative probe; got types={probe_types}"
    )


def test_verify_no_chunks_found_returns_error():
    """Fix C: cmd_verify returns error when no active chunks found for document."""
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.max_active_generation.return_value = 1
    # No chunks for this document (get_chunks returns empty list)
    store_mock.get_chunks.return_value = []
    embedder_mock = MagicMock()

    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock), \
         patch(_EMBEDDER, return_value=embedder_mock):
        code = main(["verify", "doc-nonexistent"])

    assert code == 1, "Fix C: cmd_verify must return error when no chunks found"


def test_verify_no_active_generation_returns_error():
    """Fix C: cmd_verify returns error when document has no active generation."""
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.max_active_generation.return_value = None  # no active gen
    store_mock.get_chunks.return_value = []
    embedder_mock = MagicMock()

    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock), \
         patch(_EMBEDDER, return_value=embedder_mock):
        code = main(["verify", "doc-no-gen"])

    assert code == 1, "Fix C: cmd_verify must return error when no active generation"


def test_verify_derives_all_three_probe_types_from_chunk_text():
    """Fix: cmd_verify must derive probes from real chunk TEXT (not doc metadata).

    The previous implementation used store.scroll() which returns document
    metadata (no "text" field), so specific_word was never found and only
    topical+negative probes ran.  Now store.get_chunks() is used (returns full
    chunk payloads including "text"), and all three probe types must be derived.

    This test asserts:
    - run_probes is called with at least 3 probes
    - One probe is ProbeType.topical
    - One probe is ProbeType.specific (derived from a word in chunk text)
    - One probe is ProbeType.negative
    """
    chunk_with_text = {
        "document_id": "doc-text-001",
        "ingest_generation": 1,
        "title": "Sample Document",
        "text": "The quantum entanglement phenomenon demonstrates unique properties.",
        "source_id": "src-001",
        "source_type": "markdown",
        "document_path": "/tmp/sample.md",
        "document_sha256": "abc123",
        "chunk_index": 0,
        "heading_path": None,
        "pdf_page": None,
        "tags": [],
        "producer": "user",
        "ingested_at": "2024-01-15T10:00:00+00:00",
        "point_id": "point-001",
    }

    verdict = _make_verdict(passed=True, index_suspect=False)
    settings_mock = _make_settings()
    store_mock = MagicMock()
    store_mock.max_active_generation.return_value = 1
    # get_chunks returns full chunk payloads WITH "text" field
    store_mock.get_chunks.return_value = [chunk_with_text]

    with patch(_LOAD_SETTINGS, return_value=settings_mock), \
         patch(_QDRANT_STORE, return_value=store_mock), \
         patch(_RUN_PROBES, return_value=verdict) as mock_run:
        code = main(["verify", "doc-text-001"])

    assert code == 0
    call_args = mock_run.call_args
    probes_passed = call_args.kwargs.get("probes") or call_args.args[3]
    probe_types = {p.probe_type for p in probes_passed}

    assert ProbeType.topical in probe_types, (
        "cmd_verify must always include a topical probe"
    )
    assert ProbeType.specific in probe_types, (
        "cmd_verify must derive a specific probe from chunk text (requires 'text' field "
        "from get_chunks); this fails if scroll() (no text) is used instead"
    )
    assert ProbeType.negative in probe_types, (
        "cmd_verify must always include a negative probe"
    )
    limits = {p.probe_type: p.limit for p in probes_passed}
    assert limits[ProbeType.topical] == settings_mock.verify_topical_limit
    assert limits[ProbeType.specific] == settings_mock.verify_specific_limit
    assert limits[ProbeType.negative] == settings_mock.verify_negative_limit
    # The get_chunks call must have been made with the correct document_id and generation
    store_mock.get_chunks.assert_called_once_with("doc-text-001", 1)
