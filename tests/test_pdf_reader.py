"""
Comprehensive test suite for pdf_reader.py.

Covers: parse_pages, _reflow_plain, styled_page_text, extract_page_text,
        page_image_coverage, is_image_heavy, get_toc, current_chapter,
        search_pdf, load_page, terminal_supports_images, file handling.
"""
import io
import os
import sys
import json
import pytest
import fitz
from pathlib import Path
from unittest.mock import patch
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent))
import pdf_reader as pr

# ── PDF fixture helpers ───────────────────────────────────────────────────────

def make_text_pdf(tmp_path, text="Hello World\nThis is a test.", pages=1):
    doc = fitz.open()
    for p in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 100), f"Page {p + 1}\n{text}", fontsize=12)
    path = str(tmp_path / "text.pdf")
    doc.save(path)
    doc.close()
    return path


def make_empty_pdf(tmp_path, pages=1):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=595, height=842)
    path = str(tmp_path / "empty.pdf")
    doc.save(path)
    doc.close()
    return path


def make_image_pdf(tmp_path):
    """PDF whose first page is mostly a raster image."""
    from PIL import Image
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    img = Image.new("RGB", (550, 800), color=(180, 180, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    page.insert_image(fitz.Rect(10, 10, 585, 830), stream=buf.getvalue())
    path = str(tmp_path / "image.pdf")
    doc.save(path)
    doc.close()
    return path


def make_toc_pdf(tmp_path, chapters=3):
    doc = fitz.open()
    for i in range(chapters):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 100), f"Chapter {i + 1}\nContent here.", fontsize=12)
    toc = [[1, f"Chapter {i + 1}", i + 1] for i in range(chapters)]
    doc.set_toc(toc)
    path = str(tmp_path / "toc.pdf")
    doc.save(path)
    doc.close()
    return path


def make_styled_pdf(tmp_path):
    """PDF with heading (large) and body text."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 80),  "Big Heading",   fontsize=24)
    page.insert_text((50, 130), "Normal body text goes here.", fontsize=12)
    path = str(tmp_path / "styled.pdf")
    doc.save(path)
    doc.close()
    return path

# ── parse_pages ───────────────────────────────────────────────────────────────

class TestParsePages:
    def test_none_returns_all(self):
        assert pr.parse_pages(None, 5) == [0, 1, 2, 3, 4]

    def test_single_page(self):
        assert pr.parse_pages("2", 5) == [1]

    def test_first_page(self):
        assert pr.parse_pages("1", 10) == [0]

    def test_last_page(self):
        assert pr.parse_pages("10", 10) == [9]

    def test_range(self):
        assert pr.parse_pages("2-4", 5) == [1, 2, 3]

    def test_full_range(self):
        assert pr.parse_pages("1-5", 5) == [0, 1, 2, 3, 4]

    def test_comma_list(self):
        assert pr.parse_pages("1,3,5", 5) == [0, 2, 4]

    def test_mixed_range_and_single(self):
        assert pr.parse_pages("1,3-5", 5) == [0, 2, 3, 4]

    def test_deduplication(self):
        assert pr.parse_pages("1-3,2-4", 5) == [0, 1, 2, 3]

    def test_sorted_output(self):
        assert pr.parse_pages("5,1,3", 5) == [0, 2, 4]

    def test_out_of_range_high(self):
        assert pr.parse_pages("10", 5) is None

    def test_out_of_range_low(self):
        assert pr.parse_pages("0", 5) is None

    def test_inverted_range(self):
        assert pr.parse_pages("5-2", 5) is None

    def test_range_exceeds_total(self):
        assert pr.parse_pages("3-10", 5) is None

    def test_invalid_string(self):
        assert pr.parse_pages("abc", 5) is None

    def test_invalid_in_list(self):
        assert pr.parse_pages("1,abc,3", 5) is None

    def test_single_page_doc(self):
        assert pr.parse_pages("1", 1) == [0]

    def test_whitespace_tolerant(self):
        assert pr.parse_pages("1, 3, 5", 5) == [0, 2, 4]

# ── _reflow_plain ─────────────────────────────────────────────────────────────

class TestReflowPlain:
    def test_hyphen_join(self):
        assert "hyphenated" in pr._reflow_plain("hyphen-\nated")

    def test_soft_newline_becomes_space(self):
        result = pr._reflow_plain("hello\nworld")
        assert result == "hello world"

    def test_double_newline_preserved(self):
        result = pr._reflow_plain("para one\n\npara two")
        assert "para one" in result
        assert "para two" in result
        assert "\n\n" in result

    def test_multiple_spaces_collapsed(self):
        result = pr._reflow_plain("too   many   spaces")
        assert "  " not in result

    def test_empty_string(self):
        assert pr._reflow_plain("") == ""

    def test_no_change_needed(self):
        assert pr._reflow_plain("simple text") == "simple text"

    def test_stripped(self):
        assert pr._reflow_plain("  text  ") == "text"

    def test_hyphen_only_before_non_space(self):
        # "word- \nnext" should NOT join (space before newline)
        result = pr._reflow_plain("word-\nnext")
        assert "wordnext" in result

    def test_preserves_multiple_paragraphs(self):
        result = pr._reflow_plain("a\n\nb\n\nc")
        parts = [p.strip() for p in result.split("\n\n")]
        assert "a" in parts
        assert "b" in parts
        assert "c" in parts

# ── styled_page_text ──────────────────────────────────────────────────────────

class TestStyledPageText:
    def test_returns_rich_text(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        result = pr.styled_page_text(doc[0])
        assert isinstance(result, Text)
        doc.close()

    def test_extracts_content(self, tmp_path):
        path = make_text_pdf(tmp_path, text="Hello styled world")
        doc = fitz.open(path)
        result = pr.styled_page_text(doc[0])
        assert "Hello styled world" in result.plain
        doc.close()

    def test_empty_page_returns_empty_text(self, tmp_path):
        path = make_empty_pdf(tmp_path)
        doc = fitz.open(path)
        result = pr.styled_page_text(doc[0])
        assert result.plain.strip() == ""
        doc.close()

    def test_heading_has_different_style(self, tmp_path):
        path = make_styled_pdf(tmp_path)
        doc = fitz.open(path)
        result = pr.styled_page_text(doc[0])
        # Heading text should be present and the object should have spans
        assert "Big Heading" in result.plain
        assert len(result._spans) > 0
        doc.close()

    def test_heading_style_is_bold_colour(self, tmp_path):
        path = make_styled_pdf(tmp_path)
        doc = fitz.open(path)
        result = pr.styled_page_text(doc[0])
        # Find the span covering "Big Heading"
        heading_start = result.plain.index("Big Heading")
        heading_styles = [
            s.style for s in result._spans
            if s.start <= heading_start < s.end
        ]
        assert any("bold" in str(style).lower() for style in heading_styles)
        doc.close()

# ── extract_page_text (plain) ─────────────────────────────────────────────────

class TestExtractPageText:
    def test_extracts_text(self, tmp_path):
        path = make_text_pdf(tmp_path, text="Extraction test content")
        doc = fitz.open(path)
        text = pr.extract_page_text(doc[0], ocr=False, ocr_threshold=50)
        assert "Extraction test content" in text
        doc.close()

    def test_empty_page_no_ocr(self, tmp_path):
        path = make_empty_pdf(tmp_path)
        doc = fitz.open(path)
        text = pr.extract_page_text(doc[0], ocr=False, ocr_threshold=50)
        assert text == ""
        doc.close()

    def test_no_ocr_flag_skips_ocr(self, tmp_path):
        path = make_empty_pdf(tmp_path)
        doc = fitz.open(path)
        with patch.object(pr, "ocr_page") as mock_ocr:
            pr.extract_page_text(doc[0], ocr=False, ocr_threshold=50)
            mock_ocr.assert_not_called()
        doc.close()

    def test_ocr_called_when_text_sparse(self, tmp_path):
        path = make_empty_pdf(tmp_path)
        doc = fitz.open(path)
        with patch.object(pr, "ocr_page", return_value="OCR result") as mock_ocr:
            result = pr.extract_page_text(doc[0], ocr=True, ocr_threshold=50)
            mock_ocr.assert_called_once()
        assert "OCR result" in result
        doc.close()

    def test_ocr_not_called_when_text_sufficient(self, tmp_path):
        long_text = "A" * 200
        path = make_text_pdf(tmp_path, text=long_text)
        doc = fitz.open(path)
        with patch.object(pr, "ocr_page") as mock_ocr:
            pr.extract_page_text(doc[0], ocr=True, ocr_threshold=50)
            mock_ocr.assert_not_called()
        doc.close()

# ── page_image_coverage ───────────────────────────────────────────────────────

class TestPageImageCoverage:
    def test_text_page_low_coverage(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        assert pr.page_image_coverage(doc[0]) < 0.3
        doc.close()

    def test_image_page_high_coverage(self, tmp_path):
        path = make_image_pdf(tmp_path)
        doc = fitz.open(path)
        assert pr.page_image_coverage(doc[0]) > 0.5
        doc.close()

    def test_coverage_bounded_0_to_1(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        c = pr.page_image_coverage(doc[0])
        assert 0.0 <= c <= 1.0
        doc.close()

    def test_empty_page_zero_coverage(self, tmp_path):
        path = make_empty_pdf(tmp_path)
        doc = fitz.open(path)
        assert pr.page_image_coverage(doc[0]) == 0.0
        doc.close()

# ── is_image_heavy ────────────────────────────────────────────────────────────

class TestIsImageHeavy:
    def test_text_page_not_image_heavy(self, tmp_path):
        path = make_text_pdf(tmp_path, text="A" * 200)
        doc = fitz.open(path)
        page = doc[0]
        text = page.get_text("text").strip()
        assert not pr.is_image_heavy(page, text, ocr_threshold=50)
        doc.close()

    def test_image_page_is_image_heavy(self, tmp_path):
        path = make_image_pdf(tmp_path)
        doc = fitz.open(path)
        page = doc[0]
        text = page.get_text("text").strip()   # will be ""
        assert pr.is_image_heavy(page, text, ocr_threshold=50)
        doc.close()

    def test_sparse_text_but_low_image_coverage_not_heavy(self, tmp_path):
        # Empty page: no images, no text → coverage = 0 → NOT image heavy
        path = make_empty_pdf(tmp_path)
        doc = fitz.open(path)
        page = doc[0]
        assert not pr.is_image_heavy(page, "", ocr_threshold=50)
        doc.close()

    def test_full_page_image_heavy_despite_text(self, tmp_path):
        # Full-page image (>70% coverage) → image heavy even with lots of text
        # (photography book pages have captions alongside full-page photos)
        path = make_image_pdf(tmp_path)
        doc = fitz.open(path)
        page = doc[0]
        assert pr.is_image_heavy(page, "A" * 200, ocr_threshold=50)
        doc.close()

# ── get_toc / current_chapter ─────────────────────────────────────────────────

class TestTOC:
    def test_get_toc_returns_entries(self, tmp_path):
        path = make_toc_pdf(tmp_path, chapters=3)
        doc = fitz.open(path)
        toc = pr.get_toc(doc)
        assert len(toc) == 3
        assert toc[0] == (1, "Chapter 1", 1)
        doc.close()

    def test_get_toc_empty_when_none(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        assert pr.get_toc(doc) == []
        doc.close()

    def test_current_chapter_returns_most_recent(self, tmp_path):
        path = make_toc_pdf(tmp_path, chapters=3)
        doc = fitz.open(path)
        toc = pr.get_toc(doc)
        assert pr.current_chapter(toc, 2) == "Chapter 2"
        assert pr.current_chapter(toc, 3) == "Chapter 3"
        doc.close()

    def test_current_chapter_before_first_entry(self, tmp_path):
        path = make_toc_pdf(tmp_path, chapters=3)
        doc = fitz.open(path)
        toc = pr.get_toc(doc)
        # Page 0 is before any chapter entry (chapters start at 1-based page 1)
        result = pr.current_chapter(toc, 0)
        assert result is None
        doc.close()

    def test_current_chapter_empty_toc(self):
        assert pr.current_chapter([], 5) is None

    def test_current_chapter_only_level1(self, tmp_path):
        """current_chapter should only track level-1 entries."""
        path = make_toc_pdf(tmp_path, chapters=2)
        doc = fitz.open(path)
        doc.close()
        toc = [(1, "Ch 1", 1), (2, "Section 1.1", 1), (1, "Ch 2", 5)]
        assert pr.current_chapter(toc, 3) == "Ch 1"
        assert pr.current_chapter(toc, 6) == "Ch 2"

# ── search_pdf ────────────────────────────────────────────────────────────────

class TestSearchPDF:
    def test_finds_text_on_correct_page(self, tmp_path):
        path = make_text_pdf(tmp_path, text="Unique phrase XYZ", pages=3)
        doc = fitz.open(path)
        results = pr.search_pdf(doc, "Unique phrase XYZ", list(range(3)))
        assert len(results) > 0
        doc.close()

    def test_no_results_for_absent_text(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        assert pr.search_pdf(doc, "NOTPRESENTATALL", [0]) == []
        doc.close()

    def test_respects_page_subset(self, tmp_path):
        path = make_text_pdf(tmp_path, text="Find me", pages=5)
        doc = fitz.open(path)
        # Search only pages 0–1; text is on all pages but we restrict
        results = pr.search_pdf(doc, "Find me", [0, 1])
        assert all(r in [0, 1] for r in results)
        doc.close()

    def test_returns_sorted_indices(self, tmp_path):
        path = make_text_pdf(tmp_path, text="common", pages=4)
        doc = fitz.open(path)
        results = pr.search_pdf(doc, "common", list(range(4)))
        assert results == sorted(results)
        doc.close()

    def test_empty_page_subset_returns_empty(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        assert pr.search_pdf(doc, "Hello", []) == []
        doc.close()

# ── load_page ─────────────────────────────────────────────────────────────────

class TestLoadPage:
    def test_text_page_returns_rich(self, tmp_path):
        path = make_text_pdf(tmp_path, text="A" * 200)
        doc = fitz.open(path)
        ctype, content = pr.load_page(doc, 0, ocr=False, ocr_threshold=50, can_show_images=False)
        assert ctype == "rich"
        assert isinstance(content, Text)
        doc.close()

    def test_empty_page_returns_empty(self, tmp_path):
        path = make_empty_pdf(tmp_path)
        doc = fitz.open(path)
        ctype, content = pr.load_page(doc, 0, ocr=False, ocr_threshold=50, can_show_images=False)
        assert ctype == "empty"
        assert content is None
        doc.close()

    def test_image_page_returns_image_when_supported(self, tmp_path):
        path = make_image_pdf(tmp_path)
        doc = fitz.open(path)
        ctype, content = pr.load_page(doc, 0, ocr=False, ocr_threshold=50, can_show_images=True)
        assert ctype == "image"
        assert isinstance(content, bytes)
        assert content[:4] == b"\x89PNG"   # PNG magic bytes
        doc.close()

    def test_image_page_falls_back_to_ocr(self, tmp_path):
        path = make_image_pdf(tmp_path)
        doc = fitz.open(path)
        with patch.object(pr, "ocr_page", return_value="OCR text here"):
            ctype, content = pr.load_page(doc, 0, ocr=True, ocr_threshold=50, can_show_images=False)
        assert ctype == "ocr"
        assert "OCR text here" in content
        doc.close()

    def test_image_page_no_images_no_ocr_returns_empty(self, tmp_path):
        path = make_image_pdf(tmp_path)
        doc = fitz.open(path)
        ctype, content = pr.load_page(doc, 0, ocr=False, ocr_threshold=50, can_show_images=False)
        assert ctype == "empty"
        doc.close()

# ── terminal protocol detection ───────────────────────────────────────────────

class TestTerminalProtocolDetection:
    def test_iterm2_uses_protocol(self):
        with patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"}, clear=False):
            assert pr._terminal_supports_protocol() is True

    def test_wezterm_uses_protocol(self):
        with patch.dict(os.environ, {"TERM_PROGRAM": "WezTerm"}, clear=False):
            assert pr._terminal_supports_protocol() is True

    def test_kitty_env_uses_protocol(self):
        with patch.dict(os.environ, {"KITTY_WINDOW_ID": "1"}, clear=False):
            assert pr._terminal_supports_protocol() is True

    def test_kitty_term_uses_protocol(self):
        with patch.dict(os.environ, {"TERM": "xterm-kitty"}, clear=False):
            assert pr._terminal_supports_protocol() is True

    def test_apple_terminal_no_protocol(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TERM_PROGRAM", "KITTY_WINDOW_ID", "TERM")}
        env["TERM_PROGRAM"] = "Apple_Terminal"
        env["TERM"] = "xterm-256color"
        with patch.dict(os.environ, env, clear=True):
            assert pr._terminal_supports_protocol() is False

    def test_apple_terminal_still_renders_ansi(self, tmp_path):
        """Terminal.app should use ANSI half-block rendering, not return empty."""
        path = make_image_pdf(tmp_path)
        doc = fitz.open(path)
        # With can_show_images=True, image-heavy page always returns "image" type
        ctype, content = pr.load_page(doc, 0, ocr=False, ocr_threshold=50, can_show_images=True)
        assert ctype == "image"
        assert content[:4] == b"\x89PNG"
        doc.close()

# ── File handling ─────────────────────────────────────────────────────────────

class TestFileHandling:
    def test_opens_valid_pdf(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        assert doc.page_count == 1
        doc.close()

    def test_multi_page_count(self, tmp_path):
        path = make_text_pdf(tmp_path, pages=7)
        doc = fitz.open(path)
        assert doc.page_count == 7
        doc.close()

    def test_metadata_is_dict(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        assert isinstance(doc.metadata, dict)
        assert "title" in doc.metadata
        doc.close()

    def test_invalid_file_raises(self, tmp_path):
        bad = str(tmp_path / "bad.pdf")
        Path(bad).write_text("not a pdf")
        with pytest.raises(Exception):
            fitz.open(bad)

# ── resume state ──────────────────────────────────────────────────────────────

class TestResumeState:
    def test_save_and_load(self, tmp_path, monkeypatch):
        pdf = str(tmp_path / "book.pdf")
        cache = tmp_path / "cache"
        monkeypatch.setattr(pr, "_state_file",
                            lambda p: cache / f"{Path(p).stem}.json")
        cache.mkdir()
        pr.save_resume(pdf, 42)
        state = pr.load_resume(pdf)
        assert state["last_page"] == 42

    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        pdf = str(tmp_path / "nonexistent.pdf")
        cache = tmp_path / "cache"
        monkeypatch.setattr(pr, "_state_file",
                            lambda p: cache / f"{Path(p).stem}.json")
        cache.mkdir()
        assert pr.load_resume(pdf) == {}

    def test_load_corrupt_returns_empty(self, tmp_path, monkeypatch):
        pdf = str(tmp_path / "book.pdf")
        cache = tmp_path / "cache"
        monkeypatch.setattr(pr, "_state_file",
                            lambda p: cache / f"{Path(p).stem}.json")
        cache.mkdir()
        (cache / "book.json").write_text("{{corrupt")
        assert pr.load_resume(pdf) == {}

# ── helper utilities ──────────────────────────────────────────────────────────

class TestHelpers:
    def test_most_common_basic(self):
        # 12 appears 3 times — clear winner
        assert pr._most_common([12, 12, 14, 12, 10]) == 12

    def test_most_common_tie_picks_smallest(self):
        # Tied frequency: body text (smallest) should win
        assert pr._most_common([24, 12]) == 12

    def test_most_common_single(self):
        assert pr._most_common([9]) == 9

    def test_most_common_empty(self):
        assert pr._most_common([]) == 12   # default fallback

    def test_render_page_image_returns_png(self, tmp_path):
        path = make_text_pdf(tmp_path)
        doc = fitz.open(path)
        png = pr.render_page_image(doc[0], dpi=72)
        assert isinstance(png, bytes)
        assert png[:4] == b"\x89PNG"
        doc.close()
