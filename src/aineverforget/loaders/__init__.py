"""aineverforget.loaders — Loader registry and Protocol.

Loaders know how to read one kind of Source and emit normalized Documents.
The registry maps source_type string → Loader instance; adding a new Source
type means adding a Loader and registering it — nothing else changes.

No heavy imports at module level: importable with only stdlib + pydantic.
Heavy per-loader deps (mistune, pypdf, pdfplumber) are imported lazily inside
each loader's ``load()`` method body.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from aineverforget.models import Document


# ---------------------------------------------------------------------------
# Loader verdict — used by PDF loader to signal content quality
# ---------------------------------------------------------------------------


class LoaderVerdict(str, Enum):
    """Outcome of a Loader's attempt to extract text from a Source file.

    Emitted in ``Document.meta["loader_verdict"]`` so downstream steps and
    the CLI can surface actionable information to the user.

    Values
    ------
    ok:
        Text extracted successfully with high confidence.
    encrypted:
        The file is password-protected and cannot be read without a key.
        Requires user intervention; ingest is blocked.
    scanned:
        No text layer found (likely a scanned image-only PDF).
        OCR is out of scope for v1; ingest is blocked.
    low_confidence:
        Text was extracted but quality signals are poor (e.g. many garbled
        characters, very low char-per-page ratio).  Ingest proceeds but the
        Document is flagged; the indexer may emit a warning.
    """

    ok = "ok"
    encrypted = "encrypted"
    scanned = "scanned"
    low_confidence = "low_confidence"


# ---------------------------------------------------------------------------
# Loader Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Loader(Protocol):
    """Protocol that every Source-type Loader must implement.

    A Loader knows how to read one kind of Source (e.g. markdown/txt, PDF)
    and yield one or more normalized ``Document`` objects from it.

    Implementations must:
    - Accept a filesystem path (``pathlib.Path``).
    - Yield at least one ``Document`` for a valid, non-empty Source.
    - Set ``Document.meta["loader_verdict"]`` to a ``LoaderVerdict`` value.
    - Import heavy dependencies lazily inside ``load()`` to keep module-level
      imports clean.

    Methods
    -------
    load(path) -> Iterable[Document]
        Read *path* and yield Documents.  May yield multiple Documents for
        bundled or multi-section Sources.
    """

    def load(self, path: Path) -> Iterable[Document]:
        """Read *path* and yield normalized Documents.

        Parameters
        ----------
        path:
            Absolute filesystem path to the Source file.

        Yields
        ------
        Document
            Normalized content with all identity fields populated (source_id,
            source_type, document_id, document_path, document_sha256, title,
            producer, raw_text, meta).

        Notes
        -----
        Loaders set ``Document.meta["loader_verdict"]`` to a ``LoaderVerdict``
        value.  Encrypted / scanned files still yield a Document (with empty
        raw_text and an appropriate verdict) so the caller can surface the
        issue to the user rather than silently skipping.
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_registry: dict[str, Loader] = {}


def register_loader(source_type: str, loader: Loader) -> None:
    """Register *loader* for *source_type*.

    Parameters
    ----------
    source_type:
        The source_type string that will appear in ``Document.source_type``
        and Chunk payloads (e.g. ``"markdown"``, ``"pdf"``).
    loader:
        An object implementing the ``Loader`` Protocol.

    Raises
    ------
    TypeError
        If *loader* does not implement the ``Loader`` Protocol.
    ValueError
        If *source_type* is already registered (use ``force=True`` to replace).
    """
    if not isinstance(loader, Loader):
        raise TypeError(f"{loader!r} does not implement the Loader protocol.")
    if source_type in _registry:
        raise ValueError(
            f"source_type {source_type!r} is already registered. "
            "Pass force=True to replace, or unregister first."
        )
    _registry[source_type] = loader


def register_loader_force(source_type: str, loader: Loader) -> None:
    """Register *loader* for *source_type*, replacing any existing entry.

    For use in tests that need to swap in a fake loader.
    """
    if not isinstance(loader, Loader):
        raise TypeError(f"{loader!r} does not implement the Loader protocol.")
    _registry[source_type] = loader


def get_loader(source_type: str) -> Loader:
    """Return the registered Loader for *source_type*.

    Parameters
    ----------
    source_type:
        Source type string (e.g. ``"markdown"``, ``"pdf"``).

    Returns
    -------
    Loader
        The registered loader.

    Raises
    ------
    KeyError
        If no loader is registered for *source_type*.
    """
    try:
        return _registry[source_type]
    except KeyError:
        available = ", ".join(sorted(_registry)) or "(none)"
        raise KeyError(
            f"No loader registered for source_type={source_type!r}. "
            f"Available: {available}."
        )


def registered_source_types() -> list[str]:
    """Return the list of currently registered source_type strings."""
    return sorted(_registry)


def infer_source_type(path: Path) -> str:
    """Infer the source_type string from a file's extension.

    Parameters
    ----------
    path:
        Filesystem path to a Source file.

    Returns
    -------
    str
        ``"pdf"`` for ``.pdf`` files; ``"markdown"`` for ``.md``/``.txt``
        and other text extensions.

    Raises
    ------
    ValueError
        If the extension is not recognized.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix in {".md", ".txt", ".markdown", ".rst", ".text"}:
        return "markdown"
    raise ValueError(
        f"Cannot infer source_type for extension {suffix!r} (path={path}). "
        "Pass --source-type explicitly."
    )
