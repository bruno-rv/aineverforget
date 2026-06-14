"""aineverforget.config — runtime settings.

All values have sensible defaults and can be overridden via environment
variables (AINF_* prefix) or CLI flags passed by the orchestrator.

Implementation note: uses plain pydantic BaseModel with explicit env-var
reading via os.environ so that pydantic-settings is NOT a hard dependency
(keeps the test-time dep surface to pydantic-only).

No heavy imports: this module must be importable with only stdlib + pydantic.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


def _env(key: str, default: str) -> str:
    """Read AINF_{KEY} from environment, falling back to *default*."""
    return os.environ.get(f"AINF_{key.upper()}", default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(f"AINF_{key.upper()}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings(BaseModel):
    """Global runtime configuration for aineverforget.

    Environment variable prefix: AINF_

    Examples
    --------
    Override collection at runtime::

        AINF_COLLECTION=ainf_corpus_bgem3_v2 aineverforget status
    """

    # ── Qdrant ──────────────────────────────────────────────────────────────
    collection: str = Field(
        default="ainf_corpus_bgem3_v1",
        description=(
            "Qdrant collection name.  Versioned so a model change is an explicit "
            "re-index into a new collection, never a silent mismatch."
        ),
    )
    qdrant_url: str = Field(
        default="http://127.0.0.1:6333",
        description="URL of the local Qdrant instance.",
    )

    # ── Embedder ────────────────────────────────────────────────────────────
    embed_model: str = Field(
        default="BAAI/bge-m3",
        description=(
            "FlagEmbedding BGEM3FlagModel checkpoint.  Produces both dense (1024-dim) "
            "and sparse (lexical_weights) vectors from one local model.  "
            "No instruction prefix — follows BGE-M3 model card."
        ),
    )
    embed_dim: int = Field(
        default=1024,
        description="Dense vector dimensionality.  Must match the deployed embedder checkpoint.",
    )

    # ── Chunking — markdown / prose ─────────────────────────────────────────
    chunk_word_window: int = Field(
        default=220,
        description=(
            "Target word-window for prose and PDF chunking.  Sized to the BGE-M3 "
            "token window (~512 tokens ≈ 220 English words at average token/word ratio)."
        ),
    )
    chunk_word_overlap: int = Field(
        default=40,
        description="Word overlap between consecutive prose / PDF chunks.",
    )

    # ── Retriever Quality Gate predicate constants ───────────────────────────
    # Per PLAN.md § Quality Gates + ADR-0003:
    #   candidate_count >= 1 AND (dense_hits >= 1 OR sparse_hits >= 1)
    #   AND citationable_count >= 1
    # These are per-modality pre-fusion counts — NOT a fused-RRF score floor.
    retriever_min_candidate_count: int = Field(
        default=1,
        description="Minimum total candidates after RRF fusion to pass the retriever gate.",
    )
    retriever_min_dense_hits: int = Field(
        default=1,
        description="Minimum dense-prefetch hits required (OR with sparse) for the gate.",
    )
    retriever_min_sparse_hits: int = Field(
        default=1,
        description="Minimum sparse-prefetch hits required (OR with dense) for the gate.",
    )
    retriever_min_citationable_count: int = Field(
        default=1,
        description=(
            "Minimum citationable candidates (text non-empty, document_path present) "
            "among the fused results to pass the retriever gate."
        ),
    )

    # ── Verify probes ────────────────────────────────────────────────────────
    verify_topical_limit: int = Field(
        default=10,
        description="Top-k for topical probe (document must appear in results).",
    )
    verify_specific_limit: int = Field(
        default=5,
        description="Top-k for specific probe (known-fact substring must be present).",
    )
    verify_negative_limit: int = Field(
        default=5,
        description="Top-k for negative probe (unrelated query must NOT surface the pending gen).",
    )


def load_settings(**overrides: object) -> Settings:
    """Build a Settings instance reading AINF_* env vars, then applying *overrides*.

    Called by CLI main() so flags like --collection can override env defaults.
    """
    return Settings(
        collection=str(overrides.get("collection", _env("collection", "ainf_corpus_bgem3_v1"))),
        qdrant_url=str(overrides.get("qdrant_url", _env("qdrant_url", "http://127.0.0.1:6333"))),
        embed_model=str(overrides.get("embed_model", _env("embed_model", "BAAI/bge-m3"))),
        embed_dim=int(overrides.get("embed_dim", _env_int("embed_dim", 1024))),
        chunk_word_window=int(overrides.get("chunk_word_window", _env_int("chunk_word_window", 220))),
        chunk_word_overlap=int(overrides.get("chunk_word_overlap", _env_int("chunk_word_overlap", 40))),
        retriever_min_candidate_count=int(
            overrides.get("retriever_min_candidate_count", _env_int("retriever_min_candidate_count", 1))
        ),
        retriever_min_dense_hits=int(
            overrides.get("retriever_min_dense_hits", _env_int("retriever_min_dense_hits", 1))
        ),
        retriever_min_sparse_hits=int(
            overrides.get("retriever_min_sparse_hits", _env_int("retriever_min_sparse_hits", 1))
        ),
        retriever_min_citationable_count=int(
            overrides.get("retriever_min_citationable_count", _env_int("retriever_min_citationable_count", 1))
        ),
        verify_topical_limit=int(overrides.get("verify_topical_limit", _env_int("verify_topical_limit", 10))),
        verify_specific_limit=int(overrides.get("verify_specific_limit", _env_int("verify_specific_limit", 5))),
        verify_negative_limit=int(overrides.get("verify_negative_limit", _env_int("verify_negative_limit", 5))),
    )
