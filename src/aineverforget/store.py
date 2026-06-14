"""aineverforget.store — Qdrant vector store for the aineverforget Corpus.

Collection schema (per PLAN.md § Phase A, items 5–6 and ADR-0002)
------------------------------------------------------------------
Collection name:   ``ainf_corpus_bgem3_v1`` (versioned — per settings).
Vectors:
    ``"dense"``  — VectorParams(size=1024, distance=COSINE)
    ``"sparse"`` — SparseVectorParams() (for BGE-M3 lexical weights)

Payload indexes (created by ``ensure_collection()``):
    full-text:  ``text``          (MatchText for lexscan)
    keyword:    ``ingest_state``  (visibility gate — every read filters active)
    keyword:    ``source_type``
    keyword:    ``document_path``
    keyword:    ``tags``          (array keyword)
    datetime:   ``ingested_at``   (ISO-8601 string stored as datetime index)
    keyword:    ``document_id``   (for generation queries)

Read path invariant
-------------------
EVERY read method (``search``, ``lexscan``, ``scroll``, ``status``,
``max_active_generation``, ``verification_view_filter``) filters
``ingest_state=active`` EXCEPT:
    - ``verification_view_filter`` — returns a combined filter that matches
      EITHER ``ingest_state=active`` OR the specific pending generation
      being verified (see docstring).
    - ``delete_generations`` / ``promote_generation`` / ``gc`` — payload
      mutation / delete methods that operate on non-active Chunks by design.

No heavy imports at module level.  Import qdrant_client lazily inside each
method body via ``self._models()`` (same pattern as neverforget store.py).
"""

from __future__ import annotations

from typing import Any

from aineverforget.embedding import PassageEmbedding, QueryEmbedding
from aineverforget.models import Chunk, IngestState, RetrievedChunk, SearchResult


class QdrantStore:
    """Qdrant-backed store for the aineverforget Corpus.

    All methods that interact with Qdrant import ``qdrant_client`` lazily via
    ``self._models()`` so the class is instantiable without qdrant-client
    installed (useful for unit tests that mock the client).

    Parameters
    ----------
    url:
        Qdrant server URL (default ``"http://127.0.0.1:6333"``).
    collection_name:
        Target collection (default ``"ainf_corpus_bgem3_v1"``).
    client:
        Optional pre-built QdrantClient (for injection in tests).
    """

    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:6333",
        collection_name: str = "ainf_corpus_bgem3_v1",
        client: Any | None = None,
    ) -> None:
        self.url = url
        self.collection_name = collection_name
        self._client = client  # None = lazy-init on first use

    # ------------------------------------------------------------------
    # Collection lifecycle
    # ------------------------------------------------------------------

    def ensure_collection(self) -> None:
        """Create the collection and all payload indexes if they don't exist.

        Idempotent: safe to call on every startup; skips creation steps that
        already exist.

        Collection layout:
        - vectors_config: ``{"dense": VectorParams(size=1024, distance=COSINE)}``
        - sparse_vectors_config: ``{"sparse": SparseVectorParams()}``

        Payload indexes created (all idempotent):
        - full-text index on ``text``     → enables ``lexscan`` MatchText queries
        - keyword index on ``ingest_state`` → enables fast active-only filtering
        - keyword index on ``source_type``
        - keyword index on ``document_path``
        - keyword index on ``tags``        (array/keyword)
        - datetime index on ``ingested_at``
        - keyword index on ``document_id`` → enables fast generation queries

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed (raised at lazy-import time).
        """
        m = self._models()
        client = self._get_client()

        if not client.collection_exists(self.collection_name):
            client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": m.VectorParams(size=1024, distance=m.Distance.COSINE)
                },
                sparse_vectors_config={
                    "sparse": m.SparseVectorParams()
                },
            )

        # Create payload indexes idempotently — catch exceptions for already-exists
        _keyword = m.PayloadSchemaType.KEYWORD
        _keyword_indexes = [
            "ingest_state",
            "source_type",
            "source_id",
            "document_path",
            "tags",
            "document_id",
        ]
        for field in _keyword_indexes:
            try:
                client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field,
                    field_schema=_keyword,
                )
            except Exception:
                pass

        # Datetime index on ingested_at
        try:
            client.create_payload_index(
                collection_name=self.collection_name,
                field_name="ingested_at",
                field_schema=m.PayloadSchemaType.DATETIME,
            )
        except Exception:
            pass

        # Full-text index on text (enables MatchText in lexscan)
        try:
            client.create_payload_index(
                collection_name=self.collection_name,
                field_name="text",
                field_schema=m.TextIndexParams(
                    type="text",
                    tokenizer=m.TokenizerType.WORD,
                    lowercase=True,
                ),
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Ingest write path
    # ------------------------------------------------------------------

    def upsert_chunks(
        self,
        chunks: list[Chunk],
        embeddings: list[PassageEmbedding],
    ) -> None:
        """Upsert Chunk points into Qdrant with dense+sparse vectors.

        Constructs a ``PointStruct`` for each (chunk, embedding) pair:
        - ``id`` = ``chunk.point_id`` (deterministic UUIDv5)
        - ``vector`` = ``{"dense": embedding.dense, "sparse":
          SparseVector(indices=embedding.sparse.indices,
          values=embedding.sparse.values)}``
        - ``payload`` = ``chunk.to_payload()``

        All chunks in this batch share the same ``ingest_generation`` and
        ``ingest_state=pending``.  Upsert is idempotent for the same point_id.

        Parameters
        ----------
        chunks:
            Chunks to upsert.  Must all have ``ingest_state=IngestState.pending``.
        embeddings:
            Dense+sparse embeddings in the same order as *chunks*.

        Raises
        ------
        ValueError
            If ``len(chunks) != len(embeddings)``.
        """
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"len(chunks)={len(chunks)} != len(embeddings)={len(embeddings)}"
            )

        m = self._models()
        client = self._get_client()

        points = []
        for chunk, emb in zip(chunks, embeddings):
            points.append(
                m.PointStruct(
                    id=chunk.point_id,
                    vector={
                        "dense": emb.dense,
                        "sparse": m.SparseVector(
                            indices=emb.sparse.indices,
                            values=emb.sparse.values,
                        ),
                    },
                    payload=chunk.to_payload(),
                )
            )

        client.upsert(
            collection_name=self.collection_name,
            points=points,
        )

    # ------------------------------------------------------------------
    # Read path — all filter ingest_state=active (except noted)
    # ------------------------------------------------------------------

    def search(
        self,
        query: QueryEmbedding,
        *,
        limit: int = 10,
        source_id: str | None = None,
        document_path: str | None = None,
        since: str | None = None,
        tags: list[str] | None = None,
        view_filter: Any | None = None,
    ) -> SearchResult:
        """Hybrid (dense+sparse RRF) search over active Chunks.

        Uses Qdrant Query API with prefetch:
            prefetch = [
                Prefetch(query=query.dense, using="dense", limit=limit),
                Prefetch(query=SparseVector(query.sparse.indices,
                         query.sparse.values), using="sparse", limit=limit),
            ]
            query = FusionQuery(Fusion.RRF)
            query_filter = Filter(must=[
                FieldCondition(ingest_state, MatchValue("active")),
                ... optional additional filters ...
            ])

        Returns a ``SearchResult`` envelope that includes:
        - Fused candidates (``candidates: list[RetrievedChunk]``)
        - ``dense_hits``: number of dense prefetch results (pre-fusion count)
        - ``sparse_hits``: number of sparse prefetch results (pre-fusion count)
        - ``candidate_count = len(candidates)``
        - ``citationable_count``: candidates with non-empty ``text`` AND
          non-empty ``document_path`` (needed for the retriever Quality Gate)

        Parameters
        ----------
        query:
            Dense+sparse query embedding from ``BGEM3Embedder.encode_query()``.
        limit:
            Maximum number of RRF-fused results to return.
        source_id:
            If set, add ``FieldCondition(source_id, MatchValue(...))`` filter.
        document_path:
            If set, add ``FieldCondition(document_path, MatchValue(...))`` filter.
        since:
            If set, add ``FieldCondition(ingested_at, DatetimeRange(gte=...))``
            filter.  ISO-8601 string.
        tags:
            If set, add ``FieldCondition(tags, MatchAny(tags))`` filter.

        Returns
        -------
        SearchResult
            Envelope with candidates, dense_hits, sparse_hits, candidate_count,
            citationable_count.  Returns empty SearchResult (all zeros/empty)
            if Qdrant returns no results — never raises on empty corpus.

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()
        client = self._get_client()

        # Build filter: use caller-supplied view_filter if provided (e.g. verification
        # view that combines active + pending-generation), otherwise default to
        # active-only + optional attribute filters.
        if view_filter is not None:
            query_filter = view_filter
        else:
            must_conditions = [
                m.FieldCondition(
                    key="ingest_state",
                    match=m.MatchValue(value="active"),
                )
            ]
            must_conditions.extend(self._optional_filters(m, source_id, document_path, since, tags))
            query_filter = m.Filter(must=must_conditions)

        sparse_vec = m.SparseVector(
            indices=query.sparse.indices,
            values=query.sparse.values,
        )

        # Count dense/sparse pre-fusion hits via individual query_points calls
        dense_response = client.query_points(
            collection_name=self.collection_name,
            query=query.dense,
            using="dense",
            query_filter=query_filter,
            limit=limit,
            with_payload=False,
            with_vectors=False,
        )
        dense_hits = len(getattr(dense_response, "points", dense_response))

        sparse_response = client.query_points(
            collection_name=self.collection_name,
            query=sparse_vec,
            using="sparse",
            query_filter=query_filter,
            limit=limit,
            with_payload=False,
            with_vectors=False,
        )
        sparse_hits = len(getattr(sparse_response, "points", sparse_response))

        # Hybrid RRF fusion via Query API
        response = client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                m.Prefetch(
                    query=query.dense,
                    using="dense",
                    limit=limit,
                    filter=query_filter,
                ),
                m.Prefetch(
                    query=sparse_vec,
                    using="sparse",
                    limit=limit,
                    filter=query_filter,
                ),
            ],
            query=m.FusionQuery(fusion=m.Fusion.RRF),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )

        points = getattr(response, "points", response)

        candidates: list[RetrievedChunk] = []
        for pt in points:
            p = pt.payload or {}
            candidates.append(
                RetrievedChunk(
                    score=pt.score,
                    point_id=str(pt.id),
                    source_id=p.get("source_id", ""),
                    source_type=p.get("source_type", ""),
                    document_id=p.get("document_id", ""),
                    document_path=p.get("document_path", ""),
                    document_sha256=p.get("document_sha256", ""),
                    title=p.get("title", ""),
                    chunk_index=p.get("chunk_index", 0),
                    heading_path=p.get("heading_path"),
                    pdf_page=p.get("pdf_page"),
                    tags=p.get("tags", []),
                    text=p.get("text", ""),
                    producer=p.get("producer", ""),
                    ingested_at=p.get("ingested_at", ""),
                    ingest_generation=p.get("ingest_generation", 0),
                )
            )

        # Fix B: Corpus-local max-gen dedup.  A result-local max (scanning only
        # returned rows) fails when a query matches only old-gen content: the
        # stale generation appears to be "the max" among returned rows even
        # though a newer active generation exists in the corpus.  We must call
        # max_active_generation() per unique document_id to get the true corpus
        # max, then discard any returned chunk whose ingest_generation is below
        # that corpus max.
        if view_filter is None:
            unique_doc_ids = {c.document_id for c in candidates if c.document_id}
            corpus_max_gen: dict[str, int | None] = {
                doc_id: self.max_active_generation(doc_id)
                for doc_id in unique_doc_ids
            }
            candidates = [
                c for c in candidates
                if not c.document_id
                or corpus_max_gen.get(c.document_id) is None
                or c.ingest_generation == corpus_max_gen[c.document_id]
            ]

        citationable_count = sum(
            1 for c in candidates if c.text and c.document_path
        )

        return SearchResult(
            query="",  # caller sets this if needed; store doesn't know the raw query string
            candidates=candidates,
            dense_hits=dense_hits,
            sparse_hits=sparse_hits,
            candidate_count=len(candidates),
            citationable_count=citationable_count,
        )

    def lexscan(
        self,
        term: str,
        *,
        source_id: str | None = None,
        document_path: str | None = None,
        since: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """Exhaustive content enumeration via full-text payload index.

        Uses Qdrant ``scroll`` with a ``MatchText(term)`` filter on the
        ``text`` payload field.  Paginates until all matching points are
        returned — this is NOT a top-k query.

        All results filtered to ``ingest_state=active``.

        Parameters
        ----------
        term:
            Search term for ``MatchText`` full-text filter.
        source_id:
            Optional additional filter on ``source_id``.
        document_path:
            Optional additional filter on ``document_path``.
        since:
            Optional ``ingested_at`` lower-bound filter (ISO-8601 string).
        tags:
            Optional ``tags`` filter (match any).
        page_size:
            Scroll page size (number of points per Qdrant scroll call).

        Returns
        -------
        dict[str, Any]
            Keys:
            - ``"term"`` (str): the search term.
            - ``"chunk_count"`` (int): total matching Chunks.
            - ``"document_count"`` (int): distinct document_ids matched.
            - ``"chunks"`` (list[dict]): all matching Chunk payloads (active only),
              each a flat dict of all payload fields + ``"point_id"`` +
              ``"score"`` = null (lexscan has no relevance score).

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()
        client = self._get_client()

        must_conditions = [
            m.FieldCondition(
                key="ingest_state",
                match=m.MatchValue(value="active"),
            ),
            m.FieldCondition(
                key="text",
                match=m.MatchText(text=term),
            ),
        ]
        must_conditions.extend(self._optional_filters(m, source_id, document_path, since, tags))
        scroll_filter = m.Filter(must=must_conditions)

        chunks: list[dict[str, Any]] = []
        offset = None

        while True:
            result, next_offset = client.scroll(
                collection_name=self.collection_name,
                scroll_filter=scroll_filter,
                limit=page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for pt in result:
                p = dict(pt.payload or {})
                p["point_id"] = str(pt.id)
                p["score"] = None
                chunks.append(p)

            if next_offset is None:
                break
            offset = next_offset

        # Fix B: Corpus-local max-gen dedup (same rationale as search()).
        # Use max_active_generation() per unique doc_id so a query matching
        # only old-gen content doesn't surface the stale generation.
        unique_doc_ids_lex = {c.get("document_id") for c in chunks if c.get("document_id")}
        corpus_max_gen_lex: dict[str, int | None] = {
            doc_id: self.max_active_generation(doc_id)
            for doc_id in unique_doc_ids_lex
        }
        chunks = [
            c for c in chunks
            if not c.get("document_id")
            or corpus_max_gen_lex.get(c.get("document_id")) is None
            or c.get("ingest_generation", 0) == corpus_max_gen_lex.get(c.get("document_id"))
        ]

        doc_ids = {c.get("document_id") for c in chunks if c.get("document_id")}

        # Count total occurrences of term across all matching chunks (case-insensitive)
        term_lower = term.lower()
        occurrence_count = sum(
            c.get("text", "").lower().count(term_lower) for c in chunks
        )

        return {
            "term": term,
            "chunk_count": len(chunks),
            "document_count": len(doc_ids),
            "occurrence_count": occurrence_count,
            "chunks": chunks,
        }

    def scroll(
        self,
        *,
        source_id: str | None = None,
        source_type: str | None = None,
        document_path: str | None = None,
        since: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """Metadata enumeration: payload-filter scroll over active Chunks.

        Returns ALL matching active Chunks, deduplicated by document_id keeping
        the max active ingest_generation per document.

        All results filtered to ``ingest_state=active``.

        Parameters
        ----------
        source_id:
            Optional filter on ``source_id``.
        source_type:
            Optional filter on ``source_type``.
        document_path:
            Optional filter on ``document_path``.
        since:
            Optional ``ingested_at`` lower-bound filter (ISO-8601 string).
        tags:
            Optional ``tags`` filter (match any).
        page_size:
            Scroll page size (points per Qdrant scroll call).

        Returns
        -------
        dict[str, Any]
            Keys:
            - ``"document_count"`` (int): distinct documents matched.
            - ``"chunk_count"`` (int): total active Chunks matched.
            - ``"documents"`` (list[dict]): one entry per unique document_id,
              including representative metadata fields and the max active
              ingest_generation.

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()
        client = self._get_client()

        must_conditions: list[Any] = [
            m.FieldCondition(
                key="ingest_state",
                match=m.MatchValue(value="active"),
            )
        ]
        if source_id is not None:
            must_conditions.append(
                m.FieldCondition(key="source_id", match=m.MatchValue(value=source_id))
            )
        if source_type is not None:
            must_conditions.append(
                m.FieldCondition(key="source_type", match=m.MatchValue(value=source_type))
            )
        must_conditions.extend(self._optional_filters(m, None, document_path, since, tags))
        scroll_filter = m.Filter(must=must_conditions)

        all_chunks: list[dict[str, Any]] = []
        offset = None

        while True:
            result, next_offset = client.scroll(
                collection_name=self.collection_name,
                scroll_filter=scroll_filter,
                limit=page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for pt in result:
                p = dict(pt.payload or {})
                p["point_id"] = str(pt.id)
                all_chunks.append(p)

            if next_offset is None:
                break
            offset = next_offset

        # Fix: Corpus-local max-gen dedup (same pattern as search/lexscan).
        # The filtered scroll may return only old-gen rows for a document (e.g.
        # when --tag/--since matches old-gen but not new-gen content).  A
        # result-local max picks the stale generation as "the max among returned
        # rows".  We must call max_active_generation() to get the true corpus max
        # per document_id, then discard any chunk whose ingest_generation is below
        # that corpus max before building doc_map and counting chunk_count.
        unique_doc_ids_scroll = {
            c.get("document_id") for c in all_chunks if c.get("document_id")
        }
        corpus_max_gen_scroll: dict[str, int | None] = {
            doc_id: self.max_active_generation(doc_id)
            for doc_id in unique_doc_ids_scroll
        }
        all_chunks = [
            c for c in all_chunks
            if not c.get("document_id")
            or corpus_max_gen_scroll.get(c.get("document_id")) is None
            or c.get("ingest_generation", 0) == corpus_max_gen_scroll.get(c.get("document_id"))
        ]

        # Deduplicate by document_id, keeping max ingest_generation per document
        doc_map: dict[str, dict[str, Any]] = {}
        for chunk in all_chunks:
            doc_id = chunk.get("document_id", "")
            gen = chunk.get("ingest_generation", 0)
            if doc_id not in doc_map or gen > doc_map[doc_id].get("ingest_generation", 0):
                doc_map[doc_id] = {
                    "document_id": doc_id,
                    "document_path": chunk.get("document_path", ""),
                    "source_id": chunk.get("source_id", ""),
                    "source_type": chunk.get("source_type", ""),
                    "title": chunk.get("title", ""),
                    "producer": chunk.get("producer", ""),
                    "ingested_at": chunk.get("ingested_at", ""),
                    "ingest_generation": gen,
                    "tags": chunk.get("tags", []),
                }

        documents = list(doc_map.values())

        return {
            "document_count": len(documents),
            "chunk_count": len(all_chunks),
            "documents": documents,
        }

    # ------------------------------------------------------------------
    # Generation management (ingest update flow)
    # ------------------------------------------------------------------

    def max_active_generation(self, document_id: str) -> int | None:
        """Return the maximum active ingest_generation for *document_id*.

        Filters ``ingest_state=active`` AND ``document_id=<document_id>``,
        returns the maximum ``ingest_generation`` value found, or ``None``
        if no active Chunks exist for this document.

        Parameters
        ----------
        document_id:
            The document_id to query.

        Returns
        -------
        int | None
            Maximum active generation, or ``None`` if no active Chunks found.

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()
        client = self._get_client()

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

        max_gen: int | None = None
        offset = None

        while True:
            result, next_offset = client.scroll(
                collection_name=self.collection_name,
                scroll_filter=scroll_filter,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for pt in result:
                p = pt.payload or {}
                gen = p.get("ingest_generation")
                if gen is not None:
                    if max_gen is None or gen > max_gen:
                        max_gen = int(gen)

            if next_offset is None:
                break
            offset = next_offset

        return max_gen

    def get_chunks(
        self,
        document_id: str,
        generation: int,
        *,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Return all active chunk payloads for *document_id* / *generation*.

        Each returned dict contains the full payload (including ``"text"``)
        plus a ``"point_id"`` key.  Only chunks with
        ``ingest_state=active`` are returned.

        Parameters
        ----------
        document_id:
            The document_id to fetch chunks for.
        generation:
            The ingest_generation to fetch chunks for.
        page_size:
            Scroll page size (points per Qdrant scroll call).

        Returns
        -------
        list[dict[str, Any]]
            List of payload dicts (may be empty if no matching active chunks).
        """
        m = self._models()
        client = self._get_client()

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
                m.FieldCondition(
                    key="ingest_generation",
                    match=m.MatchValue(value=generation),
                ),
            ]
        )

        chunks: list[dict[str, Any]] = []
        offset = None

        while True:
            result, next_offset = client.scroll(
                collection_name=self.collection_name,
                scroll_filter=scroll_filter,
                limit=page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for pt in result:
                p = dict(pt.payload or {})
                p["point_id"] = str(pt.id)
                chunks.append(p)

            if next_offset is None:
                break
            offset = next_offset

        return chunks

    def promote_generation(
        self,
        document_id: str,
        generation: int,
    ) -> int:
        """Promote *generation* Chunks from pending → active for *document_id*.

        Uses ``set_payload`` on matched points (stable point_ids — no delete/
        re-upsert required because point IDs are generation-independent).

        Parameters
        ----------
        document_id:
            The document whose pending generation to promote.
        generation:
            The generation number to promote (must currently be ``pending``).

        Returns
        -------
        int
            Number of Chunks promoted.

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()
        client = self._get_client()

        promote_filter = m.Filter(
            must=[
                m.FieldCondition(
                    key="document_id",
                    match=m.MatchValue(value=document_id),
                ),
                m.FieldCondition(
                    key="ingest_generation",
                    match=m.MatchValue(value=generation),
                ),
                m.FieldCondition(
                    key="ingest_state",
                    match=m.MatchValue(value="pending"),
                ),
            ]
        )

        # Count first so we know how many were promoted
        count_result = client.count(
            collection_name=self.collection_name,
            count_filter=promote_filter,
            exact=True,
        )
        promoted_count = count_result.count

        if promoted_count > 0:
            client.set_payload(
                collection_name=self.collection_name,
                payload={"ingest_state": "active"},
                points=m.FilterSelector(filter=promote_filter),
            )

        return promoted_count

    def delete_generations(
        self,
        document_id: str,
        *,
        states: list[IngestState] | None = None,
        generation: int | None = None,
        max_generation_to_delete: int | None = None,
    ) -> int:
        """Delete Chunks for *document_id* matching the given filters.

        Parameters
        ----------
        document_id:
            The document to target.
        states:
            List of ``IngestState`` values to match.  If ``None``, matches all
            states.
        generation:
            If set, only delete Chunks with this exact ``ingest_generation``.
        max_generation_to_delete:
            If set, only delete Chunks with
            ``ingest_generation <= max_generation_to_delete``.

        Returns
        -------
        int
            Number of Chunks deleted.

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()
        client = self._get_client()

        must_conditions: list[Any] = [
            m.FieldCondition(
                key="document_id",
                match=m.MatchValue(value=document_id),
            )
        ]

        if states is not None:
            state_values = [s.value for s in states]
            must_conditions.append(
                m.FieldCondition(
                    key="ingest_state",
                    match=m.MatchAny(any=state_values),
                )
            )

        if generation is not None:
            must_conditions.append(
                m.FieldCondition(
                    key="ingest_generation",
                    match=m.MatchValue(value=generation),
                )
            )
        elif max_generation_to_delete is not None:
            must_conditions.append(
                m.FieldCondition(
                    key="ingest_generation",
                    range=m.Range(lte=max_generation_to_delete),
                )
            )

        delete_filter = m.Filter(must=must_conditions)

        # Count first
        count_result = client.count(
            collection_name=self.collection_name,
            count_filter=delete_filter,
            exact=True,
        )
        deleted_count = count_result.count

        if deleted_count > 0:
            client.delete(
                collection_name=self.collection_name,
                points_selector=m.FilterSelector(filter=delete_filter),
            )

        return deleted_count

    def verification_view_filter(
        self,
        document_id: str,
        generation: int,
    ) -> Any:
        """Return the Qdrant Filter for the verify-probe view.

        The verification view matches Chunks that are EITHER in the active
        Corpus OR belong to the specific pending generation being verified:

            Filter(
                should=[
                    FieldCondition(ingest_state, MatchValue("active")),
                    Filter(
                        must=[
                            FieldCondition(document_id, MatchValue(document_id)),
                            FieldCondition(ingest_generation, MatchValue(generation)),
                        ]
                    ),
                ]
            )

        Parameters
        ----------
        document_id:
            The document_id whose pending generation is being verified.
        generation:
            The pending generation number (G+1) to include in the view.

        Returns
        -------
        Any
            A ``qdrant_client.models.Filter`` instance.

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()

        return m.Filter(
            should=[
                m.FieldCondition(
                    key="ingest_state",
                    match=m.MatchValue(value="active"),
                ),
                m.Filter(
                    must=[
                        m.FieldCondition(
                            key="document_id",
                            match=m.MatchValue(value=document_id),
                        ),
                        m.FieldCondition(
                            key="ingest_generation",
                            match=m.MatchValue(value=generation),
                        ),
                    ]
                ),
            ]
        )

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def gc(self) -> dict[str, int]:
        """Retire superseded Chunks and orphaned pending/failed records.

        GC pass (per PLAN.md § Identity model):
        - For each document_id with an active generation: delete all Chunks with
          ingest_generation < max_active_generation (superseded).
        - Delete all Chunks with ingest_state=pending or ingest_state=failed
          across all documents (orphaned from crashed/failed ingests).

        Safe invariant: gc NEVER touches the current max-active-generation
        Chunks, so concurrent reads always see a consistent active Corpus.

        Returns
        -------
        dict[str, int]
            Keys:
            - ``"superseded_deleted"`` (int)
            - ``"orphan_deleted"`` (int)
            - ``"documents_affected"`` (int)

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()
        client = self._get_client()

        superseded_deleted = 0
        orphan_deleted = 0
        documents_affected: set[str] = set()

        # Step 1: Find all document_ids that have active chunks
        # Scroll all active chunks, collect doc_id → max_gen
        active_filter = m.Filter(
            must=[
                m.FieldCondition(
                    key="ingest_state",
                    match=m.MatchValue(value="active"),
                )
            ]
        )

        doc_max_gen: dict[str, int] = {}
        offset = None
        while True:
            result, next_offset = client.scroll(
                collection_name=self.collection_name,
                scroll_filter=active_filter,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for pt in result:
                p = pt.payload or {}
                doc_id = p.get("document_id")
                gen = p.get("ingest_generation")
                if doc_id and gen is not None:
                    gen = int(gen)
                    if doc_id not in doc_max_gen or gen > doc_max_gen[doc_id]:
                        doc_max_gen[doc_id] = gen

            if next_offset is None:
                break
            offset = next_offset

        # Step 2: For each doc with active chunks, delete superseded active generations
        for doc_id, max_gen in doc_max_gen.items():
            if max_gen <= 1:
                # No generations before max to delete
                continue
            sup_filter = m.Filter(
                must=[
                    m.FieldCondition(
                        key="document_id",
                        match=m.MatchValue(value=doc_id),
                    ),
                    m.FieldCondition(
                        key="ingest_state",
                        match=m.MatchValue(value="active"),
                    ),
                    m.FieldCondition(
                        key="ingest_generation",
                        range=m.Range(lte=max_gen - 1),
                    ),
                ]
            )
            count_result = client.count(
                collection_name=self.collection_name,
                count_filter=sup_filter,
                exact=True,
            )
            n = count_result.count
            if n > 0:
                client.delete(
                    collection_name=self.collection_name,
                    points_selector=m.FilterSelector(filter=sup_filter),
                )
                superseded_deleted += n
                documents_affected.add(doc_id)

        # Step 3: Delete ALL orphaned pending/failed chunks (across ALL documents)
        orphan_filter = m.Filter(
            must=[
                m.FieldCondition(
                    key="ingest_state",
                    match=m.MatchAny(any=["pending", "failed"]),
                )
            ]
        )

        # Collect doc_ids affected before deleting
        offset = None
        orphan_doc_ids: set[str] = set()
        while True:
            result, next_offset = client.scroll(
                collection_name=self.collection_name,
                scroll_filter=orphan_filter,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for pt in result:
                p = pt.payload or {}
                doc_id = p.get("document_id")
                if doc_id:
                    orphan_doc_ids.add(doc_id)

            if next_offset is None:
                break
            offset = next_offset

        orphan_count_result = client.count(
            collection_name=self.collection_name,
            count_filter=orphan_filter,
            exact=True,
        )
        orphan_n = orphan_count_result.count
        if orphan_n > 0:
            client.delete(
                collection_name=self.collection_name,
                points_selector=m.FilterSelector(filter=orphan_filter),
            )
            orphan_deleted = orphan_n
            documents_affected.update(orphan_doc_ids)

        return {
            "superseded_deleted": superseded_deleted,
            "orphan_deleted": orphan_deleted,
            "documents_affected": len(documents_affected),
        }

    def status(self) -> dict[str, Any]:
        """Return collection health and corpus statistics.

        Returns
        -------
        dict[str, Any]
            Keys:
            - ``"collection"`` (str): collection name.
            - ``"qdrant_url"`` (str): configured Qdrant URL.
            - ``"collection_exists"`` (bool)
            - ``"point_count"`` (int | None): total Qdrant point count (all states).
            - ``"active_chunk_count"`` (int): active Chunks only.
            - ``"document_count"`` (int): distinct document_ids with active Chunks.
            - ``"source_count"`` (int): distinct source_ids with active Chunks.
            - ``"last_ingested_at"`` (str | None): ISO-8601 timestamp of the
              most recent ``ingested_at`` among active Chunks.
            - ``"qdrant_healthy"`` (bool): whether the Qdrant HTTP health
              endpoint returns 200.

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        m = self._models()
        client = self._get_client()

        collection_exists = client.collection_exists(self.collection_name)

        point_count: int | None = None
        active_chunk_count = 0
        document_count = 0
        source_count = 0
        last_ingested_at: str | None = None
        qdrant_healthy = False

        if collection_exists:
            try:
                coll_info = client.get_collection(self.collection_name)
                point_count = getattr(coll_info, "points_count", None)
            except Exception:
                pass

            # Gather active chunk stats via scroll (two-pass max-gen dedup)
            active_filter = m.Filter(
                must=[
                    m.FieldCondition(
                        key="ingest_state",
                        match=m.MatchValue(value="active"),
                    )
                ]
            )
            all_active: list[dict[str, Any]] = []
            offset = None

            while True:
                result, next_offset = client.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=active_filter,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for pt in result:
                    all_active.append(pt.payload or {})
                if next_offset is None:
                    break
                offset = next_offset

            # Build max ingest_generation per document_id
            max_gen_per_doc: dict[str, int] = {}
            for p in all_active:
                doc_id = p.get("document_id")
                if doc_id:
                    gen = p.get("ingest_generation", 0)
                    if doc_id not in max_gen_per_doc or gen > max_gen_per_doc[doc_id]:
                        max_gen_per_doc[doc_id] = gen

            # Count only max-gen chunks
            doc_ids: set[str] = set()
            source_ids: set[str] = set()
            for p in all_active:
                doc_id = p.get("document_id")
                gen = p.get("ingest_generation", 0)
                if doc_id and gen != max_gen_per_doc.get(doc_id):
                    continue  # skip stale generation chunks
                active_chunk_count += 1
                src_id = p.get("source_id")
                ia = p.get("ingested_at")
                if doc_id:
                    doc_ids.add(doc_id)
                if src_id:
                    source_ids.add(src_id)
                if ia:
                    if last_ingested_at is None or ia > last_ingested_at:
                        last_ingested_at = ia

            document_count = len(doc_ids)
            source_count = len(source_ids)

        # Health check — try a simple collection list
        try:
            client.get_collections()
            qdrant_healthy = True
        except Exception:
            qdrant_healthy = False

        return {
            "collection": self.collection_name,
            "qdrant_url": self.url,
            "collection_exists": collection_exists,
            "point_count": point_count,
            "active_chunk_count": active_chunk_count,
            "document_count": document_count,
            "source_count": source_count,
            "last_ingested_at": last_ingested_at,
            "qdrant_healthy": qdrant_healthy,
        }

    # ------------------------------------------------------------------
    # Private helpers (implement alongside the public methods)
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return the Qdrant client, lazy-initializing if needed.

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        if self._client is not None:
            return self._client
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise RuntimeError(
                "qdrant-client is not installed. "
                "Install aineverforget with qdrant support."
            ) from exc
        self._client = QdrantClient(url=self.url)
        return self._client

    @staticmethod
    def _models() -> Any:
        """Return the qdrant_client.models namespace (lazy import).

        Raises
        ------
        RuntimeError
            If qdrant-client is not installed.
        """
        try:
            from qdrant_client import models
        except ImportError as exc:
            raise RuntimeError(
                "qdrant-client is not installed. "
                "Install aineverforget with qdrant support."
            ) from exc
        return models

    @staticmethod
    def _optional_filters(
        m: Any,
        source_id: str | None,
        document_path: str | None,
        since: str | None,
        tags: list[str] | None,
    ) -> list[Any]:
        """Build optional filter conditions shared across read methods."""
        conditions: list[Any] = []
        if source_id is not None:
            conditions.append(
                m.FieldCondition(key="source_id", match=m.MatchValue(value=source_id))
            )
        if document_path is not None:
            conditions.append(
                m.FieldCondition(key="document_path", match=m.MatchValue(value=document_path))
            )
        if since is not None:
            conditions.append(
                m.FieldCondition(
                    key="ingested_at",
                    range=m.DatetimeRange(gte=since),
                )
            )
        if tags:
            conditions.append(
                m.FieldCondition(key="tags", match=m.MatchAny(any=tags))
            )
        return conditions
