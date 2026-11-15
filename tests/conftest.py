"""Test setup: an isolated database, a fixed 'today', and a parameterised invoice builder."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before any app module is imported: paths are resolved at import time.
_TMP = tempfile.mkdtemp(prefix="invoice-desk-tests-")
os.environ["INVOICE_DESK_DATA"] = _TMP
os.environ["INVOICE_DESK_TODAY"] = "2026-11-16"
os.environ["INVOICE_DESK_AI"] = "off"

import pytest  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app import config, db, seed  # noqa: E402
from app.money import number_to_words_indian  # noqa: E402
from app.pipeline.runner import run_pipeline  # noqa: E402

W, H = A4

SHREEJI = dict(vendor="Shreeji Packaging Industries", gstin="29AAFCS8842K1Z6", bank="50200048213377", ifsc="HDFC0001234")
PO_0412_LINES = [
    ("5-ply corrugated shipping box 18 x 12 x 10 in", 2000, "pcs", 42.50),
    ("BOPP packaging tape 48 mm x 65 m", 300, "rolls", 38.00),
    ("Bubble wrap roll 1 m x 100 m", 40, "rolls", 1150.00),
]


def _inr(x: float) -> str:
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{whole}.{frac}"


def make_invoice(path: Path, *, vendor: str, gstin: str | None, number: str | None = "T/2026/0001",
                 date: str | None = "10-Nov-2026", po: str | None = None, lines=(), tax: str = "split",
                 tax_rate: float = 18.0, bank: str | None = None, ifsc: str | None = None, words: str | bool = True,
                 line_amounts: dict | None = None, total: float | None = None, lump: tuple | None = None) -> float:
    """A plain tax invoice. Any element can be altered or left out to build a specific scenario.

    tax: "split" (CGST+SGST), "igst", or "inclusive" (tax buried in the total; use ``lump``).
    lump: (description, gross_amount) for a single bundled line with no quantity or rate.
    """
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(W / 2, H - 38, "TAX INVOICE")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, H - 64, vendor)
    c.setFont("Helvetica", 8.5)
    c.drawString(40, H - 79, "Industrial Estate, Bengaluru 560058, Karnataka")
    if gstin:
        c.drawString(40, H - 91, f"GSTIN: {gstin}")
    y = H - 120
    c.setFont("Helvetica", 9)
    if number:
        c.drawString(40, y, f"Invoice No. : {number}")
    if po:
        c.drawString(330, y, f"PO No. : {po}")
    y -= 15
    if date:
        c.drawString(40, y, f"Invoice Date : {date}")
    y -= 24
    c.drawString(40, y, "Bill To")
    y -= 13
    c.drawString(40, y, config.BUYER["name"])
    y -= 12
    c.drawString(40, y, f"GSTIN: {config.BUYER['gstin']}")
    y -= 28

    c.setFont("Helvetica-Bold", 8.5)
    if lump:
        c.drawString(46, y, "Description")
        c.drawRightString(W - 46, y, "Amount (INR)")
        y -= 20
        c.setFont("Helvetica", 9)
        c.drawString(46, y, lump[0])
        c.drawRightString(W - 46, y, _inr(lump[1]))
        y -= 26
        grand = lump[1]
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(46, y, f"Total amount payable (inclusive of GST @ {tax_rate:g}%)")
        c.drawRightString(W - 46, y, _inr(grand))
        y -= 20
    else:
        c.drawString(46, y, "Sl")
        c.drawString(70, y, "Description of Goods")
        c.drawRightString(398, y, "Qty")
        c.drawString(408, y, "Unit")
        c.drawRightString(480, y, "Rate (INR)")
        c.drawRightString(W - 46, y, "Amount (INR)")
        y -= 20
        c.setFont("Helvetica", 9)
        subtotal = 0.0
        for i, (desc, q, unit, rate) in enumerate(lines, start=1):
            amount = (line_amounts or {}).get(i, round(q * rate, 2))
            subtotal += round(q * rate, 2)
            c.drawString(46, y, str(i))
            c.drawString(70, y, desc)
            c.drawRightString(398, y, f"{q:,}")
            c.drawString(408, y, unit)
            c.drawRightString(480, y, _inr(rate))
            c.drawRightString(W - 46, y, _inr(amount))
            y -= 17
        y -= 10
        tax_amount = round(subtotal * tax_rate / 100, 2)
        rows = [("Taxable Value", subtotal)]
        if tax == "split":
            half = round(tax_amount / 2, 2)
            rows += [(f"CGST @ {tax_rate / 2:g}%", half), (f"SGST @ {tax_rate / 2:g}%", half)]
        else:
            rows += [(f"IGST @ {tax_rate:g}%", tax_amount)]
        grand = total if total is not None else round(subtotal + tax_amount, 2)
        rows.append(("Grand Total", grand))
        for label, val in rows:
            c.drawString(372, y, label)
            c.drawRightString(W - 46, y, _inr(val))
            y -= 15
    y -= 6
    c.setFont("Helvetica", 8.5)
    if words:
        text = words if isinstance(words, str) else f"Rupees {number_to_words_indian(int(round(grand)))} Only"
        c.drawString(40, y, f"Amount in words: {text}")
        y -= 22
    if bank:
        c.drawString(40, y, f"Bank Details: A/c No. {bank}     IFSC: {ifsc or ''}")
    c.save()
    return grand


class Result:
    def __init__(self, run: dict, events: list[dict]):
        self.run = run
        self.events = events
        self.decision = run["decision"]
        self.result = run["result"]["decision"] if run.get("result") else None
        self.stages = {e["stage"]: e["payload"] for e in events if e["type"] == "stage_completed"}
        self.checks = [c for p in self.stages.values() for c in p["checks"]]
        self.codes = {c["code"] for c in self.checks if c["code"]}

    def check(self, code: str) -> dict:
        return next(c for c in self.checks if c["code"] == code)


def process(path: Path, sample_key: str | None = None) -> Result:
    run = db.create_run(Path(path).name, str(path), "sample" if sample_key else "upload", sample_key, 0)
    run_pipeline(run["id"])
    return Result(db.get_run(run["id"]), db.get_events(run["id"]))


def process_sample(key: str) -> Result:
    s = next(x for x in seed.SAMPLES if x["key"] == key)
    return process(config.SAMPLES_DIR / s["file"], key)


@pytest.fixture(autouse=True)
def fresh_demo(monkeypatch):
    monkeypatch.setenv("INVOICE_DESK_AI", "off")
    seed.reset_demo()
    yield


@pytest.fixture
def pdf(tmp_path):
    """Build an invoice PDF in a temp folder and return its path."""
    def _build(name: str = "invoice.pdf", **kw) -> Path:
        path = tmp_path / name
        make_invoice(path, **kw)
        return path
    return _build


def make_tally_invoice(path: Path, *, number: str = "SPI/2026-27/4001", dated: str = "9-Nov-26",
                       po: str = "PO-2026-0412", lines=PO_0412_LINES) -> float:
    """The layout Tally prints, which a large share of Indian invoices use.

    Three things make it harder than the demo layouts: values sit *under* their labels
    rather than beside them; the tax rows and the total live inside the item table with no
    rate printed; and an HSN summary underneath repeats the word "Total" next to the tax
    amount, not the invoice amount.
    """
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(W / 2, H - 36, "Tax Invoice")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, H - 60, "Shreeji Packaging Industries")
    c.setFont("Helvetica", 8)
    c.drawString(40, H - 72, "Plot 14, KIADB Industrial Area, Peenya, Bengaluru")
    c.drawString(40, H - 83, "GSTIN/UIN: 29AAFCS8842K1Z6")
    c.drawString(40, H - 94, "State Name : Karnataka, Code : 29")
    # right-hand grid: label on one row, value on the next
    for x, label, value, y in [(310, "Invoice No.", number, H - 60), (440, "Dated", dated, H - 60),
                               (310, "Delivery Note", "", H - 88), (440, "Mode/Terms of Payment", "30 Days", H - 88),
                               (310, "Buyer's Order No.", po, H - 116), (440, "Dated", "6-Oct-26", H - 116)]:
        c.setFont("Helvetica", 7.5)
        c.drawString(x, y, label)
        c.setFont("Helvetica-Bold", 8.5)
        if value:
            c.drawString(x, y - 11, value)
    c.setFont("Helvetica", 7.5)
    c.drawString(40, H - 116, "Buyer (Bill to)")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, H - 128, config.BUYER["name"])
    c.setFont("Helvetica", 8)
    c.drawString(40, H - 139, f"GSTIN/UIN : {config.BUYER['gstin']}")

    y = H - 172
    c.setFont("Helvetica", 8)
    c.drawString(42, y, "Sl No.")
    c.drawString(80, y, "Description of Goods")
    c.drawString(300, y, "HSN/SAC")
    c.drawRightString(400, y, "Quantity")
    c.drawRightString(450, y, "Rate")
    c.drawString(460, y, "per")
    c.drawRightString(W - 42, y, "Amount")
    y -= 18
    subtotal = 0.0
    total_qty = 0
    for i, (desc, q, unit, rate) in enumerate(lines, start=1):
        amount = round(q * rate, 2)
        subtotal += amount
        total_qty += q
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(42, y, str(i))
        c.drawString(80, y, desc)
        c.setFont("Helvetica", 8.5)
        c.drawString(300, y, "4819")
        c.drawRightString(400, y, f"{q:,} {unit}")
        c.drawRightString(450, y, _inr(rate))
        c.drawString(460, y, unit)
        c.drawRightString(W - 42, y, _inr(amount))
        y -= 15
    cgst = sgst = round(subtotal * 0.09, 2)
    total = round(subtotal + cgst + sgst, 2)
    y -= 6
    c.setFont("Helvetica-BoldOblique", 8.5)
    for label, val in [("CGST", cgst), ("SGST", sgst)]:
        c.drawRightString(290, y, label)
        c.drawRightString(W - 42, y, _inr(val))
        y -= 14
    y -= 4
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(290, y, "Total")
    c.drawRightString(400, y, f"{total_qty:,}")
    c.drawRightString(W - 42, y, _inr(total))
    y -= 20
    c.setFont("Helvetica", 7.5)
    c.drawString(42, y, "Amount Chargeable (in words)")
    c.drawRightString(W - 42, y, "E. & O.E")
    y -= 12
    c.setFont("Helvetica-Bold", 9)
    c.drawString(42, y, f"INR {number_to_words_indian(int(round(total)))} Only")
    # HSN summary: a second table with its own "Total" row
    y -= 26
    c.setFont("Helvetica", 7.5)
    c.drawString(42, y, "HSN/SAC")
    c.drawRightString(250, y, "Taxable Value")
    c.drawRightString(340, y, "Central Tax Amount")
    c.drawRightString(440, y, "State Tax Amount")
    c.drawRightString(W - 42, y, "Total Tax Amount")
    y -= 13
    c.setFont("Helvetica", 8)
    c.drawString(42, y, "4819")
    c.drawRightString(250, y, _inr(subtotal))
    c.drawRightString(340, y, _inr(cgst))
    c.drawRightString(440, y, _inr(sgst))
    c.drawRightString(W - 42, y, _inr(cgst + sgst))
    y -= 13
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(120, y, "Total")
    c.drawRightString(250, y, _inr(subtotal))
    c.drawRightString(340, y, _inr(cgst))
    c.drawRightString(440, y, _inr(sgst))
    c.drawRightString(W - 42, y, _inr(cgst + sgst))
    y -= 16
    c.setFont("Helvetica", 7.5)
    c.drawString(42, y, f"Tax Amount (in words) : INR {number_to_words_indian(int(round(cgst + sgst)))} Only")
    y -= 24
    c.drawString(42, y, "Company's Bank Details")
    y -= 11
    c.drawString(42, y, "A/c No. : 50200048213377      Branch & IFS Code : Peenya & HDFC0001234")
    c.drawRightString(W - 42, 70, "for Shreeji Packaging Industries")
    c.drawRightString(W - 42, 46, "Authorised Signatory")
    c.save()
    return total


def make_zoho_style_invoice(path: Path) -> float:
    """Accounting-software layout: 'Invoice#', 'P.O.#', 'CGST9 (9%)', 'Balance Due', words with 'Indian Rupee'."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, H - 56, "Shreeji Packaging Industries")
    c.setFont("Helvetica", 8.5)
    c.drawString(40, H - 70, "Peenya 2nd Stage, Bengaluru 560058")
    c.drawString(40, H - 82, "GSTIN 29AAFCS8842K1Z6")
    c.setFont("Helvetica", 20)
    c.drawRightString(W - 40, H - 60, "TAX INVOICE")
    c.setFont("Helvetica", 9)
    for i, (label, value) in enumerate([("Invoice#", "SPI/2026-27/4100"), ("Invoice Date", "09/11/2026"),
                                        ("Terms", "Net 30"), ("Due Date", "09/12/2026"), ("P.O.#", "PO-2026-0412")]):
        y = H - 110 - i * 14
        c.drawString(40, y, label)
        c.drawString(130, y, f": {value}")
    c.drawString(40, H - 196, "Bill To")
    c.drawString(40, H - 209, config.BUYER["name"])
    c.drawString(40, H - 221, f"GSTIN {config.BUYER['gstin']}")
    y = H - 256
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(40, y, "#")
    c.drawString(60, y, "Item & Description")
    c.drawRightString(380, y, "Qty")
    c.drawRightString(460, y, "Rate")
    c.drawRightString(W - 40, y, "Amount")
    c.setFont("Helvetica", 9)
    subtotal = 0.0
    for i, (desc, q, unit, rate) in enumerate(PO_0412_LINES, start=1):
        y -= 22
        subtotal += q * rate
        c.drawString(40, y, str(i))
        c.drawString(60, y, desc)
        c.drawRightString(380, y, f"{q:,.2f}")
        c.drawRightString(460, y, f"{rate:,.2f}")
        c.drawRightString(W - 40, y, f"{q * rate:,.2f}")
        c.setFont("Helvetica", 7.5)
        c.drawString(360, y - 9, unit)
        c.setFont("Helvetica", 9)
    half = round(subtotal * 0.09, 2)
    total = round(subtotal + 2 * half, 2)
    y -= 34
    for label, val in [("Sub Total", f"{subtotal:,.2f}"), ("CGST9 (9%)", f"{half:,.2f}"), ("SGST9 (9%)", f"{half:,.2f}"),
                       ("Total", f"₹{total:,.2f}".replace("₹", "Rs.")), ("Balance Due", f"Rs.{total:,.2f}")]:
        c.drawRightString(460, y, label)
        c.drawRightString(W - 40, y, val)
        y -= 16
    y -= 4
    c.drawString(40, y, f"Total In Words: Indian Rupee {number_to_words_indian(int(round(total)))} Only")
    c.save()
    return total


def make_gst_columns_invoice(path: Path) -> float:
    """Each line carries its own tax columns, and Rate is printed before Qty:
    Description | HSN | Rate | Qty | Taxable | GST % | GST Amt | Total"""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(36, H - 56, "Shreeji Packaging Industries")
    c.setFont("Helvetica", 8.5)
    c.drawString(36, H - 70, "GSTIN: 29AAFCS8842K1Z6")
    c.drawString(36, H - 96, "Invoice Number: SPI/2026-27/4200")
    c.drawString(300, H - 96, "Invoice Date: 09 Nov 2026")
    c.drawString(36, H - 110, "Purchase Order: PO-2026-0412")
    c.drawString(36, H - 136, f"Billed to: {config.BUYER['name']}, GSTIN {config.BUYER['gstin']}")
    y = H - 170
    cols = [36, 262, 322, 370, 432, 462, 512, W - 36]
    c.setFont("Helvetica-Bold", 8)
    c.drawString(cols[0], y, "Description")
    for x, label in zip(cols[1:], ["HSN", "Rate", "Qty", "Taxable", "GST %", "GST Amt", "Total"]):
        c.drawRightString(x, y, label)
    c.setFont("Helvetica", 8)
    taxable_sum = tax_sum = 0.0
    for desc, q, _unit, rate in PO_0412_LINES:
        y -= 16
        taxable = q * rate
        gst = round(taxable * 0.18, 2)
        taxable_sum += taxable
        tax_sum += gst
        c.drawString(cols[0], y, desc)
        for x, text in zip(cols[1:], ["4819", _inr(rate), f"{q:,}", _inr(taxable), "18", _inr(gst), _inr(taxable + gst)]):
            c.drawRightString(x, y, text)
    y -= 26
    total = round(taxable_sum + tax_sum, 2)
    for label, val in [("Taxable Amount", taxable_sum), ("IGST @ 18%", tax_sum), ("Invoice Total", total)]:
        c.drawRightString(462, y, label)
        c.drawRightString(W - 36, y, _inr(val))
        y -= 14
    c.drawString(36, y - 10, f"Rupees {number_to_words_indian(int(round(total)))} Only")
    c.save()
    return total


def make_freeform(path: Path, rows, *, size: float = 9, line_height: float = 16, top: float = 56) -> None:
    """Draw arbitrary text at chosen x positions: a terse way to build one-off layouts in tests.

    ``rows`` is a list. Each entry is a list of cells ``(x, text)`` or ``(x, text, "r")`` (right-aligned
    at x), drawn on one line; ``""`` leaves a blank line; ``None`` starts a new page.
    """
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", size)
    y = H - top
    for row in rows:
        if row is None:
            c.showPage()
            c.setFont("Helvetica", size)
            y = H - top
            continue
        for cell in row or []:
            x, text = cell[0], cell[1]
            if len(cell) > 2 and cell[2] == "r":
                c.drawRightString(x, y, text)
            else:
                c.drawString(x, y, text)
        y -= line_height
    c.save()


R = W - 46  # right edge used for amount columns in freeform layouts


def shreeji_head(number="SPI/2026-27/6001", date="10-Nov-2026", po="PO-2026-0412"):
    """Letterhead and header rows shared by the freeform Shreeji invoices."""
    rows = [[(40, "Shreeji Packaging Industries")], [(40, "Peenya 2nd Stage, Bengaluru 560058")],
            [(40, "GSTIN: 29AAFCS8842K1Z6")], "",
            [(40, f"Invoice No. : {number}")] + ([(330, f"PO No. : {po}")] if po else [])]
    if date:
        rows.append([(40, f"Invoice Date : {date}")])
    rows += ["", [(40, f"Bill To: {config.BUYER['name']}, GSTIN {config.BUYER['gstin']}")], ""]
    return rows


def item_rows(lines=PO_0412_LINES):
    rows = [[(46, "Sl"), (70, "Description of Goods"), (398, "Qty", "r"), (408, "Unit"), (480, "Rate", "r"),
             (R, "Amount", "r")]]
    for i, (desc, q, unit, rate) in enumerate(lines, start=1):
        rows.append([(46, str(i)), (70, desc), (398, f"{q:,}", "r"), (408, unit), (480, _inr(rate), "r"),
                     (R, _inr(q * rate), "r")])
    return rows
