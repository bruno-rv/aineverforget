Vector Store Selection — Architecture Decision Record
Author: Carlos Rivera
Date: 2026-06-09
Service: VectorCore

## TL;DR

Chose Qdrant (self-hosted) as the vector store for VectorCore. Rejected Pinecone because data leaving the perimeter violates our on-premise requirement. bge-m3 provides both dense and sparse embeddings; RRF handles hybrid search fusion. No SaaS vector store is acceptable under current data governance policy.

## Key Concepts

- **Qdrant** — open-source vector database, self-hosted on our infra, supports named vectors for dense + sparse in a single collection. Primary candidate.
- **Pinecone** — managed SaaS vector store. High performance, good developer experience, but data is processed and stored on Pinecone's infrastructure. Disqualified.
- **bge-m3 model** — BGE-M3 from BAAI; produces both dense embeddings (1024-dim) and sparse SPLADE-style lexical vectors in a single forward pass. Chosen for dual-encoder support.
- **RRF (Reciprocal Rank Fusion)** — score fusion method for combining dense and sparse ranked lists without requiring score normalization. Used for hybrid search result merging in VectorCore.
- **On-premise requirement** — data governance policy mandates that all document embeddings and raw text remain within our own infrastructure. Eliminates any SaaS-hosted vector store option.
- **Hybrid search** — combination of dense semantic search and sparse lexical search, fused via RRF, to improve recall across both semantic and exact-match query patterns.
- **Schema migration** — not directly relevant here, but VectorCore's collection schema is versioned; migrations to the vector collection schema follow a blue-green approach similar to the DataSync migration plan.

## Key Decisions

1. **Qdrant selected** over Pinecone, Weaviate Cloud, and Zilliz Cloud. Self-hosted, on-premise data control, active open-source development.
2. **Pinecone rejected** — SaaS model means data leaves the perimeter. Non-starter under current policy regardless of performance characteristics.
3. **bge-m3 chosen** for embeddings — single model produces dense and sparse vectors, reducing serving complexity vs. running two separate models.
4. **RRF adopted** for hybrid fusion — simpler than learned fusion, no training data required, competitive performance on our internal benchmarks.
5. **Named vectors** used in Qdrant collection — `dense` and `sparse` stored under separate named vector slots in a single point.

## Action Items

- [ ] Carlos Rivera: finalize Qdrant collection schema (vector dims, distance metrics, payload indexes) by 2026-06-16
- [ ] Carlos Rivera: benchmark bge-m3 throughput on target hardware; report p99 latency at expected QPS
- [ ] Infra team: provision Qdrant nodes with persistent storage, schedule backup job
- [ ] Carlos Rivera: integrate RRF fusion into VectorCore search path; add integration test covering hybrid recall
- [ ] Data governance: confirm written sign-off on bge-m3 + Qdrant stack as policy-compliant
