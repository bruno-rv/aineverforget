"""Isolate WHY verify fails on a legit doc against the real server."""
from __future__ import annotations
import tempfile
from pathlib import Path

from aineverforget.config import load_settings
from aineverforget.embedding import BGEM3Embedder
from aineverforget.store import QdrantStore
from aineverforget.chunking import chunk_document
from aineverforget.loaders.text import MarkdownLoader
from aineverforget.verify import Probe, ProbeType, run_probes
from aineverforget.models import IngestState

COLL = "ainf_dbg_v1"
settings = load_settings(collection=COLL)
store = QdrantStore(url=settings.qdrant_url, collection_name=COLL)
client = store._get_client()
try:
    client.delete_collection(COLL)
except Exception:
    pass
store.ensure_collection()

tmp = Path(tempfile.mkdtemp())
md = tmp / "n.md"
md.write_text(
    "# Nota sobre Curseduca\n\n## Contexto\nA plataforma Curseduca hospeda aulas de "
    "engenharia de dados. O termo distintivo aqui e Marmota para testar o full-text.\n\n"
    "## Decisao\nUsamos Qdrant com vetores densos e esparsos e fusao RRF.\n",
    encoding="utf-8",
)
docs = list(MarkdownLoader().load(md))
doc = docs[0]
print("document_id:", doc.document_id)
chunks = chunk_document(doc, settings, ingest_generation=1, embedding_model=settings.embed_model)
print("n_chunks:", len(chunks))
emb = BGEM3Embedder()
embs = emb.encode_passages([c.text for c in chunks])
GEN = 1
pend = []
for c in chunks:
    pend.append(c.model_copy(update={"ingest_generation": GEN, "ingest_state": IngestState.pending}))
store.upsert_chunks(pend, embs)
print("upserted pending chunks")

# raw presence
m = store._models()
allpts, _ = client.scroll(collection_name=COLL, limit=50, with_payload=True)
print("raw point count:", len(allpts))
for p in allpts[:3]:
    print("  state=", p.payload.get("ingest_state"), "gen=", p.payload.get("ingest_generation"),
          "text[:40]=", repr(p.payload.get("text", "")[:40]))

vfilter = store.verification_view_filter(doc.document_id, GEN)
print("\n-- MatchText probes against verification view --")
for term in ["Marmota", "Qdrant", "Qdrant engenharia de dados", "Curseduca"]:
    flt = m.Filter(
        must=[m.FieldCondition(key="text", match=m.MatchText(text=term))],
    )
    # combine with view: emulate what verify does
    pts, _ = client.scroll(collection_name=COLL, scroll_filter=flt, limit=50, with_payload=True)
    print(f"  MatchText({term!r}) [text-only] -> {len(pts)} hits")

print("\n-- run_probes (the real verify path) --")
probes = [
    Probe(probe_type=ProbeType.topical, query="Qdrant"),
    Probe(probe_type=ProbeType.specific, query="Marmota", expected_substring="Marmota"),
    Probe(probe_type=ProbeType.negative, query="basquete"),
]
verdict = run_probes(store, doc.document_id, GEN, probes)
print("index_suspect:", verdict.index_suspect, "negative_deferred:", verdict.negative_deferred)
for r in verdict.probe_results:
    print(f"  {r.probe_type} passed={r.passed} deferred={getattr(r,'deferred',None)} "
          f"matched={len(getattr(r,'matched_chunk_ids',[]))} detail={getattr(r,'detail','')!r}")

try:
    client.delete_collection(COLL)
except Exception:
    pass
