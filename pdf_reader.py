#!/usr/bin/env python3
"""PDF CLI Reader — styled text, inline images, OCR, search, TOC, resume."""

import sys
import tempfile
import os
import re
import base64
import json
import click
import fitz
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from rich.live import Live
from rich.spinner import Spinner
from rich.padding import Padding
from rich import box as rich_box

console = Console()

# ── Terminal background detection ─────────────────────────────────────────────

def _is_dark_terminal() -> bool:
    """
    Best-effort detection of terminal background colour.

    Strategy (in order of reliability):
    1. COLORFGBG="fg;bg" — set by Konsole, xterm, iTerm2, Terminal.app.
       Background index < 8 → dark; ≥ 8 (bright-white etc.) → light.
    2. TERM_PROGRAM-specific fallbacks (Apple_Terminal defaults to dark).
    3. Default to dark (the overwhelming majority of developer terminals).
    """
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        try:
            bg_index = int(colorfgbg.split(";")[-1].strip())
            return bg_index < 8          # 0–7 = dark colours; 8–15 = bright/light
        except (ValueError, IndexError):
            pass
    # Terminal.app on macOS defaults to a dark theme in recent versions
    return True


# ── Terminal image support ────────────────────────────────────────────────────

def _terminal_supports_protocol():
    """True only for terminals with native inline-image protocol support."""
    return (
        os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "Hyper")
        or "KITTY_WINDOW_ID" in os.environ
        or os.environ.get("TERM") == "xterm-kitty"
    )

def display_image(png_bytes):
    """
    Display a PNG in the terminal.
    - iTerm2 / WezTerm / Hyper → native inline protocol (best quality)
    - Kitty                    → Kitty graphics protocol
    - Everything else          → ANSI half-block art (works in Terminal.app
                                 and any terminal with 24-bit colour support)
    """
    if "KITTY_WINDOW_ID" in os.environ or os.environ.get("TERM") == "xterm-kitty":
        _display_kitty(png_bytes)
    elif os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "Hyper"):
        _display_iterm2(png_bytes)
    else:
        _display_ansi_blocks(png_bytes)

def _display_iterm2(png_bytes):
    encoded = base64.b64encode(png_bytes).decode()
    sys.stdout.write(f"\033]1337;File=inline=1;width=auto;height=auto:{encoded}\a\n")
    sys.stdout.flush()

def _display_kitty(png_bytes):
    chunk_size = 4096
    encoded = base64.b64encode(png_bytes).decode()
    chunks = [encoded[i:i + chunk_size] for i in range(0, len(encoded), chunk_size)]
    for idx, chunk in enumerate(chunks):
        more = 1 if idx < len(chunks) - 1 else 0
        header = f"a=T,f=100,m={more}" if idx == 0 else f"m={more}"
        sys.stdout.write(f"\033_G{header};{chunk}\033\\")
    sys.stdout.write("\n")
    sys.stdout.flush()

def _display_ansi_blocks(png_bytes):
    """
    Render image as ANSI 24-bit colour half-block characters (▀).
    Each character cell encodes two pixel rows:
      foreground = top pixel colour, background = bottom pixel colour.
    Works in macOS Terminal.app and any terminal with true-colour support.
    """
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

    # Available terminal space (leave room for header + footer + nav)
    term_w = max(20, console.width - 2)     # character columns
    term_h = max(10, console.height - 10)   # character rows

    # Character cells are ~2× taller than wide (aspect ≈ 0.5, width/height).
    # ▀ encodes 2 pixel rows per character row.
    # Correct mapping: char_rows = pixel_h × cell_w/cell_h × 0.5
    #                             = pixel_h × 0.5 × 0.5 × 2 = pixel_h × 0.5
    # So: char_rows_used = pixel_h × 0.5
    # Constraint: pixel_w ≤ term_w  AND  pixel_h × 0.5 ≤ term_h
    scale_by_w = term_w / img.width
    scale_by_h = term_h / (img.height * 0.5)
    scale = min(scale_by_w, scale_by_h, 1.0)   # never upscale

    target_w = max(20, int(img.width * scale))
    target_h  = int(img.height * scale * 0.5 * 2)  # *2 → pairs of pixel rows
    target_h  = max(2, target_h + target_h % 2)     # keep even

    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:
        resample = Image.LANCZOS                      # Pillow < 9

    img    = img.resize((target_w, target_h), resample)
    pixels = img.load()

    rows = []
    for y in range(0, target_h, 2):
        row = ""
        for x in range(target_w):
            r1, g1, b1 = pixels[x, y]
            r2, g2, b2 = pixels[x, y + 1] if y + 1 < target_h else (0, 0, 0)
            row += (
                f"\033[38;2;{r1};{g1};{b1}m"   # foreground = top pixel
                f"\033[48;2;{r2};{g2};{b2}m"   # background = bottom pixel
                "▀"
            )
        rows.append(row + "\033[0m")

    sys.stdout.write("\n".join(rows) + "\n")
    sys.stdout.flush()

# ── Page classification ───────────────────────────────────────────────────────

IMAGE_HEAVY_THRESHOLD = 0.45

def page_image_coverage(page):
    """Fraction of page area covered by images (0.0–1.0)."""
    page_area = page.rect.width * page.rect.height
    if page_area == 0:
        return 0.0
    image_area = sum(
        fitz.Rect(img["bbox"]).width * fitz.Rect(img["bbox"]).height
        for img in page.get_image_info()
    )
    return min(image_area / page_area, 1.0)

def is_image_heavy(page, text, ocr_threshold):
    """True when a page is dominated by images.
    - coverage > 0.7 → always render as image (full-page photos, title pages)
    - coverage > threshold AND sparse text → render as image (figures with captions)
    """
    coverage = page_image_coverage(page)
    if coverage > 0.7:
        return True
    return coverage > IMAGE_HEAVY_THRESHOLD and len(text) < ocr_threshold

def render_page_image(page, dpi=200):
    return page.get_pixmap(dpi=dpi).tobytes("png")

# ── Styled text extraction ────────────────────────────────────────────────────

def styled_page_text(page):
    """
    Extract page text as a Rich Text object with styles derived from font metadata:
      - Large fonts → heading (bold + colour scaled to size ratio)
      - Bold font flag or bold font name → bold
      - Italic flag or italic font name → italic
    Blocks are separated by a blank line; lines within a block are joined.
    """
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]

    # Dominant font size = body text reference.
    # Skip sub-1pt spans — they are embedded font data / glyph index tables
    # that leak through get_text("dict") in some academic PDFs.
    sizes = [
        round(span["size"])
        for block in blocks if block["type"] == 0
        for line in block["lines"]
        for span in line["spans"]
        if span["size"] >= 1 and _is_readable(span["text"])
    ]
    # Restrict to >=9pt for body-size voting: footnotes and figure labels
    # (typically 7–8pt) can outnumber body spans on figure-heavy pages,
    # causing body_size to be underestimated and body text to appear as headings.
    # Academic papers (e.g. chord_sigcomm) genuinely use 9pt body text, so 9
    # is the safe lower bound.  Fall back to the full list if nothing qualifies.
    size_candidates = [s for s in sizes if s >= 9] or sizes
    body_size = _most_common(size_candidates) if size_candidates else 12

    result = Text()
    first_block = True

    for block in blocks:
        if block["type"] != 0:          # skip image-type blocks
            continue

        block_text = Text()
        prev_line_ended_hyphen = False

        # Detect if this block is predominantly monospaced (code / terminal output)
        block_is_mono = False
        _mono_font_keywords = ("mono", "courier", "code", "consol", "inconsolata", "terminal", "typewriter")
        for _line in block["lines"]:
            for _sp in _line["spans"]:
                if _sp["size"] >= 1 and _is_readable(_sp["text"]):
                    _f = _sp["font"].lower()
                    if bool(_sp["flags"] & 8) or any(w in _f for w in _mono_font_keywords):
                        block_is_mono = True
                        break
            if block_is_mono:
                break

        for line_idx, line in enumerate(block["lines"]):
            line_text = Text()

            for span in line["spans"]:
                raw = span["text"]
                if not raw.strip() or span["size"] < 1 or not _is_readable(raw):
                    continue
                # Strip non-printable chars (e.g. \x03 ETX used as word separators
                # in diagram label fonts).  They pass _is_readable as a minority
                # but render as circles/boxes in the terminal.
                raw = ''.join(c for c in raw if c.isprintable())
                if not raw.strip():
                    continue
                size  = round(span["size"])
                flags = span["flags"]   # 1=super, 2=italic, 4=serif, 8=mono, 16=bold
                font  = span["font"].lower()

                is_bold    = bool(flags & 16) or any(w in font for w in ("bold", "heavy", "black"))
                is_italic  = bool(flags & 2)  or "italic" in font or "oblique" in font
                is_heading = size >= body_size * 1.2
                is_mono    = block_is_mono

                line_text.append(raw, style=_span_style(is_heading, is_bold, is_italic, size, body_size, is_mono=is_mono))

            if block_is_mono:
                # Preserve original line structure — no joining, no hyphen stripping
                if line_text.plain:
                    block_text.append_text(line_text)
                    block_text.append("\n")
            else:
                plain_line = line_text.plain.strip()
                if not plain_line:
                    continue

                if block_text.plain:
                    # Join lines: hyphenated → no separator; otherwise space
                    if prev_line_ended_hyphen:
                        pass                # previous append already stripped the hyphen
                    else:
                        block_text.append(" ")

                # Strip trailing hyphen and note it for the next line join
                if plain_line.endswith("-"):
                    # Rebuild line_text without the trailing hyphen
                    line_text = _strip_trailing_char(line_text)
                    prev_line_ended_hyphen = True
                else:
                    prev_line_ended_hyphen = False

                block_text.append_text(line_text)

        if not block_text.plain.strip():
            continue

        if not first_block:
            result.append("\n\n")
        result.append_text(block_text)
        first_block = False

    return result


def _is_readable(text):
    """True if the majority of characters are printable non-space characters.

    Requiring >50% (not just any one) filters out spans that are mostly
    control characters — e.g. PDF image labels encoded with non-standard
    font mappings (bytes like \\x14\\x15\\x1b appear alongside one printable
    char, so the old 'any' check wrongly passed them through, skewing the
    body-size calculation and rendering garbage in the output).
    """
    if not text:
        return False
    printable = sum(1 for c in text if c.isprintable() and not c.isspace())
    return printable > 0 and printable / len(text) > 0.5

def _span_style(is_heading, is_bold, is_italic, size, body_size, is_mono=False):
    parts = []
    if is_mono:
        # Code/mono spans: neutral grey — dark terminal uses a light grey (#ABABAB, ~7:1),
        # light terminal uses a dark grey (#595959, ~7:1 on white).
        parts.append("#ABABAB" if _is_dark_terminal() else "#595959")
        if is_bold:
            parts.append("bold")
        if is_italic:
            parts.append("italic")
    elif is_heading and body_size > 0:
        ratio = size / body_size
        if ratio >= 1.8:
            # H1: bright yellow — high visibility, ~17:1 contrast
            parts.append("bold bright_yellow")
        elif ratio >= 1.4:
            # H2: bright cyan — ~9:1 contrast
            parts.append("bold bright_cyan")
        else:
            # H3: bright blue — ~5:1 contrast (bold blue was ~2.6:1, fails WCAG)
            parts.append("bold bright_blue")
    elif is_bold:
        parts.append("bold")
    if is_italic and not is_heading and not is_mono:
        parts.append("italic")
    return " ".join(parts) if parts else ""


def _most_common(lst):
    if not lst:
        return 12
    counts = {}
    for x in lst:
        counts[x] = counts.get(x, 0) + 1
    peak = max(counts.values())
    # When sizes are tied in frequency, body text is always the smallest
    return min(k for k, v in counts.items() if v == peak)


def _strip_trailing_char(rich_text):
    """Return a copy of rich_text with the last character removed."""
    plain = rich_text.plain
    if not plain:
        return rich_text
    target_len = len(plain.rstrip()) - 1   # remove the trailing '-' after strip
    out = Text()
    count = 0
    for span in rich_text._spans:
        span_str = plain[span.start:span.end]
        if count + len(span_str) <= target_len:
            out.append(span_str, style=span.style)
            count += len(span_str)
        else:
            remaining = target_len - count
            if remaining > 0:
                out.append(span_str[:remaining], style=span.style)
            break
    return out

# ── Plain text extraction (for --plain / OCR fallback) ───────────────────────

def _reflow_plain(text):
    text = re.sub(r"-\n(\S)", r"\1", text)
    paragraphs = re.split(r"\n{2,}", text)
    paragraphs = [re.sub(r"\n", " ", p) for p in paragraphs]
    paragraphs = [re.sub(r" {2,}", " ", p).strip() for p in paragraphs]
    return "\n\n".join(p for p in paragraphs if p)

def extract_page_text(page, ocr, ocr_threshold):
    """Plain reflowed text, with OCR fallback for sparse pages."""
    text = page.get_text("text").strip()
    if len(text) < ocr_threshold and ocr:
        text = ocr_page(page).strip()
    return _reflow_plain(text)

# ── OCR ──────────────────────────────────────────────────────────────────────

def ocr_page(page):
    from ocrmac import ocrmac
    pix = page.get_pixmap(dpi=200)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(pix.tobytes("png"))
        tmp_path = f.name
    try:
        annotations = ocrmac.OCR(tmp_path).recognize()
        return "\n".join(text for text, *_ in annotations)
    finally:
        os.unlink(tmp_path)

# ── TOC ──────────────────────────────────────────────────────────────────────

def get_toc(doc):
    return [(level, title, page) for level, title, page in doc.get_toc()]

def current_chapter(toc, page_1based):
    result = None
    for level, title, page in toc:
        if level == 1 and page <= page_1based:
            result = title
    return result

def show_toc_interactive(doc, current_page_1based=None):
    """
    Interactive scrollable TOC. Arrow/j/k navigate; Enter jumps; q cancels.
    current_page_1based highlights where the reader currently is.
    Returns a 1-based page number or None.
    """
    toc = get_toc(doc)
    if not toc:
        console.print("[dim]No table of contents found in this PDF.[/dim]")
        return None

    entries = toc

    # Start selection on the entry closest to current page
    selected = 0
    if current_page_1based is not None:
        for idx, (_, _, page) in enumerate(entries):
            if page <= current_page_1based:
                selected = idx

    def _max_visible():
        """Recompute on every render so resize is handled automatically."""
        return max(4, console.height - 7)

    def _clamped_offset(sel, offset):
        mv = _max_visible()
        if sel < offset:
            return sel
        if sel >= offset + mv:
            return sel - mv + 1
        return offset

    scroll_offset = _clamped_offset(selected, max(0, selected - _max_visible() // 2))

    def _render(sel, offset):
        mv = _max_visible()
        body = Text()
        visible = entries[offset:offset + mv]
        for idx, (level, title, page) in enumerate(visible):
            abs_idx = offset + idx
            indent = "  " * (level - 1)
            if abs_idx == sel:
                body.append(f" ▶ {indent}", style="bold cyan")
                body.append(title, style="bold cyan reverse")
                body.append(f"  p.{page}\n", style="bold dim reverse")
            else:
                body.append(f"   {indent}", style="")
                body.append(title, style="dim" if level > 1 else "")
                body.append(f"  p.{page}\n", style="dim")
        if len(entries) > mv:
            shown_end = min(offset + mv, len(entries))
            body.append(f"\n  {offset + 1}–{shown_end} of {len(entries)}", style="dim")
        body.append("\n\n  ↑/↓  j/k  move    Enter  jump    q  cancel", style="dim")
        panel_title = "Table of Contents"
        if current_page_1based:
            panel_title += f"  ·  currently on p.{current_page_1based}"
        return Panel(body, title=panel_title, border_style="blue",
                     box=rich_box.ROUNDED, padding=(0, 1))

    def _draw():
        # Move cursor to top-left (no flash/clear), render the panel,
        # then erase any leftover content below it.
        sys.stdout.write("\033[H")
        sys.stdout.flush()
        console.print(_render(selected, scroll_offset))
        sys.stdout.write("\033[J")
        sys.stdout.flush()

    console.clear()
    _draw()
    while True:
        ch = click.getchar()
        if ch in ("\x1b[A", "k"):
            selected = max(0, selected - 1)
            scroll_offset = _clamped_offset(selected, scroll_offset)
        elif ch in ("\x1b[B", "j"):
            selected = min(len(entries) - 1, selected + 1)
            scroll_offset = _clamped_offset(selected, scroll_offset)
        elif ch in ("\r", "\n"):
            return entries[selected][2]
        elif ch in ("q", "\x1b", "\x03"):
            return None
        else:
            continue              # unknown key — skip redraw
        _draw()

# ── Search ────────────────────────────────────────────────────────────────────

def search_pdf(doc, query, page_indices):
    return [i for i in page_indices if doc[i].search_for(query)]

# ── Resume state ──────────────────────────────────────────────────────────────

def _state_file(pdf_path):
    cache = Path.home() / ".cache" / "pdf-reader"
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"{Path(pdf_path).stem[:80]}.json"

def load_resume(pdf_path):
    p = _state_file(pdf_path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}

def save_resume(pdf_path, page_index):
    _state_file(pdf_path).write_text(json.dumps({"last_page": page_index}))

# ── Page loader ───────────────────────────────────────────────────────────────

def load_page(doc, i, ocr, ocr_threshold, can_show_images):
    """
    Returns (content_type, content):
      "rich"  → Rich Text with styling
      "image" → PNG bytes for inline display
      "ocr"   → plain string from OCR
      "empty" → None
    """
    page = doc[i]
    raw_text = page.get_text("text").strip()

    if is_image_heavy(page, raw_text, ocr_threshold):
        if can_show_images:
            return "image", render_page_image(page)
        if ocr:
            ocr_text = ocr_page(page).strip()
            if ocr_text:
                return "ocr", _reflow_plain(ocr_text)
        return "empty", None

    rich_text = styled_page_text(page)
    if rich_text.plain.strip():
        return "rich", rich_text
    return "empty", None

# ── UI helpers ────────────────────────────────────────────────────────────────

def _progress_bar(current, total, width=36):
    """Build a Unicode block progress bar string."""
    pct = current / total if total else 0
    filled = int(width * pct)
    return "█" * filled + "░" * (width - filled)

def _render_header(book_title, chapter, page_num, total, pos, set_size, search_query, search_results, page_label=None):
    """Structured header: title · chapter on line 1, progress bar on line 2."""
    body = Text()

    # ── row 1: book title + chapter ──────────────────────────────────────────
    short_title = book_title[:55] if book_title else "PDF Reader"
    body.append(f"  {short_title}", style="bold white")
    if chapter:
        body.append("  ╌  ", style="dim")
        body.append(chapter[:50], style="bright_cyan")   # cyan was ~4:1; bright_cyan ~9:1
    body.append("\n")

    # ── row 2: progress bar + page counter ───────────────────────────────────
    bar_width = max(20, min(40, console.width - 40))
    bar = _progress_bar(page_num, total, bar_width)
    pct = int(page_num / total * 100) if total else 0
    display_page = page_label if page_label else str(page_num)
    body.append(f"  {bar}", style="dodger_blue2")       # blue was ~2.6:1; dodger_blue2 ~4.5:1
    body.append(f"  Page {display_page} / {total}", style="bold")
    body.append(f"  {pct}%", style="dim")
    if set_size != total:
        body.append(f"  [{pos + 1} of {set_size} selected]", style="dim")

    # ── search indicator ─────────────────────────────────────────────────────
    if search_query:
        n = len(search_results)
        body.append(f"  ·  🔍 '{search_query}'", style="bright_yellow")
        body.append(f" ({n} page{'s' if n != 1 else ''})", style="yellow")

    return Panel(body, box=rich_box.HORIZONTALS, border_style="blue", padding=(0, 0))


def _render_footer(has_toc, has_search, search_active, is_image_page=False):
    """Compact shortcut bar shown at the bottom of every page."""

    items = [
        ("SPACE/→", "next"),
        ("b/←",     "back"),
        ("[/]",      "±10"),
        ("g",        "go to"),
    ]
    if has_toc:
        items.append(("t", "TOC"))
    items.append(("/", "search"))
    if has_search:
        items.append(("n/N", "match"))
    items.append(("i", "image view"))
    items.append(("v", "open in Preview"))
    items.append(("q", "quit"))

    row = Text()
    row.append("  ")
    for key, desc in items:
        row.append(f" {key} ", style="bold black on white")
        row.append(f" {desc}  ", style="dim")

    return Panel(row, box=rich_box.HORIZONTALS, border_style="dim blue", padding=(0, 0))

# ── Interactive reader ────────────────────────────────────────────────────────

def _interactive(doc, pdf_path, page_indices, total, ocr, ocr_threshold, can_show_images):
    toc = get_toc(doc)
    has_toc = len(toc) > 0
    book_title = (doc.metadata.get("title") or Path(pdf_path).stem)

    # Resume from last position
    state = load_resume(pdf_path)
    last = state.get("last_page")
    pos = 0
    if last is not None and last in page_indices:
        pos = page_indices.index(last)
        console.clear()
        console.print(f"[dim]  ↩  Resuming from page {last + 1}[/dim]\n")

    search_results = []
    search_query = ""

    while 0 <= pos < len(page_indices):
        i = page_indices[pos]
        save_resume(pdf_path, i)

        chapter = current_chapter(toc, i + 1)

        # ── clear screen before each page ────────────────────────────────────
        console.clear()

        # ── header ───────────────────────────────────────────────────────────
        page_label = doc[i].get_label() or None
        console.print(_render_header(
            book_title, chapter,
            page_num=i + 1, total=total,
            pos=pos, set_size=len(page_indices),
            search_query=search_query, search_results=search_results,
            page_label=page_label,
        ))

        # ── content ──────────────────────────────────────────────────────────
        with Live(Spinner("dots", text=" Loading…"), console=console, transient=True):
            ctype, content = load_page(doc, i, ocr, ocr_threshold, can_show_images)

        if ctype == "image":
            display_image(content)
            # Some books (photography, design) have full-page images that also
            # carry substantial body text overlaid or alongside the photo.
            # Render that text below the image so it isn't silently lost.
            page_obj = doc[i]
            if len(page_obj.get_text("text").strip()) > 300:
                page_text = styled_page_text(page_obj)
                if page_text.plain.strip():
                    if search_query:
                        page_text.highlight_words([search_query],
                                                  style="bold black on bright_yellow")
                    console.print(Padding(page_text, (1, 0, 0, 3)))
        elif ctype == "rich":
            if search_query:
                content.highlight_words([search_query], style="bold black on bright_yellow")
            console.print(Padding(content, (0, 0, 0, 3)))
        elif ctype == "ocr":
            console.print(Padding(Text(content), (0, 0, 0, 3)))
        else:
            console.print(Padding(Text("  No text found on this page.", style="dim italic"), (0, 0, 0, 3)))

        if search_results and i in search_results:
            match_pos = search_results.index(i) + 1
            console.print(f"\n   [bright_yellow]Search result {match_pos} / {len(search_results)} for '[bold]{search_query}[/bold]'[/bright_yellow]")

        # ── footer ───────────────────────────────────────────────────────────
        console.print()
        console.print(_render_footer(has_toc, bool(search_results), bool(search_query),
                                     is_image_page=(ctype == "image")))

        # ── key navigation ───────────────────────────────────────────────────
        while True:
            ch = click.getchar()

            # Next page
            if ch in (" ", "\r", "\x1b[C", "l"):
                pos = min(pos + 1, len(page_indices))
                break

            # Prev page  (\x7f = Mac Delete key, \x1b[D = ← arrow)
            elif ch in ("\x1b[D", "b", "h", "p", "\x7f"):
                pos = max(pos - 1, -1)
                break

            # Jump ±10
            elif ch == "]":
                pos = min(pos + 10, len(page_indices) - 1)
                break
            elif ch == "[":
                pos = max(pos - 10, 0)
                break

            # First / last page
            elif ch in ("\x1b[H", "^"):           # Home key or ^
                pos = 0
                break
            elif ch in ("\x1b[F", "$"):            # End key or $
                pos = len(page_indices) - 1
                break

            # Go to specific page
            elif ch == "g":
                try:
                    raw = click.prompt("\n   Go to page", prompt_suffix=" › ")
                    target = int(raw)
                    if 1 <= target <= total and (target - 1) in page_indices:
                        pos = page_indices.index(target - 1)
                    else:
                        console.print(f"   [bold bright_red]Page {target} is not in the current page set.[/bold bright_red]")
                except (ValueError, click.Abort):
                    pass
                break

            # Table of contents
            elif ch == "t":
                target = show_toc_interactive(doc, current_page_1based=i + 1)
                if target is not None and 1 <= target <= total and (target - 1) in page_indices:
                    pos = page_indices.index(target - 1)
                break

            # Search
            elif ch == "/":
                try:
                    query = click.prompt("\n   Search", prompt_suffix=" › ")
                    if query.strip():
                        with Live(Spinner("dots", text=" Searching…"), console=console, transient=True):
                            search_results = search_pdf(doc, query.strip(), page_indices)
                        search_query = query.strip()
                        if search_results:
                            console.print(f"   [bold bright_green]Found on {len(search_results)} page(s). Use n/N to navigate.[/bold bright_green]")
                            pos = page_indices.index(search_results[0])
                        else:
                            console.print(f"   [bold bright_red]'{query}' not found.[/bold bright_red]")
                except click.Abort:
                    pass
                break

            # Next search match
            elif ch == "n":
                if search_results:
                    ahead = [p for p in search_results if page_indices.index(p) > pos]
                    if ahead:
                        pos = page_indices.index(ahead[0])
                    else:
                        console.print("   [dim]No more matches forward.[/dim]")
                break

            # Prev search match
            elif ch == "N":
                if search_results:
                    behind = [p for p in search_results if page_indices.index(p) < pos]
                    if behind:
                        pos = page_indices.index(behind[-1])
                    else:
                        console.print("   [dim]No more matches backward.[/dim]")
                break

            # Open current page in system viewer (macOS Preview)
            elif ch == "v":
                import tempfile, subprocess
                png = content if ctype == "image" else render_page_image(doc[i])
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                    tf.write(png)
                    tf.flush()
                    subprocess.Popen(["open", tf.name])
                # no break — stay on same page

            # Inline image view of the current page
            elif ch == "i":
                console.clear()
                console.print(_render_header(
                    book_title, chapter,
                    page_num=i + 1, total=total,
                    pos=pos, set_size=len(page_indices),
                    search_query=search_query, search_results=search_results,
                    page_label=page_label,
                ))
                with Live(Spinner("dots", text=" Rendering…"), console=console, transient=True):
                    img_bytes = content if ctype == "image" else render_page_image(doc[i])
                display_image(img_bytes)
                console.print("\n   [dim]Viewing page as image. Press any key to return.[/dim]")
                click.getchar()
                break  # redisplay the page in normal mode

            # Quit
            elif ch in ("q", "\x03"):
                console.print()
                return

    console.print()

# ── Page range parser ─────────────────────────────────────────────────────────

def parse_pages(spec, total):
    """Parse '1-3', '2,4,7', or None (all pages) into sorted 0-based indices."""
    if spec is None:
        return list(range(total))
    indices = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                start, end = int(start), int(end)
                if start < 1 or end > total or start > end:
                    return None
                indices.update(range(start - 1, end))
            except ValueError:
                return None
        else:
            try:
                n = int(part)
                if n < 1 or n > total:
                    return None
                indices.add(n - 1)
            except ValueError:
                return None
    return sorted(indices)

# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False))
@click.option("-p", "--pages", default=None, help="Page range, e.g. 1-3 or 2,4,7")
@click.option("--ocr/--no-ocr", default=True, show_default=True,
              help="Use OCR for image-heavy pages")
@click.option("--ocr-threshold", default=50, show_default=True,
              help="Min chars before falling back to OCR")
@click.option("--plain", is_flag=True, help="Plain text output (good for piping)")
@click.option("--meta", is_flag=True, help="Show PDF metadata and exit")
@click.option("--images/--no-images", default=True, show_default=True,
              help="Display image pages inline (ANSI blocks in any terminal; native protocol in iTerm2/Kitty)")
def main(pdf_path, pages, ocr, ocr_threshold, plain, meta, images):
    """Read a PDF in the terminal with styled text, images, search, and TOC navigation."""
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        console.print(f"[bold bright_red]Error opening PDF:[/bold bright_red] {e}")
        sys.exit(1)

    if meta:
        info = doc.metadata
        toc = get_toc(doc)
        if plain:
            for k, v in info.items():
                if v:
                    click.echo(f"{k}: {v}")
            click.echo(f"pages: {doc.page_count}")
            click.echo(f"toc_entries: {len(toc)}")
        else:
            lines = [f"[bold]{k}:[/bold] {v}" for k, v in info.items() if v]
            lines += [
                f"[bold]pages:[/bold] {doc.page_count}",
                f"[bold]toc entries:[/bold] {len(toc)}",
            ]
            console.print(Panel("\n".join(lines), title="PDF Metadata", border_style="blue"))
        return

    total = doc.page_count
    page_indices = parse_pages(pages, total)
    if page_indices is None:
        console.print("[bold bright_red]Invalid page range.[/bold bright_red]")
        sys.exit(1)

    if plain:
        for i in page_indices:
            click.echo(f"\n--- Page {i + 1} ---\n")
            click.echo(extract_page_text(doc[i], ocr, ocr_threshold))
        return

    can_show_images = images          # ANSI fallback works in any colour terminal
    _interactive(doc, pdf_path, page_indices, total, ocr, ocr_threshold, can_show_images)


if __name__ == "__main__":
    main()
