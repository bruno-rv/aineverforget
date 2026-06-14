"""Tests for aineverforget.chunking — Document → Chunk splitting strategies.

Tests are self-contained and do NOT require qdrant, FlagEmbedding, or file I/O.
All Documents are built from constants; no real files or real PDFs are accessed.

Coverage areas
--------------
1. Markdown strategy:
   - Fenced code blocks are never split (atomic).
   - Tables are never split (atomic).
   - Multiple ATX sections → correct heading_path on each chunk.
   - chunk_index is globally contiguous 0..N-1.
2. Prose strategy:
   - Word-window counts and overlap are correct.
   - Short documents below min_chunk_words threshold: still emit one chunk.
3. PDF strategy:
   - pdf_page is set per page.
   - chunk_index is globally monotonic across pages.
4. Common invariants:
   - Empty raw_text → empty list.
   - ingest_state is always pending.
   - chunker_version is CHUNKER_VERSION.
   - chunk_end_word offsets are 0-based, half-open (exclusive).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aineverforget.chunking import CHUNKER_VERSION, chunk_document
from aineverforget.config import Settings
from aineverforget.models import Chunk, Document, IngestState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_SHA = "a" * 64
SAMPLE_INGESTED_AT = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


def _make_document(
    raw_text: str,
    source_type: str = "markdown",
    meta: dict | None = None,
) -> Document:
    return Document(
        source_id="/test/source",
        source_type=source_type,
        document_id="test-doc-001",
        document_path="/test/source/doc.md",
        document_sha256=SAMPLE_SHA,
        title="Test Document",
        producer="user",
        raw_text=raw_text,
        meta=meta or {"loader_version": "text:1.0"},
    )


def _default_settings(**overrides) -> Settings:
    kwargs = {
        "chunk_word_window": 220,
        "chunk_word_overlap": 40,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _chunk(doc: Document, **settings_overrides) -> list[Chunk]:
    return chunk_document(
        doc,
        _default_settings(**settings_overrides),
        ingest_generation=1,
        embedding_model="BAAI/bge-m3",
    )


# ---------------------------------------------------------------------------
# 1. Markdown strategy: fenced code block atomicity
# ---------------------------------------------------------------------------


class TestMarkdownCodeBlock:
    MD = """# Title

## Introduction

This is some introductory prose before the code block.

```python
def answer():
    # This is the implementation
    x = 1
    y = 2
    return x + y
```

After the code block we have more prose text here.
"""

    def test_code_block_is_single_chunk(self) -> None:
        """A fenced code block must appear in exactly one Chunk (never split)."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        # Find chunks containing the code content
        code_chunks = [c for c in chunks if "def answer" in c.text]
        assert len(code_chunks) == 1, (
            f"Code block appeared in {len(code_chunks)} chunks — must be exactly 1. "
            f"Chunks: {[c.text[:60] for c in chunks]}"
        )

    def test_code_block_not_mixed_with_prose(self) -> None:
        """The code block chunk must not contain surrounding prose."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        code_chunk = next(c for c in chunks if "def answer" in c.text)
        assert "introductory prose" not in code_chunk.text
        assert "more prose text" not in code_chunk.text

    def test_chunk_indices_contiguous(self) -> None:
        """chunk_index values must be 0, 1, 2, … without gaps."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# 2. Markdown strategy: table atomicity
# ---------------------------------------------------------------------------


class TestMarkdownTable:
    MD = """# Report

## Summary

Some prose before the table.

| Column A | Column B | Column C |
|----------|----------|----------|
| Alpha    | Beta     | Gamma    |
| Delta    | Epsilon  | Zeta     |
| Eta      | Theta    | Iota     |

Some prose after the table.
"""

    def test_table_is_single_chunk(self) -> None:
        """A markdown table must appear in exactly one Chunk (never split)."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        table_chunks = [c for c in chunks if "Alpha" in c.text and "Delta" in c.text]
        assert len(table_chunks) == 1, (
            f"Table appeared in {len(table_chunks)} chunks — must be exactly 1. "
            f"Chunks: {[c.text[:80] for c in chunks]}"
        )

    def test_table_chunk_contains_all_rows(self) -> None:
        """The table chunk must contain all row data."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        table_chunk = next(c for c in chunks if "Alpha" in c.text)
        assert "Eta" in table_chunk.text
        assert "Iota" in table_chunk.text

    def test_table_chunk_not_mixed_with_prose(self) -> None:
        """The table chunk must not contain surrounding prose."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        table_chunk = next(c for c in chunks if "Alpha" in c.text)
        assert "prose before" not in table_chunk.text
        assert "prose after" not in table_chunk.text


# ---------------------------------------------------------------------------
# 3. Markdown strategy: heading_path tracking across ## sections
# ---------------------------------------------------------------------------


class TestMarkdownHeadingPath:
    MD = """# Document Title

## Section One

Prose in section one.

### Subsection A

Prose under subsection A.

## Section Two

Prose in section two.

```python
code_in_section_two = True
```
"""

    def _get_chunks(self) -> list[Chunk]:
        doc = _make_document(self.MD)
        return _chunk(doc)

    def test_heading_path_none_or_top_for_section_one(self) -> None:
        """Prose in Section One must carry Section One in heading_path."""
        chunks = self._get_chunks()
        prose_one = [c for c in chunks if "Prose in section one" in c.text]
        assert len(prose_one) >= 1
        hp = prose_one[0].heading_path
        assert hp is not None
        assert "Section One" in hp

    def test_subsection_heading_path_includes_ancestors(self) -> None:
        """Prose under Subsection A must include both parent and sub heading."""
        chunks = self._get_chunks()
        prose_sub = [c for c in chunks if "Prose under subsection A" in c.text]
        assert len(prose_sub) >= 1
        hp = prose_sub[0].heading_path
        assert hp is not None
        assert "Section One" in hp
        assert "Subsection A" in hp

    def test_section_two_does_not_include_section_one(self) -> None:
        """Prose in Section Two must NOT include Section One in heading_path."""
        chunks = self._get_chunks()
        prose_two = [c for c in chunks if "Prose in section two" in c.text]
        assert len(prose_two) >= 1
        hp = prose_two[0].heading_path
        assert hp is not None
        assert "Section Two" in hp
        assert "Section One" not in hp

    def test_code_block_inherits_section_two_heading_path(self) -> None:
        """Code block in Section Two must have Section Two in heading_path."""
        chunks = self._get_chunks()
        code_chunk = next(c for c in chunks if "code_in_section_two" in c.text)
        hp = code_chunk.heading_path
        assert hp is not None
        assert "Section Two" in hp

    def test_chunk_index_contiguous(self) -> None:
        """chunk_index must be 0, 1, 2, … without gaps."""
        chunks = self._get_chunks()
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# 4. Markdown: combined code block + table + multiple sections
# ---------------------------------------------------------------------------


class TestMarkdownCombined:
    """Integration test: code block + table + multiple sections together."""

    MD = """# Combined Doc

## Intro

Intro prose paragraph.

## Data

Here is the data table:

| Name  | Value |
|-------|-------|
| foo   | 1     |
| bar   | 2     |

## Code

Here is the code:

```bash
echo "hello world"
```

## Conclusion

Final paragraph of text here.
"""

    def test_no_split_across_atomic_blocks(self) -> None:
        """Neither the table nor the code block should be split."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)

        # Table: all rows in one chunk
        table_chunks = [c for c in chunks if "foo" in c.text and "bar" in c.text]
        assert len(table_chunks) == 1, "Table split across chunks"

        # Code: echo in exactly one chunk
        code_chunks = [c for c in chunks if "echo" in c.text]
        assert len(code_chunks) == 1, "Code block split across chunks"

    def test_chunk_index_globally_contiguous(self) -> None:
        """chunk_index must be globally contiguous 0..N-1 across all strategies."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        assert len(chunks) >= 1
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_heading_path_on_every_content_chunk(self) -> None:
        """Every chunk with content (not just headings) must have a heading_path."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        for chunk in chunks:
            assert chunk.heading_path is not None, (
                f"chunk_index={chunk.chunk_index} has no heading_path: {chunk.text[:60]!r}"
            )

    def test_ingest_state_pending(self) -> None:
        """All chunks must have ingest_state=pending (not yet verified)."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        for chunk in chunks:
            assert chunk.ingest_state == IngestState.pending

    def test_chunker_version_set(self) -> None:
        """All chunks must carry the CHUNKER_VERSION constant."""
        doc = _make_document(self.MD)
        chunks = _chunk(doc)
        for chunk in chunks:
            assert chunk.chunker_version == CHUNKER_VERSION


# ---------------------------------------------------------------------------
# 5. Prose strategy: word-window correctness
# ---------------------------------------------------------------------------


class TestProseWordWindow:
    def _prose_doc(self, n_words: int) -> Document:
        raw = " ".join(f"word{i}" for i in range(n_words))
        return _make_document(raw, source_type="prose")

    def test_single_chunk_for_short_prose(self) -> None:
        """Prose shorter than one window emits exactly one Chunk."""
        doc = self._prose_doc(100)
        chunks = _chunk(doc, chunk_word_window=220, chunk_word_overlap=40)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0

    def test_two_chunks_for_long_prose(self) -> None:
        """Prose longer than one window emits multiple Chunks."""
        doc = self._prose_doc(400)
        chunks = _chunk(doc, chunk_word_window=220, chunk_word_overlap=40)
        assert len(chunks) >= 2

    def test_overlap_between_consecutive_chunks(self) -> None:
        """Consecutive chunks overlap by approximately chunk_word_overlap words."""
        n = 500
        window = 220
        overlap = 40
        doc = self._prose_doc(n)
        chunks = _chunk(doc, chunk_word_window=window, chunk_word_overlap=overlap)
        assert len(chunks) >= 2
        # chunk_end_word of chunk[i] minus chunk_start_word of chunk[i+1] == overlap
        for i in range(len(chunks) - 1):
            actual_overlap = chunks[i].chunk_end_word - chunks[i + 1].chunk_start_word
            assert actual_overlap == overlap, (
                f"Overlap between chunk {i} and {i+1}: expected {overlap}, got {actual_overlap}"
            )

    def test_offsets_are_zero_based_exclusive(self) -> None:
        """chunk_start_word is 0-based; chunk_end_word is exclusive (half-open)."""
        doc = self._prose_doc(50)
        chunks = _chunk(doc, chunk_word_window=220, chunk_word_overlap=40)
        assert len(chunks) == 1
        assert chunks[0].chunk_start_word == 0
        assert chunks[0].chunk_end_word == 50  # exclusive: [0, 50)

    def test_first_chunk_start_is_zero(self) -> None:
        """First chunk always starts at word 0."""
        doc = self._prose_doc(300)
        chunks = _chunk(doc)
        assert chunks[0].chunk_start_word == 0

    def test_chunk_text_matches_word_slice(self) -> None:
        """chunk text must match the word slice defined by start/end offsets."""
        n = 260
        doc = self._prose_doc(n)
        chunks = _chunk(doc, chunk_word_window=220, chunk_word_overlap=40)
        all_words = doc.raw_text.split()
        for chunk in chunks:
            expected = " ".join(all_words[chunk.chunk_start_word:chunk.chunk_end_word])
            assert chunk.text == expected, (
                f"chunk {chunk.chunk_index} text mismatch"
            )

    def test_chunk_indices_contiguous(self) -> None:
        """chunk_index must be 0, 1, 2, … without gaps for prose."""
        doc = self._prose_doc(600)
        chunks = _chunk(doc)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# 6. PDF strategy: pdf_page assignment + global monotonic chunk_index
# ---------------------------------------------------------------------------


class TestPdfChunking:
    def _pdf_doc(self, page_texts: list[str]) -> Document:
        raw_text = "\n\n".join(page_texts)
        return _make_document(
            raw_text,
            source_type="pdf",
            meta={
                "loader_version": "pdf:1.0",
                "loader_verdict": "ok",
                "page_count": len(page_texts),
                "page_texts": page_texts,
            },
        )

    def test_pdf_page_set_per_page(self) -> None:
        """Each chunk must carry the correct pdf_page index."""
        pages = [
            " ".join(f"pageone_word{i}" for i in range(50)),
            " ".join(f"pagetwo_word{i}" for i in range(50)),
            " ".join(f"pagethree_word{i}" for i in range(50)),
        ]
        doc = self._pdf_doc(pages)
        chunks = _chunk(doc)

        page0_chunks = [c for c in chunks if "pageone_word" in c.text]
        page1_chunks = [c for c in chunks if "pagetwo_word" in c.text]
        page2_chunks = [c for c in chunks if "pagethree_word" in c.text]

        assert all(c.pdf_page == 0 for c in page0_chunks), "Page 0 chunks should have pdf_page=0"
        assert all(c.pdf_page == 1 for c in page1_chunks), "Page 1 chunks should have pdf_page=1"
        assert all(c.pdf_page == 2 for c in page2_chunks), "Page 2 chunks should have pdf_page=2"

    def test_chunk_index_globally_monotonic(self) -> None:
        """chunk_index must be globally contiguous 0..N-1 across all pages."""
        pages = [
            " ".join(f"p0w{i}" for i in range(100)),
            " ".join(f"p1w{i}" for i in range(100)),
        ]
        doc = self._pdf_doc(pages)
        chunks = _chunk(doc)
        assert len(chunks) >= 1
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_heading_path_none_for_pdf(self) -> None:
        """PDF chunks must have heading_path=None (no markdown structure)."""
        pages = [" ".join(f"word{i}" for i in range(30))]
        doc = self._pdf_doc(pages)
        chunks = _chunk(doc)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.heading_path is None

    def test_pdf_source_type_preserved(self) -> None:
        """All PDF chunks must carry source_type='pdf'."""
        pages = [" ".join(f"word{i}" for i in range(30))]
        doc = self._pdf_doc(pages)
        chunks = _chunk(doc)
        for chunk in chunks:
            assert chunk.source_type == "pdf"

    def test_multi_page_pdf_large_pages(self) -> None:
        """PDF with pages large enough to produce multiple chunks each."""
        page0 = " ".join(f"p0w{i}" for i in range(300))
        page1 = " ".join(f"p1w{i}" for i in range(300))
        doc = self._pdf_doc([page0, page1])
        chunks = _chunk(doc, chunk_word_window=220, chunk_word_overlap=40)

        # Each 300-word page should produce multiple chunks
        page0_chunks = [c for c in chunks if "p0w" in c.text]
        page1_chunks = [c for c in chunks if "p1w" in c.text]
        assert len(page0_chunks) >= 2
        assert len(page1_chunks) >= 2

        # pdf_page must be correct
        assert all(c.pdf_page == 0 for c in page0_chunks)
        assert all(c.pdf_page == 1 for c in page1_chunks)

        # Global contiguous index
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# 7. Common invariants
# ---------------------------------------------------------------------------


class TestCommonInvariants:
    def test_empty_raw_text_returns_empty_list(self) -> None:
        """Empty raw_text (e.g. scanned PDF) returns []."""
        doc = _make_document("", source_type="markdown")
        chunks = _chunk(doc)
        assert chunks == []

    def test_empty_whitespace_raw_text_returns_empty_list(self) -> None:
        """Whitespace-only raw_text also returns []."""
        doc = _make_document("   \n\n  ", source_type="prose")
        chunks = _chunk(doc)
        assert chunks == []

    def test_ingest_state_always_pending(self) -> None:
        """All chunks from chunk_document must be pending (not active/failed)."""
        doc = _make_document("Hello world this is a test document.", source_type="prose")
        chunks = _chunk(doc)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.ingest_state == IngestState.pending

    def test_chunker_version_always_set(self) -> None:
        """All chunks must carry CHUNKER_VERSION."""
        doc = _make_document("Test text for chunker version check.", source_type="prose")
        chunks = _chunk(doc)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.chunker_version == CHUNKER_VERSION

    def test_embedding_model_propagated(self) -> None:
        """embedding_model argument must appear in every chunk."""
        doc = _make_document("Test text.", source_type="prose")
        chunks = chunk_document(
            doc,
            _default_settings(),
            ingest_generation=1,
            embedding_model="BAAI/bge-m3-custom",
        )
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.embedding_model == "BAAI/bge-m3-custom"

    def test_ingest_generation_propagated(self) -> None:
        """ingest_generation argument must appear in every chunk."""
        doc = _make_document("Test text.", source_type="prose")
        chunks = chunk_document(
            doc,
            _default_settings(),
            ingest_generation=7,
            embedding_model="BAAI/bge-m3",
        )
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.ingest_generation == 7

    def test_document_fields_propagated(self) -> None:
        """Document identity fields must be copied to every Chunk."""
        doc = _make_document("Test text.", source_type="prose")
        chunks = _chunk(doc)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.source_id == doc.source_id
            assert chunk.document_id == doc.document_id
            assert chunk.document_sha256 == doc.document_sha256
            assert chunk.document_path == doc.document_path
            assert chunk.title == doc.title

    def test_ingested_at_is_utc_aware(self) -> None:
        """ingested_at must be a timezone-aware UTC datetime."""
        doc = _make_document("Test text.", source_type="prose")
        chunks = _chunk(doc)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.ingested_at.tzinfo is not None

    def test_prose_fallback_for_unknown_source_type(self) -> None:
        """Unknown source_type falls back to prose windowing."""
        doc = _make_document("Hello world from an unknown source type.", source_type="txt")
        chunks = _chunk(doc)
        assert len(chunks) >= 1
        assert chunks[0].chunk_index == 0
        assert chunks[0].heading_path is None
        assert chunks[0].pdf_page is None


# ---------------------------------------------------------------------------
# Fix #12: Heading word offset must advance running_word_offset
# ---------------------------------------------------------------------------


class TestHeadingWordOffset:
    """Heading tokens must advance running_word_offset so prose chunks report
    accurate chunk_start_word positions relative to the full document."""

    def test_prose_after_heading_has_nonzero_start_word(self) -> None:
        """Prose after a heading must not start at word 0 if heading words precede it."""
        # Heading has 3 words: "Architecture Notes Section"
        # Prose follows: "This is the content."
        md = "# Architecture Notes Section\n\nThis is the content."
        doc = _make_document(md, source_type="markdown")
        chunks = _chunk(doc)
        prose_chunks = [c for c in chunks if "content" in c.text]
        assert prose_chunks, "expected at least one prose chunk containing 'content'"
        # The prose chunk must start AFTER the heading's words
        assert prose_chunks[0].chunk_start_word > 0, (
            f"prose chunk_start_word={prose_chunks[0].chunk_start_word} should be >0 "
            f"(heading words must advance the offset)"
        )

    def test_two_sections_have_increasing_word_offsets(self) -> None:
        """Chunks in section 2 must have higher chunk_start_word than section 1."""
        md = (
            "# Section One\n\n"
            "First section content here.\n\n"
            "# Section Two\n\n"
            "Second section content here."
        )
        doc = _make_document(md, source_type="markdown")
        chunks = _chunk(doc)
        # Collect section start words
        s1_chunks = [c for c in chunks if "First section" in c.text]
        s2_chunks = [c for c in chunks if "Second section" in c.text]
        assert s1_chunks and s2_chunks
        assert s2_chunks[0].chunk_start_word > s1_chunks[0].chunk_start_word, (
            "second section prose must start at higher word offset than first section prose"
        )

    def test_heading_marker_tokens_counted_in_offset(self) -> None:
        """Fix F: heading offset must include the '#' marker token(s).

        A level-1 heading '# Title' has TWO tokens: '#' and 'Title'.
        Before the fix, only 'Title' (1 word) was counted; after the fix,
        '# Title'.split() == 2 words are counted.

        We test by checking that a prose chunk after a heading has a
        chunk_start_word consistent with the heading's FULL token count
        (marker + text), not just its text word count.
        """
        # '## Two Words' → "## Two Words".split() = ['##', 'Two', 'Words'] = 3 tokens
        # Prose: 'Alpha beta gamma' (3 words)
        # After the heading, prose chunk_start_word must be >= 3 (not 2).
        md = "## Two Words\n\nAlpha beta gamma."
        doc = _make_document(md, source_type="markdown")
        chunks = _chunk(doc)
        prose = [c for c in chunks if "Alpha" in c.text]
        assert prose, "expected prose chunk after heading"
        # '## Two Words' splits to 3 tokens; prose must start at word index >= 3
        assert prose[0].chunk_start_word >= 3, (
            f"chunk_start_word={prose[0].chunk_start_word} but heading "
            f"'## Two Words' has 3 tokens (## + Two + Words); "
            "Fix F: marker tokens must be counted in offset math"
        )


# ---------------------------------------------------------------------------
# Fix G: producer propagation through chunk_document → strategy functions
# ---------------------------------------------------------------------------


class TestProducerPropagation:
    """chunk_document(producer=X) must propagate X to all emitted Chunks.

    Fix G: _chunk_markdown, _chunk_pdf, _chunk_prose previously ignored the
    producer arg (it was computed in chunk_document but not forwarded).
    After the fix, every Chunk.producer must equal the overridden value.
    """

    def _make_doc(self, text: str, source_type: str) -> Document:
        return _make_document(text, source_type=source_type)

    def test_prose_producer_propagated(self) -> None:
        doc = self._make_doc(
            "This is a prose document with enough words to produce chunks reliably.",
            source_type="prose",
        )
        chunks = chunk_document(
            doc,
            _default_settings(),
            ingest_generation=1,
            embedding_model="BAAI/bge-m3",
            producer="knowledge-indexer",
        )
        assert chunks, "prose doc must produce chunks"
        for c in chunks:
            assert c.producer == "knowledge-indexer", (
                f"chunk.producer={c.producer!r}; Fix G: producer override must propagate"
            )

    def test_markdown_producer_propagated(self) -> None:
        md = (
            "# Section Alpha\n\n"
            "This is markdown content about engineering systems and architecture. "
            "It spans enough words to exercise the markdown chunker path reliably."
        )
        doc = self._make_doc(md, source_type="markdown")
        chunks = chunk_document(
            doc,
            _default_settings(),
            ingest_generation=1,
            embedding_model="BAAI/bge-m3",
            producer="custom-agent",
        )
        assert chunks, "markdown doc must produce chunks"
        for c in chunks:
            assert c.producer == "custom-agent", (
                f"chunk.producer={c.producer!r}; Fix G: producer must be 'custom-agent'"
            )

    def test_pdf_producer_propagated(self) -> None:
        doc = _make_document(
            "PDF page content with enough words for chunking purposes.",
            source_type="pdf",
            meta={
                "loader_version": "pdf:1.0",
                "page_texts": [
                    "First page of PDF content with adequate text for chunking.",
                    "Second page content also requires adequate text length.",
                ],
            },
        )
        chunks = chunk_document(
            doc,
            _default_settings(),
            ingest_generation=1,
            embedding_model="BAAI/bge-m3",
            producer="pdf-extractor",
        )
        assert chunks, "pdf doc must produce chunks"
        for c in chunks:
            assert c.producer == "pdf-extractor", (
                f"chunk.producer={c.producer!r}; Fix G: pdf producer must propagate"
            )

    def test_document_producer_used_when_no_override(self) -> None:
        """When producer='' in chunk_document, Document.producer is used as fallback."""
        doc = _make_document("Fallback producer test content.", source_type="prose")
        # doc.producer is "user" from _make_document
        chunks = chunk_document(
            doc,
            _default_settings(),
            ingest_generation=1,
            embedding_model="BAAI/bge-m3",
            # no producer kwarg → resolved_producer = document.producer = "user"
        )
        assert chunks
        for c in chunks:
            assert c.producer == "user", (
                f"chunk.producer={c.producer!r}; should fall back to document.producer='user'"
            )
