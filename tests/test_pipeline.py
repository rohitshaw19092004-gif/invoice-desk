"""End-to-end behaviour of the pipeline: the seven demo invoices, and the cases around them."""
from __future__ import annotations

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app import db
from tests.conftest import PO_0412_LINES, SHREEJI, process, process_sample

EXPECTED = {
    "happy": "APPROVED",
    "masked_variance": "NEEDS_REVIEW",
    "split_partial": "APPROVED",
    "split_overbill": "NEEDS_REVIEW",
    "scanned_no_po": "APPROVED",
    "disguised_duplicate": "REJECTED",
    "missing_fields": "RETURNED",
}


# ------------------------------------------------------------ the demo set ---

@pytest.mark.parametrize("key,decision", EXPECTED.items())
def test_each_sample_reaches_its_decision_from_a_clean_state(key, decision):
    """Every sample must give its outcome on its own, whatever was or was not run before it."""
    assert process_sample(key).decision == decision


def test_whole_inbox_in_arrival_order():
    assert [process_sample(k).decision for k in EXPECTED] == list(EXPECTED.values())


def test_whole_inbox_in_reverse_order():
    keys = list(EXPECTED)[::-1]
    assert [process_sample(k).decision for k in keys] == [EXPECTED[k] for k in keys]


# -------------------------------------------------------------- happy path ---

def test_happy_path_posts_the_invoice_and_closes_the_po():
    r = process_sample("happy")
    assert r.run["vendor_name"] == "Shreeji Packaging Industries"
    assert r.run["invoice_number"] == "SPI/2026-27/1047"
    assert r.run["po_number"] == "PO-2026-0412"
    assert r.run["total"] == 168032.00
    assert not [c for c in r.checks if c["status"] == "fail"]
    pay = r.result["payment"]
    assert pay["pay_date"] == "2026-12-04" and pay["amount"] == 168032.00
    assert pay["bank_account_masked"] == "••••3377"  # the account on file, masked
    po = db.get_po("PO-2026-0412")
    assert po["status"] == "fully_billed" and po["remaining_value"] == 0
    assert [i["invoice_number"] for i in po["invoices"]] == ["SPI/2026-27/1047"]


def test_running_the_same_invoice_twice_rejects_the_second_copy():
    assert process_sample("happy").decision == "APPROVED"
    again = process_sample("happy")
    assert again.decision == "REJECTED"
    assert "DUPLICATE_FILE" in again.codes
    assert again.stages["reconcile"]["status"] == "skipped"
    assert len(db.get_po("PO-2026-0412")["invoices"]) == 1  # nothing posted twice


# ------------------------------------------- edge 1: totals agree, lines don't ---

def test_masked_variance_is_caught_at_line_level_and_routed_to_the_buyer():
    r = process_sample("masked_variance")
    pv = r.check("PRICE_VARIANCE")
    assert pv["route"] == "buyer" and pv["data"]["over_amount"] == 60500.00
    assert pv["data"]["variance_pct"] == 8.8
    assert "MASKED_VARIANCE" in r.codes  # the note that a total-only match would have passed it
    assert "VALUE_OVER_PO" not in r.codes  # header value really is within the PO
    assert r.result["route"]["name"] == "Arjun Mehta"
    assert "₹9,69,842.00" in r.result["options"][1]  # short-pay figure at PO prices incl. tax
    assert db.get_po("PO-2026-0398")["billed_value"] == 0  # held, not posted


def test_price_within_tolerance_is_approved(pdf):
    lines = list(PO_0412_LINES)
    lines[1] = ("BOPP packaging tape 48 mm x 65 m", 300, "rolls", 38.50)  # +1.3% against a 2% tolerance
    r = process(pdf(number="SPI/2026-27/2001", po="PO-2026-0412", lines=lines, **SHREEJI))
    assert r.decision == "APPROVED"
    assert any("within tolerance" in c["title"] for c in r.checks)


def test_changing_the_tolerance_in_policy_changes_the_decision():
    assert process_sample("masked_variance").decision == "NEEDS_REVIEW"
    from app import seed
    seed.reset_demo()
    db.set_policy({"price_tolerance_pct": 10})
    r = process_sample("masked_variance")
    assert r.decision == "APPROVED"  # 8.8% is now inside tolerance
    po = db.get_po("PO-2026-0398")
    assert po["status"] == "partially_billed" and po["remaining_value"] == 62500.00  # one laptop still to come


# ------------------------------------------------------------ edge 2: split PO ---

def test_split_po_tracks_what_is_open_across_invoices():
    before = db.get_po("PO-2026-0405")
    assert [l["remaining_qty"] for l in before["lines"]] == [3000, 100]  # after two earlier part-shipments
    r = process_sample("split_partial")
    assert r.decision == "APPROVED"
    assert r.run["po_number"] == "PO-2026-0405"  # found although it only appears inside a sentence
    after = db.get_po("PO-2026-0405")
    assert [l["remaining_qty"] for l in after["lines"]] == [1000, 0]
    assert after["status"] == "partially_billed"


def test_overbilling_is_measured_against_what_is_open_now():
    alone = process_sample("split_overbill")
    assert alone.check("QTY_OVER_PO")["data"]["excess_qty"] == 500  # 3,500 billed, 3,000 open
    from app import seed
    seed.reset_demo()
    process_sample("split_partial")
    after = process_sample("split_overbill")
    assert after.check("QTY_OVER_PO")["data"]["excess_qty"] == 2500  # now only 1,000 open
    assert after.decision == "NEEDS_REVIEW" and after.result["route"]["name"] == "Sana Qureshi"


# ------------------------------ edge 3: scan, no PO number, tax inside the total ---

def test_scanned_invoice_is_read_inferred_and_normalised():
    r = process_sample("scanned_no_po")
    assert r.stages["read"]["data"]["file_kind"] == "scanned"
    assert r.stages["read"]["data"]["ocr_confidence"] >= 85
    f = r.stages["extract"]["data"]["fields"]
    assert f["invoice_number"] == "GLF/26-27/0456" and f["invoice_date"] == "2026-11-03"
    assert f["po_number"] is None and f["tax_inclusive"] is True
    assert f["bank_account"] == "917020045871236"  # OCR split the digits with a space; rejoined
    v = r.stages["validate"]["data"]
    assert (v["subtotal"], v["tax"], v["total"]) == (207000.00, 37260.00, 244260.00)
    pm = r.stages["po_match"]["data"]
    assert pm["method"] == "inferred" and pm["po"]["po_number"] == "PO-2026-0361"
    assert [c["po_number"] for c in pm["candidates"]] == ["PO-2026-0361", "PO-2026-0362"]
    assert r.stages["reconcile"]["data"]["mode"] == "lump"
    assert r.decision == "APPROVED"
    assert db.get_po("PO-2026-0361")["status"] == "fully_billed"
    assert db.get_po("PO-2026-0362")["billed_value"] == 0  # the November PO is untouched


def test_po_inference_refuses_to_choose_between_equal_candidates(pdf):
    """Same vendor, two open POs for the same amount, and nothing on the invoice says which month."""
    path = pdf(vendor="GreenLeaf Facility Services Pvt. Ltd.", gstin="29AAGCG6120H1Z9", number="GLF/26-27/0470",
               date="04-Nov-2026", tax="inclusive", lump=("Facility management services as per contract", 244260.00))
    r = process(path)
    assert r.decision == "NEEDS_REVIEW"
    assert "PO_AMBIGUOUS" in r.codes and r.check("PO_AMBIGUOUS")["route"] == "ap"
    assert r.stages["reconcile"]["status"] == "skipped"


def test_low_ocr_confidence_goes_to_a_person():
    db.set_policy({"ocr_min_confidence": 99})
    r = process_sample("scanned_no_po")
    assert r.decision == "NEEDS_REVIEW" and "LOW_OCR_CONFIDENCE" in r.codes


# ---------------------------------------------------------- edge 4: duplicates ---

def test_disguised_duplicate_is_matched_to_the_paid_original():
    r = process_sample("disguised_duplicate")
    d = r.stages["duplicate"]["data"]["match"]
    assert d["kind"] == "reissue" and d["original"]["invoice_number"] == "SPI/2026-27/0982"
    assert d["original"]["status"] == "paid"
    assert r.stages["reconcile"]["status"] == "skipped"
    assert "SPI/2026-27/0982" in r.result["message"]["body"]
    assert r.result["message"]["to"] == "accounts@shreejipackaging.example"


def test_consecutive_invoice_numbers_are_not_duplicates(pdf):
    """…/0982 was paid; …/0983 is the vendor's next invoice, not a copy."""
    r = process(pdf(number="SPI/2026-27/0983", po="PO-2026-0412", lines=PO_0412_LINES, **SHREEJI))
    assert r.decision == "APPROVED"


def test_same_number_with_a_different_amount_is_still_rejected(pdf):
    r = process(pdf(number="SPI/2026-27/0982", po="PO-2026-0412", lines=PO_0412_LINES, **SHREEJI))
    assert r.decision == "REJECTED" and "DUPLICATE_NUMBER" in r.codes


def test_revision_suffix_with_a_different_amount_is_a_question_not_a_rejection(pdf):
    r = process(pdf(number="SPI/2026-27/0982-R", po="PO-2026-0412", lines=PO_0412_LINES, **SHREEJI))
    assert r.decision == "NEEDS_REVIEW" and "POSSIBLE_REVISION" in r.codes


def test_identical_content_under_a_new_number_is_flagged(pdf):
    """Orbit's OPS/26-27/0204 (3,000 catalogues + 200 standees) arrives again as …/0299."""
    path = pdf(vendor="Orbit Print & Signage", gstin="29ABXPO5567L1Z2", number="OPS/26-27/0299", date="10-Nov-2026",
               po="PO-2026-0405", bank="38921045566", ifsc="SBIN0004567",
               lines=[("Festive season product catalogue, A4, 24 pages, 4-colour", 3000, "copies", 18.40),
                      ("Retractable standee 6 ft x 2.5 ft with print", 200, "nos", 320.00)])
    r = process(path)
    assert r.decision == "NEEDS_REVIEW" and "POSSIBLE_DUPLICATE" in r.codes


# ------------------------------------------------- guardrail: missing information ---

def test_missing_number_and_date_goes_back_to_the_vendor_without_guessing():
    r = process_sample("missing_fields")
    f = r.stages["extract"]["data"]["fields"]
    assert f["invoice_number"] is None
    assert f["invoice_date"] is None  # the billing-period dates were not mistaken for it
    assert r.check("MISSING_FIELDS")["data"]["missing"] == ["invoice number", "invoice date"]
    msg = r.result["message"]
    assert msg["to"] == "ar@swiftline.example"
    assert "Invoice number" in msg["body"] and "Invoice date" in msg["body"]
    assert db.get_po("PO-2026-0417")["billed_value"] == 0


# ----------------------------------------------------------- vendor controls ---

def test_changed_bank_account_is_held_for_treasury(pdf):
    kw = dict(SHREEJI, bank="50200099887766")
    r = process(pdf(number="SPI/2026-27/2002", po="PO-2026-0412", lines=PO_0412_LINES, **kw))
    assert r.decision == "NEEDS_REVIEW"
    assert r.check("BANK_MISMATCH")["route"] == "treasury"
    assert r.result["route"]["queue"] == "Treasury & Controls"


def test_unknown_vendor_is_held_for_onboarding_and_not_matched_to_a_po(pdf):
    r = process(pdf(vendor="Bright Star Traders", gstin="29AAACB1234C1Z5", number="BST/101", po="PO-2026-0412",
                    lines=PO_0412_LINES))
    assert r.decision == "NEEDS_REVIEW"
    assert r.check("VENDOR_UNKNOWN")["route"] == "procurement"
    assert "PO_VENDOR_UNVERIFIED" in r.codes
    assert r.stages["reconcile"]["status"] == "skipped"
    assert r.run["result"]["posting"] is None  # nothing that could ever be posted


def test_blocked_vendor_is_rejected(pdf):
    r = process(pdf(vendor="Apex Office Mart", gstin="29AAHCA9087Q1Z3", number="AOM/2026/77",
                    lines=[("A4 copier paper 75 GSM", 100, "reams", 245.00)]))
    assert r.decision == "REJECTED" and "VENDOR_BLOCKED" in r.codes
    assert "on hold" in r.result["message"]["body"]


def test_vendor_is_matched_by_name_when_the_tax_id_is_absent(pdf):
    r = process(pdf(vendor="Shreeji Packaging Inds.", gstin=None, number="SPI/2026-27/2003", po="PO-2026-0412",
                    lines=PO_0412_LINES, bank=SHREEJI["bank"], ifsc=SHREEJI["ifsc"]))
    assert r.run["vendor_name"] == "Shreeji Packaging Industries"
    assert r.decision == "APPROVED"


def test_right_name_wrong_tax_id_is_held(pdf):
    r = process(pdf(vendor="Shreeji Packaging Industries", gstin="29AAFCS9999K1Z6", number="SPI/2026-27/2004",
                    po="PO-2026-0412", lines=PO_0412_LINES))
    assert r.decision == "NEEDS_REVIEW" and r.check("GSTIN_MISMATCH")["route"] == "procurement"


# ------------------------------------------------------------- PO references ---

def test_po_number_that_does_not_exist_gets_a_suggestion_and_a_person(pdf):
    r = process(pdf(number="SPI/2026-27/2005", po="PO-2026-0421", lines=PO_0412_LINES, **SHREEJI))
    assert r.decision == "NEEDS_REVIEW"
    assert "PO-2026-0412" in r.check("PO_NOT_FOUND")["title"]


def test_po_issued_to_another_vendor(pdf):
    r = process(pdf(number="SPI/2026-27/2006", po="PO-2026-0398", lines=PO_0412_LINES, **SHREEJI))
    assert r.decision == "NEEDS_REVIEW" and "PO_VENDOR_MISMATCH" in r.codes
    assert r.stages["reconcile"]["status"] == "skipped"


def test_po_number_written_differently_still_matches(pdf):
    r = process(pdf(number="SPI/2026-27/2007", po="PO/2026/412", lines=PO_0412_LINES, **SHREEJI))
    assert r.run["po_number"] == "PO-2026-0412" and r.decision == "APPROVED"


def test_line_that_is_not_on_the_po(pdf):
    lines = PO_0412_LINES + [("Freight and forwarding charges", 1, "lot", 2500.00)]
    r = process(pdf(number="SPI/2026-27/2008", po="PO-2026-0412", lines=lines, **SHREEJI))
    assert r.decision == "NEEDS_REVIEW"
    assert {"LINE_NOT_ON_PO", "VALUE_OVER_PO"} <= r.codes


def test_invoice_lines_in_a_different_order_and_wording_still_match(pdf):
    lines = [("Bubble wrap 1m x 100m roll", 40, "rolls", 1150.00),
             ("Corrugated shipping box, 5-ply, 18x12x10 in", 2000, "pcs", 42.50),
             ("Packaging tape BOPP 48mm x 65m", 300, "rolls", 38.00)]
    r = process(pdf(number="SPI/2026-27/2009", po="PO-2026-0412", lines=lines, **SHREEJI))
    assert r.decision == "APPROVED"
    rows = r.stages["reconcile"]["data"]["rows"]
    assert [row["po_line_no"] for row in rows] == [3, 1, 2]


# ------------------------------------------------------------ invoice checks ---

def test_line_arithmetic_that_does_not_tie(pdf):
    r = process(pdf(number="SPI/2026-27/2010", po="PO-2026-0412", lines=PO_0412_LINES, line_amounts={2: 12400.00},
                    **SHREEJI))
    assert r.decision == "NEEDS_REVIEW"
    assert {"LINE_ARITHMETIC", "LINES_VS_SUBTOTAL"} <= r.codes


def test_total_that_does_not_equal_subtotal_plus_tax(pdf):
    r = process(pdf(number="SPI/2026-27/2011", po="PO-2026-0412", lines=PO_0412_LINES, total=169032.00, words=False,
                    **SHREEJI))
    assert r.decision == "NEEDS_REVIEW" and "TOTAL_ARITHMETIC" in r.codes


def test_amount_in_words_that_disagrees_with_the_figure(pdf):
    r = process(pdf(number="SPI/2026-27/2012", po="PO-2026-0412", lines=PO_0412_LINES,
                    words="Rupees One Lakh Sixty Eight Thousand Only", **SHREEJI))
    assert r.decision == "NEEDS_REVIEW" and "WORDS_MISMATCH" in r.codes


def test_wrong_tax_rate(pdf):
    r = process(pdf(number="SPI/2026-27/2013", po="PO-2026-0412", lines=PO_0412_LINES, tax="igst", tax_rate=28,
                    **SHREEJI))
    assert r.decision == "NEEDS_REVIEW" and r.check("TAX_MISMATCH")["route"] == "ap"


def test_future_dated_invoice(pdf):
    r = process(pdf(number="SPI/2026-27/2014", date="20-Dec-2026", po="PO-2026-0412", lines=PO_0412_LINES, **SHREEJI))
    assert r.decision == "NEEDS_REVIEW" and "FUTURE_DATED" in r.codes


def test_stale_invoice(pdf):
    r = process(pdf(number="SPI/2025-26/0100", date="02-Jan-2026", po="PO-2026-0412", lines=PO_0412_LINES, **SHREEJI))
    assert r.decision == "NEEDS_REVIEW" and "STALE_INVOICE" in r.codes


def test_clean_invoice_above_the_straight_through_limit_gets_a_second_look():
    db.set_policy({"auto_approve_limit": 100000})
    r = process_sample("happy")
    assert r.decision == "NEEDS_REVIEW" and "ABOVE_AUTO_LIMIT" in r.codes
    assert r.result["route"]["name"] == "Meera Iyer"


# ------------------------------------------------------------------ bad files ---

def test_a_file_that_is_not_a_pdf(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_text("this is a text file with a .pdf name")
    r = process(path)
    assert r.decision == "RETURNED" and "NOT_A_PDF" in r.codes
    assert all(r.stages[s]["status"] == "skipped" for s in ("read", "extract", "validate", "reconcile"))


def test_a_blank_pdf(tmp_path):
    path = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.showPage()
    c.save()
    r = process(path)
    assert r.decision == "RETURNED" and "UNREADABLE" in r.codes


def test_a_corrupt_pdf(tmp_path):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"\x00garbage\xff" * 50)
    r = process(path)
    assert r.decision == "RETURNED" and r.run["status"] == "done"


def test_a_pdf_that_is_not_an_invoice(tmp_path):
    path = tmp_path / "letter.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 11)
    for i, line in enumerate(["Dear team,", "Please find attached the minutes of the quarterly vendor meeting",
                              "held at our Bengaluru office. We discussed delivery schedules and packaging",
                              "improvements for the coming season. Kind regards, Operations."]):
        c.drawString(60, 760 - i * 18, line)
    c.save()
    r = process(path)
    assert r.decision == "RETURNED" and "MISSING_FIELDS" in r.codes
    assert r.run["status"] == "done"


# ----------------------------------------------------- time, currency, wording ---

@pytest.mark.parametrize("today", ["2026-11-20", "2026-12-20", "2027-03-01"])
def test_demo_outcomes_do_not_depend_on_the_day_it_is_run(monkeypatch, today):
    """The demo may be given days or weeks after it was built; every sample must still land the same way."""
    monkeypatch.setenv("INVOICE_DESK_TODAY", today)
    assert [process_sample(k).decision for k in EXPECTED] == list(EXPECTED.values())


def test_overdue_invoice_is_paid_in_the_next_run_not_back_dated(monkeypatch):
    monkeypatch.setenv("INVOICE_DESK_TODAY", "2026-12-20")
    r = process_sample("scanned_no_po")  # dated 3 Nov, net 15 days: fell due 18 Nov
    assert r.decision == "APPROVED"
    assert r.result["payment"]["pay_date"] == "2026-12-20"
    assert "PAST_DUE" in {x["code"] for x in r.result["reasons"]}
    assert "20 Dec 2026" in r.result["headline"]


def test_foreign_currency_invoice_is_shown_in_its_own_currency(tmp_path):
    from reportlab.lib.pagesizes import letter
    path = tmp_path / "us.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, h - 60, "Acme Cloud Hosting, Inc.")
    c.setFont("Helvetica", 10)
    c.drawRightString(w - 50, h - 80, "Invoice #: INV-20418")
    c.drawRightString(w - 50, h - 94, "Date: 10/28/2026")
    c.drawString(50, h - 140, "Bill To: Aurelia Home & Living Pvt. Ltd.")
    y = h - 190
    c.drawString(50, y, "Description")
    c.drawRightString(380, y, "Qty")
    c.drawRightString(460, y, "Unit Price")
    c.drawRightString(w - 50, y, "Amount")
    for desc, q, rate, amount in [("Object storage (GB-month)", "1,500", "$0.023", "$34.50"),
                                  ("Premium support plan", "1", "$400.00", "$400.00")]:
        y -= 18
        c.drawString(50, y, desc)
        c.drawRightString(380, y, q)
        c.drawRightString(460, y, rate)
        c.drawRightString(w - 50, y, amount)
    y -= 30
    c.drawRightString(460, y, "Total Due")
    c.drawRightString(w - 50, y, "$434.50")
    c.save()
    r = process(path)
    f = r.stages["extract"]["data"]["fields"]
    assert f["currency"] == "USD" and f["invoice_number"] == "INV-20418"
    assert f["invoice_date"] == "2026-10-28"  # 28 cannot be a month, so it is read month-first
    lines = r.stages["extract"]["data"]["line_items"]
    assert lines[0]["unit_price"] == 0.023  # sub-cent rates keep their precision
    assert r.decision == "NEEDS_REVIEW" and {"VENDOR_UNKNOWN", "FOREIGN_CURRENCY"} <= r.codes
    assert r.result["headline"].startswith("Held for Deepak Shah (Procurement, Vendor Master): Acme Cloud Hosting")
    assert "USD 434.50" in r.result["message"]["subject"] and "₹" not in r.result["message"]["subject"]
    # and the next run goes back to rupees
    assert "₹1,68,032.00" in process_sample("happy").result["headline"]


# ------------------------------------------- a layout the demo set does not use ---

def test_tally_style_invoice_is_read_and_approved(tmp_path):
    """Values under their labels, tax rows inside the table with no rate, and a second 'Total' in the HSN summary."""
    from tests.conftest import make_tally_invoice
    path = tmp_path / "tally.pdf"
    total = make_tally_invoice(path)
    r = process(path)
    f = r.stages["extract"]["data"]["fields"]
    assert f["invoice_number"] == "SPI/2026-27/4001"
    assert f["invoice_date"] == "2026-11-09"        # the first "Dated", not the order's
    assert f["po_number"] == "PO-2026-0412" and f["po_reference_style"] == "labelled"
    assert f["total"] == total == 168032.00         # not the 25,632.00 beside "Total" in the tax summary
    assert f["tax_amount"] == 25632.00
    lines = r.stages["extract"]["data"]["line_items"]
    assert [l["amount"] for l in lines] == [85000.00, 11400.00, 46000.00]  # CGST/SGST are not line items
    assert f["amount_in_words"].startswith("INR One Lakh Sixty Eight Thousand")
    assert r.decision == "APPROVED"


def test_a_blank_labelled_field_is_not_filled_from_the_row_below(tmp_path):
    """'Invoice No. :' with nothing after it is a blank field, even when a number-like value sits right under it."""
    path = tmp_path / "blank_number.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 15)
    c.drawString(40, h - 60, "Shreeji Packaging Industries")
    c.setFont("Helvetica", 9)
    c.drawString(40, h - 76, "GSTIN: 29AAFCS8842K1Z6")
    c.drawString(40, h - 104, "Invoice No. :")                 # left blank by the vendor
    c.drawString(330, h - 104, "Invoice Date : 10-Nov-2026")
    c.drawString(40, h - 118, "DC/2026/7781")                  # a delivery-challan number, directly underneath
    c.drawString(330, h - 118, "PO No. : PO-2026-0412")
    c.drawString(40, h - 160, "Description of Goods")
    c.drawRightString(400, h - 160, "Qty")
    c.drawRightString(480, h - 160, "Rate")
    c.drawRightString(w - 46, h - 160, "Amount")
    c.drawString(40, h - 178, "Bubble wrap roll 1 m x 100 m")
    c.drawRightString(400, h - 178, "40")
    c.drawRightString(480, h - 178, "1,150.00")
    c.drawRightString(w - 46, h - 178, "46,000.00")
    c.drawString(372, h - 204, "Taxable Value")
    c.drawRightString(w - 46, h - 204, "46,000.00")
    c.drawString(372, h - 218, "IGST @ 18%")
    c.drawRightString(w - 46, h - 218, "8,280.00")
    c.drawString(372, h - 232, "Grand Total")
    c.drawRightString(w - 46, h - 232, "54,280.00")
    c.save()
    r = process(path)
    f = r.stages["extract"]["data"]["fields"]
    assert f["invoice_number"] is None and f["invoice_date"] == "2026-11-10"
    assert r.decision == "RETURNED" and r.check("MISSING_FIELDS")["data"]["missing"] == ["invoice number"]


def test_accounting_software_layout(tmp_path):
    """'Invoice#', 'P.O.#', 'CGST9 (9%)', the unit printed under the quantity, 'Balance Due', 'Indian Rupee … Only'."""
    from tests.conftest import make_zoho_style_invoice
    path = tmp_path / "zoho.pdf"
    make_zoho_style_invoice(path)
    r = process(path)
    data = r.stages["extract"]["data"]
    f = data["fields"]
    assert (f["invoice_number"], f["po_number"], f["due_date"]) == ("SPI/2026-27/4100", "PO-2026-0412", "2026-12-09")
    assert (f["subtotal"], f["tax_amount"], f["total"]) == (142400.00, 25632.00, 168032.00)
    assert [t["label"] for t in data["tax_lines"]] == ["CGST", "SGST"]
    assert [(l["qty"], l["unit"], l["unit_price"]) for l in data["line_items"]] == [
        (2000, "pcs", 42.5), (300, "rolls", 38.0), (40, "rolls", 1150.0)]
    assert "pcs" not in data["line_items"][0]["description"]
    assert r.decision == "APPROVED"


def test_per_line_tax_columns_with_rate_before_quantity(tmp_path):
    """Description | HSN | Rate | Qty | Taxable | GST % | GST Amt | Total: the line amount is the taxable value."""
    from tests.conftest import make_gst_columns_invoice
    path = tmp_path / "gstcols.pdf"
    make_gst_columns_invoice(path)
    r = process(path)
    lines = r.stages["extract"]["data"]["line_items"]
    assert [(l["qty"], l["unit_price"], l["amount"]) for l in lines] == [
        (2000, 42.5, 85000.00), (300, 38.0, 11400.00), (40, 1150.0, 46000.00)]
    assert lines[0]["description"] == "5-ply corrugated shipping box 18 x 12 x 10 in"
    assert r.decision == "APPROVED"
