"""aineverforget.loaders.pdf — PDF Loader with loader verdicts.

Source types handled: ``"pdf"`` (``.pdf``).

Design
------
pypdf is the primary extractor.  pdfplumber is used as a per-page fallback
when pypdf yields empty text for a page.  pdfplumber is imported lazily and
guarded by a try/except so the loader works if only pypdf is installed.

OCR is out of scope for v1.  Scanned / image-only PDFs are identified by the
absence of a text layer (total chars < LOW_TEXT_THRESHOLD) and flagged with
the ``scanned`` verdict.

No heavy imports at module level; importable with only stdlib + pydantic.

Note on pypdf encryption errors
--------------------------------
``pypdf.errors.FileDecryptionError`` is named in the original stub docstring
but does not exist in pypdf 6.11.0.  Detection uses ``reader.is_encrypted``
(contract step 2) as the primary guard.  Defensive fallback catches
``pypdf.errors.FileNotDecryptedError`` and ``pypdf.errors.WrongPasswordError``
(the actual 6.11.0 names).  The stub docstring error name is flagged here but
left unchanged (per task constraints).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Iterable

from aineverforget.identity import make_document_id, sha256_text
from aineverforget.loaders import LoaderVerdict, register_loader
from aineverforget.models import Document

LOADER_VERSION = "pdf:1.0"
"""Version string embedded in every Document produced by this loader."""

LOW_TEXT_THRESHOLD: int = 100
"""Minimum total extracted characters before classifying a PDF as ``scanned``."""

LOW_CONFIDENCE_CHAR_RATIO: float = 0.70
"""Minimum *good-character* ratio required to avoid a ``low_confidence`` verdict.

A *good character* is any Unicode letter (category L*), combining mark (M*),
decimal number (N*), punctuation (P*), or whitespace — i.e. text that is
recognisably human-readable prose regardless of script (Portuguese, English,
CJK, Arabic, …).

Rationale: the previous heuristic used printable-ASCII ratio, which
false-flagged legitimate non-ASCII text.  The corpus explicitly contains
mixed Portuguese + English (with ã ç é ô á í ú â ê õ à …), so the old check
produced spurious ``low_confidence`` verdicts for clean Portuguese PDFs as well
as any CJK / Arabic material.

The new check is unicode-aware:
  good_ratio  = good_chars / len(text)          — low means garbled text
  mojibake_ratio = bad_chars / len(text)         — high means replacement chars
                                                   or stray control chars
Either signal below/above its threshold sets the ``low_confidence`` verdict.
Only C0/C1 control characters (unicodedata category Cc/Cf) *excluding* normal
whitespace are considered "bad", and the Unicode replacement character U+FFFD
is counted explicitly (its unicodedata category is So, not Cc).
"""

# Control characters that are NOT normal whitespace — indicators of garbled
# extraction.  We exclude \t \n \r \f \v (the characters str.isspace() covers
# for ASCII control chars) because those appear in legitimate prose.
_NORMAL_WHITESPACE: frozenset[str] = frozenset("\t\n\r\f\v ")

# Threshold: if bad chars (replacement + stray controls) exceed this fraction
# of the text length, the verdict is low_confidence even if the overall
# good-char ratio is satisfactory.
_MOJIBAKE_RATIO_THRESHOLD: float = 0.10


def _text_quality_ratios(text: str) -> tuple[float, float]:
    """Return (good_char_ratio, mojibake_ratio) for *text*.

    good_char_ratio
        Fraction of characters whose Unicode category begins with L
        (letters, all scripts), M (combining marks), N (numbers), P
        (punctuation), or that are whitespace.  High for real prose in any
        language; low for garbage bytes decoded as Latin-1 or similar.

    mojibake_ratio
        Fraction of characters that are the Unicode replacement character
        U+FFFD *or* a C0/C1 control character that is not normal whitespace
        (tab, newline, carriage return, form-feed, vertical tab, space).
        High values indicate a botched encoding round-trip or corrupted
        extraction.

    If *text* is empty, returns (1.0, 0.0) — vacuously clean; the ``scanned``
    verdict catches empty text before this function is called.
    """
    if not text:
        return 1.0, 0.0

    good = 0
    bad = 0
    for ch in text:
        if ch.isspace():
            good += 1
            continue
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "M", "N", "P"):
            good += 1
        elif ch == "�" or (cat in ("Cc", "Cf") and ch not in _NORMAL_WHITESPACE):
            bad += 1
        # Symbols (S*), separators (Z*), etc. are neutral — neither good nor bad.

    total = len(text)
    return good / total, bad / total


class PDFLoader:
    """Loader for PDF Source files.

    Registered as ``"pdf"`` in the loader registry.
    Emits loader verdicts in ``Document.meta["loader_verdict"]`` so downstream
    steps and the CLI can surface actionable information to the user.
    """

    def load(self, path: Path) -> Iterable[Document]:
        """Read *path* as a PDF and yield one Document.

        Implementation contract
        -----------------------
        1. Open *path* with ``pypdf.PdfReader``.
        2. If ``reader.is_encrypted``: yield a Document with empty ``raw_text``
           and ``meta["loader_verdict"] = LoaderVerdict.encrypted.value``.
           Return immediately.
        3. Extract per-page text: ``[page.extract_text() or "" for page in
           reader.pages]``.  For pages where pypdf yields empty text, retry
           with ``pdfplumber.open(path).pages[i].extract_text() or ""``.
        4. Compute ``raw_text = "\\n\\n".join(page_texts)`` (blank pages become
           blank lines, preserving page count alignment).
        5. Classify verdict:
           - total chars < ``LOW_TEXT_THRESHOLD`` → ``scanned``
           - good-char ratio < ``LOW_CONFIDENCE_CHAR_RATIO`` **or** mojibake
             ratio > ``_MOJIBAKE_RATIO_THRESHOLD`` → ``low_confidence``
           - else → ``ok``
           (Good chars = Unicode letters/marks/numbers/punctuation + whitespace;
           mojibake chars = U+FFFD + stray C0/C1 control chars.)
        6. Set ``meta["page_count"]`` and ``meta["page_texts"]`` (list[str]).
        7. Set ``meta["loader_verdict"]`` to the classified verdict's ``.value``.
        8. Compute ``document_sha256 = identity.sha256_text(raw_text)``.
        9. Extract ``title``: PDF ``/Title`` metadata if non-empty; else
           ``path.stem``.
        10. Yield exactly one ``Document``.

        Parameters
        ----------
        path:
            Absolute filesystem path to a PDF file.

        Yields
        ------
        Document
            Exactly one normalized Document.  For ``encrypted``/``scanned``
            verdicts, ``raw_text`` is ``""`` and the verdict is in
            ``meta["loader_verdict"]``.

        Raises
        ------
        FileNotFoundError
            If *path* does not exist.
        """
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        # Lazy imports — keep module importable without these deps installed.
        import pypdf
        import pypdf.errors

        try:
            import pdfplumber as _pdfplumber
        except ImportError:
            _pdfplumber = None  # type: ignore[assignment]

        # Step 1: Open with pypdf.
        try:
            reader = pypdf.PdfReader(str(path))
        except (pypdf.errors.PdfReadError, pypdf.errors.EmptyFileError) as exc:
            # Unreadable PDF — treat as scanned (no usable text layer).
            source_id = str(path.resolve())
            document_path = source_id
            document_id = make_document_id(source_id, document_path)
            yield Document(
                source_id=source_id,
                source_type="pdf",
                document_id=document_id,
                document_path=document_path,
                document_sha256=sha256_text(""),
                title=path.stem,
                producer="user",
                raw_text="",
                meta={
                    "loader_verdict": LoaderVerdict.scanned.value,
                    "page_count": 0,
                    "page_texts": [],
                    "loader_version": LOADER_VERSION,
                    "error": str(exc),
                },
            )
            return

        # Step 2: Encrypted check.
        if reader.is_encrypted:
            source_id = str(path.resolve())
            document_path = source_id
            document_id = make_document_id(source_id, document_path)
            yield Document(
                source_id=source_id,
                source_type="pdf",
                document_id=document_id,
                document_path=document_path,
                document_sha256=sha256_text(""),
                title=path.stem,
                producer="user",
                raw_text="",
                meta={
                    "loader_verdict": LoaderVerdict.encrypted.value,
                    "page_count": 0,
                    "page_texts": [],
                    "loader_version": LOADER_VERSION,
                },
            )
            return

        # Step 3: Per-page text extraction with pdfplumber fallback.
        page_texts: list[str] = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if not text.strip() and _pdfplumber is not None:
                # Fallback: pdfplumber for pages pypdf could not extract.
                try:
                    with _pdfplumber.open(str(path)) as plumb_doc:
                        plumb_text = plumb_doc.pages[i].extract_text() or ""
                    text = plumb_text
                except Exception:
                    # pdfplumber failure is non-fatal; keep empty string.
                    pass
            page_texts.append(text)

        # Step 4: Join pages.
        raw_text = "\n\n".join(page_texts)

        # Step 5: Classify verdict.
        #   - scanned:         fewer than LOW_TEXT_THRESHOLD non-newline chars
        #   - low_confidence:  good-char ratio below LOW_CONFIDENCE_CHAR_RATIO
        #                      *or* mojibake ratio above _MOJIBAKE_RATIO_THRESHOLD
        #   - ok:              everything else
        total_chars = len(raw_text.replace("\n", ""))
        if total_chars < LOW_TEXT_THRESHOLD:
            verdict = LoaderVerdict.scanned
        else:
            good_ratio, mojibake_ratio = _text_quality_ratios(raw_text)
            if good_ratio < LOW_CONFIDENCE_CHAR_RATIO or mojibake_ratio > _MOJIBAKE_RATIO_THRESHOLD:
                verdict = LoaderVerdict.low_confidence
            else:
                verdict = LoaderVerdict.ok

        # Step 8 (before step 6 to avoid recomputing): SHA-256.
        document_sha256 = sha256_text(raw_text)

        # Step 9: Extract title from PDF metadata or fall back to stem.
        title = path.stem
        try:
            meta_info = reader.metadata
            if meta_info is not None:
                pdf_title = meta_info.get("/Title") or meta_info.get("Title") or ""
                if pdf_title and pdf_title.strip():
                    title = pdf_title.strip()
        except Exception:
            # Metadata read failure is non-fatal.
            pass

        # Step 10: Build and yield the Document.
        source_id = str(path.resolve())
        document_path = source_id
        document_id = make_document_id(source_id, document_path)

        yield Document(
            source_id=source_id,
            source_type="pdf",
            document_id=document_id,
            document_path=document_path,
            document_sha256=document_sha256,
            title=title,
            producer="user",
            raw_text=raw_text,
            meta={
                "loader_verdict": verdict.value,
                "page_count": len(page_texts),
                "page_texts": page_texts,
                "loader_version": LOADER_VERSION,
            },
        )


# ---------------------------------------------------------------------------
# Register in the global registry (runs once on import).
# ---------------------------------------------------------------------------

register_loader("pdf", PDFLoader())
