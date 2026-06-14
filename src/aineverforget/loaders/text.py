"""aineverforget.loaders.text — Markdown / plain-text Loader.

Source types handled: ``"markdown"`` (covers ``.md``, ``.txt``, ``.rst``,
``.markdown``, ``.text``).

Design
------
The Loader's job is to extract raw text and a title.  Heading-path tracking
and block splitting are the *chunker's* responsibility.  The full markdown
source is passed through as-is so the chunker can parse structure.

Mistune is intentionally NOT imported here: the module header notes in the
stub described an aspirational approach (mistune for block-level parsing at
load time) but the frozen `load()` docstring and task prompt are clear —
keep raw_text intact, chunker handles heading/block parsing.

No heavy imports at module level; importable with only stdlib + pydantic.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from aineverforget.identity import make_document_id, sha256_text
from aineverforget.loaders import LoaderVerdict, register_loader
from aineverforget.models import Document

LOADER_VERSION = "text:1.0"
"""Version string embedded in every Document produced by this loader.

Bump this constant whenever the loader logic changes in a way that would
alter the Documents it produces (e.g. heading extraction logic, block
splitting strategy).  The version propagates to Chunk.loader_version and is
stored in Qdrant for provenance and re-index detection.
"""

# ATX H1 heading pattern: optional leading whitespace, one '#', space, text.
_H1_RE = re.compile(r"^#{1}\s+(.+)$", re.MULTILINE)


class MarkdownLoader:
    """Loader for markdown and plain-text Source files.

    Registered as ``"markdown"`` in the loader registry.

    Source types: ``.md``, ``.txt``, ``.rst``, ``.markdown``, ``.text``.
    """

    def load(self, path: Path) -> Iterable[Document]:
        """Read *path* as UTF-8 markdown/text and yield one Document.

        Implementation contract
        -----------------------
        1. Read ``path`` as UTF-8 text; if the file cannot be decoded, fall
           back to ``latin-1``.
        2. Compute ``document_sha256 = identity.sha256_text(raw_text)``.
        3. Extract ``title``: use the first H1 heading if present; fall back
           to ``path.stem``.
        4. Set ``heading_path`` extraction state for the chunker: the raw_text
           carries full markdown so the chunker (``chunking.py``) can parse
           heading structure.  The Loader does NOT chunk; it extracts the full
           text.
        5. Set ``meta["loader_verdict"] = LoaderVerdict.ok.value``.
        6. Set ``meta["heading_extraction"] = True`` as a hint to the chunker
           that heading-aware block parsing should be applied.
        7. Yield exactly one ``Document``.

        Parameters
        ----------
        path:
            Absolute filesystem path to a markdown or text file.

        Yields
        ------
        Document
            Exactly one normalized Document with all identity fields populated.

        Raises
        ------
        FileNotFoundError
            If *path* does not exist.
        """
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        # Step 1: Read file, UTF-8 with latin-1 fallback.
        try:
            raw_text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw_text = path.read_text(encoding="latin-1")

        # Step 2: Content hash (document identity, not version order).
        document_sha256 = sha256_text(raw_text)

        # Step 3: Extract title from first ATX H1; fall back to stem.
        m = _H1_RE.search(raw_text)
        title = m.group(1).strip() if m else path.stem

        # Step 4-7: source_id = canonical absolute path of the file.
        source_id = str(path.resolve())
        document_path = source_id
        document_id = make_document_id(source_id, document_path)

        yield Document(
            source_id=source_id,
            source_type="markdown",
            document_id=document_id,
            document_path=document_path,
            document_sha256=document_sha256,
            title=title,
            producer="user",
            raw_text=raw_text,
            meta={
                "loader_verdict": LoaderVerdict.ok.value,
                "heading_extraction": True,
                "loader_version": LOADER_VERSION,
            },
        )


# ---------------------------------------------------------------------------
# Register in the global registry (runs once on import).
# ---------------------------------------------------------------------------

register_loader("markdown", MarkdownLoader())
