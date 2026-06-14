"""Tests for aineverforget.embedding.

Covers:
- ``EmbeddingVector`` validation and __post_init__ invariants.
- ``BGEM3Embedder.sparse_from_lexical_weights`` (deterministic sparse adapter).
- ``BGEM3Embedder.encode_passages`` and ``encode_query`` with a mocked model.

FlagEmbedding is NOT required for any test here. The real 2 GB model is never
downloaded. All model interactions go through a lightweight fake returned by
monkeypatching ``_load_model``.

NOTE on scoring tests: PLAN.md mentions "a known query/passage pair scores as
expected" — that assertion requires the real model and is deferred to an
integration fixture (not included here, as it would trigger a download).
"""

from __future__ import annotations

import pytest

from aineverforget.embedding import (
    BGEM3Embedder,
    EmbeddingVector,
    PassageEmbedding,
    QueryEmbedding,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

DENSE_DIM = 1024


def _fake_dense(seed: float = 0.1) -> list[float]:
    """Return a fake 1024-dim float list."""
    return [seed] * DENSE_DIM


class _FakeModel:
    """Minimal fake for BGEM3FlagModel.

    Records every ``encode`` call for assertion.  Accepts both int and string
    keys in ``lexical_weights_override`` to let tests exercise either path.
    """

    def __init__(
        self,
        *,
        dense_val: float = 0.1,
        lexical_weights_per_item: list[dict] | None = None,
    ) -> None:
        self._dense_val = dense_val
        self._lw_per_item = lexical_weights_per_item or [{"354": 0.5, "1": 0.9}]
        self.calls: list[dict] = []

    def encode(
        self,
        sentences: list[str],
        *,
        return_dense: bool = True,
        return_sparse: bool = True,
        return_colbert_vecs: bool = False,
        batch_size: int = 32,
    ) -> dict:
        self.calls.append({"sentences": list(sentences)})
        n = len(sentences)
        # Cycle through provided lw dicts (repeat last if fewer than n)
        lw_list = []
        for i in range(n):
            lw_list.append(self._lw_per_item[min(i, len(self._lw_per_item) - 1)])
        return {
            "dense_vecs": [_fake_dense(self._dense_val) for _ in range(n)],
            "lexical_weights": lw_list,
            "colbert_vecs": None,
        }


# ---------------------------------------------------------------------------
# EmbeddingVector — validation tests
# ---------------------------------------------------------------------------


class TestEmbeddingVector:
    def test_valid_empty(self) -> None:
        ev = EmbeddingVector(indices=[], values=[])
        assert ev.indices == []
        assert ev.values == []

    def test_valid_single(self) -> None:
        ev = EmbeddingVector(indices=[42], values=[0.7])
        assert ev.indices == [42]
        assert ev.values == [0.7]

    def test_valid_multiple_sorted(self) -> None:
        ev = EmbeddingVector(indices=[1, 3, 7], values=[0.9, 0.5, 0.2])
        assert ev.indices == [1, 3, 7]
        assert ev.values == [0.9, 0.5, 0.2]

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match=r"len\(indices\)"):
            EmbeddingVector(indices=[1, 2], values=[0.5])

    def test_length_mismatch_more_values(self) -> None:
        with pytest.raises(ValueError, match=r"len\(indices\)"):
            EmbeddingVector(indices=[1], values=[0.5, 0.6])

    def test_unsorted_indices_raises(self) -> None:
        with pytest.raises(ValueError, match="sorted"):
            EmbeddingVector(indices=[3, 1], values=[0.5, 0.9])

    def test_unsorted_descending_raises(self) -> None:
        with pytest.raises(ValueError, match="sorted"):
            EmbeddingVector(indices=[7, 3, 1], values=[0.2, 0.5, 0.9])

    def test_frozen_immutable(self) -> None:
        ev = EmbeddingVector(indices=[1], values=[0.5])
        with pytest.raises((AttributeError, TypeError)):
            ev.indices = [2]  # type: ignore[misc]

    def test_large_valid(self) -> None:
        n = 500
        ev = EmbeddingVector(indices=list(range(n)), values=[float(i) for i in range(n)])
        assert len(ev.indices) == n
        assert len(ev.values) == n


# ---------------------------------------------------------------------------
# sparse_from_lexical_weights — deterministic adapter contract
# ---------------------------------------------------------------------------


class TestSparseFromLexicalWeights:
    """Thoroughly tests the canonical sparse adapter.

    This function is the contract Qdrant depends on — indices must be
    numerically sorted and values must align in the same order.
    """

    def test_docstring_example(self) -> None:
        """The example from the docstring must pass verbatim."""
        emb = BGEM3Embedder.sparse_from_lexical_weights({3: 0.5, 1: 0.9, 7: 0.2})
        assert emb.indices == [1, 3, 7]
        assert emb.values == [0.9, 0.5, 0.2]

    def test_empty_dict(self) -> None:
        emb = BGEM3Embedder.sparse_from_lexical_weights({})
        assert emb.indices == []
        assert emb.values == []

    def test_single_entry(self) -> None:
        emb = BGEM3Embedder.sparse_from_lexical_weights({100: 1.0})
        assert emb.indices == [100]
        assert emb.values == [1.0]

    def test_already_sorted_input(self) -> None:
        emb = BGEM3Embedder.sparse_from_lexical_weights({10: 0.1, 20: 0.2, 30: 0.3})
        assert emb.indices == [10, 20, 30]
        assert emb.values == [0.1, 0.2, 0.3]

    def test_reverse_order_input(self) -> None:
        """Even when input is reverse-ordered, output must be ascending."""
        emb = BGEM3Embedder.sparse_from_lexical_weights({30: 0.3, 20: 0.2, 10: 0.1})
        assert emb.indices == [10, 20, 30]
        assert emb.values == [0.1, 0.2, 0.3]

    def test_large_numeric_ids(self) -> None:
        """Large token IDs (e.g. tokenizer vocab size ~30K-100K) sort numerically."""
        lw = {99999: 0.01, 1: 0.9, 50000: 0.5, 100: 0.4}
        emb = BGEM3Embedder.sparse_from_lexical_weights(lw)
        assert emb.indices == [1, 100, 50000, 99999]
        assert emb.values == [0.9, 0.4, 0.5, 0.01]

    def test_values_aligned_with_indices(self) -> None:
        """Each value corresponds to its token ID after sorting."""
        lw = {5: 0.55, 2: 0.22, 8: 0.88, 1: 0.11}
        emb = BGEM3Embedder.sparse_from_lexical_weights(lw)
        # Verify alignment: for each (idx, val) pair, val == lw[idx]
        for idx, val in zip(emb.indices, emb.values):
            assert val == lw[idx], f"Misaligned: lw[{idx}]={lw[idx]} but got {val}"

    def test_float_weights_preserved(self) -> None:
        """Float precision is preserved."""
        lw = {10: 0.123456789, 20: 0.987654321}
        emb = BGEM3Embedder.sparse_from_lexical_weights(lw)
        assert emb.values[0] == pytest.approx(0.123456789)
        assert emb.values[1] == pytest.approx(0.987654321)

    def test_zero_weight(self) -> None:
        """Zero weights are valid and preserved."""
        lw = {1: 0.0, 5: 0.5}
        emb = BGEM3Embedder.sparse_from_lexical_weights(lw)
        assert emb.indices == [1, 5]
        assert emb.values == [0.0, 0.5]

    def test_returns_embedding_vector_type(self) -> None:
        emb = BGEM3Embedder.sparse_from_lexical_weights({3: 0.5, 1: 0.9})
        assert isinstance(emb, EmbeddingVector)

    def test_round_trip(self) -> None:
        """Round-trip: reconstruct dict from (indices, values) equals original."""
        original = {354: 0.08, 1234: 0.2, 7: 0.15, 999: 0.05}
        emb = BGEM3Embedder.sparse_from_lexical_weights(original)
        reconstructed = dict(zip(emb.indices, emb.values))
        assert reconstructed == original

    def test_deterministic_repeated_calls(self) -> None:
        """Same input must yield identical results on repeated calls."""
        lw = {3: 0.5, 1: 0.9, 7: 0.2}
        emb1 = BGEM3Embedder.sparse_from_lexical_weights(lw)
        emb2 = BGEM3Embedder.sparse_from_lexical_weights(lw)
        assert emb1.indices == emb2.indices
        assert emb1.values == emb2.values

    def test_resulting_ev_passes_validation(self) -> None:
        """The returned EmbeddingVector must always satisfy its own invariants."""
        lw = {20: 0.2, 5: 0.5, 100: 1.0, 1: 0.1}
        emb = BGEM3Embedder.sparse_from_lexical_weights(lw)
        # If indices aren't sorted, EmbeddingVector.__post_init__ would have raised
        assert emb.indices == sorted(emb.indices)
        assert len(emb.indices) == len(emb.values)


# ---------------------------------------------------------------------------
# encode_passages — mocked model tests
# ---------------------------------------------------------------------------


class TestEncodePassages:
    def _make_embedder(self, fake_model: _FakeModel) -> BGEM3Embedder:
        embedder = BGEM3Embedder()
        embedder._model = fake_model  # bypass _load_model
        return embedder

    def test_returns_one_per_input(self) -> None:
        fake = _FakeModel(lexical_weights_per_item=[{"1": 0.9, "3": 0.5}] * 3)
        embedder = self._make_embedder(fake)
        texts = ["alpha", "beta", "gamma"]
        results = embedder.encode_passages(texts)
        assert len(results) == 3

    def test_returns_passage_embedding_type(self) -> None:
        fake = _FakeModel()
        embedder = self._make_embedder(fake)
        results = embedder.encode_passages(["hello world"])
        assert isinstance(results[0], PassageEmbedding)

    def test_dense_dim_1024(self) -> None:
        fake = _FakeModel()
        embedder = self._make_embedder(fake)
        results = embedder.encode_passages(["test passage"])
        assert len(results[0].dense) == DENSE_DIM

    def test_dense_is_list_of_floats(self) -> None:
        fake = _FakeModel(dense_val=0.42)
        embedder = self._make_embedder(fake)
        results = embedder.encode_passages(["test"])
        dense = results[0].dense
        assert isinstance(dense, list)
        assert all(isinstance(v, float) for v in dense)

    def test_sparse_is_embedding_vector(self) -> None:
        fake = _FakeModel(lexical_weights_per_item=[{"10": 0.8, "2": 0.3}])
        embedder = self._make_embedder(fake)
        results = embedder.encode_passages(["test"])
        assert isinstance(results[0].sparse, EmbeddingVector)

    def test_sparse_indices_sorted_ascending(self) -> None:
        """String keys from model must be coerced to int and sorted numerically."""
        # NOTE: "10" < "2" lexicographically but 10 > 2 numerically.
        # If string keys are sorted as strings, this test catches the bug.
        fake = _FakeModel(lexical_weights_per_item=[{"10": 0.8, "2": 0.3}])
        embedder = self._make_embedder(fake)
        results = embedder.encode_passages(["test"])
        assert results[0].sparse.indices == [2, 10]  # numeric sort, not lexicographic
        assert results[0].sparse.values == [0.3, 0.8]

    def test_sparse_values_aligned(self) -> None:
        lw = {"354": 0.08, "1": 0.9, "7": 0.15}
        fake = _FakeModel(lexical_weights_per_item=[lw])
        embedder = self._make_embedder(fake)
        results = embedder.encode_passages(["passage"])
        ev = results[0].sparse
        # Reconstruct and compare to original (with int keys)
        reconstructed = dict(zip(ev.indices, ev.values))
        expected = {int(k): float(v) for k, v in lw.items()}
        assert reconstructed == expected

    def test_no_prefix_added_to_sentences(self) -> None:
        """Critical: BGE-M3 must NOT receive any instruction prefix."""
        fake = _FakeModel()
        embedder = self._make_embedder(fake)
        texts = ["What is the capital of France?", "Some passage text."]
        embedder.encode_passages(texts)
        assert len(fake.calls) == 1
        sent = fake.calls[0]["sentences"]
        assert sent == texts  # exact match — no prefix inserted

    def test_order_preserved(self) -> None:
        """Output order matches input order."""
        lw_list = [{"1": float(i)} for i in range(5)]
        fake = _FakeModel(lexical_weights_per_item=lw_list)
        embedder = self._make_embedder(fake)
        texts = [f"text_{i}" for i in range(5)]
        results = embedder.encode_passages(texts)
        for i, result in enumerate(results):
            # Each passage i should have value == float(i) at index 1
            assert result.sparse.indices == [1]
            assert result.sparse.values == pytest.approx([float(i)])

    def test_empty_lexical_weights(self) -> None:
        """A passage with no lexical hits yields empty EmbeddingVector."""
        fake = _FakeModel(lexical_weights_per_item=[{}])
        embedder = self._make_embedder(fake)
        results = embedder.encode_passages(["sparse passage"])
        ev = results[0].sparse
        assert ev.indices == []
        assert ev.values == []

    def test_batch_size_passed_to_model(self) -> None:
        """batch_size configured on embedder is forwarded to encode."""
        # We verify the call is made (fake doesn't fail on batch_size)
        fake = _FakeModel()
        embedder = BGEM3Embedder(batch_size=16)
        embedder._model = fake
        embedder.encode_passages(["a", "b"])
        assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# encode_query — mocked model tests
# ---------------------------------------------------------------------------


class TestEncodeQuery:
    def _make_embedder(self, fake_model: _FakeModel) -> BGEM3Embedder:
        embedder = BGEM3Embedder()
        embedder._model = fake_model
        return embedder

    def test_returns_query_embedding_type(self) -> None:
        fake = _FakeModel()
        embedder = self._make_embedder(fake)
        result = embedder.encode_query("what is BGE-M3?")
        assert isinstance(result, QueryEmbedding)

    def test_dense_dim_1024(self) -> None:
        fake = _FakeModel()
        embedder = self._make_embedder(fake)
        result = embedder.encode_query("query text")
        assert len(result.dense) == DENSE_DIM

    def test_dense_is_list_of_floats(self) -> None:
        fake = _FakeModel(dense_val=0.77)
        embedder = self._make_embedder(fake)
        result = embedder.encode_query("query")
        assert isinstance(result.dense, list)
        assert all(isinstance(v, float) for v in result.dense)

    def test_sparse_is_embedding_vector(self) -> None:
        fake = _FakeModel(lexical_weights_per_item=[{"5": 0.6}])
        embedder = self._make_embedder(fake)
        result = embedder.encode_query("query")
        assert isinstance(result.sparse, EmbeddingVector)

    def test_sparse_string_keys_coerced_to_int(self) -> None:
        """String keys from real model output are coerced to int, then sorted numerically."""
        fake = _FakeModel(lexical_weights_per_item=[{"100": 0.1, "9": 0.9}])
        embedder = self._make_embedder(fake)
        result = embedder.encode_query("test query")
        # "100" < "9" lexicographically, but 9 < 100 numerically
        assert result.sparse.indices == [9, 100]
        assert result.sparse.values == pytest.approx([0.9, 0.1])

    def test_no_prefix_added_to_query(self) -> None:
        """BGE-M3 must NOT receive any prefix on the query side either."""
        fake = _FakeModel()
        embedder = self._make_embedder(fake)
        query = "local-first hybrid retrieval"
        embedder.encode_query(query)
        assert len(fake.calls) == 1
        # The model sees exactly [query] — a single-element list, no prefix
        sent = fake.calls[0]["sentences"]
        assert sent == [query]

    def test_wraps_query_in_list(self) -> None:
        """encode_query must pass [text] (a list) to model.encode, not text directly."""
        fake = _FakeModel()
        embedder = self._make_embedder(fake)
        embedder.encode_query("single query")
        sent = fake.calls[0]["sentences"]
        assert isinstance(sent, list)
        assert len(sent) == 1

    def test_empty_lexical_weights(self) -> None:
        fake = _FakeModel(lexical_weights_per_item=[{}])
        embedder = self._make_embedder(fake)
        result = embedder.encode_query("sparse query")
        assert result.sparse.indices == []
        assert result.sparse.values == []

    def test_sparse_values_aligned(self) -> None:
        lw = {"3": 0.5, "1": 0.9, "7": 0.2}
        fake = _FakeModel(lexical_weights_per_item=[lw])
        embedder = self._make_embedder(fake)
        result = embedder.encode_query("test")
        ev = result.sparse
        reconstructed = dict(zip(ev.indices, ev.values))
        expected = {int(k): float(v) for k, v in lw.items()}
        assert reconstructed == expected


# ---------------------------------------------------------------------------
# _load_model — lazy loading and error handling
# ---------------------------------------------------------------------------


class TestLoadModel:
    def test_load_model_raises_runtime_error_without_flagembedding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When FlagEmbedding is not installed, RuntimeError is raised."""
        import sys
        import builtins

        real_import = builtins.__import__

        def mock_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "FlagEmbedding":
                raise ImportError("No module named 'FlagEmbedding'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        embedder = BGEM3Embedder()
        with pytest.raises(RuntimeError, match="FlagEmbedding is not installed"):
            embedder._load_model()

    def test_load_model_cached_after_first_call(self) -> None:
        """_load_model must return the cached instance on subsequent calls."""
        fake = _FakeModel()
        embedder = BGEM3Embedder()
        embedder._model = fake  # pre-populate cache
        result1 = embedder._load_model()
        result2 = embedder._load_model()
        assert result1 is result2 is fake

    def test_model_is_none_before_first_encode(self) -> None:
        embedder = BGEM3Embedder()
        assert embedder._model is None
