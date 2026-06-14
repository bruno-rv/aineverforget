# aineverforget

aineverforget is a local-first, eval-gated personal knowledge base. It ingests
Markdown, text, and PDF sources into Qdrant, then searches the indexed corpus
with BGE-M3 dense and sparse embeddings.

Use it when you want meeting notes, transcripts, project notes, or generated
summaries to become searchable local memory with citations back to the source
chunks.

## What is included

- Python package and CLI: `aineverforget`
- Agent and skill specifications under `.codex/` and `.claude/`
- Deterministic eval fixtures and scripts under `tests/eval/` and `scripts/`
- Pinned development dependency lockfile: `requirements-dev.lock`

The repository does not vendor Python wheels, the BGE-M3 model files, Docker, or
a Qdrant database. A fresh machine still needs Python, package index access, and
a running Qdrant service for real ingest and search.

## Requirements

- Python 3.11 or newer. CI uses Python 3.12, and the lockfile is generated for
  Python 3.12.
- Network access to PyPI or your corporate Python package mirror.
- Network or local model-cache access for the first real `FlagEmbedding`
  BGE-M3 model load.
- Docker, or another way to run Qdrant, for real ingest/search and the live
  retrieval eval.

## Fresh clone setup

Clone the repository and install the pinned dependencies:

```bash
git clone https://github.com/bruno-rv/aineverforget.git
cd aineverforget

PYTHON=python3.12  # or any Python 3.11+ interpreter
$PYTHON -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.lock
python -m pip install -e . --no-deps
```

The lockfile install keeps transitive dependency versions stable. The editable
install with `--no-deps` registers the local `aineverforget` package without
asking pip to resolve dependencies again.

## Verify the deterministic setup

Run these checks after setup. They do not require a live Qdrant server or a real
embedding model download:

```bash
python -m pytest
python scripts/eval_scorers.py
python scripts/eval_gate_synthesis.py
aineverforget --help
```

## Run Qdrant locally

Start Qdrant before running real ingest, search, or the live retrieval eval:

```bash
docker run -d --name qdrant -p 6333:6333 qdrant/qdrant:latest
```

The default runtime configuration expects Qdrant at `http://127.0.0.1:6333`.

## Basic CLI usage

Check the installation and Qdrant connection:

```bash
aineverforget status
aineverforget status --json
```

Ingest one or more source files:

```bash
aineverforget ingest notes/meeting.md --tag meeting --producer user
```

Use `--source-id` when you need a stable logical identifier across machines:

```bash
aineverforget ingest notes/meeting.md \
  --source-id custom://meetings/2026-06-14 \
  --tag meeting
```

Search the active corpus:

```bash
aineverforget search "What did we decide about Qdrant?" --limit 5
```

Use exact lexical enumeration when you need all matching chunks for a term:

```bash
aineverforget lexscan "Qdrant" --count
```

List active document metadata:

```bash
aineverforget scroll --json
```

See all commands and options:

```bash
aineverforget --help
aineverforget ingest --help
aineverforget search --help
```

## Configuration

Runtime settings use the `AINF_` environment variable prefix. Common overrides:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AINF_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant server URL |
| `AINF_COLLECTION` | `ainf_corpus_bgem3_v1` | Qdrant collection name |
| `AINF_EMBED_MODEL` | `BAAI/bge-m3` | `FlagEmbedding` model checkpoint |
| `AINF_EMBED_DIM` | `1024` | Dense vector dimension |

Example:

```bash
AINF_QDRANT_URL=http://localhost:6333 aineverforget status
```

## Live retrieval eval

After Qdrant is running, ingest the frozen eval corpus and run retrieval checks:

```bash
python scripts/eval_retrieval.py --ingest
```

This path uses the real embedder and may download the BGE-M3 model the first
time it runs.

## Updating dependencies

Project dependency ranges live in `pyproject.toml`. The pinned development
environment lives in `requirements-dev.lock`.

After changing dependencies, regenerate the lockfile with Python 3.12:

```bash
uv pip compile pyproject.toml \
  --extra dev \
  --python-version 3.12 \
  -o requirements-dev.lock
```

Then rerun the deterministic setup checks:

```bash
python -m pytest
python scripts/eval_scorers.py
python scripts/eval_gate_synthesis.py
```
