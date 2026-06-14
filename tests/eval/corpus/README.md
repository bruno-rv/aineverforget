## Eval Corpus — Frozen Test Fixtures

This directory contains frozen source notes used by the aineverforget eval harness (Phase D).

### What this is

Three synthetic notes covering different ingest paths (raw transcript, pre-structured, technical design)
plus fixture YAML files in `../fixtures/` that encode gold assertions against these exact contents.
Together they form the stable mini-corpus for evaluating note-summarizer, knowledge-indexer,
knowledge-retriever, and answer-synthesizer agents.

### WARNING: Do not edit these files

Content changes rotate `document_sha256` → rotate all `point_id`s → invalidate every retrieval gold
fixture in `tests/eval/fixtures/`. If you need to change a file, follow the re-derivation procedure
below and update ALL affected fixture YAML files in the same commit.

### Stable source_id convention

Each corpus file uses its repo-relative path as the `source_id` when ingesting:

    aineverforget ingest --source-id tests/eval/corpus/<file>.md <abs_path> --json

This makes `document_id` path-stable (independent of where the repo is checked out).

### Pre-computed document_ids

| File | source_id | document_id |
|------|-----------|-------------|
| note_raw_transcript.md | tests/eval/corpus/note_raw_transcript.md | 6131b682-d4c2-5c9e-a78b-7d35067e0b12 |
| note_prestructured.md | tests/eval/corpus/note_prestructured.md | b39899a7-6a26-5ed7-a6d3-85c27a21119c |
| note_technical.md | tests/eval/corpus/note_technical.md | b37eb527-d616-50e8-97ed-ce0952062741 |

### Re-deriving document_ids after a forced file change

`document_id` is `UUIDv5(POINT_NAMESPACE, "{source_id}|{source_id}")` where
`POINT_NAMESPACE = a1b2c3d4-e5f6-7890-abcd-ef1234567890`.

```
python3 -c "import uuid; NS=uuid.UUID('a1b2c3d4-e5f6-7890-abcd-ef1234567890'); sid='tests/eval/corpus/<file>.md'; print(uuid.uuid5(NS, f'{sid}|{sid}'))"
```

Replace `<file>` with the corpus filename (without path prefix). After re-deriving, update
`tests/eval/fixtures/knowledge_indexer.yaml`, `knowledge_retriever.yaml`, and
`answer_synthesizer.yaml` to reflect the new IDs.
