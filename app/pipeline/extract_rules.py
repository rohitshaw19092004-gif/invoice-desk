"""Stage 3 (built-in parser): positioned text -> structured invoice.

This is the deterministic extractor. It is the default when no AI credential is
configured and the safety net when the AI call fails. It works from labels and
column geometry rather than fixed templates, so it copes with layouts it has not
seen, and it refuses to guess: a field that is not clearly labelled stays empty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

from .. import config
from ..money import D, find_periods, is_number_words, parse_amount, parse_date, q2, words_to_number
from .reader import Document, Row, Word

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
PO_SHAPE_RE = re.compile(r"\bP\.?O\.?[-/# ]{0,2}\d{2,4}[-/]\d{2,6}\b", re.I)

UNIT_WORDS = {
    "pcs", "pc", "nos", "no", "units", "unit", "rolls", "roll", "kg", "kgs", "ltr", "ltrs", "hrs", "hr", "hours",
    "month", "months", "lot", "lots", "set", "sets", "box", "boxes", "copies", "trips", "shipments", "each", "ea",
    "mtr", "mtrs", "days", "day", "licences", "licenses", "users", "pkt", "pkts", "bags", "sheets", "reams",
}

DOC_TITLES = re.compile(
    r"^(tax\s+invoice|invoice|bill|service\s+invoice|proforma(\s+invoice)?|original(\s+for\s+recipient)?|"
    r"\(?original.*|duplicate.*|credit\s+note|debit\s+note|retail\s+invoice|bill\s+of\s+supply|cash\s+memo)$",
    re.I,
)
COMPANY_HINT = re.compile(
    r"\b(pvt|private|ltd|limited|llp|inc|corp|co\.|company|industries|enterprises|services|solutions|logistics|"
    r"traders|trading|associates|systems|technologies|print|signage|packaging|mart|agencies|& sons|and sons)\b",
    re.I,
)


@dataclass
class FieldMeta:
    confidence: float
    raw: str = ""
    page: Optional[int] = None
    bbox: Optional[list[float]] = None  # [x0, y0, x1, y1] as fractions of the page
    note: str = ""


@dataclass
class LineItem:
    description: str
    qty: Optional[Decimal]
    unit: Optional[str]
    unit_price: Optional[Decimal]
    amount: Optional[Decimal]
    confidence: float = 1.0
    page: Optional[int] = None
    bbox: Optional[list[float]] = None
    derived_price: bool = False


@dataclass
class Extraction:
    method: str = "rules"  # "rules" | "ai"
    model: Optional[str] = None
    vendor_name: Optional[str] = None
    vendor_gstin: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None  # ISO
    due_date: Optional[str] = None
    po_number: Optional[str] = None
    po_reference_style: Optional[str] = None  # "labelled" | "in running text"
    currency: str = "INR"
    subtotal: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    tax_rate: Optional[Decimal] = None  # percent
    tax_lines: list[dict] = field(default_factory=list)
    total: Optional[Decimal] = None
    tax_inclusive: bool = False
    amount_in_words: Optional[str] = None
    amount_in_words_complete: bool = True  # False if the text may continue beyond what was read
    doc_type: Optional[str] = None  # set when the page is titled as something other than an invoice
    bank_account: Optional[str] = None
    ifsc: Optional[str] = None
    line_items: list[LineItem] = field(default_factory=list)
    periods: list[tuple[int, int]] = field(default_factory=list)
    date_ambiguous: bool = False
    notes: list[str] = field(default_factory=list)
    meta: dict[str, FieldMeta] = field(default_factory=dict)

    def confidence(self) -> Optional[float]:
        keys = ["vendor_name", "invoice_number", "invoice_date", "total"]
        vals = [self.meta[k].confidence for k in keys if k in self.meta and getattr(self, k) is not None]
        vals += [li.confidence for li in self.line_items]
        return round(sum(vals) / len(vals), 3) if vals else None


# ------------------------------------------------------------ row helpers ---

class RowText:
    """A row's text with a map from character offsets back to the words on the page."""

    def __init__(self, row: Row):
        self.row = row
        self.spans: list[tuple[int, int, Word]] = []
        parts = []
        pos = 0
        for ci, cell in enumerate(row.cells):
            if ci:
                parts.append("\t")
                pos += 1
            for wi, w in enumerate(cell.words):
                if wi:
                    parts.append(" ")
                    pos += 1
                self.spans.append((pos, pos + len(w.text), w))
                parts.append(w.text)
                pos += len(w.text)
        self.text = "".join(parts)

    def words_in(self, a: int, b: int) -> list[Word]:
        return [w for s, e, w in self.spans if s < b and e > a]


def _bbox(words: list[Word], doc: Document, page_no: int) -> Optional[list[float]]:
    page = doc.page(page_no)
    if not words or not page:
        return None
    pad = 1.5
    return [
        round(max(0.0, (min(w.x0 for w in words) - pad) / page.width), 4),
        round(max(0.0, (min(w.y0 for w in words) - pad) / page.height), 4),
        round(min(1.0, (max(w.x1 for w in words) + pad) / page.width), 4),
        round(min(1.0, (max(w.y1 for w in words) + pad) / page.height), 4),
    ]


def _conf(words: list[Word]) -> float:
    """Digital text is certain about its characters; OCR text is as sure as its weakest word."""
    if not words:
        return 0.9
    if all(w.conf >= 100 for w in words):
        return 0.99
    return round(min(0.99, (sum(w.conf for w in words) / len(words)) / 100.0), 3)


def _meta(words: list[Word], doc: Document, row: Row, raw: str, note: str = "") -> FieldMeta:
    return FieldMeta(confidence=_conf(words), raw=raw, page=row.page, bbox=_bbox(words, doc, row.page), note=note)


# ------------------------------------------------------ labelled fields ---

_SEP = r"[\s.:#\-–—]*"

INVOICE_NO_LABELS = [
    r"(?:tax\s+)?invoice\s*(?:no|num(?:ber)?|#)\.?",
    r"inv\.?\s*(?:no|num(?:ber)?|#)\.?",
    r"bill\s*(?:no|num(?:ber)?|#)\.?",
    r"(?:document|doc|voucher)\s*(?:no|num(?:ber)?)\.?",
]
INVOICE_DATE_LABELS = [
    r"(?:tax\s+)?invoice\s*date",
    r"inv\.?\s*date",
    r"bill\s*date",
    r"date\s*of\s*(?:issue|invoice)",
    r"issued(?:\s*on)?",
    r"dated?",
]
DUE_DATE_LABELS = [r"due\s*date", r"payment\s*due(?:\s*(?:date|by|on))?", r"pay\s*by", r"due(?:\s*(?:on|by))?"]
PO_LABELS = [
    r"buyer'?s?\s*order\s*(?:no|num(?:ber)?|ref(?:erence)?)\.?",
    r"(?:customer|cust\.?|client|your|our)?\s*(?:p\.?\s?o\.?|purchase\s*order)\s*(?:no|num(?:ber)?|ref(?:erence)?|#)\.?",
    r"purchase\s*order",
    r"(?:your|our)?\s*order\s*(?:no|num(?:ber)?|ref(?:erence)?|#)\.?",
    r"(?:against|as\s*per|vide|ref(?:erence)?\.?:?)\s*(?:your\s*|our\s*)?(?:order|p\.?o\.?)",
]
# A bare "Date:" must not be one of these other dates.
_NOT_INVOICE_DATE = {"due", "order", "po", "p.o", "delivery", "dispatch", "despatch", "supply", "challan", "ship",
                     "shipping", "lr", "ack", "payment", "expiry", "eway", "bill", "start", "end", "dc"}

_TOKEN_WITH_DIGIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\-_.]*\d[A-Za-z0-9/\-_]*|\d")


def _clean_token(tok: str) -> str:
    return tok.strip().strip(".,;:()[]")


def _find_labelled(rows: list[RowText], labels: list[str], parse: Callable[[str], Optional[tuple]],
                   guard: Optional[Callable[[RowText, re.Match], bool]] = None):
    """First label (in priority order) whose value parses. Returns (value, raw, words, row, label_text)."""
    for pat in labels:
        rx = re.compile(r"(?<![A-Za-z])" + pat + r"(?![A-Za-z])", re.I)
        for ri, rt in enumerate(rows):
            for m in rx.finditer(rt.text):
                if guard and not guard(rt, m):
                    continue
                rest = rt.text[m.end():]
                lead = re.match(_SEP, rest).end()
                start = m.end() + lead
                region_end = rt.text.find("\t", start)
                if region_end == -1:
                    region_end = len(rt.text)
                region = rt.text[start:region_end]
                parsed = parse(region)
                if parsed:
                    value, raw_start, raw_end = parsed
                    a, b = start + raw_start, start + raw_end
                    return value, rt.text[a:b], rt.words_in(a, b), rt.row, m.group(0).strip()
                # Stacked layout: the label stands alone in its cell and the value is printed
                # directly beneath it. Only tried when no colon follows the label, because a
                # label with a colon and nothing after it ("Invoice No. :") is a blank field,
                # and a blank field must stay blank.
                between = rest[:lead]
                alone = ":" not in between and (start >= len(rt.text) or "\t" in between)
                if alone and ri + 1 < len(rows) and rows[ri + 1].row.page == rt.row.page:
                    label_words = rt.words_in(m.start(), m.end())
                    below = rows[ri + 1].row
                    cell = next((c for c in below.cells if label_words and abs(c.x0 - label_words[0].x0) <= 14), None)
                    if cell is not None:
                        cell_rt = RowText(Row(page=below.page, cells=[cell]))
                        parsed = parse(cell_rt.text)
                        if parsed:
                            value, a, b = parsed
                            return value, cell_rt.text[a:b], cell_rt.words_in(a, b), below, m.group(0).strip()
    return None


# Words that can follow a blank "Invoice No. :" in the same cell and must not be read as a series prefix.
_NOT_A_PREFIX = {"date", "dated", "invoice", "bill", "order", "no", "number", "due", "gstin", "page", "total",
                 "amount", "ref", "dt", "of", "to", "for", "and", "the", "terms", "state", "code", "place", "mode",
                 "period", "customer", "vendor", "buyer", "from", "on", "at", "as", "per"}


def _parse_number_token(region: str):
    """An invoice or PO number at the very start of the value region.

    Accepts 'SPI/2026-27/1047', 'INV-2026-11-07' and a spaced series prefix ('INV 1047', 'A 1234').
    Rejects a value that is nothing but a date, and anything that does not begin the region.
    """
    lead = re.match(r"[\s.:#\-–—]*", region).end()
    body = region[lead:]
    prefix = ""
    pm = re.match(r"([A-Za-z]{1,8})\s+(?=\S*\d)", body)
    if pm and pm.group(1).lower() not in _NOT_A_PREFIX:
        prefix = pm.group(0)
    tm = _TOKEN_WITH_DIGIT.match(body[len(prefix):])
    if not tm:
        return None
    raw = (prefix + tm.group(0)).rstrip(".,;:)")
    value = re.sub(r"\s+", " ", raw).strip()
    if len(value) < 2 or len(value) > 32:
        return None
    _d, date_text, _amb = parse_date(value)
    if date_text and len(date_text) >= len(value) - 1:
        return None  # the whole value is a date, not a number
    return value, lead, lead + len(raw)


def _parse_date_value(region: str):
    d, raw, amb = parse_date(region)
    if not d:
        return None
    start = region.find(raw)
    if region[:start].strip(" .:#-–—"):
        return None  # the date is not the first thing after the label
    return (d, amb), start, start + len(raw)


def _date_guard(rt: RowText, m: re.Match) -> bool:
    label = m.group(0).lower().strip()
    if label in ("date", "dated"):
        before = rt.text[: m.start()].rstrip()
        prev = re.findall(r"[A-Za-z.]+", before[-18:])
        prev_word = prev[-1].lower().strip(".") if prev else ""
        if prev_word in _NOT_INVOICE_DATE:
            return False
        after = rt.text[m.end(): m.end() + 3]
        starts_cell = m.start() == 0 or rt.text[m.start() - 1] == "\t"
        own_cell = starts_cell and (m.end() == len(rt.text) or rt.text[m.end()] == "\t")
        if label == "date" and not own_cell and not re.match(r"\s*[:\-–]", after):
            return False  # "date" inside prose, not a label
        if label == "dated" and not starts_cell and prev_word not in ("invoice", "bill"):
            return False  # "…challan no. 4471 dated 02-Oct-2026" dates something else
    return True


def _due_guard(rt: RowText, m: re.Match) -> bool:
    if m.group(0).lower().strip() in ("due", "due on", "due by"):
        before = rt.text[: m.start()].rstrip().lower()
        if before.endswith(("total", "amount", "balance")):
            return False  # "Total due", "Amount due" are totals, not dates
    return True


# ----------------------------------------------------------- line items ---

_HEAD_DESC = re.compile(r"\b(description|particulars|items?|goods|services?|details|products?|materials?)\b", re.I)
_HEAD_QTY = re.compile(r"\b(qty|quantity|shipments|units|nos|hours|hrs)\b", re.I)
_HEAD_RATE = re.compile(r"\b(rate|price|unit\s*cost|mrp)\b", re.I)
_HEAD_AMT = re.compile(r"\b(amount|total|value)\b", re.I)

_SUBTOTAL_RE = re.compile(
    r"\b(sub[\s\-]*total|taxable\s*(?:value|amount|amt)|total\s*before\s*tax|amount\s*before\s*tax|net\s*amount|"
    r"basic\s*(?:amount|value)|total\s*excl\w*)\b",
    re.I,
)
_TOTAL_STRONG_RE = re.compile(
    r"\b(grand\s*total|total\s*amount(?:\s*payable)?|amount\s*payable|net\s*payable|total\s*payable|total\s*due|"
    r"balance\s*due|amount\s*due|bill\s*amount|invoice\s*total|(?:total\s*)?invoice\s*value|invoice\s*amount)\b",
    re.I,
)
_TOTAL_WEAK_RE = re.compile(r"(?<![A-Za-z\-])total\b", re.I)
_TAX_RE = re.compile(r"\b(cgst|sgst|igst|utgst|gst|vat|cess|service\s*tax|sales\s*tax|tax)(?=\d|\b)", re.I)
_RATE_RE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*%")
_INCLUSIVE_RE = re.compile(r"\b(inclusive\s+of|incl\.?|including)\s+(?:of\s+)?(?:all\s+)?(?:applicable\s+)?(gst|tax(?:es)?|vat|igst)\b",
                           re.I)
_END_OF_TABLE_RE = re.compile(
    r"^\s*(amount\s+(?:chargeable\s+)?\(?in\s+words|rupees\b|inr\s+[a-z]+\s|in\s+words|bank\b|declaration|terms\b|"
    r"e\.?\s*&\s*o\.?e|round(?:ed|ing)?\s*off)",
    re.I)
# A row whose only words are a tax name ("CGST", "Add: SGST 9%", "IGST (18%)") is a tax line, not an item.
_TAX_LABEL_ONLY = re.compile(
    r"^(?:add|less|output|input)?\s*(?:central|state|integrated|union)?\s*(?:cgst|sgst|igst|utgst|gst|vat|cess|tax)"
    r"(?:\s+(?:amount|payable|output|input))?$",
    re.I)


# Page furniture that can sit between the rows of a table running over several pages.
_NOT_AN_ITEM_RE = re.compile(r"\bpage\s*\d+\b|^\s*\(?(continued|contd\.?|carried\s+(forward|over)|brought\s+forward|c/f|b/f)\b",
                             re.I)
_MONTH_YEAR_END = re.compile(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s,'\-]*\d{4}$")
_DOC_TYPE_RE = re.compile(
    r"\b(credit\s+note|debit\s+note|pro\s*-?\s*forma(?:\s+invoice)?|quotation|estimate|delivery\s+challan|"
    r"purchase\s+order|statement\s+of\s+account|payment\s+receipt|receipt\s+voucher|sales\s+order)\b", re.I)


def _is_text_cell(cell) -> bool:
    """A description cell: it holds real words, not just a number with its unit."""
    words = [w for w in re.findall(r"[A-Za-z]{3,}", cell.text) if w.lower() not in UNIT_WORDS]
    return len(words) >= 2


def _alpha_text(flat: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]+", " ", flat)).strip()


def _is_header(row: Row) -> bool:
    text = row.flat
    if any(parse_amount(w.text) is not None and ("." in w.text or "," in w.text) for w in row.words):
        return False
    groups = [bool(rx.search(text)) for rx in (_HEAD_DESC, _HEAD_QTY, _HEAD_RATE, _HEAD_AMT)]
    return (groups[0] and groups[3]) or sum(groups) >= 3


def _numeric_words(row: Row, ocr: bool) -> list[tuple[Word, Decimal]]:
    out = []
    for w in row.words:
        val = parse_amount(w.text, ocr=ocr)
        if val is not None:
            out.append((w, val))
    return out


def _multiplies(a: Decimal, b: Decimal, target: Decimal) -> bool:
    return abs(a * b - target) <= max(Decimal("0.05"), abs(target) * Decimal("0.0005"))


def _row_amount(row: Row, amount_x0: Optional[float], ocr: bool) -> Optional[tuple[Word, Decimal]]:
    """The right-most number sitting in the amount column."""
    nums = _numeric_words(row, ocr)
    if amount_x0 is not None:
        nums = [(w, v) for w, v in nums if w.x1 >= amount_x0 - 2]
    return nums[-1] if nums else None


def _parse_items(doc: Document, rows: list[Row]) -> tuple[list[LineItem], Optional[int], Optional[float]]:
    """Returns (items, index of the first row after the table, x of the amount column)."""
    header_i = next((i for i, r in enumerate(rows) if _is_header(r)), None)
    if header_i is None:
        return [], None, None
    header = rows[header_i]
    amount_x0 = header.cells[-1].x0
    ocr = doc.page(header.page).method == "ocr"
    qty_head = next((w for w in header.words if _HEAD_QTY.search(w.text)), None)
    rate_head = next((w for w in header.words if _HEAD_RATE.search(w.text)), None)
    rate_first = bool(qty_head and rate_head and rate_head.x0 < qty_head.x0)
    # A table with only "Description | Amount" has no quantity to find: every line is a lump sum.
    has_qty_cols = bool(qty_head or rate_head)
    has_hsn_col = bool(re.search(r"\b(hsn|sac)\b", header.flat, re.I))
    items: list[LineItem] = []
    last_item_row: Optional[Row] = None
    end_i = len(rows)
    for i in range(header_i + 1, len(rows)):
        row = rows[i]
        flat = row.flat
        if _NOT_AN_ITEM_RE.search(flat) or _is_header(row):
            continue  # page footer, "carried forward", or the header repeated on the next page
        if _SUBTOTAL_RE.search(flat) or _TOTAL_STRONG_RE.search(flat) or _END_OF_TABLE_RE.search(flat) or (
            _TOTAL_WEAK_RE.search(flat) and _row_amount(row, amount_x0, ocr)
        ) or (_TAX_RE.search(flat) and _RATE_RE.search(flat) and _row_amount(row, amount_x0, ocr)) or (
            _TAX_LABEL_ONLY.match(_alpha_text(flat)) and _row_amount(row, amount_x0, ocr)
        ):
            end_i = i
            break
        amt = _row_amount(row, amount_x0, ocr)
        if amt is None:
            # The description continues on the next line. Only lines directly under the item count:
            # a letterhead repeated at the top of the next page is not part of anyone's description.
            letters = sum(ch.isalpha() for ch in flat)
            adjacent = (last_item_row is not None and row.page == last_item_row.page
                        and row.y0 - last_item_row.y1 <= 30)
            if items and adjacent and flat.strip().lower().strip(".") in UNIT_WORDS:
                items[-1].unit = items[-1].unit or flat.strip().lower().strip(".")
                last_item_row = row
            elif items and adjacent and letters >= 3:
                items[-1].description = (items[-1].description + " " + flat.replace("\t", " ")).strip()
                last_item_row = row
            continue
        amt_word, amount = amt
        before = [(w, v) for w, v in _numeric_words(row, ocr) if w.x1 < amt_word.x0] if has_qty_cols else []
        # numbers standing in their own columns, as opposed to sizes and years inside a description
        text_words = {id(w) for c in row.cells if _is_text_cell(c) for w in c.words}
        first = row.cells[0]
        if len(row.cells) > 2 and re.fullmatch(r"\d{1,3}[.)]?", first.text.strip()):
            text_words |= {id(w) for w in first.words}  # the serial number
        columns = [(w, v) for w, v in before if id(w) not in text_words]
        qty = price = None
        qty_word = None
        first_word = None  # left-most of the quantity/rate pair: where the description stops
        derived = False

        # Quantity x rate must reproduce the amount. That is what tells a quantity from an HSN
        # code, a box size or a year, and it works on layouts never seen before.
        left = right = None
        for j in range(len(before) - 1, 0, -1):
            k = next((k for k in range(j - 1, -1, -1) if _multiplies(before[k][1], before[j][1], amount)), None)
            if k is not None:
                left, right = before[k], before[j]
                break
        if left is None:
            # Per-line tax columns (… Qty | Rate | Taxable | GST % | GST Amt | Total): the product
            # equals the taxable value, a column to the left of the line total, and that value is
            # the line amount to compare with a pre-tax PO. Only numbers in their own columns are
            # considered, neighbours first, so sizes inside a description cannot pair up by chance.
            money = [t for t in range(2, len(columns)) if "." in columns[t][0].text or "," in columns[t][0].text]
            for t in money:
                if _multiplies(columns[t - 2][1], columns[t - 1][1], columns[t][1]):
                    left, right, amount = columns[t - 2], columns[t - 1], columns[t][1]
                    break
            if left is None:
                for t in money:
                    hit = next(((k, j) for j in range(t - 1, 0, -1) for k in range(j - 1, -1, -1)
                                if _multiplies(columns[k][1], columns[j][1], columns[t][1])), None)
                    if hit:
                        left, right, amount = columns[hit[0]], columns[hit[1]], columns[t][1]
                        break
        if left is None and len(columns) >= 2:
            # two numbers that do not multiply out: keep them as printed; validation will flag it
            left, right = columns[-2], columns[-1]
        if left is not None:
            first_word = left[0]
            # the header says which of the two is the quantity
            (qty_word, qty), (_w, price) = (right, left) if rate_first else (left, right)
        elif len(columns) == 1 and columns[0][1] > 0:
            cand_word, cand = columns[0]
            # a single number between description and amount: quantity, with the rate implied
            if cand == cand.to_integral_value() and "." not in cand_word.text:
                qty_word, qty, price, derived = cand_word, cand, q2(amount / cand), True
        first_word = first_word or qty_word
        limit_x = first_word.x0 if first_word else amt_word.x0
        desc_cells = [c for c in row.cells if c.x0 < limit_x and sum(ch.isalpha() for ch in c.text) >= 3]
        desc = " ".join(c.text for c in desc_cells).strip()
        # the cell may run on into the HSN/qty words when columns are tight: trim at the first number
        if first_word is not None and desc_cells:
            kept = [w.text for c in desc_cells for w in c.words if w.x1 <= first_word.x0]
            desc = " ".join(kept).strip()
        if has_hsn_col and not _MONTH_YEAR_END.search(desc):
            desc = re.sub(r"\s+\d{4,8}$", "", desc)  # an HSN/SAC code run into the description
        desc = re.sub(r"^\d{1,3}[.)]\s+(?=[A-Za-z])", "", desc)  # leading "1." serial number
        unit = None
        if qty_word is not None:
            idx = row.words.index(qty_word)
            if idx + 1 < len(row.words) and row.words[idx + 1].text.lower().strip(".") in UNIT_WORDS:
                unit = row.words[idx + 1].text.lower().strip(".")
        used = [w for w in row.words]
        items.append(LineItem(description=desc or flat.replace("\t", " "), qty=qty, unit=unit, unit_price=price,
                              amount=amount, confidence=_conf([amt_word] + ([qty_word] if qty_word else [])),
                              page=row.page, bbox=_bbox(used, doc, row.page), derived_price=derived))
        last_item_row = row
    return items, end_i, amount_x0


# -------------------------------------------------------------- vendor ---

def _vendor_name(doc: Document, rows: list[Row]) -> Optional[tuple[str, list[Word], Row]]:
    buyer = config.BUYER["name"].lower()
    buyer_short = config.BUYER["short_name"].lower()
    candidates = []
    for row in rows[:8]:
        cell = row.cells[0]
        text = cell.text.strip()
        if len(text) < 4 or DOC_TITLES.match(text):
            continue
        low = text.lower()
        if buyer_short in low or low in buyer:
            break  # we have reached the "bill to" block
        if re.search(r"\b(gstin|invoice|bill\s+no|date|phone|email|@)\b", low) or sum(c.isdigit() for c in text) > 3:
            continue
        letters = sum(c.isalpha() for c in text)
        if letters < 4:
            continue
        height = max(w.y1 - w.y0 for w in cell.words)
        score = height + (6 if COMPANY_HINT.search(text) else 0) - 0.4 * row.index
        candidates.append((score, text, cell.words, row))
    if candidates:
        candidates.sort(key=lambda c: -c[0])
        _, text, words, row = candidates[0]
        return text, words, row
    # fall back to the signature line: "For <Company>"
    for row in reversed(rows[-10:]):
        m = re.search(r"\bfor\s+([A-Z][\w&.,' \-]{3,60})", row.flat)
        if m and config.BUYER["short_name"].lower() not in m.group(1).lower():
            name = re.split(r"\s+-\s+|\s{2,}", m.group(1))[0].strip()
            return name, row.words, row
    return None


# ---------------------------------------------------------------- main ---

def extract(doc: Document) -> Extraction:
    ex = Extraction(method="rules")
    rows = doc.rows
    if not rows:
        return ex
    rts = [RowText(r) for r in rows]
    full_text = doc.text
    ocr = doc.is_ocr

    # ---- vendor identity
    buyer_gstin = config.BUYER["gstin"]
    for rt in rts:
        for m in GSTIN_RE.finditer(rt.text.upper()):
            if m.group(0) != buyer_gstin and not ex.vendor_gstin:
                ex.vendor_gstin = m.group(0)
                ex.meta["vendor_gstin"] = _meta(rt.words_in(m.start(), m.end()), doc, rt.row, m.group(0))
    v = _vendor_name(doc, rows)
    if v:
        ex.vendor_name = v[0]
        ex.meta["vendor_name"] = _meta(v[1], doc, v[2], v[0])

    # ---- what kind of document is this? A title, not a mention: short, and without numbers.
    for row in rows[:10]:
        for cell in row.cells:
            text = cell.text.strip()
            m = _DOC_TYPE_RE.search(text)
            if m and len(text.split()) <= 4 and not any(ch.isdigit() for ch in text):
                ex.doc_type = re.sub(r"\s+", " ", m.group(1)).title()
                break
        if ex.doc_type:
            break

    # ---- header fields
    hit = _find_labelled(rts, INVOICE_NO_LABELS, _parse_number_token)
    if hit:
        ex.invoice_number = hit[0]
        ex.meta["invoice_number"] = _meta(hit[2], doc, hit[3], hit[1], note=f'labelled "{hit[4]}"')

    hit = _find_labelled(rts, INVOICE_DATE_LABELS, _parse_date_value, guard=_date_guard)
    if hit:
        (d, amb), raw = hit[0], hit[1]
        ex.invoice_date = d.isoformat()
        ex.date_ambiguous = amb
        note = f'labelled "{hit[4]}"'
        if amb:
            note += "; numeric date read day-first"
        ex.meta["invoice_date"] = _meta(hit[2], doc, hit[3], raw, note=note)

    hit = _find_labelled(rts, DUE_DATE_LABELS, _parse_date_value, guard=_due_guard)
    if hit:
        ex.due_date = hit[0][0].isoformat()
        ex.meta["due_date"] = _meta(hit[2], doc, hit[3], hit[1], note=f'labelled "{hit[4]}"')

    hit = _find_labelled(rts, PO_LABELS, _parse_number_token)
    if hit:
        ex.po_number = hit[0].upper()
        label = hit[4]
        in_prose = bool(re.match(r"(?i)(against|as\s*per|vide|ref)", label))
        ex.po_reference_style = "in running text" if in_prose else "labelled"
        ex.meta["po_number"] = _meta(hit[2], doc, hit[3], hit[1], note=f'after "{label}"')
    else:
        for rt in rts:
            m = PO_SHAPE_RE.search(rt.text)
            if m:
                ex.po_number = re.sub(r"\s+", "", m.group(0)).upper()
                ex.po_reference_style = "in running text"
                ex.meta["po_number"] = _meta(rt.words_in(m.start(), m.end()), doc, rt.row, m.group(0),
                                             note="PO-shaped reference found in body text")
                break

    # ---- line items
    items, end_i, amount_x0 = _parse_items(doc, rows)
    ex.line_items = items

    # ---- amount in words (read first: it arbitrates between candidate totals below)
    words_value = None
    for row in rows:
        m = re.search(r"\b(?:rupees?|inr|rs\.?)\b(.+)", row.flat, re.I)
        if not m or re.search(r"\btax\s+amount\b", row.flat, re.I):
            continue
        text = m.group(0)
        used_words = list(row.words)
        # "... Sixty Eight Thousand" / "Thirty Two Only": keep reading while the next line is
        # nothing but number words and the sentence has not reached "Only".
        nxt = row.index + 1
        while not re.search(r"\bonly\b", text, re.I) and nxt < len(rows) and nxt <= row.index + 2 \
                and rows[nxt].page == row.page and is_number_words(rows[nxt].flat):
            text += " " + rows[nxt].flat
            used_words += rows[nxt].words
            nxt += 1
        value = words_to_number(text)
        if value is not None and value > 0:
            words_value = value
            ex.amount_in_words = re.sub(r"\s+", " ", text).strip(" .")
            ex.amount_in_words_complete = bool(re.search(r"\bonly\b", text, re.I))
            ex.meta["amount_in_words"] = _meta(used_words, doc, row, ex.amount_in_words)
            break

    # ---- totals block
    summary_rows = rows[end_i:] if end_i is not None else rows
    strong_totals: list[tuple] = []
    weak_totals: list[tuple] = []
    last_tax_i = None
    tax_summary = None
    for si, row in enumerate(summary_rows):
        flat = row.flat
        amt = _row_amount(row, amount_x0, ocr) if amount_x0 is not None else None
        if amt is None:
            nums = [(w, val) for w, val in _numeric_words(row, ocr) if "." in w.text or "," in w.text]
            amt = nums[-1] if nums else None
        if amt is None:
            continue
        word, value = amt
        if _SUBTOTAL_RE.search(flat):
            if ex.subtotal is None:
                ex.subtotal = value
                ex.meta["subtotal"] = _meta([word], doc, row, word.text)
            continue
        if _TOTAL_STRONG_RE.search(flat):
            strong_totals.append((value, word, row, si))
            continue
        if _TAX_RE.search(flat) and not _INCLUSIVE_RE.search(flat) and _TOTAL_WEAK_RE.search(flat):
            tax_summary = (value, word, row)  # "Total Tax", "Total GST": the sum of the tax lines
            last_tax_i = si
            continue
        if _TAX_RE.search(flat) and not _INCLUSIVE_RE.search(flat):
            label = _TAX_RE.search(flat).group(1).upper()
            rate = _RATE_RE.search(flat)
            ex.tax_lines.append({"label": label, "rate": D(rate.group(1)) if rate else None, "amount": value,
                                 "bbox": _bbox([word], doc, row.page), "page": row.page})
            last_tax_i = si
            continue
        if _TOTAL_WEAK_RE.search(flat):
            weak_totals.append((value, word, row, si))
    # Which row is *the* total? A page can say "Total" several times (items total, tax summary).
    #   1. the candidate that equals the amount in words, if there is one;
    #   2. else an explicit label ("Grand Total", "Amount Payable"): the last one printed;
    #   3. else the first plain "Total" after the tax lines, or the last one if none follows them.
    chosen = None
    if words_value is not None:
        chosen = next((c for c in strong_totals + weak_totals if abs(c[0] - words_value) < 1), None)
    if chosen is None and strong_totals:
        chosen = strong_totals[-1]
    if chosen is None and weak_totals:
        after_tax = [c for c in weak_totals if last_tax_i is not None and c[3] > last_tax_i]
        chosen = after_tax[0] if after_tax else weak_totals[-1]
    if chosen:
        ex.total = chosen[0]
        ex.meta["total"] = _meta([chosen[1]], doc, chosen[2], chosen[1].text)
    if not ex.tax_lines and tax_summary is not None:
        ex.tax_lines.append({"label": "TAX", "rate": None, "amount": tax_summary[0],
                             "bbox": _bbox([tax_summary[1]], doc, tax_summary[2].page), "page": tax_summary[2].page})
    if ex.tax_lines:
        ex.tax_amount = sum((t["amount"] for t in ex.tax_lines), Decimal("0"))
        rates = [t["rate"] for t in ex.tax_lines if t["rate"] is not None]
        if rates and len(rates) == len(ex.tax_lines):
            ex.tax_rate = sum(rates, Decimal("0"))
        first = ex.tax_lines[0]
        ex.meta["tax_amount"] = FieldMeta(confidence=0.99 if not ocr else 0.9, raw="", page=first["page"],
                                          bbox=first["bbox"])

    # ---- tax included in the total rather than shown separately
    inc = _INCLUSIVE_RE.search(full_text)
    if inc and not ex.tax_lines:
        ex.tax_inclusive = True
        window = full_text[max(0, inc.start() - 40): inc.end() + 40]
        rate = _RATE_RE.search(window)
        if rate:
            ex.tax_rate = D(rate.group(1))

    # ---- bank details
    for rt in rts:
        m = re.search(r"(?:a/c|acc(?:oun)?t)\.?\s*(?:no|num(?:ber)?)?\.?\s*[:\-]?\s*(\d[\d ]{6,24}\d)", rt.text, re.I)
        if m and not ex.bank_account:
            ex.bank_account = re.sub(r"\D", "", m.group(1))
            ex.meta["bank_account"] = _meta(rt.words_in(m.start(1), m.end(1)), doc, rt.row, m.group(1))
        m = IFSC_RE.search(rt.text)
        if m and not ex.ifsc:
            ex.ifsc = m.group(0)

    # ---- currency and service period
    if re.search(r"\b(usd|us\$)\b|\$", full_text, re.I) and not re.search(r"\b(inr|rs\.?|rupees)\b|₹", full_text, re.I):
        ex.currency = "USD"
    elif re.search(r"\b(eur)\b|€", full_text, re.I) and not re.search(r"\b(inr|rs\.?|rupees)\b|₹", full_text, re.I):
        ex.currency = "EUR"
    else:
        ex.currency = config.BUYER["currency"]
    period_text = " ".join(li.description for li in ex.line_items)
    for row in rows:
        if re.search(r"\b(period|for\s+the\s+month|month\s+of)\b", row.flat, re.I):
            period_text += " " + row.flat
    ex.periods = sorted(find_periods(period_text))
    return ex
