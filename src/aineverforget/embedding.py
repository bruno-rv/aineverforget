"""aineverforget.embedding — BGEM3FlagModel dense+sparse embedder.

Embedding contract (per PLAN.md § Phase A, item 4 and ADR-0002)
----------------------------------------------------------------
- Model:          ``FlagEmbedding.BGEM3FlagModel`` with ``return_dense=True,
                  return_sparse=True``.
- Dense:          1024-dim float vector (cosine similarity in Qdrant).
- Sparse:         BGE-M3's ``lexical_weights`` is a ``{token_id: weight}`` map.
                  Adapter converts it to a Qdrant-ready ``EmbeddingVector``
                  (sorted indices + corresponding values).
- No prefix:      BGE-M3 model card specifies NO instruction prefix for either
                  queries or passages (unlike e5-style models).
- Pinned version: the model checkpoint and tokenizer are pinned; a fixture
                  test asserts that a known query/passage pair's indices/values
                  round-trip correctly.

Sparse adapter contract
-----------------------
``lexical_weights`` from BGEM3FlagModel is a ``dict[int, float]`` mapping
token IDs to weights (keys may be strings in the raw model output — coerced
to int before this adapter is called).  Convert to ``EmbeddingVector`` as:
    indices = sorted(lexical_weights.keys())
    values  = [lexical_weights[i] for i in indices]

No heavy imports at module level: ``FlagEmbedding`` is imported lazily inside
method bodies to keep the package importable without FlagEmbedding installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Return types (concrete, final — implement against these exactly)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingVector:
    """Sparse vector in Qdrant SparseVector format.

    Attributes
    ----------
    indices:
        Sorted list of token IDs (ascending).
    values:
        Weights corresponding to *indices* in the same order.

    Invariant
    ---------
    ``len(indices) == len(values)`` and ``indices`` is strictly ascending.
    """

    indices: list[int]
    values: list[float]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise ValueError(
                f"EmbeddingVector: len(indices)={len(self.indices)} != "
                f"len(values)={len(self.values)}"
            )
        if self.indices != sorted(self.indices):
            raise ValueError("EmbeddingVector: indices must be sorted (ascending).")


@dataclass(frozen=True)
class PassageEmbedding:
    """Dense + sparse embedding for a single passage.

    Attributes
    ----------
    dense:
        1024-dimensional float vector (L2-normalized, cosine metric in Qdrant).
    sparse:
        Sparse lexical vector in Qdrant SparseVector format.
    """

    dense: list[float]
    sparse: EmbeddingVector


@dataclass(frozen=True)
class QueryEmbedding:
    """Dense + sparse embedding for a query.

    Identical structure to ``PassageEmbedding`` but semantically distinct —
    some model families use different encoding paths for queries vs passages.
    BGE-M3 does not differentiate; the types are separate for future
    flexibility and type-safety.
    """

    dense: list[float]
    sparse: EmbeddingVector


# ---------------------------------------------------------------------------
# Embedder class
# ---------------------------------------------------------------------------


class BGEM3Embedder:
    """BGE-M3 dense+sparse embedder using FlagEmbedding.BGEM3FlagModel.

    Encodes both passages (at ingest) and queries (at retrieval) into
    1024-dim dense + sparse lexical vectors from a single local model.

    Encoding contract (ADR-0002, PLAN.md § Phase A item 4):
    - Model card: NO instruction prefix for queries or passages.
    - Dense: L2-normalized 1024-dim float vector.
    - Sparse: ``lexical_weights`` {token_id: weight} → ``EmbeddingVector``
      (sorted indices, corresponding values).
    - Model checkpoint and tokenizer must be pinned.

    The model is loaded lazily on first encode call (not at init time) to
    keep import and instantiation costs low when the embedder is not used
    (e.g. ``aineverforget status``).

    Parameters
    ----------
    model_name:
        FlagEmbedding model checkpoint name (default ``"BAAI/bge-m3"``).
    use_fp16:
        Whether to load the model in fp16 (reduces RAM; default ``True``).
    batch_size:
        Encoding batch size passed to BGEM3FlagModel (default 32).
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        *,
        use_fp16: bool = True,
        batch_size: int = 32,
    ) -> None:
        self.model_name = model_name
        self.use_fp16 = use_fp16
        self.batch_size = batch_size
        self._model: object | None = None  # loaded lazily

    def encode_passages(self, texts: list[str]) -> list[PassageEmbedding]:
        """Encode a batch of passage texts for ingest.

        Calls ``BGEM3FlagModel.encode(texts, return_dense=True,
        return_sparse=True)`` and converts each result to a
        ``PassageEmbedding``.

        Sparse adapter: ``lexical_weights[i]`` is a ``{token_id: weight}``
        dict for the i-th passage.  Keys may be strings (FlagEmbedding
        internal representation); they are coerced to int before sorting so
        that ``sparse_from_lexical_weights`` receives ``dict[int, float]``.

        No instruction prefix is added — per BGE-M3 model card.

        Parameters
        ----------
        texts:
            Batch of passage strings to encode.  No instruction prefix.

        Returns
        -------
        list[PassageEmbedding]
            One ``PassageEmbedding`` per input text, in the same order.
            ``embedding.dense`` is a 1024-element float list.
            ``embedding.sparse`` is an ``EmbeddingVector`` with sorted indices.

        Raises
        ------
        RuntimeError
            If FlagEmbedding is not installed.
        """
        model = self._load_model()
        output = model.encode(
            texts,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
            batch_size=self.batch_size,
        )
        dense_vecs = output["dense_vecs"]
        lexical_weights_list = output["lexical_weights"]

        results: list[PassageEmbedding] = []
        for i, text in enumerate(texts):
            dv = dense_vecs[i]
            dense: list[float] = dv.tolist() if hasattr(dv, "tolist") else list(dv)
            lw_raw = lexical_weights_list[i]
            # Coerce string keys to int (FlagEmbedding may emit str token IDs)
            lw: dict[int, float] = {int(k): float(v) for k, v in lw_raw.items()}
            sparse = self.sparse_from_lexical_weights(lw)
            results.append(PassageEmbedding(dense=dense, sparse=sparse))
        return results

    def encode_query(self, text: str) -> QueryEmbedding:
        """Encode a single query string for retrieval.

        Uses the same BGEM3FlagModel encoding path as ``encode_passages()``
        (no separate query-specific path for BGE-M3, per model card).

        No instruction prefix is added — per BGE-M3 model card.

        Parameters
        ----------
        text:
            Query string.  No instruction prefix.

        Returns
        -------
        QueryEmbedding
            ``query.dense``: 1024-element float list.
            ``query.sparse``: ``EmbeddingVector`` with sorted indices.

        Raises
        ------
        RuntimeError
            If FlagEmbedding is not installed.
        """
        model = self._load_model()
        # Always pass a list; take index [0]
        output = model.encode(
            [text],
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
            batch_size=1,
        )
        dv = output["dense_vecs"][0]
        dense: list[float] = dv.tolist() if hasattr(dv, "tolist") else list(dv)
        lw_raw = output["lexical_weights"][0]
        # Coerce string keys to int (FlagEmbedding may emit str token IDs)
        lw: dict[int, float] = {int(k): float(v) for k, v in lw_raw.items()}
        sparse = self.sparse_from_lexical_weights(lw)
        return QueryEmbedding(dense=dense, sparse=sparse)

    def _load_model(self) -> object:
        """Lazily load BGEM3FlagModel on first encode call.

        Import FlagEmbedding inside this method (NOT at module level) so the
        package remains importable without FlagEmbedding installed.

        Returns
        -------
        BGEM3FlagModel
            The loaded model instance (also stored as ``self._model``).

        Raises
        ------
        RuntimeError
            If FlagEmbedding is not installed.
        """
        if self._model is not None:
            return self._model
        try:
            from FlagEmbedding import BGEM3FlagModel  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding is not installed. "
                "Install it with: pip install FlagEmbedding"
            ) from exc
        self._model = BGEM3FlagModel(self.model_name, use_fp16=self.use_fp16)
        return self._model

    @staticmethod
    def sparse_from_lexical_weights(lexical_weights: dict[int, float]) -> EmbeddingVector:
        """Convert BGE-M3 lexical_weights dict to an EmbeddingVector.

        This is the canonical sparse adapter contract (PLAN.md § Phase A,
        item 4).  Pure, deterministic, and independent of FlagEmbedding.

        Parameters
        ----------
        lexical_weights:
            ``{token_id: weight}`` dict as returned by BGEM3FlagModel for a
            single passage or query (caller must have already coerced keys to
            ``int`` if needed — see ``encode_passages`` / ``encode_query``).

        Returns
        -------
        EmbeddingVector
            ``indices`` = sorted token IDs; ``values`` = corresponding weights
            in the same order.

        Examples
        --------
        >>> emb = BGEM3Embedder.sparse_from_lexical_weights({3: 0.5, 1: 0.9, 7: 0.2})
        >>> emb.indices
        [1, 3, 7]
        >>> emb.values
        [0.9, 0.5, 0.2]
        """
        indices = sorted(lexical_weights.keys())
        values = [lexical_weights[i] for i in indices]
        return EmbeddingVector(indices=indices, values=values)
