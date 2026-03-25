# pdf-cli-reader

A Mac CLI PDF reader with styled text, inline images, full-text search, interactive table of contents, and resume support. Runs entirely from a project-local Python virtual environment — no global installs required.

---

## Features

- **Styled text rendering** — headings, bold, italic, and monospaced/code blocks are detected from PDF font metadata and rendered with appropriate colors and styles
- **Inline image display** — image-heavy pages render as ANSI half-block art in any true-color terminal (macOS Terminal.app, iTerm2, etc.); iTerm2 and Kitty get native high-quality protocol rendering
- **OCR fallback** — scanned/image-only PDFs are processed with macOS Vision framework via `ocrmac` (no Tesseract binary required)
- **Interactive table of contents** — arrow-key navigation with smooth in-place redraws, no screen flashing
- **Full-text search** — search across all pages with `n`/`N` to jump between matches, highlighted in page text
- **Resume support** — automatically saves and restores your last reading position per PDF
- **Lazy page loading** — pages are loaded one at a time; large PDFs open instantly
- **Structured header** — book title, current chapter, progress bar, and page counter on every page
- **Persistent footer** — all keyboard shortcuts visible at all times
- **WCAG-compliant colors** — all UI colors checked for minimum 4.5:1 contrast ratio; adapts between dark and light terminal themes

---

## Requirements

- macOS (uses macOS Vision framework for OCR)
- Python 3.9+
- No system-level dependencies — everything installs into the project `.venv/`

---

## Installation

```bash
git clone https://github.com/ashish-puttaa/pdf-cli-reader-vibes.git
cd pdf-cli-reader-vibes

python3 -m venv .venv
.venv/bin/pip install pymupdf rich click ocrmac pillow
```

Then make the launcher executable:

```bash
chmod +x pdf
```

Optionally symlink to your PATH for global access:

```bash
ln -s "$(pwd)/pdf" /usr/local/bin/pdf
```

---

## Usage

```
./pdf <path-to-pdf> [OPTIONS]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `-p`, `--pages` | all | Page range, e.g. `1-10` or `2,4,7` |
| `--ocr` / `--no-ocr` | on | OCR image-heavy pages with macOS Vision |
| `--ocr-threshold` | 50 | Min characters on a page before falling back to OCR |
| `--images` / `--no-images` | on | Render image pages inline |
| `--plain` | off | Plain text output (good for piping or scripts) |
| `--meta` | off | Show PDF metadata and exit |

### Examples

```bash
# Read a book
./pdf books/my-book.pdf

# Start from page 42
./pdf books/my-book.pdf -p 42

# Read pages 10–50 only
./pdf books/my-book.pdf -p 10-50

# Dump plain text
./pdf books/my-book.pdf --plain > output.txt

# Show metadata
./pdf books/my-book.pdf --meta
```

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` / `→` / `l` | Next page |
| `Delete` / `←` / `b` / `h` / `p` | Previous page |
| `]` | Jump forward 10 pages |
| `[` | Jump back 10 pages |
| `^` / `Home` | First page |
| `$` / `End` | Last page |
| `g` | Go to specific page number |
| `t` | Open interactive table of contents |
| `/` | Search |
| `n` | Next search match |
| `N` | Previous search match |
| `v` | Open current image page in macOS Preview (full resolution) |
| `q` | Quit |

### Table of Contents

| Key | Action |
|-----|--------|
| `↑` / `k` | Move up |
| `↓` / `j` | Move down |
| `Enter` | Jump to selected chapter |
| `q` / `Esc` | Close TOC |

---

## How It Works

### Text rendering

Pages are parsed with PyMuPDF's `get_text("dict")` which returns font metadata per span. The reader uses this to detect:

- **Headings** — spans whose font size is ≥ 1.2× the body font size
- **Bold / italic** — font flags bitmask (bits 16 and 2) or font name keywords
- **Monospaced / code blocks** — font flags bit 8 or font names containing `mono`, `courier`, `code`, etc.
- **Garbage spans** — sub-1pt spans (embedded font data) are filtered out

Lines within prose blocks are reflowed (joined with spaces, hyphenation preserved). Lines within monospaced blocks are preserved exactly as-is.

### Image rendering

Pages are classified as image-heavy if:
- Image coverage > 70% of page area (always renders as image — handles photography books with captions), or
- Image coverage > 45% **and** text is sparse (< 50 characters)

Image-heavy pages are rasterized at 200 DPI and displayed via:
1. **Kitty graphics protocol** — if `KITTY_WINDOW_ID` or `TERM=xterm-kitty`
2. **iTerm2 inline images** — if `TERM_PROGRAM=iTerm.app` or `WezTerm`/`Hyper`
3. **ANSI half-block art** — universal fallback using `▀` with 24-bit fg/bg colors, scales to fit terminal width and height

Press `v` on any image page to open it in macOS Preview at full resolution.

### OCR

When a page is image-heavy but `--images` is disabled (or the page has no detected images), OCR is attempted using `ocrmac`, which calls the macOS Vision framework directly via PyObjC. No Tesseract binary is needed.

### Resume

Reading position is saved to `~/.cache/pdf-reader/<filename>.json` after each page turn and restored on next open.

---

## Color Scheme

Colors are chosen for WCAG AA compliance (≥ 4.5:1 contrast ratio) against both dark and light terminal backgrounds.

| Element | Dark terminal | Light terminal |
|---------|--------------|----------------|
| H1 headings | `bright_yellow` | `bright_yellow` |
| H2 headings | `bright_cyan` | `bright_cyan` |
| H3 headings | `bright_blue` | `bright_blue` |
| Code / monospaced | `#ABABAB` (light grey) | `#595959` (dark grey) |
| Search highlight | black on `bright_yellow` | black on `bright_yellow` |

Terminal theme is auto-detected via the `COLORFGBG` environment variable (set by Terminal.app, iTerm2, Konsole, and xterm). Falls back to dark if unset.

---

## Project Structure

```
pdf-cli-reader/
├── pdf              # Launcher shell script
├── pdf_reader.py    # Main application
├── tests/
│   └── test_pdf_reader.py   # 79 unit tests
├── books/           # Your PDF collection (gitignored)
└── .venv/           # Project-local Python environment (gitignored)
```

---

## Running Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

All 79 tests cover: text extraction, styled rendering, image classification, OCR fallback, TOC parsing, search, page navigation, resume state, and terminal protocol detection.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `pymupdf` | PDF parsing, text/image extraction, page rendering |
| `rich` | Terminal UI — styled text, panels, live updates, spinner |
| `click` | CLI argument parsing and single-keypress input |
| `ocrmac` | macOS Vision OCR via PyObjC (no Tesseract needed) |
| `pillow` | Image processing for ANSI half-block rendering |
