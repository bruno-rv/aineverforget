"""aineverforget.chunking — Document → Chunk splitting strategies.

Strategies (per PLAN.md § Phase A, item 3):
- ``markdown``:   block/heading-aware.  Never splits a fenced code block or
                  table.  Attaches ``heading_path`` to every Chunk.
- ``prose``:      word-window (~220 words / 40 overlap).  For plain text
                  sources without markdown structure.
- ``pdf``:        page-aware → word-window.  Per-page text split with
                  word-window within pages; ``pdf_page`` carried on each Chunk.

Strategy dispatch is keyed on ``Document.source_type`` (``"markdown"``/``"pdf"``/
other → ``"prose"``).  ``Document.meta["page_texts"]`` carries per-page text
for PDF.

No heavy imports at module level.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aineverforget.config import Settings
from aineverforget.models import Chunk, Document, IngestState

CHUNKER_VERSION = "chunker:1.0"
"""Version string embedded in every Chunk produced by this module.

Bump when the chunking logic changes in a way that would produce different
Chunk boundaries for the same Document (triggers re-index requirement).
"""

# Word-window constants mirroring neverforget's chunk_transcript defaults.
# min_tail_words: if a trailing window is shorter than this, merge it into the
# previous window rather than emitting a tiny orphan chunk.
# min_chunk_words: only emit a chunk if it has at least this many words.
# These are not in Settings (config is frozen) so live here as module constants.
_MIN_TAIL_WORDS = 80
_MIN_CHUNK_WORDS = 25


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_document(
    document: Document,
    settings: Settings,
    *,
    ingest_generation: int,
    embedding_model: str,
    producer: str = "",
) -> list[Chunk]:
    """Split *document* into Chunks using the appropriate strategy.

    Strategy dispatch (keyed on ``document.source_type``):
    - ``"markdown"``:  heading-aware block splitting via ``_chunk_markdown()``.
    - ``"pdf"``:       page-aware → word-window via ``_chunk_pdf()``.
    - anything else:   prose word-window via ``_chunk_prose()``.

    Every returned Chunk has:
    - ``ingest_state = IngestState.pending`` (visibility gate; set before upsert)
    - ``ingest_generation`` = caller-supplied value (allocated by
      ``identity.next_ingest_generation`` before this call)
    - ``chunk_index`` = 0-based index within the document
    - ``chunk_start_word`` / ``chunk_end_word`` = word offsets in ``document.raw_text``
    - ``chunker_version = CHUNKER_VERSION``
    - ``embedding_model`` = caller-supplied model checkpoint name
    - ``ingested_at`` = ``datetime.now(timezone.utc)`` at call time

    Parameters
    ----------
    document:
        Normalized Document to split.
    settings:
        Runtime settings supplying ``chunk_word_window`` and
        ``chunk_word_overlap``.
    ingest_generation:
        Monotonic generation integer for the new pending batch.  Must be
        ``identity.next_ingest_generation(store.max_active_generation(doc_id))``.
    embedding_model:
        Model checkpoint name to store in each Chunk's payload (e.g.
        ``"BAAI/bge-m3"``).
    producer:
        Producer name; defaults to ``document.producer`` if empty.

    Returns
    -------
    list[Chunk]
        Ordered list of Chunks (``chunk_index`` 0, 1, 2, …).  Never empty for
        a non-empty Document; returns ``[]`` for a Document with empty
        ``raw_text`` (e.g. an encrypted PDF with a ``scanned`` verdict).
    """
    if not document.raw_text.strip():
        return []

    resolved_producer = producer or document.producer

    if document.source_type == "markdown":
        return _chunk_markdown(
            document,
            settings,
            ingest_generation=ingest_generation,
            embedding_model=embedding_model,
            producer=resolved_producer,
        )
    elif document.source_type == "pdf":
        return _chunk_pdf(
            document,
            settings,
            ingest_generation=ingest_generation,
            embedding_model=embedding_model,
            producer=resolved_producer,
        )
    else:
        return _chunk_prose(
            document,
            settings,
            ingest_generation=ingest_generation,
            embedding_model=embedding_model,
            producer=resolved_producer,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _make_chunk(
    document: Document,
    *,
    ingest_generation: int,
    embedding_model: str,
    chunk_index: int,
    chunk_start_word: int,
    chunk_end_word: int,
    text: str,
    heading_path: str | None = None,
    pdf_page: int | None = None,
    loader_version: str = "",
    producer: str = "",
) -> Chunk:
    """Build a Chunk from common fields, filling in defaults from *document*."""
    return Chunk(
        source_id=document.source_id,
        source_type=document.source_type,
        document_id=document.document_id,
        document_path=document.document_path,
        document_sha256=document.document_sha256,
        ingest_generation=ingest_generation,
        ingest_state=IngestState.pending,
        title=document.title,
        chunk_index=chunk_index,
        chunk_start_word=chunk_start_word,
        chunk_end_word=chunk_end_word,
        heading_path=heading_path,
        pdf_page=pdf_page,
        tags=[],
        producer=producer or document.producer,
        ingested_at=datetime.now(timezone.utc),
        loader_version=loader_version or document.meta.get("loader_version", ""),
        chunker_version=CHUNKER_VERSION,
        embedding_model=embedding_model,
        text=text,
    )


def _extract_token_text(token: dict[str, Any]) -> str:
    """Recursively extract plain text from a mistune v3 AST token."""
    if token.get("type") in ("text", "code_span", "raw_html", "linebreak"):
        return token.get("raw", "")
    children = token.get("children") or []
    return "".join(_extract_token_text(c) for c in children)


def _token_to_source(token: dict[str, Any]) -> str:
    """Convert a mistune v3 AST token back to a representative text string.

    For code blocks: the raw code content (fenced block).
    For tables: reconstruct a simple flat representation for word counting.
    For headings and paragraphs: the extracted plain text.
    For blank_line: empty string.
    """
    t = token.get("type", "")
    if t == "block_code":
        # Include the code content for word counting
        raw = token.get("raw", "")
        info = token.get("attrs", {}).get("info") or ""
        if info:
            return f"```{info}\n{raw}```"
        return f"```\n{raw}```"
    if t == "table":
        # Flatten table text for word counting
        return _extract_token_text(token)
    if t == "blank_line":
        return ""
    if t == "heading":
        level = token.get("attrs", {}).get("level", 1)
        text = _extract_token_text(token)
        return "#" * level + " " + text
    # paragraph, list, blockquote, etc. — extract plain text
    return _extract_token_text(token)


def _word_count(text: str) -> int:
    """Count whitespace-separated words in *text*."""
    return len(text.split())


def _compute_word_ranges(
    n_words: int,
    chunk_words: int,
    overlap_words: int,
    min_tail_words: int = _MIN_TAIL_WORDS,
) -> list[tuple[int, int]]:
    """Compute (start, end) word-index ranges for a word-window slide.

    Ranges are 0-based, half-open: ``[start, end)``.
    The last range may be merged into the previous one if it is shorter
    than *min_tail_words* (avoids tiny orphan chunks).

    Parameters
    ----------
    n_words:       Total words to slice.
    chunk_words:   Target window size (words per chunk).
    overlap_words: Overlap between consecutive windows.
    min_tail_words: Minimum words in the trailing window before merging.

    Returns
    -------
    list of (start, end) tuples.  Empty list if ``n_words == 0``.
    """
    if n_words == 0:
        return []

    stride = chunk_words - overlap_words
    if stride <= 0:
        stride = 1

    ranges: list[tuple[int, int]] = []
    start = 0
    while start < n_words:
        end = min(start + chunk_words, n_words)
        ranges.append((start, end))
        if end == n_words:
            break
        start += stride

    # Merge short tail into previous window
    if len(ranges) > 1:
        tail_start, tail_end = ranges[-1]
        if tail_end - tail_start < min_tail_words:
            prev_start, _ = ranges[-2]
            ranges[-2] = (prev_start, tail_end)
            ranges.pop()

    return ranges


def _chunk_prose(
    document: Document,
    settings: Settings,
    *,
    ingest_generation: int,
    embedding_model: str,
    word_offset: int = 0,
    pdf_page: int | None = None,
    heading_path: str | None = None,
    chunk_index_start: int = 0,
    producer: str = "",
) -> list[Chunk]:
    """Word-window chunking for prose / plain-text Documents.

    Parameters
    ----------
    word_offset:
        Starting word index in the *document*'s full raw_text (used by
        ``_chunk_pdf()`` to maintain correct ``chunk_start_word`` offsets
        when called per-page).
    pdf_page:
        Page index to assign to all Chunks produced by this call (None for
        non-PDF sources).
    heading_path:
        Heading ancestry to assign to all Chunks (None for non-markdown).
    chunk_index_start:
        Starting ``chunk_index`` value (used when this is one of many
        per-page calls so indices are globally monotonic).

    Returns
    -------
    list[Chunk]
        Word-window Chunks with ``chunk_start_word``/``chunk_end_word`` set
        relative to the full Document word list.

    Notes
    -----
    Offsets are 0-based, half-open: ``chunk_start_word`` is the index of
    the first word; ``chunk_end_word`` is the index one past the last word
    (exclusive).  This matches the models.py docstring contract.

    The neverforget reference uses 1-based ``start_word + 1`` — that pattern
    is NOT copied here.

    Word counts below ``_MIN_CHUNK_WORDS`` within a window are merged or
    dropped during range computation via ``min_tail_words`` logic; a complete
    document below that threshold still emits one chunk (guarded in
    ``chunk_document`` by checking for non-empty raw_text).
    """
    # Determine the text to window — either the raw_text (prose, full doc)
    # or a section/page slice passed via model_copy from callers.
    text = document.raw_text
    words = text.split()

    if not words:
        return []

    chunk_words = settings.chunk_word_window
    overlap_words = settings.chunk_word_overlap
    ranges = _compute_word_ranges(len(words), chunk_words, overlap_words)

    if not ranges:
        return []

    chunks: list[Chunk] = []
    for local_i, (start, end) in enumerate(ranges):
        chunk_text = " ".join(words[start:end])
        chunks.append(
            _make_chunk(
                document,
                ingest_generation=ingest_generation,
                embedding_model=embedding_model,
                chunk_index=chunk_index_start + local_i,
                # Offsets are relative to the full Document word list
                chunk_start_word=word_offset + start,
                chunk_end_word=word_offset + end,
                text=chunk_text,
                heading_path=heading_path,
                pdf_page=pdf_page,
                producer=producer,
            )
        )

    return chunks


def _chunk_pdf(
    document: Document,
    settings: Settings,
    *,
    ingest_generation: int,
    embedding_model: str,
    producer: str = "",
) -> list[Chunk]:
    """Page-aware word-window chunking for PDF Documents.

    Reads ``document.meta["page_texts"]`` (list[str]) from the PDFLoader.
    Applies the prose word-window strategy within each page, setting
    ``pdf_page`` on each Chunk.  Falls back to prose-on-raw_text if
    ``page_texts`` is missing or empty (e.g. unit tests that supply a
    minimal Document without a real PDF Loader).

    Parameters mirror ``chunk_document()``.
    """
    page_texts: list[str] = document.meta.get("page_texts") or []

    if not page_texts:
        # Fallback: treat raw_text as a single page
        return _chunk_prose(
            document,
            settings,
            ingest_generation=ingest_generation,
            embedding_model=embedding_model,
            pdf_page=0,
            producer=producer,
        )

    # Build full-document word list so chunk_start_word / chunk_end_word are
    # monotonically increasing across all pages (relative to the concatenated
    # raw_text that the loader joins with "\n\n").
    # raw_text = "\n\n".join(page_texts) — compute word offsets per page.
    chunks: list[Chunk] = []
    running_word_offset = 0
    chunk_index_start = 0

    for page_idx, page_text in enumerate(page_texts):
        page_doc = document.model_copy(update={"raw_text": page_text})
        page_chunks = _chunk_prose(
            page_doc,
            settings,
            ingest_generation=ingest_generation,
            embedding_model=embedding_model,
            word_offset=running_word_offset,
            pdf_page=page_idx,
            chunk_index_start=chunk_index_start,
            producer=producer,
        )
        chunks.extend(page_chunks)

        # Advance the word offset by the number of words on this page plus
        # the "\n\n" separator (counted as 0 words — split() ignores them).
        page_words = len(page_text.split())
        running_word_offset += page_words
        chunk_index_start += len(page_chunks)

    return chunks


def _chunk_markdown(
    document: Document,
    settings: Settings,
    *,
    ingest_generation: int,
    embedding_model: str,
    producer: str = "",
) -> list[Chunk]:
    """Heading-aware block chunking for markdown/text Documents.

    Yields Chunks with ``heading_path`` set to the pipe-joined ATX heading
    ancestry above each block.  Fenced code blocks and tables are atomic;
    never split across a chunk boundary (even if the block exceeds the window).

    Implementation notes
    --------------------
    - Uses mistune v3 AST (``renderer=None``) with the ``table`` plugin.
    - Heading stack tracks ATX headings at their level; deeper headings are
      popped before pushing a same/shallower-level heading.
    - Word offsets are maintained via a running cursor over the full
      ``document.raw_text`` word list.  Mistune tokens lack source positions,
      so offsets are approximate (block text is counted, not character-mapped).
    - Code blocks and tables are ALWAYS emitted as a single atomic Chunk, even
      if the block exceeds ``settings.chunk_word_window``.
    - Prose accumulation: blocks are batched until adding the next would exceed
      the window, then flushed.

    Parameters mirror ``chunk_document()``.
    """
    import mistune  # lazy import — heavy dep

    md_parser = mistune.create_markdown(renderer=None, plugins=["table"])
    tokens: list[dict] = md_parser(document.raw_text) or []

    # Running state
    chunks: list[Chunk] = []
    chunk_index = 0
    # heading_stack: list of (level, text) — e.g. [(1, "Title"), (2, "Arch")]
    heading_stack: list[tuple[int, str]] = []
    # Accumulated prose blocks waiting to be flushed into a chunk
    prose_texts: list[str] = []
    prose_word_count = 0
    # Word cursor tracks approximate position in raw_text
    running_word_offset = 0
    prose_start_word = 0  # word offset where current prose accumulation began

    def current_heading_path() -> str | None:
        if not heading_stack:
            return None
        return " | ".join(
            "#" * level + " " + text for level, text in heading_stack
        )

    def flush_prose() -> None:
        nonlocal chunk_index, running_word_offset, prose_start_word, prose_texts, prose_word_count
        if not prose_texts:
            return
        combined = " ".join(prose_texts)
        words = combined.split()
        if not words:
            prose_texts = []
            prose_word_count = 0
            return
        # Word-window the accumulated prose section
        ranges = _compute_word_ranges(
            len(words),
            settings.chunk_word_window,
            settings.chunk_word_overlap,
        )
        hp = current_heading_path()
        for start, end in ranges:
            chunk_text = " ".join(words[start:end])
            chunks.append(
                _make_chunk(
                    document,
                    ingest_generation=ingest_generation,
                    embedding_model=embedding_model,
                    chunk_index=chunk_index,
                    chunk_start_word=prose_start_word + start,
                    chunk_end_word=prose_start_word + end,
                    text=chunk_text,
                    heading_path=hp,
                    producer=producer,
                )
            )
            chunk_index += 1
        running_word_offset = prose_start_word + len(words)
        prose_texts = []
        prose_word_count = 0
        prose_start_word = running_word_offset

    for token in tokens:
        t = token.get("type", "")

        if t == "blank_line":
            # Blank lines are not content; skip
            continue

        if t == "heading":
            # Flush prose accumulated before this heading
            flush_prose()
            level = token.get("attrs", {}).get("level", 1)
            text = _extract_token_text(token)
            # Pop headings at the same or deeper level
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            # Advance word offset to account for heading tokens (including the
            # "#"*level marker) so that subsequent prose chunks report accurate
            # chunk_start_word positions.  _token_to_source returns "# text"
            # form, so we count that to stay consistent with what callers see.
            heading_words = ("#" * level + " " + text).split()
            running_word_offset += len(heading_words)
            prose_start_word = running_word_offset
            # Headings themselves are not emitted as separate chunks;
            # they update the heading_path for subsequent prose/code/table.
            continue

        if t in ("block_code", "table"):
            # Atomic blocks — flush any pending prose first, then emit alone.
            flush_prose()
            block_text = _token_to_source(token)
            block_words = block_text.split()
            n = len(block_words)
            # Even if block exceeds chunk_word_window, emit as one chunk
            # (per PLAN.md: "Large code/table blocks: keep intact even if over the window")
            chunks.append(
                _make_chunk(
                    document,
                    ingest_generation=ingest_generation,
                    embedding_model=embedding_model,
                    chunk_index=chunk_index,
                    chunk_start_word=running_word_offset,
                    chunk_end_word=running_word_offset + n,
                    text=block_text,
                    heading_path=current_heading_path(),
                    producer=producer,
                )
            )
            chunk_index += 1
            running_word_offset += n
            prose_start_word = running_word_offset
            continue

        # Prose token (paragraph, list, blockquote, thematic_break, etc.)
        block_text = _extract_token_text(token)
        block_words = block_text.split()
        n = len(block_words)

        if n == 0:
            continue

        # If adding this block would exceed the window, flush first
        if prose_word_count > 0 and prose_word_count + n > settings.chunk_word_window:
            flush_prose()
            prose_start_word = running_word_offset

        prose_texts.append(block_text)
        prose_word_count += n
        running_word_offset += n

    # Flush any remaining prose
    flush_prose()

    # Edge case: document had content but produced no chunks (e.g. only
    # headings with no body text).  Emit the raw_text as one prose chunk.
    if not chunks and document.raw_text.strip():
        return _chunk_prose(
            document,
            settings,
            ingest_generation=ingest_generation,
            embedding_model=embedding_model,
            producer=producer,
        )

    return chunks
