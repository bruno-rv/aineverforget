"""Real integration smoke test — live bge-m3 embedder + live Qdrant server.

Validates the things mocks/:memory: cannot:
  H1  real BGEM3FlagModel output shape (dense dim, sparse int-coercion belief)
  H2  full-text MatchText on a REAL server (lexscan)
  H3  verify probes against the real hybrid-capable server (ingest<->verify seam)
  H4  real ingest->verify->promote->search end-to-end, no monkeypatching

Throwaway collection `ainf_smoke_v1`, dropped at start and end. Run:
    .venv/bin/python scripts/smoke_real.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

OK, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((OK if cond else FAIL, name, detail))
    print(f"[{OK if cond else FAIL}] {name} {('- ' + detail) if detail else ''}", flush=True)


def main() -> int:
    from aineverforget.config import load_settings
    from aineverforget.embedding import BGEM3Embedder
    from aineverforget.store import QdrantStore
    from aineverforget.ingest import ingest_paths
    from aineverforget.verify import Probe, ProbeType

    COLL = "ainf_smoke_v1"
    settings = load_settings(collection=COLL)

    # ---- H1: real embedder shape -----------------------------------------
    print("\n# H1 real bge-m3 embedder (first run downloads ~2.3GB model)...", flush=True)
    embedder = BGEM3Embedder()
    passages = embedder.encode_passages([
        "Engenharia de dados com Qdrant e embeddings densos e esparsos.",
        "RAG pipelines ground answers in retrieved chunks.",
    ])
    p0 = passages[0]
    check("dense dim == 1024", len(p0.dense) == 1024, f"got {len(p0.dense)}")
    check("sparse indices ascending+int",
          p0.sparse.indices == sorted(p0.sparse.indices)
          and all(isinstance(i, int) for i in p0.sparse.indices),
          f"n={len(p0.sparse.indices)} sample={p0.sparse.indices[:5]}")
    check("sparse len(indices)==len(values)", len(p0.sparse.indices) == len(p0.sparse.values))
    q = embedder.encode_query("o que é engenharia de dados com Qdrant?")
    check("query dense dim == 1024", len(q.dense) == 1024)

    # ---- server setup ----------------------------------------------------
    store = QdrantStore(url=settings.qdrant_url, collection_name=COLL)
    client = store._get_client()
    try:
        client.delete_collection(COLL)
    except Exception:
        pass
    store.ensure_collection()
    check("ensure_collection on real server", True)

    # ---- sample document -------------------------------------------------
    tmp = Path(tempfile.mkdtemp())
    md = tmp / "nota_curseduca.md"
    md.write_text(
        "# Nota sobre Curseduca\n\n"
        "## Contexto\n"
        "A plataforma Curseduca hospeda aulas de engenharia de dados. "
        "O termo distintivo aqui é Marmota para testar o full-text lexscan.\n\n"
        "## Decisão\n"
        "Usamos Qdrant com vetores densos e esparsos e fusão RRF.\n",
        encoding="utf-8",
    )

    probes = [
        Probe(probe_type=ProbeType.topical, query="Qdrant engenharia de dados"),
        Probe(probe_type=ProbeType.specific, query="Marmota", expected_substring="Marmota"),
        Probe(probe_type=ProbeType.negative, query="basquete futebol receita de bolo"),
    ]

    # ---- H3/H4: real ingest -> verify -> promote -------------------------
    print("\n# H3/H4 real ingest->verify->promote (no monkeypatch)...", flush=True)
    report = ingest_paths([md], settings=settings, store=store, embedder=embedder, probes=probes)
    outcomes = [r.outcome for r in report.results] if hasattr(report, "results") else []
    check("ingest produced a result", bool(outcomes), f"outcomes={[str(o) for o in outcomes]}")
    indexed = any(str(o).endswith("success") or str(o) == "success" for o in outcomes)
    check("document INDEXED (verify passed, promoted active)", indexed,
          f"outcomes={[str(o) for o in outcomes]}")

    # ---- H4: real hybrid search ------------------------------------------
    sr = store.search(embedder.encode_query("aulas de engenharia de dados na Curseduca"), limit=5)
    cands = getattr(sr, "candidates", [])
    check("hybrid search returns the doc", len(cands) >= 1,
          f"candidate_count={getattr(sr,'candidate_count',len(cands))} "
          f"dense_hits={getattr(sr,'dense_hits','?')} sparse_hits={getattr(sr,'sparse_hits','?')}")

    # ---- H2: real full-text lexscan --------------------------------------
    lx = store.lexscan("Marmota")  # lexscan returns a dict
    lx_count = lx.get("document_count", 0)
    check("full-text lexscan finds distinctive term (REAL server)", bool(lx_count),
          f"chunk_count={lx.get('chunk_count')} document_count={lx_count}")
    lx_none = store.lexscan("xyzzy_nonexistent_term_qpw")
    lx_none_count = lx_none.get("document_count", 0)
    check("lexscan returns nothing for absent term", not lx_none_count, f"count={lx_none_count}")

    # ---- status + gc -----------------------------------------------------
    st = store.status()
    check("status reports active chunks", (st.get("active_chunk_count", 0) or 0) >= 1, str(st))

    # ---- Finding #3: two active generations -> reads return only MAX -----
    # Manufacture a second active generation WITHOUT gc'ing the first, then
    # assert search/lexscan/status report ONLY the max generation per doc.
    print("\n# Finding #3 two active gens -> only max visible (REAL server)...", flush=True)
    from aineverforget import chunking
    from aineverforget.loaders import get_loader, infer_source_type
    import aineverforget.loaders.text  # noqa: F401  (registration)

    loader = get_loader(infer_source_type(md))
    doc2 = list(loader.load(md))[0]
    g1 = store.max_active_generation(doc2.document_id)
    g2 = (g1 or 0) + 1
    gen2_chunks = chunking.chunk_document(
        doc2, settings, ingest_generation=g2, embedding_model=settings.embed_model
    )
    gen2_emb = embedder.encode_passages([c.text for c in gen2_chunks])
    store.upsert_chunks(gen2_chunks, gen2_emb)          # lands pending @ gen2
    promoted = store.promote_generation(doc2.document_id, g2)  # gen2 -> active
    # NOTE: deliberately DO NOT retire gen1 -> two active generations coexist.
    check("two active generations coexist (gen1 + gen2)",
          store.max_active_generation(doc2.document_id) == g2 and promoted == len(gen2_chunks),
          f"g1={g1} g2={g2} promoted={promoted}/{len(gen2_chunks)}")

    # search: every returned candidate for this doc must be gen2 (the max)
    sr2 = store.search(
        embedder.encode_query("aulas de engenharia de dados na Curseduca"), limit=10
    )
    cands2 = [c for c in getattr(sr2, "candidates", []) if c.document_id == doc2.document_id]
    search_only_max = bool(cands2) and all(c.ingest_generation == g2 for c in cands2)
    check("search returns ONLY max generation", search_only_max,
          f"gens={sorted({c.ingest_generation for c in cands2})} expected=[{g2}]")

    # lexscan: distinctive term lives in both gens, but only gen2 must surface
    lx2 = store.lexscan("Marmota")
    lx2_chunks = [c for c in lx2.get("chunks", []) if c.get("document_id") == doc2.document_id]
    lex_only_max = bool(lx2_chunks) and all(
        c.get("ingest_generation") == g2 for c in lx2_chunks
    )
    check("lexscan returns ONLY max generation", lex_only_max,
          f"doc_count={lx2.get('document_count')} chunk_count={lx2.get('chunk_count')} "
          f"gens={sorted({c.get('ingest_generation') for c in lx2_chunks})} expected=[{g2}]")

    # status: document_count must still be 1 (deduped by doc, not inflated by 2 gens)
    st2 = store.status()
    check("status document_count deduped across generations",
          st2.get("document_count") == 1,
          f"document_count={st2.get('document_count')} active_chunk_count={st2.get('active_chunk_count')}")

    # ---- Finding #1: bare ingest (probes=None) is fail-closed ------------
    # The CLI `aineverforget ingest` without --no-verify maps to require_verify=True;
    # probes=None there MUST raise rather than silently promote unverified.
    print("\n# Finding #1 bare ingest does NOT promote unverified (fail-closed)...", flush=True)
    raised = False
    try:
        ingest_paths([md], settings=settings, store=store, embedder=embedder, probes=None)
    except ValueError:
        raised = True
    except Exception:
        raised = False
    check("bare ingest (probes=None, require_verify default) raises ValueError", raised,
          "fail-closed contract prevents silent unverified promotion")

    gc = store.gc()
    check("gc runs clean", isinstance(gc, dict), str(gc))

    # ---- cleanup ---------------------------------------------------------
    try:
        client.delete_collection(COLL)
    except Exception:
        pass

    print("\n==== SUMMARY ====", flush=True)
    n_fail = sum(1 for r in results if r[0] == FAIL)
    for status, name, detail in results:
        print(f"  {status}  {name}  {detail}")
    print(f"\n{len(results)-n_fail}/{len(results)} passed, {n_fail} failed", flush=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
