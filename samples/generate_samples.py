"""Generate the demo invoice PDFs.

Five vendors, five different layouts, the way a real AP inbox looks:

  01  Shreeji Packaging   classic GST tax invoice, CGST + SGST split, Indian digit grouping
  02  Nexa IT             modern layout, "Invoice #", long dates, IGST, western grouping
  03  Orbit Print         billing-software look, "Bill No", dotted dates, PO buried in a sentence
  04  Orbit Print         same vendor, final part-shipment
  05  GreenLeaf           letterhead that has been printed, stamped and SCANNED (image only)
  06  Shreeji Packaging   a paid invoice re-issued with a new date and a reformatted number
  07  Swiftline           invoice number and date left blank

Run:  python samples/generate_samples.py
"""
from __future__ import annotations

import io
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402
from PIL import Image, ImageEnhance, ImageFilter  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.config import BUYER  # noqa: E402
from app.money import number_to_words_indian  # noqa: E402

OUT = ROOT / "samples" / "invoices"
W, H = A4  # 595.27 x 841.89 pt


def inr(x: float) -> str:
    """Indian grouping: 1,68,032.00"""
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


def intl(x: float) -> str:
    return f"{x:,.2f}"


def qty(x) -> str:
    return f"{x:,}"


def words(total: float) -> str:
    return f"Rupees {number_to_words_indian(int(round(total)))} Only"


# =========================================================== 01 / 06 Shreeji ===

def shreeji(path: Path, *, number: str, inv_date: str, due_date: str, po: str, po_date: str, lines, note: str = ""):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Tax Invoice {number}")
    c.setAuthor("Shreeji Packaging Industries")
    navy = colors.HexColor("#1F2F4F")

    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawCentredString(W / 2, H - 38, "TAX INVOICE")
    c.setFont("Helvetica", 7.5)
    c.drawRightString(W - 40, H - 38, "(Original for Recipient)")

    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(40, H - 66, "SHREEJI PACKAGING INDUSTRIES")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 8.5)
    c.drawString(40, H - 81, "Plot 14, KIADB Industrial Area, Peenya 2nd Stage, Bengaluru 560058, Karnataka")
    c.drawString(40, H - 93, "GSTIN: 29AAFCS8842K1Z6     State Code: 29     Phone: 080-5550 0142")
    c.setStrokeColor(navy)
    c.setLineWidth(1.2)
    c.line(40, H - 102, W - 40, H - 102)

    # header grid: two label/value columns
    y = H - 122
    rows = [
        ("Invoice No.", number, "Buyer's Order No.", po),
        ("Invoice Date", inv_date, "Order Date", po_date),
        ("Due Date", due_date, "Payment Terms", "30 days"),
    ]
    for l1, v1, l2, v2 in rows:
        c.setFont("Helvetica", 9)
        c.drawString(40, y, l1)
        c.drawString(118, y, ":")
        c.setFont("Helvetica-Bold", 9)
        c.drawString(126, y, v1)
        c.setFont("Helvetica", 9)
        c.drawString(330, y, l2)
        c.drawString(424, y, ":")
        c.setFont("Helvetica-Bold", 9)
        c.drawString(432, y, v2)
        y -= 15

    y -= 8
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(40, y, "Bill To")
    c.drawString(330, y, "Ship To")
    c.setFont("Helvetica", 8.5)
    y -= 13
    c.drawString(40, y, BUYER["name"])
    c.drawString(330, y, "Aurelia Fulfilment Centre")
    y -= 12
    c.drawString(40, y, "4th Floor, Prestige Tech Park, Outer Ring Road")
    c.drawString(330, y, "Survey No. 71, Hoskote Industrial Area")
    y -= 12
    c.drawString(40, y, "Bengaluru 560103, Karnataka")
    c.drawString(330, y, "Bengaluru Rural 562114")
    y -= 12
    c.drawString(40, y, f"GSTIN: {BUYER['gstin']}")

    # items table
    y -= 24
    cols = {"sl": 46, "desc": 70, "hsn": 330, "qty": 398, "unit": 408, "rate": 480, "amt": W - 46}
    c.setFillColor(colors.HexColor("#E9EDF4"))
    c.rect(40, y - 6, W - 80, 18, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(cols["sl"], y, "Sl")
    c.drawString(cols["desc"], y, "Description of Goods")
    c.drawString(cols["hsn"] - 16, y, "HSN")
    c.drawRightString(cols["qty"], y, "Qty")
    c.drawString(cols["unit"], y, "Unit")
    c.drawRightString(cols["rate"], y, "Rate (INR)")
    c.drawRightString(cols["amt"], y, "Amount (INR)")
    y -= 20
    subtotal = 0.0
    c.setFont("Helvetica", 9)
    for i, (desc, hsn, q, unit, rate) in enumerate(lines, start=1):
        amount = round(q * rate, 2)
        subtotal += amount
        c.drawString(cols["sl"], y, str(i))
        c.drawString(cols["desc"], y, desc)
        c.drawString(cols["hsn"] - 16, y, hsn)
        c.drawRightString(cols["qty"], y, qty(q))
        c.drawString(cols["unit"], y, unit)
        c.drawRightString(cols["rate"], y, inr(rate))
        c.drawRightString(cols["amt"], y, inr(amount))
        y -= 17
    c.setStrokeColor(colors.HexColor("#9AA3B2"))
    c.setLineWidth(0.6)
    c.line(40, y + 6, W - 40, y + 6)

    cgst = round(subtotal * 0.09, 2)
    sgst = round(subtotal * 0.09, 2)
    total = round(subtotal + cgst + sgst, 2)
    y -= 12
    for label, val, bold in [("Taxable Value", subtotal, False), ("CGST @ 9%", cgst, False),
                             ("SGST @ 9%", sgst, False), ("Grand Total", total, True)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.5 if bold else 9)
        c.drawString(372, y, label)
        c.drawRightString(cols["amt"], y, inr(val))
        y -= 15
    y -= 6
    c.setFont("Helvetica", 8.5)
    c.drawString(40, y, f"Amount in words: {words(total)}")
    y -= 24
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(40, y, "Bank Details")
    c.setFont("Helvetica", 8.5)
    y -= 12
    c.drawString(40, y, "HDFC Bank, Peenya Branch     A/c No. 50200048213377     IFSC: HDFC0001234")
    if note:
        y -= 20
        c.setFont("Helvetica-Oblique", 8.5)
        c.drawString(40, y, note)
    c.setFont("Helvetica", 8)
    c.drawString(40, 92, "Declaration: We declare that this invoice shows the actual price of the goods described")
    c.drawString(40, 81, "and that all particulars are true and correct.")
    c.setFont("Helvetica-Bold", 8.5)
    c.drawRightString(W - 40, 92, "For Shreeji Packaging Industries")
    c.setFont("Helvetica", 8)
    c.drawRightString(W - 40, 56, "Authorised Signatory")
    c.save()
    return total


# ================================================================ 02 Nexa IT ===

def nexa(path: Path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("Invoice NXT/2026/3381")
    c.setAuthor("Nexa IT Solutions LLP")
    teal = colors.HexColor("#0F766E")
    grey = colors.HexColor("#6B7280")

    c.setFillColor(teal)
    c.rect(0, H - 14, W, 14, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#111827"))
    c.setFont("Helvetica-Bold", 16)
    c.drawString(44, H - 58, "NEXA IT SOLUTIONS LLP")
    c.setFont("Helvetica", 8.5)
    c.setFillColor(grey)
    c.drawString(44, H - 73, "Unit 502, Cyber One, Sector 30A, Vashi, Navi Mumbai 400703")
    c.drawString(44, H - 85, "GSTIN 27AAQFN3391P1ZV  -  billing@nexait.example")

    c.setFillColor(teal)
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - 44, H - 60, "INVOICE")
    c.setFillColor(colors.HexColor("#111827"))
    c.setFont("Helvetica", 9)
    c.drawRightString(W - 44, H - 80, "Invoice # NXT/2026/3381")
    c.drawRightString(W - 44, H - 94, "Issued: November 6, 2026")
    c.drawRightString(W - 44, H - 108, "Due: December 21, 2026")
    c.drawRightString(W - 44, H - 122, "Customer PO Ref: PO-2026-0398")

    y = H - 158
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(grey)
    c.drawString(44, y, "BILLED TO")
    c.drawString(300, y, "DELIVERED TO")
    c.setFillColor(colors.HexColor("#111827"))
    c.setFont("Helvetica", 9)
    y -= 14
    c.drawString(44, y, BUYER["name"])
    c.drawString(300, y, "Aurelia Home & Living - IT Stores")
    y -= 12
    c.drawString(44, y, "4th Floor, Prestige Tech Park, Outer Ring Road")
    c.drawString(300, y, "4th Floor, Prestige Tech Park")
    y -= 12
    c.drawString(44, y, "Bengaluru 560103")
    c.drawString(300, y, "Bengaluru 560103")
    y -= 12
    c.drawString(44, y, f"GSTIN {BUYER['gstin']}")

    y -= 34
    c.setStrokeColor(colors.HexColor("#D1D5DB"))
    c.setLineWidth(0.8)
    c.line(44, y + 14, W - 44, y + 14)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(grey)
    c.drawString(44, y, "ITEM")
    c.drawRightString(352, y, "QTY")
    c.drawRightString(452, y, "UNIT PRICE")
    c.drawRightString(W - 44, y, "LINE TOTAL")
    c.line(44, y - 7, W - 44, y - 7)
    c.setFillColor(colors.HexColor("#111827"))

    items = [
        ("Business laptop 14 in, Core i5, 16 GB RAM, 512 GB SSD", "Serial nos. NX14-88213 to NX14-88223. 3-year onsite warranty.",
         11, 68000.00),
        ("24 in Full HD IPS monitor", "With HDMI cable and height-adjustable stand.", 12, 11200.00),
    ]
    y -= 24
    subtotal = 0.0
    for desc, sub, q, price in items:
        amount = q * price
        subtotal += amount
        c.setFont("Helvetica", 9.5)
        c.drawString(44, y, desc)
        c.drawRightString(352, y, str(q))
        c.drawRightString(452, y, intl(price))
        c.drawRightString(W - 44, y, intl(amount))
        y -= 12
        c.setFont("Helvetica", 8)
        c.setFillColor(grey)
        c.drawString(44, y, sub)
        c.setFillColor(colors.HexColor("#111827"))
        y -= 20
    c.line(44, y + 8, W - 44, y + 8)

    igst = round(subtotal * 0.18, 2)
    total = subtotal + igst
    y -= 10
    c.setFont("Helvetica", 9.5)
    c.drawString(352, y, "Subtotal")
    c.drawRightString(W - 44, y, intl(subtotal))
    y -= 16
    c.drawString(352, y, "IGST (18%)")
    c.drawRightString(W - 44, y, intl(igst))
    y -= 8
    c.line(352, y, W - 44, y)
    y -= 16
    c.setFont("Helvetica-Bold", 11)
    c.drawString(352, y, "Total due (INR)")
    c.drawRightString(W - 44, y, intl(total))

    y -= 46
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(grey)
    c.drawString(44, y, "PAYMENT")
    c.setFillColor(colors.HexColor("#111827"))
    c.setFont("Helvetica", 9)
    y -= 14
    c.drawString(44, y, "Pay by NEFT / RTGS to ICICI Bank, Vashi Branch")
    y -= 12
    c.drawString(44, y, "Account No: 004105019876     IFSC: ICIC0000041")
    y -= 24
    c.setFont("Helvetica", 8)
    c.setFillColor(grey)
    c.drawString(44, y, "Balance 1 laptop is on back-order and will be invoiced separately on dispatch.")
    c.drawString(44, 60, "Nexa IT Solutions LLP  -  LLPIN AAQ-3391  -  This is a computer-generated invoice.")
    c.save()
    return total


# ============================================================ 03/04 Orbit ===

def orbit(path: Path, *, number: str, dated: str, subject: str, items):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Bill {number}")
    c.setAuthor("Orbit Print & Signage")
    c.setFont("Courier-Bold", 15)
    c.drawString(42, H - 52, "Orbit Print & Signage")
    c.setFont("Courier", 8.5)
    c.drawString(42, H - 66, "#27, 3rd Cross, Lalbagh Road, Bengaluru 560027")
    c.drawString(42, H - 78, "GSTIN 29ABXPO5567L1Z2    Ph 080-5550 0177")
    c.setDash(2, 2)
    c.line(42, H - 86, W - 42, H - 86)
    c.setDash()

    y = H - 106
    c.setFont("Courier-Bold", 9.5)
    c.drawString(42, y, f"Bill No: {number}")
    c.drawRightString(W - 42, y, f"Dated: {dated}")
    y -= 20
    c.setFont("Courier", 9)
    c.drawString(42, y, f"To: M/s {BUYER['name']}")
    y -= 12
    c.drawString(42, y, "    Prestige Tech Park, Outer Ring Road, Bengaluru 560103")
    y -= 12
    c.drawString(42, y, f"    GSTIN {BUYER['gstin']}")
    y -= 22
    c.setFont("Courier-Bold", 9)
    c.drawString(42, y, f"Sub: {subject}")

    y -= 26
    c.setDash(2, 2)
    c.line(42, y + 12, W - 42, y + 12)
    c.setFont("Courier-Bold", 8.5)
    c.drawString(42, y, "Particulars")
    c.drawString(300, y, "HSN")
    c.drawRightString(400, y, "Qty")
    c.drawRightString(470, y, "Rate")
    c.drawRightString(W - 42, y, "Amount (Rs.)")
    c.line(42, y - 6, W - 42, y - 6)
    c.setDash()
    y -= 22
    c.setFont("Courier", 8.5)
    subtotal = 0.0
    for desc_lines, hsn, q, unit, rate in items:
        amount = round(q * rate, 2)
        subtotal += amount
        c.drawString(42, y, desc_lines[0])
        c.drawString(300, y, hsn)
        c.drawRightString(400, y, f"{qty(q)} {unit}")
        c.drawRightString(470, y, f"{rate:.2f}")
        c.drawRightString(W - 42, y, inr(amount))
        for extra in desc_lines[1:]:
            y -= 11
            c.drawString(42, y, extra)
        y -= 18
    c.setDash(2, 2)
    c.line(42, y + 8, W - 42, y + 8)
    c.setDash()
    cgst = round(subtotal * 0.09, 2)
    sgst = round(subtotal * 0.09, 2)
    total = round(subtotal + cgst + sgst, 2)
    y -= 8
    for label, val, bold in [("Total before tax", subtotal, False), ("Add: CGST 9%", cgst, False),
                             ("Add: SGST 9%", sgst, False), ("Bill Amount", total, True)]:
        c.setFont("Courier-Bold" if bold else "Courier", 9.5 if bold else 9)
        c.drawString(330, y, label)
        c.drawRightString(W - 42, y, inr(val))
        y -= 14
    y -= 10
    c.setFont("Courier", 8.5)
    c.drawString(42, y, f"{words(total)}.")
    y -= 22
    c.drawString(42, y, "Our bank: State Bank of India, Lalbagh Road. A/c 38921045566  IFSC SBIN0004567")
    y -= 12
    c.drawString(42, y, "Payment within 30 days of bill date. Subject to Bengaluru jurisdiction.")
    c.setFont("Courier-Bold", 8.5)
    c.drawRightString(W - 42, 96, "For Orbit Print & Signage")
    c.setFont("Courier", 8)
    c.drawRightString(W - 42, 60, "Authorised Signatory")
    c.save()
    return total


# ======================================================= 05 GreenLeaf (scan) ===

def greenleaf_page() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    green = colors.HexColor("#1E5B3A")
    c.setFillColor(green)
    c.setFont("Times-Bold", 21)
    c.drawString(56, H - 70, "GreenLeaf Facility Services Pvt. Ltd.")
    c.setFillColor(colors.black)
    c.setFont("Times-Roman", 10.5)
    c.drawString(56, H - 88, "No. 88, 2nd Main, HSR Layout Sector 6, Bengaluru - 560102")
    c.drawString(56, H - 102, "GSTIN: 29AAGCG6120H1Z9        SAC: 998533        Phone: 080-5550 0163")
    c.setStrokeColor(green)
    c.setLineWidth(1.5)
    c.line(56, H - 112, W - 56, H - 112)

    c.setFont("Times-Bold", 15)
    c.drawCentredString(W / 2, H - 142, "INVOICE")
    c.setFont("Times-Roman", 12)
    c.drawString(56, H - 174, "Invoice No: GLF/26-27/0456")
    c.drawRightString(W - 56, H - 174, "Date: 03/11/2026")

    y = H - 208
    c.setFont("Times-Roman", 11.5)
    for line in ["To,", "The Accounts Department", BUYER["name"], "4th Floor, Prestige Tech Park, Outer Ring Road",
                 "Bengaluru - 560103", f"GSTIN: {BUYER['gstin']}"]:
        c.drawString(56, y, line)
        y -= 15

    y -= 16
    c.setLineWidth(0.8)
    c.setStrokeColor(colors.black)
    c.line(56, y + 14, W - 56, y + 14)
    c.setFont("Times-Bold", 11.5)
    c.drawString(60, y, "Description")
    c.drawRightString(W - 60, y, "Amount (Rs.)")
    c.line(56, y - 6, W - 56, y - 6)
    y -= 24
    c.setFont("Times-Roman", 11.5)
    c.drawString(60, y, "Integrated facility management services for the month of")
    c.drawRightString(W - 60, y, "2,44,260.00")
    y -= 15
    c.drawString(60, y, "October 2026 at your Bengaluru office and warehouse")
    y -= 15
    c.drawString(60, y, "(housekeeping staff, consumables and supervision as per contract)")
    y -= 14
    c.line(56, y, W - 56, y)
    y -= 20
    c.setFont("Times-Bold", 11.5)
    c.drawString(60, y, "Total amount payable (inclusive of GST @ 18%)")
    c.drawRightString(W - 60, y, "2,44,260.00")
    y -= 10
    c.line(56, y, W - 56, y)
    y -= 22
    c.setFont("Times-Roman", 11)
    c.drawString(56, y, "Rupees Two Lakh Forty Four Thousand Two Hundred Sixty Only")
    y -= 30
    c.drawString(56, y, "Payment terms: within 15 days of invoice date.")
    y -= 15
    c.drawString(56, y, "Bank: Axis Bank, HSR Layout Branch")
    y -= 15
    c.drawString(56, y, "A/c No: 917020045871236        IFSC: UTIB0000789")

    # signature block
    c.setFont("Times-Roman", 11)
    c.drawString(360, 190, "For GreenLeaf Facility Services Pvt. Ltd.")
    c.setStrokeColor(colors.HexColor("#1B2A6B"))
    c.setLineWidth(1.1)
    p = c.beginPath()
    p.moveTo(392, 150)
    p.curveTo(402, 176, 412, 132, 424, 158)
    p.curveTo(432, 174, 440, 138, 452, 154)
    p.curveTo(462, 166, 470, 146, 498, 160)
    c.drawPath(p, stroke=1, fill=0)
    c.setFont("Times-Roman", 10)
    c.drawString(410, 128, "Authorised Signatory")

    # the customer's mail-room stamp, applied at an angle
    c.saveState()
    c.translate(118, 168)
    c.rotate(9)
    stamp = colors.Color(0.16, 0.24, 0.62, alpha=0.72)
    c.setStrokeColor(stamp)
    c.setFillColor(stamp)
    c.setLineWidth(1.6)
    c.roundRect(0, 0, 150, 52, 5, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(75, 34, "RECEIVED")
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(75, 20, "05 NOV 2026")
    c.setFont("Helvetica", 7)
    c.drawCentredString(75, 8, "AURELIA - ACCOUNTS PAYABLE")
    c.restoreState()
    c.save()
    return buf.getvalue()


def scan_effect(pdf_bytes: bytes, out_path: Path, dpi: int = 200, seed: int = 7):
    """Print-and-scan the page: skew, paper tint, sensor noise, soft focus, JPEG artefacts."""
    rng = random.Random(seed)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    paper = (250, 248, 242)
    img = img.rotate(0.55, resample=Image.BICUBIC, expand=False, fillcolor=paper)
    tint = Image.new("RGB", img.size, paper)
    img = Image.blend(img, tint, 0.07)
    # uneven lighting from the scanner lid: darker towards one edge
    grad = Image.linear_gradient("L").rotate(90, expand=True).resize(img.size)
    shade = Image.merge("RGB", [grad.point(lambda v: 236 + int(v * 19 / 255))] * 3)
    img = Image.composite(img, Image.blend(img, shade, 0.5), Image.new("L", img.size, 120))
    noise = Image.effect_noise(img.size, 14).convert("RGB")
    img = Image.blend(img, noise, 0.06)
    img = img.filter(ImageFilter.GaussianBlur(0.55))
    img = ImageEnhance.Contrast(img).enhance(1.12)
    # a few dust specks
    px = img.load()
    for _ in range(140):
        x, y = rng.randrange(img.width), rng.randrange(img.height)
        g = rng.randrange(120, 190)
        px[x, y] = (g, g, g)
    jpg = io.BytesIO()
    img.save(jpg, "JPEG", quality=74)
    out = pymupdf.open()
    page = out.new_page(width=W, height=H)
    page.insert_image(page.rect, stream=jpg.getvalue())
    out.set_metadata({"title": "Scan 05-11-2026", "producer": "Office MFP scan-to-email", "creator": "MFP"})
    out.save(str(out_path), deflate=True)


# ============================================================== 07 Swiftline ===

def swiftline(path: Path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("Swiftline service invoice")
    c.setAuthor("Swiftline Logistics Pvt. Ltd.")
    orange = colors.HexColor("#C2410C")
    c.setFillColor(orange)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, H - 58, "SWIFTLINE LOGISTICS PVT. LTD.")
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(40, H - 72, "Express Courier & Last-Mile Delivery")
    c.setFont("Helvetica", 8.5)
    c.drawString(40, H - 86, "Regd. Office: B-42, Okhla Industrial Area Phase II, New Delhi 110020     GSTIN 07AAVCS2290R1Z4")
    c.setStrokeColor(orange)
    c.setLineWidth(1)
    c.line(40, H - 94, W - 40, H - 94)

    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(W / 2, H - 114, "SERVICE INVOICE")

    y = H - 140
    c.setFont("Helvetica", 9.5)
    c.drawString(40, y, "Invoice No. :")
    c.setLineWidth(0.5)
    c.setStrokeColor(colors.HexColor("#888888"))
    c.line(102, y - 2, 220, y - 2)
    c.drawString(330, y, "Invoice Date :")
    c.line(398, y - 2, 520, y - 2)
    y -= 18
    c.drawString(40, y, "Billing period : 16 Oct 2026 to 31 Oct 2026")
    c.drawString(330, y, "PO Number : PO-2026-0417")
    y -= 18
    c.drawString(40, y, f"Customer : {BUYER['name']}")
    y -= 14
    c.drawString(40, y, f"Customer GSTIN : {BUYER['gstin']}        Place of supply : Karnataka (29)")

    y -= 32
    c.setFillColor(colors.HexColor("#FDEDE4"))
    c.rect(40, y - 6, W - 80, 18, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(46, y, "Service description")
    c.drawString(270, y, "SAC")
    c.drawRightString(390, y, "Shipments")
    c.drawRightString(470, y, "Rate / shipment")
    c.drawRightString(W - 46, y, "Amount (INR)")
    y -= 20
    c.setFont("Helvetica", 9)
    items = [("Last-mile delivery - metro zone", "996812", 1400, 62.00),
             ("Reverse pickup handling", "996812", 120, 85.00)]
    subtotal = 0.0
    for desc, sac, q, rate in items:
        amount = q * rate
        subtotal += amount
        c.drawString(46, y, desc)
        c.drawString(270, y, sac)
        c.drawRightString(390, y, qty(q))
        c.drawRightString(470, y, f"{rate:.2f}")
        c.drawRightString(W - 46, y, inr(amount))
        y -= 17
    c.setStrokeColor(colors.HexColor("#BBBBBB"))
    c.line(40, y + 6, W - 40, y + 6)
    igst = round(subtotal * 0.18, 2)
    total = subtotal + igst
    y -= 12
    for label, val, bold in [("Sub-total", subtotal, False), ("IGST 18%", igst, False), ("TOTAL", total, True)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10 if bold else 9)
        c.drawString(372, y, label)
        c.drawRightString(W - 46, y, inr(val))
        y -= 15
    y -= 8
    c.setFont("Helvetica", 8.5)
    c.drawString(40, y, f"Amount in words: {words(total)}")
    y -= 24
    c.drawString(40, y, "Remit to: Kotak Mahindra Bank, Okhla Branch.  Account Number 6712345098   IFSC KKBK0000958")
    y -= 14
    c.drawString(40, y, "Shipment-wise annexure available on the customer portal.")
    c.setFont("Helvetica", 8)
    c.drawRightString(W - 40, 70, "For Swiftline Logistics Pvt. Ltd.  -  Authorised Signatory")
    c.save()
    return total


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.pdf"):
        old.unlink()

    t1 = shreeji(
        OUT / "01_shreeji_packaging_SPI-1047.pdf",
        number="SPI/2026-27/1047", inv_date="04-Nov-2026", due_date="04-Dec-2026", po="PO-2026-0412",
        po_date="06-Oct-2026",
        lines=[("5-ply corrugated shipping box 18 x 12 x 10 in", "4819", 2000, "pcs", 42.50),
               ("BOPP packaging tape 48 mm x 65 m", "3919", 300, "rolls", 38.00),
               ("Bubble wrap roll 1 m x 100 m", "3923", 40, "rolls", 1150.00)],
    )
    t2 = nexa(OUT / "02_nexa_it_NXT-3381.pdf")
    t3 = orbit(
        OUT / "03_orbit_print_OPS-0231.pdf", number="OPS/26-27/0231", dated="07.11.2026",
        subject="Part supply no. 3 against your order PO-2026-0405 dt. 28.09.2026",
        items=[(["Festive season product catalogue,", "A4, 24 pages, 4-colour"], "4911", 2000, "nos", 18.40),
               (["Retractable standee 6 ft x 2.5 ft", "with print"], "4911", 100, "nos", 320.00)],
    )
    t4 = orbit(
        OUT / "04_orbit_print_OPS-0246.pdf", number="OPS/26-27/0246", dated="12.11.2026",
        subject="Final supply against your order PO-2026-0405 dt. 28.09.2026",
        items=[(["Festive season product catalogue,", "A4, 24 pages, 4-colour",
                 "(final lot incl. print overrun)"], "4911", 3500, "nos", 18.40)],
    )
    scan_effect(greenleaf_page(), OUT / "05_greenleaf_scan_GLF-0456.pdf")
    t6 = shreeji(
        OUT / "06_shreeji_packaging_SPI-0982-R.pdf",
        number="SPI-2026-27-0982-R", inv_date="05-Nov-2026", due_date="05-Dec-2026", po="PO-2026-0355",
        po_date="22-Sep-2026",
        lines=[("3-ply corrugated box 12 x 10 x 8 in", "4819", 5000, "pcs", 21.80),
               ("Kraft paper roll 80 GSM, 36 in width", "4804", 60, "rolls", 890.00)],
        note="Re-issued copy. Kindly release payment at the earliest.",
    )
    t7 = swiftline(OUT / "07_swiftline_logistics_unnumbered.pdf")
    for name, total in [("01", t1), ("02", t2), ("03", t3), ("04", t4), ("05 (scan)", 244260.0), ("06", t6), ("07", t7)]:
        print(f"  {name:<10} total {inr(total):>14}")
    print(f"Wrote {len(list(OUT.glob('*.pdf')))} invoices to {OUT}")


if __name__ == "__main__":
    main()
