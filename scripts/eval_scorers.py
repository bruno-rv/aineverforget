#!/usr/bin/env python3
"""scripts/eval_scorers.py — deterministic retrieval metrics for eval harness.

Implements recall@k and MRR (mean reciprocal rank). Pure Python, no external
deps — runnable without a live Qdrant or embedder.

Usage:
    python3 scripts/eval_scorers.py         # run self-tests and exit 0/1
    python3 -c "from scripts.eval_scorers import recall_at_k, mrr; ..."
"""

from __future__ import annotations


def recall_at_k(ranked_doc_ids: list[str], relevant_ids: set[str], k: int) -> float:
    """Fraction of relevant_ids found in top-k ranked_doc_ids.

    Returns 1.0 if any relevant id appears in top-k; 0.0 otherwise.
    (Binary recall for single-relevant-doc cases typical in eval fixtures.)
    If relevant_ids is empty, returns 1.0 (vacuously satisfied).
    """
    if not relevant_ids:
        return 1.0
    top_k = ranked_doc_ids[:k]
    for doc_id in top_k:
        if doc_id in relevant_ids:
            return 1.0
    return 0.0


def mrr(ranked_doc_ids: list[str], relevant_ids: set[str]) -> float:
    """Reciprocal rank of the first relevant document in the ranked list.

    Returns 1/position (1-indexed) of the first hit, or 0.0 if no hit found.
    """
    for position, doc_id in enumerate(ranked_doc_ids, start=1):
        if doc_id in relevant_ids:
            return 1.0 / position
    return 0.0


if __name__ == "__main__":
    import sys

    failures: list[str] = []

    def _check(name: str, actual: float, expected: float, tol: float = 1e-9) -> None:
        if abs(actual - expected) <= tol:
            print(f"PASS  {name}")
        else:
            print(f"FAIL  {name}  (got {actual!r}, expected {expected!r})")
            failures.append(name)

    # recall_at_k: hit in position 1 → 1.0
    _check(
        "recall_at_k: hit at position 1",
        recall_at_k(["doc-a", "doc-b", "doc-c"], {"doc-a"}, k=3),
        1.0,
    )

    # recall_at_k: hit in position 3, k=3 → 1.0
    _check(
        "recall_at_k: hit at position 3, k=3",
        recall_at_k(["doc-x", "doc-y", "doc-a"], {"doc-a"}, k=3),
        1.0,
    )

    # recall_at_k: hit in position 4, k=3 → 0.0
    _check(
        "recall_at_k: hit at position 4, k=3",
        recall_at_k(["doc-x", "doc-y", "doc-z", "doc-a"], {"doc-a"}, k=3),
        0.0,
    )

    # recall_at_k: empty relevant_ids → 1.0
    _check(
        "recall_at_k: empty relevant_ids",
        recall_at_k(["doc-a", "doc-b"], set(), k=5),
        1.0,
    )

    # mrr: hit at position 1 → 1.0
    _check(
        "mrr: hit at position 1",
        mrr(["doc-a", "doc-b", "doc-c"], {"doc-a"}),
        1.0,
    )

    # mrr: hit at position 3 → 0.333...
    _check(
        "mrr: hit at position 3",
        mrr(["doc-x", "doc-y", "doc-a"], {"doc-a"}),
        1.0 / 3,
        tol=1e-6,
    )

    # mrr: no hit → 0.0
    _check(
        "mrr: no hit",
        mrr(["doc-x", "doc-y", "doc-z"], {"doc-a"}),
        0.0,
    )

    if failures:
        print(f"\n{len(failures)} test(s) FAILED: {failures}")
        sys.exit(1)
    else:
        print("\nAll tests passed.")
        sys.exit(0)
