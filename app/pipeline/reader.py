"""Stage 2: turn a PDF into positioned text.

Digital PDFs carry a text layer and are read directly. Scanned PDFs are images, so
each page is rendered and passed through Tesseract OCR. Either way the result is the
same structure (words -> cells -> rows) so every later stage is format-agnostic.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pymupdf

from .. import config

MIN_TEXT_WORDS = 12  # fewer words than this on a page means "no usable text layer"
# 300 dpi is the usual choice for OCR. A small hosted instance can lower it to trade a little
# margin on poor scans for speed; the demo scan reads identically from 150 to 400 dpi.
OCR_DPI = int(os.environ.get("INVOICE_DESK_OCR_DPI", "300"))
# Tesseract's own multithreading costs more than it saves on a single page, and on a shared
# CPU it thrashes. One thread is both faster and kinder.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
OCR_NOISE_FLOOR = 40.0


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 100.0  # OCR confidence 0-100; digital text is 100


@dataclass
class Cell:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].x0

    @property
    def x1(self) -> float:
        return self.words[-1].x1


@dataclass
class Row:
    page: int
    cells: list[Cell]
    index: int = 0

    @property
    def words(self) -> list[Word]:
        return [w for c in self.cells for w in c.words]

    @property
    def text(self) -> str:
        """Cells joined by a tab so label/value parsing can respect column gaps."""
        return "\t".join(c.text for c in self.cells)

    @property
    def flat(self) -> str:
        return " ".join(c.text for c in self.cells)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)


@dataclass
class Page:
    number: int
    width: float
    height: float
    method: str  # "text" | "ocr"
    rows: list[Row] = field(default_factory=list)
    ocr_confidence: Optional[float] = None
    word_count: int = 0


@dataclass
class Document:
    pages: list[Page]
    file_kind: str  # "digital" | "scanned" | "mixed" | "empty"
    pages_in_file: int = 0  # may exceed len(pages) when the file is longer than the page limit

    @property
    def rows(self) -> list[Row]:
        return [r for p in self.pages for r in p.rows]

    @property
    def text(self) -> str:
        return "\n".join(r.flat for r in self.rows)

    @property
    def is_ocr(self) -> bool:
        """True if any text on which the result depends came from OCR."""
        return any(p.method == "ocr" and p.word_count for p in self.pages)

    @property
    def ocr_confidence(self) -> Optional[float]:
        vals = [p.ocr_confidence for p in self.pages if p.ocr_confidence is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def page(self, number: int) -> Optional[Page]:
        for p in self.pages:
            if p.number == number:
                return p
        return None


class UnreadableDocument(Exception):
    """The file is not a PDF we can open (corrupt, encrypted, wrong type)."""


def _split_cells(words: list[Word]) -> list[Cell]:
    """Group a row's words into cells wherever there is a column-sized gap."""
    if not words:
        return []
    words = sorted(words, key=lambda w: w.x0)
    chars = sum(len(w.text) for w in words) or 1
    char_w = sum(w.x1 - w.x0 for w in words) / chars
    gap_limit = max(2.2 * char_w, 7.0)
    cells: list[Cell] = [Cell([words[0]])]
    for prev, cur in zip(words, words[1:]):
        if cur.x0 - prev.x1 > gap_limit:
            cells.append(Cell([cur]))
        else:
            cells[-1].words.append(cur)
    return cells


def _rows_from_words(words: list[Word], page_no: int) -> list[Row]:
    """Cluster words into visual rows by vertical overlap."""
    if not words:
        return []
    heights = [w.y1 - w.y0 for w in words]
    med_h = statistics.median(heights)
    words = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    groups: list[list[Word]] = []
    centre = None
    for w in words:
        c = (w.y0 + w.y1) / 2
        if centre is None or abs(c - centre) > 0.55 * med_h:
            groups.append([w])
            centre = c
        else:
            groups[-1].append(w)
            centre = sum((x.y0 + x.y1) / 2 for x in groups[-1]) / len(groups[-1])
    return [Row(page=page_no, cells=_split_cells(g)) for g in groups]


def _ocr_page(page: "pymupdf.Page", page_no: int) -> tuple[list[Row], Optional[float], int]:
    import pytesseract
    from PIL import Image

    # Greyscale, straight from the rendered pixels: OCR works on grey levels anyway, and this
    # skips encoding and decoding a full-page PNG.
    pix = page.get_pixmap(dpi=OCR_DPI, alpha=False, colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    scale = 72.0 / OCR_DPI
    data = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list[Word]] = {}
    confs: list[float] = []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        # Below this, Tesseract is guessing at ruled lines, stamps and specks, not reading text.
        if not txt or conf < OCR_NOISE_FLOOR:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        word = Word(txt, x * scale, y * scale, (x + w) * scale, (y + h) * scale, conf)
        # Tesseract's own line segmentation copes with the skew of a scanned page.
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        if any(ch.isalnum() for ch in txt):
            confs.append(conf)
    rows = [Row(page=page_no, cells=_split_cells(ws)) for ws in lines.values()]
    rows.sort(key=lambda r: (r.y0, r.cells[0].x0))
    mean_conf = round(sum(confs) / len(confs), 1) if confs else None
    return rows, mean_conf, len(confs)


def _is_image_page(page: "pymupdf.Page") -> bool:
    """Does an embedded image cover a substantial part of the page (i.e. is it a scan)?"""
    try:
        area = page.rect.width * page.rect.height
        for img in page.get_images(full=True):
            for rect in page.get_image_rects(img[0]):
                if rect.width * rect.height >= 0.35 * area:
                    return True
        return False
    except Exception:
        return bool(page.get_images())


def read_pdf(path: Path, on_event: Optional[Callable[[str, dict], None]] = None) -> Document:
    """Read every page. ``on_event`` receives progress so the live view can narrate OCR."""
    notify = on_event or (lambda kind, info: None)
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # corrupt or not a PDF
        raise UnreadableDocument(f"The file could not be opened as a PDF ({exc.__class__.__name__}).") from exc
    if doc.needs_pass:
        raise UnreadableDocument("The PDF is password-protected.")
    if doc.page_count == 0:
        raise UnreadableDocument("The PDF has no pages.")

    pages: list[Page] = []
    for i, page in enumerate(doc, start=1):
        if i > config.MAX_PAGES:
            break
        raw = page.get_text("words")
        words = [Word(w[4], w[0], w[1], w[2], w[3]) for w in raw if w[4].strip()]
        # OCR is for pages that are pictures of text. A digital page with little on it (a signature
        # page, a blank trailing page) is read as it is, not sent to OCR.
        if len(words) >= MIN_TEXT_WORDS or not _is_image_page(page):
            rows = _rows_from_words(words, i)
            pages.append(Page(i, page.rect.width, page.rect.height, "text", rows, None, len(words)))
            notify("page_text", {"page": i, "words": len(words)})
        else:
            notify("ocr_start", {"page": i, "images": len(page.get_images())})
            rows, conf, n = _ocr_page(page, i)
            pages.append(Page(i, page.rect.width, page.rect.height, "ocr", rows, conf, n))
            notify("ocr_done", {"page": i, "words": n, "confidence": conf})
    pages_in_file = doc.page_count
    doc.close()

    idx = 0
    for p in pages:
        for r in p.rows:
            r.index = idx
            idx += 1

    methods = {p.method for p in pages if p.word_count}
    total_words = sum(p.word_count for p in pages)
    if total_words == 0:
        kind = "empty"
    elif methods == {"text"}:
        kind = "digital"
    elif methods == {"ocr"}:
        kind = "scanned"
    else:
        kind = "mixed"
    return Document(pages=pages, file_kind=kind, pages_in_file=pages_in_file)


def render_previews(path: Path, out_dir: Path, dpi: int = 110) -> int:
    """PNG previews of each page for the document viewer. Returns the page count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(str(path))
    n = 0
    for i, page in enumerate(doc, start=1):
        if i > config.MAX_PAGES:
            break
        page.get_pixmap(dpi=dpi, alpha=False).save(str(out_dir / f"page-{i}.png"))
        n += 1
    doc.close()
    return n
