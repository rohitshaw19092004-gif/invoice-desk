"""Demo procurement data: approved vendors, purchase orders, payment history, policy.

This stands in for the customer's ERP / PO spreadsheet. Everything the pipeline
decides is checked against what is stored here, and approved runs write back to it.
"""
from __future__ import annotations

import json
import re
import shutil

from . import config, db

# ------------------------------------------------------------------ policy ---

POLICY_DEFS = [
    {"key": "price_tolerance_pct", "label": "Unit price tolerance", "unit": "%", "default": 2.0, "min": 0, "max": 50,
     "group": "Matching",
     "help": "A line may be billed up to this much above the PO unit price. Beyond it, the PO's buyer must approve."},
    {"key": "qty_tolerance_pct", "label": "Quantity tolerance", "unit": "%", "default": 0.0, "min": 0, "max": 50,
     "group": "Matching",
     "help": "Billed quantity may exceed what is still open on a PO line by this much."},
    {"key": "total_tolerance_pct", "label": "PO value tolerance", "unit": "%", "default": 1.0, "min": 0, "max": 50,
     "group": "Matching",
     "help": "What is billed, valued at PO prices, may exceed what is still open on the PO by this percentage…"},
    {"key": "total_tolerance_abs", "label": "PO value tolerance cap", "unit": "₹", "default": 500.0, "min": 0,
     "max": 1000000, "group": "Matching",
     "help": "…and never by more than this amount. Both limits apply; the stricter one wins."},
    {"key": "rounding_tolerance", "label": "Rounding tolerance", "unit": "₹", "default": 1.0, "min": 0, "max": 100,
     "group": "Validation",
     "help": "Arithmetic on the invoice (qty × rate, lines → subtotal, subtotal + tax → total) must tie within this."},
    {"key": "tax_tolerance_pct", "label": "Tax tolerance", "unit": "%", "default": 0.5, "min": 0, "max": 10,
     "group": "Validation",
     "help": "Tax charged may differ from the PO's tax rate applied to the invoice value by this much."},
    {"key": "stale_invoice_days", "label": "Stale invoice age", "unit": "days", "default": 180, "min": 1, "max": 3650,
     "group": "Validation",
     "help": "Invoices older than this are flagged for a human to confirm they are still payable."},
    {"key": "duplicate_window_days", "label": "Duplicate look-back window", "unit": "days", "default": 90, "min": 1,
     "max": 3650, "group": "Duplicates",
     "help": "How far back to look for an invoice with identical content under a different number. "
             "A matching invoice number is checked across all history regardless of this window."},
    {"key": "po_inference_min_confidence", "label": "Inferred PO: minimum confidence", "unit": "%", "default": 80,
     "min": 0, "max": 100, "group": "PO inference",
     "help": "When an invoice carries no PO number, the best-matching open PO is accepted only above this score."},
    {"key": "po_inference_min_margin", "label": "Inferred PO: lead over runner-up", "unit": "pts", "default": 10,
     "min": 0, "max": 100, "group": "PO inference",
     "help": "…and only if it beats the next-best candidate by at least this many points. Otherwise a person picks."},
    {"key": "ocr_min_confidence", "label": "Minimum OCR confidence", "unit": "%", "default": 70, "min": 0, "max": 100,
     "group": "Extraction",
     "help": "Scanned invoices read below this confidence are sent to a person rather than trusted."},
    {"key": "auto_approve_limit", "label": "Straight-through approval limit", "unit": "₹", "default": 2500000.0,
     "min": 0, "max": 1000000000, "group": "Approval",
     "help": "Invoices above this total always get a second pair of eyes, even when everything matches."},
]

POLICY_DEFAULTS = {p["key"]: float(p["default"]) for p in POLICY_DEFS}

# ----------------------------------------------------------------- vendors ---

VENDORS = [
    {"id": "V-1001", "name": "Shreeji Packaging Industries", "aliases": ["Shreeji Packaging", "Shreeji Packaging Inds."],
     "gstin": "29AAFCS8842K1Z6", "status": "approved", "payment_terms_days": 30, "bank_name": "HDFC Bank",
     "bank_account": "50200048213377", "ifsc": "HDFC0001234", "email": "accounts@shreejipackaging.example",
     "category": "Packaging materials"},
    {"id": "V-1002", "name": "Nexa IT Solutions LLP", "aliases": ["Nexa IT Solutions", "Nexa IT"],
     "gstin": "27AAQFN3391P1ZV", "status": "approved", "payment_terms_days": 45, "bank_name": "ICICI Bank",
     "bank_account": "004105019876", "ifsc": "ICIC0000041", "email": "billing@nexait.example",
     "category": "IT hardware"},
    {"id": "V-1003", "name": "Orbit Print & Signage", "aliases": ["Orbit Print and Signage", "Orbit Print"],
     "gstin": "29ABXPO5567L1Z2", "status": "approved", "payment_terms_days": 30, "bank_name": "State Bank of India",
     "bank_account": "38921045566", "ifsc": "SBIN0004567", "email": "accounts@orbitprint.example",
     "category": "Print & marketing collateral"},
    {"id": "V-1004", "name": "GreenLeaf Facility Services Pvt. Ltd.",
     "aliases": ["GreenLeaf Facility Services", "Green Leaf Facility Services", "GreenLeaf FS"],
     "gstin": "29AAGCG6120H1Z9", "status": "approved", "payment_terms_days": 15, "bank_name": "Axis Bank",
     "bank_account": "917020045871236", "ifsc": "UTIB0000789", "email": "finance@greenleaffs.example",
     "category": "Facility management"},
    {"id": "V-1005", "name": "Swiftline Logistics Pvt. Ltd.", "aliases": ["Swiftline Logistics", "Swiftline"],
     "gstin": "07AAVCS2290R1Z4", "status": "approved", "payment_terms_days": 30, "bank_name": "Kotak Mahindra Bank",
     "bank_account": "6712345098", "ifsc": "KKBK0000958", "email": "ar@swiftline.example",
     "category": "Courier & last-mile delivery"},
    {"id": "V-1006", "name": "Apex Office Mart", "aliases": ["Apex Office Mart Pvt Ltd"],
     "gstin": "29AAHCA9087Q1Z3", "status": "blocked",
     "status_note": "Payments on hold since 02 Sep 2026: vendor KYC documents expired.",
     "payment_terms_days": 30, "bank_name": "Yes Bank", "bank_account": "002384600001245", "ifsc": "YESB0000023",
     "email": "sales@apexofficemart.example", "category": "Office supplies"},
]

# --------------------------------------------------------- purchase orders ---

_FACILITY_LINES = lambda month: [  # noqa: E731
    (f"Housekeeping staff deployment, 12 staff - {month}", 1, "month", 162000.00),
    (f"Cleaning consumables and supplies - {month}", 1, "lot", 28000.00),
    (f"Site supervisor charges - {month}", 1, "month", 17000.00),
]

PURCHASE_ORDERS = [
    {"po_number": "PO-2026-0412", "vendor_id": "V-1001", "po_date": "2026-10-06",
     "description": "Shipping cartons and packing material - Q3 replenishment",
     "buyer_name": "Priya Nair", "buyer_role": "Procurement - Packaging", "buyer_email": "priya.nair@aureliahome.example",
     "lines": [
         ("5-ply corrugated shipping box 18 x 12 x 10 in", 2000, "pcs", 42.50),
         ("BOPP packaging tape 48 mm x 65 m", 300, "rolls", 38.00),
         ("Bubble wrap roll 1 m x 100 m", 40, "rolls", 1150.00),
     ]},
    {"po_number": "PO-2026-0355", "vendor_id": "V-1001", "po_date": "2026-09-22",
     "description": "Cartons and kraft paper - festive dispatch", "closed": 1,
     "buyer_name": "Priya Nair", "buyer_role": "Procurement - Packaging", "buyer_email": "priya.nair@aureliahome.example",
     "lines": [
         ("3-ply corrugated box 12 x 10 x 8 in", 5000, "pcs", 21.80),
         ("Kraft paper roll 80 GSM, 36 in width", 60, "rolls", 890.00),
     ]},
    {"po_number": "PO-2026-0398", "vendor_id": "V-1002", "po_date": "2026-10-01",
     "description": "Laptops and monitors for new joiners - Bengaluru office",
     "buyer_name": "Arjun Mehta", "buyer_role": "IT Procurement", "buyer_email": "arjun.mehta@aureliahome.example",
     "lines": [
         ("Business laptop 14 in, Core i5, 16 GB RAM, 512 GB SSD", 12, "units", 62500.00),
         ("24 in Full HD IPS monitor", 12, "units", 11200.00),
     ]},
    {"po_number": "PO-2026-0405", "vendor_id": "V-1003", "po_date": "2026-09-28",
     "description": "Festive campaign print collateral - supplied in part shipments",
     "buyer_name": "Sana Qureshi", "buyer_role": "Marketing Operations", "buyer_email": "sana.qureshi@aureliahome.example",
     "lines": [
         ("Festive season product catalogue, A4, 24 pages, 4-colour", 10000, "copies", 18.40),
         ("Retractable standee 6 ft x 2.5 ft with print", 500, "nos", 320.00),
     ]},
    {"po_number": "PO-2026-0340", "vendor_id": "V-1004", "po_date": "2026-08-26",
     "description": "Integrated facility management services - September 2026", "closed": 1,
     "buyer_name": "Rahul Verma", "buyer_role": "Admin & Facilities", "buyer_email": "rahul.verma@aureliahome.example",
     "lines": _FACILITY_LINES("September 2026")},
    {"po_number": "PO-2026-0361", "vendor_id": "V-1004", "po_date": "2026-09-25",
     "description": "Integrated facility management services - October 2026",
     "buyer_name": "Rahul Verma", "buyer_role": "Admin & Facilities", "buyer_email": "rahul.verma@aureliahome.example",
     "lines": _FACILITY_LINES("October 2026")},
    {"po_number": "PO-2026-0362", "vendor_id": "V-1004", "po_date": "2026-10-27",
     "description": "Integrated facility management services - November 2026",
     "buyer_name": "Rahul Verma", "buyer_role": "Admin & Facilities", "buyer_email": "rahul.verma@aureliahome.example",
     "lines": _FACILITY_LINES("November 2026")},
    {"po_number": "PO-2026-0417", "vendor_id": "V-1005", "po_date": "2026-10-10",
     "description": "Last-mile delivery and reverse pickups - Q3",
     "buyer_name": "Vikram Rao", "buyer_role": "Supply Chain", "buyer_email": "vikram.rao@aureliahome.example",
     "lines": [
         ("Last-mile delivery - metro zone, per shipment", 4200, "shipments", 62.00),
         ("Reverse pickup handling, per shipment", 380, "shipments", 85.00),
     ]},
]

# ------------------------------------------------- already-accepted invoices ---
# (invoice no, vendor, PO, date, status, pay date, [(po line no, qty)])
HISTORY = [
    ("SPI/2026-27/0982", "V-1001", "PO-2026-0355", "2026-10-12", "paid", "2026-11-10", [(1, 5000), (2, 60)]),
    ("OPS/26-27/0188", "V-1003", "PO-2026-0405", "2026-10-09", "paid", "2026-11-07", [(1, 4000), (2, 200)]),
    ("OPS/26-27/0204", "V-1003", "PO-2026-0405", "2026-10-23", "scheduled", "2026-11-22", [(1, 3000), (2, 200)]),
    ("GLF/26-27/0391", "V-1004", "PO-2026-0340", "2026-10-03", "paid", "2026-10-18", [(1, 1), (2, 1), (3, 1)]),
]

# ------------------------------------------------------------ demo inbox ---

SAMPLES = [
    {"key": "happy", "file": "01_shreeji_packaging_SPI-1047.pdf", "sender": "Shreeji Packaging Industries",
     "email": "accounts@shreejipackaging.example", "subject": "Tax Invoice SPI/2026-27/1047 against PO-2026-0412",
     "received": "09:12", "tag": "Happy path", "kind": "happy", "title": "Clean invoice, exact PO match",
     "tests": "Digital PDF with an explicit PO number, itemised lines, CGST and SGST shown separately."},
    {"key": "masked_variance", "file": "02_nexa_it_NXT-3381.pdf", "sender": "Nexa IT Solutions LLP",
     "email": "billing@nexait.example", "subject": "Invoice NXT/2026/3381 - laptops and monitors",
     "received": "09:40", "tag": "Edge case 1", "kind": "edge", "title": "Totals agree, lines don't",
     "tests": "The invoice total is within 0.3% of the PO, but one line is billed 8.8% over the agreed price. "
              "A short shipment hides it."},
    {"key": "split_partial", "file": "03_orbit_print_OPS-0231.pdf", "sender": "Orbit Print & Signage",
     "email": "accounts@orbitprint.example", "subject": "Bill for part supply no. 3 - catalogues and standees",
     "received": "10:05", "tag": "Edge case 2a", "kind": "edge", "title": "Split PO: third part-shipment",
     "tests": "One PO billed across several invoices, with the PO number buried in a sentence. "
              "The process has to know what is still open."},
    {"key": "split_overbill", "file": "04_orbit_print_OPS-0246.pdf", "sender": "Orbit Print & Signage",
     "email": "accounts@orbitprint.example", "subject": "Final bill - catalogue print run",
     "received": "10:31", "tag": "Edge case 2b", "kind": "edge", "title": "Split PO: final invoice over-bills",
     "tests": "The final invoice bills more catalogues than remain on the PO after the earlier part-shipments."},
    {"key": "scanned_no_po", "file": "05_greenleaf_scan_GLF-0456.pdf", "sender": "GreenLeaf Facility Services",
     "email": "finance@greenleaffs.example", "subject": "Scan: October housekeeping invoice",
     "received": "11:18", "tag": "Edge case 3", "kind": "edge", "title": "Scanned, no PO number, tax inside the total",
     "tests": "Image-only scan. No PO reference, a single bundled line, GST included in the total, "
              "and two open POs for the same amount."},
    {"key": "disguised_duplicate", "file": "06_shreeji_packaging_SPI-0982-R.pdf",
     "sender": "Shreeji Packaging Industries", "email": "accounts@shreejipackaging.example",
     "subject": "Revised invoice SPI-2026-27-0982-R (payment pending)", "received": "12:02", "tag": "Edge case 4",
     "kind": "edge", "title": "Duplicate in disguise",
     "tests": "An invoice that was already paid, re-sent with a new date and a reformatted number. "
              "An exact-number or file-hash check would miss it."},
    {"key": "missing_fields", "file": "07_swiftline_logistics_unnumbered.pdf", "sender": "Swiftline Logistics",
     "email": "ar@swiftline.example", "subject": "Delivery charges 16-31 Oct", "received": "12:47",
     "tag": "Guardrail", "kind": "guard", "title": "Critical information missing",
     "tests": "No invoice number and no invoice date. The only dates on the page are a billing period, "
              "which must not be mistaken for the invoice date."},
]


# Words a vendor appends when re-sending an invoice. Single letters other than R are left
# alone: "…/A1" and "…/A2" are far more often two invoices in a series than two copies of one.
_REVISION_MARKERS = {"R", "REV", "REVISED", "DUP", "DUPLICATE", "COPY"}
_COUNTED_MARKERS = {"REV", "REVISED"}  # "REV 2", "REVISED 1"


def normalize_invoice_number(number: str, strip_revision: bool = False) -> str:
    """Canonical form used to recognise the same invoice number written differently.

    Punctuation, case and leading zeros are ignored, so 'SPI/2026-27/0982' and
    'spi-2026-27-982' both become 'SPI|2026|27|982'. Consecutive numbers
    (…/0982 vs …/0983) stay different.

    With ``strip_revision`` a trailing re-issue marker is dropped as well, so
    'SPI-2026-27-0982-R' and 'SPI/2026-27/0982 REV 2' reduce to the original.
    """
    if not number:
        return ""
    tokens = [str(int(t)) if t.isdigit() else t for t in re.findall(r"[A-Za-z]+|\d+", number.upper())]
    if strip_revision and len(tokens) > 2:
        # "... REV 2" / "... R1": marker followed by a one-digit revision counter
        if len(tokens) > 3 and re.fullmatch(r"\d", tokens[-1]) and tokens[-2] in _COUNTED_MARKERS:
            tokens = tokens[:-2]
        elif tokens[-1] in _REVISION_MARKERS:
            tokens = tokens[:-1]
    return "|".join(tokens)


def seed_reference_data(conn) -> None:
    for v in VENDORS:
        conn.execute(
            "INSERT INTO vendors (id, name, aliases, gstin, status, status_note, payment_terms_days, bank_name,"
            " bank_account, ifsc, email, category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (v["id"], v["name"], json.dumps(v["aliases"]), v["gstin"], v["status"], v.get("status_note"),
             v["payment_terms_days"], v["bank_name"], v["bank_account"], v["ifsc"], v["email"], v["category"]),
        )
    for po in PURCHASE_ORDERS:
        conn.execute(
            "INSERT INTO purchase_orders (po_number, vendor_id, po_date, description, currency, tax_rate, buyer_name,"
            " buyer_role, buyer_email, closed) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (po["po_number"], po["vendor_id"], po["po_date"], po["description"], "INR", 18.0, po["buyer_name"],
             po["buyer_role"], po["buyer_email"], po.get("closed", 0)),
        )
        for i, (desc, qty, unit, price) in enumerate(po["lines"], start=1):
            conn.execute(
                "INSERT INTO po_lines (po_number, line_no, description, qty, unit, unit_price) VALUES (?,?,?,?,?,?)",
                (po["po_number"], i, desc, qty, unit, price),
            )
    po_index = {po["po_number"]: po for po in PURCHASE_ORDERS}
    for number, vendor_id, po_number, inv_date, status, pay_date, alloc in HISTORY:
        lines = po_index[po_number]["lines"]
        subtotal = sum(qty * lines[ln - 1][3] for ln, qty in alloc)
        tax = round(subtotal * 0.18, 2)
        cur = conn.execute(
            "INSERT INTO ledger_invoices (run_id, vendor_id, invoice_number, invoice_number_norm, invoice_date,"
            " po_number, subtotal, tax, total, status, pay_date, file_hash, source, created_at)"
            " VALUES (NULL,?,?,?,?,?,?,?,?,?,?,NULL,'seed',?)",
            (vendor_id, number, normalize_invoice_number(number), inv_date, po_number, subtotal, tax,
             round(subtotal + tax, 2), status, pay_date, inv_date + "T10:00:00+00:00"),
        )
        for ln, qty in alloc:
            desc, _q, _u, price = lines[ln - 1]
            conn.execute(
                "INSERT INTO ledger_lines (ledger_invoice_id, po_number, po_line_no, description, qty, unit_price,"
                " amount) VALUES (?,?,?,?,?,?,?)",
                (cur.lastrowid, po_number, ln, desc, qty, price, round(qty * price, 2)),
            )
    for k, v in POLICY_DEFAULTS.items():
        conn.execute("INSERT INTO policy (key, value) VALUES (?, ?)", (k, v))


def ensure_seeded() -> None:
    db.init_schema()
    if db.query_one("SELECT COUNT(*) AS n FROM vendors")["n"] == 0:
        with db.tx() as conn:
            seed_reference_data(conn)


def reset_demo(keep_policy: bool = False) -> None:
    """Wipe runs and restore procurement data to its starting state."""
    db.init_schema()
    saved_policy = db.get_policy() if keep_policy else None
    with db.tx() as conn:
        for table in ("events", "runs", "ledger_lines", "ledger_invoices", "po_lines", "purchase_orders", "vendors",
                      "policy"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('events', 'ledger_lines', 'ledger_invoices')")
        seed_reference_data(conn)
    if saved_policy:
        db.set_policy(saved_policy)
    for folder in (config.UPLOAD_DIR, config.PREVIEW_DIR):
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
    config.ensure_dirs()


def reset_policy() -> None:
    db.set_policy(POLICY_DEFAULTS)
