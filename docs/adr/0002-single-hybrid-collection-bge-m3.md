# ADR-0002: One hybrid Qdrant collection, bge-m3 dense+sparse

- Status: Accepted
- Date: 2026-06-13
- Deciders: Bruno, Claude (grill-with-docs-codex)

## Context

aineverforget ingests heterogeneous text — notes, transcripts, summaries, PDFs,
and codebase summaries emitted by external Producers — into one searchable space
("Total Context"). The embedding model and vector layout are baked into the
collection: changing them later forces a full re-index of the whole Corpus, so
this is a hard-to-reverse choice made once.

Three constraints drove it:

1. **Multilingual.** Content is mixed Portuguese and English.
2. **Recall *and* aggregation.** The user asks both pinpoint Recall and Synthesis
   questions, including enumeration ("how many times did I mention Y") that leans
   on exact-term (lexical) match, which dense-only retrieval misses.
3. **Local-first.** Embeddings run on the user's machine.

`neverforget` used `multilingual-e5-large` (dense-only, 1024d) because its corpus
was Portuguese video transcripts. aineverforget's corpus is different (prose +
code-bearing summaries) and its retrieval needs are broader (lexical recall).

## Decision

- **One collection**, holding every Chunk from every Source type. No per-type or
  per-namespace collections — that would fight Total Context. Scoping is done with
  payload filters (`source_type`, `document_path`, `ingested_at`, optional `tags`),
  not separate collections. (Field names per PLAN.md: `source_id` is the Source
  ref, `document_path` the Document path; there is no `source_path`.)
- **Embedder: a bge-m3-class model**, multilingual, 1024-dim, emitting **dense +
  sparse** vectors from one local model. Implementation: `FlagEmbedding`'s
  `BGEM3FlagModel`, which is the library that actually produces BGE-M3 dense +
  sparse (lexical) weights — *not* sentence-transformers (dense only) or FastEmbed
  (its sparse list does not include BGE-M3). Encoding follows the BGE-M3 model card
  (no e5-style `query:`/`passage:` instruction prefix); the exact contract is
  pinned and tested at build time. Acceptable fallback if BGEM3FlagModel is
  unsuitable: BGE-M3 dense (any lib) + a FastEmbed-supported sparse model
  (SPLADE/BM25) as the sparse component. The exact checkpoint is validated against
  the dev-time Eval set; the *decision* is the strategy.
- **Hybrid retrieval**: Qdrant Query API prefetch(dense) + prefetch(sparse) with
  RRF fusion. A local reranker (bge-reranker-v2-m3) is a v1.1 precision upgrade.
- **Normalize everything to text.** Images are out of v1. If visual-similarity is
  ever wanted, it is added as a *second named vector*, not a second collection —
  this layout leaves room for that.
- **Versioned collection name** (e.g. `ainf_corpus_bgem3_v1`) so a future model
  change is an explicit re-index into a new version, never a silent mismatch.

## Alternatives considered

- **Keep e5-large, dense-only** (neverforget parity). Rejected: wastes the lexical
  recall the aggregation queries need, and e5 is weaker on code-bearing prose.
- **Dual embedders** (prose model + code-specialized model as named vectors).
  Rejected: the user wants summaries *about* code, not raw-code search — there is
  no pure-code Corpus to justify the complexity.

## Consequences

- Positive: one space, hybrid recall for both semantic and exact-term queries, all
  local, single model produces both vector kinds.
- Negative: changing the embedder later = full re-index (mitigated by the versioned
  name). bge-m3 is heavier than e5 (more RAM, slower encode) — acceptable for a
  local single-user brain.
