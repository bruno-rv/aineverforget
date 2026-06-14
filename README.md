# aineverforget
Gather meetings, conversations repo informations and store them in QDrant to provide users with contextualized answers

## Fresh clone setup

Requirements:

- Python 3.11+; CI currently runs Python 3.12
- Docker, if you want to run the live Qdrant integration eval locally
- Network/model-cache access for first real BGE-M3 embedding use via `FlagEmbedding`

Install from a new clone:

```bash
PYTHON=python3.12  # or any Python 3.11+ interpreter
$PYTHON -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.lock
python -m pip install -e . --no-deps
```

`requirements-dev.lock` is generated from `pyproject.toml` with Python 3.12:

```bash
uv pip compile pyproject.toml --extra dev --python-version 3.12 -o requirements-dev.lock
```

Verify the deterministic local setup:

```bash
python -m pytest
python scripts/eval_scorers.py
python scripts/eval_gate_synthesis.py
```

Optional live retrieval eval:

```bash
docker run -d --name qdrant -p 6333:6333 qdrant/qdrant:latest
python scripts/eval_retrieval.py --ingest
```
