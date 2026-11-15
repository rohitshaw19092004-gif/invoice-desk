"""Regression tests for defects found by an independent review of the pipeline.

Each test reproduces the input that used to give a wrong decision, a wrong figure or a wrong
sentence, and pins the corrected behaviour.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, seed
from app.main import app
from app.pipeline import extract_llm
from tests.conftest import (PO_0412_LINES, R, SHREEJI, item_rows, make_freeform, process, process_sample,
                            shreeji_head)

GREENLEAF = dict(vendor="GreenLeaf Facility Services Pvt. Ltd.", gstin="29AAGCG6120H1Z9")
TOTALS_0412 = [[(372, "Taxable Value"), (R, "1,42,400.00", "r")], [(372, "IGST @ 18%"), (R, "25,632.00", "r")],
               [(372, "Grand Total"), (R, "1,68,032.00", "r")]]


def run(tmp_path, rows, name="t.pdf"):
    path = tmp_path / name
    make_freeform(path, rows)
    return process(path)


def approve(run_id, action="approve"):
    with TestClient(app) as c:
        return c.post(f"/api/runs/{run_id}/review", json={"action": action})


# ----------------------------------------------------- wrong approval / wrong money ---

def test_invoice_with_no_readable_line_table_is_never_approved_on_its_total(tmp_path):
    """Used to approve ₹10,03,000 against ₹1,42,400 open, because 'value ₹0.00' was within the PO."""
    r = run(tmp_path, shreeji_head() + [
        [(70, "Supply of packing material as discussed"), (R, "8,50,000.00", "r")], "",
        [(372, "IGST @ 18%"), (R, "1,53,000.00", "r")], [(372, "Grand Total"), (R, "10,03,000.00", "r")]])
    assert r.decision == "NEEDS_REVIEW" and "NO_LINE_ITEMS" in r.codes
    assert r.stages["reconcile"]["status"] == "skipped"
    assert db.get_po("PO-2026-0412")["billed_value"] == 0


def test_lump_sum_slightly_over_the_open_value_still_draws_the_po_down(pdf):
    """₹300 over, inside the ₹500 allowance: approved, and the PO must end up fully billed."""
    first = process(pdf("a.pdf", number="GLF/26-27/0501", date="04-Nov-2026", tax="inclusive",
                        lump=("Facility management services for the month of November 2026", 244614.00), **GREENLEAF))
    assert first.decision == "APPROVED" and first.run["po_number"] == "PO-2026-0362"
    po = db.get_po("PO-2026-0362")
    assert po["status"] == "fully_billed" and po["remaining_value"] == 0
    assert po["billed_value"] == 207300.00
    # a second invoice for the same month can no longer be approved against it
    second = process(pdf("b.pdf", number="GLF/26-27/0502", date="05-Nov-2026", tax="inclusive",
                         lump=("Facility management services for the month of November 2026", 244260.00), **GREENLEAF))
    assert second.decision != "APPROVED"
    assert db.get_po("PO-2026-0362")["billed_value"] == 207300.00


def test_reviewer_approved_part_lump_sum_draws_the_po_down_pro_rata(pdf):
    """Used to post with no allocations, leaving the PO fully open for a second full invoice."""
    part = process(pdf("a.pdf", number="GLF/26-27/0510", date="04-Nov-2026", tax="inclusive",
                       lump=("Facility management services for the month of November 2026", 118000.00), **GREENLEAF))
    assert part.decision == "NEEDS_REVIEW" and "BUNDLED_PARTIAL" in part.codes
    assert approve(part.run["id"]).status_code == 200
    po = db.get_po("PO-2026-0362")
    assert po["billed_value"] == pytest.approx(100000.00, abs=0.05)
    assert po["remaining_value"] == pytest.approx(107000.00, abs=0.05)
    full = process(pdf("b.pdf", number="GLF/26-27/0511", date="06-Nov-2026", tax="inclusive",
                       lump=("Facility management services for the month of November 2026", 244260.00), **GREENLEAF))
    # no longer payable against that PO: it is held (the PO now fits too poorly to be inferred, or is over-billed)
    assert full.decision == "NEEDS_REVIEW" and full.codes & {"VALUE_OVER_PO", "PO_AMBIGUOUS"}
    assert db.get_po("PO-2026-0362")["billed_value"] == pytest.approx(100000.00, abs=0.05)


def test_approving_an_invoice_that_quoted_another_vendors_po_does_not_post_to_that_po(pdf):
    r = process(pdf(number="SPI/2026-27/6002", po="PO-2026-0405", lines=PO_0412_LINES, **SHREEJI))
    assert "PO_VENDOR_MISMATCH" in r.codes
    assert r.run["result"]["posting"]["po_number"] is None
    before = db.get_po("PO-2026-0405")
    assert approve(r.run["id"]).json()["review"]["posted"] is True
    after = db.get_po("PO-2026-0405")
    assert (after["billed_value"], len(after["invoices"])) == (before["billed_value"], len(before["invoices"]))


def test_series_letters_are_not_revision_suffixes(pdf):
    """GLF/2026/A1 and GLF/2026/A2 are two invoices. The second used to be rejected as a copy of the first."""
    a1 = process(pdf("a1.pdf", number="GLF/2026/A1", date="03-Nov-2026", tax="inclusive",
                     lump=("Facility management services for the month of October 2026", 244260.00), **GREENLEAF))
    a2 = process(pdf("a2.pdf", number="GLF/2026/A2", date="05-Nov-2026", tax="inclusive",
                     lump=("Facility management services for the month of November 2026", 244260.00), **GREENLEAF))
    assert (a1.decision, a1.run["po_number"]) == ("APPROVED", "PO-2026-0361")
    assert (a2.decision, a2.run["po_number"]) == ("APPROVED", "PO-2026-0362")


def test_revision_suffix_on_a_different_order_is_a_question_not_a_rejection(pdf):
    """Same total and a '-R' number, but a different PO and different goods: not a copy."""
    r = process(pdf(number="SPI/2026-27/0982-R", po="PO-2026-0412", **SHREEJI,
                    lines=[("5-ply corrugated shipping box 18 x 12 x 10 in", 2000, "pcs", 42.50),
                           ("BOPP packaging tape 48 mm x 65 m", 300, "rolls", 38.00),
                           ("Bubble wrap roll 1 m x 100 m", 40, "rolls", 1150.00)]))
    assert r.decision == "NEEDS_REVIEW" and "POSSIBLE_REVISION" in r.codes


def test_a_date_mentioned_in_a_sentence_does_not_outrank_the_date_field(tmp_path):
    """'… challan No. 4471 dated 02-Oct-2026' used to be taken as the invoice date."""
    rows = shreeji_head(date=None) + item_rows() + [""] + TOTALS_0412
    rows.insert(5, [(40, "Date : 10-Nov-2026")])
    rows.insert(8, [(40, "Goods supplied against delivery challan No. 4471 dated 02-Oct-2026")])
    r = run(tmp_path, rows)
    assert r.stages["extract"]["data"]["fields"]["invoice_date"] == "2026-11-10"
    assert r.decision == "APPROVED" and r.result["payment"]["pay_date"] == "2026-12-10"


# ------------------------------------------- wrong hold or return on a valid invoice ---

def test_sparse_digital_pages_are_not_sent_to_ocr(tmp_path):
    """A signature page and a blank trailing page used to turn a digital file into a 'partly scanned' one."""
    rows = shreeji_head() + item_rows() + [""] + TOTALS_0412 + [None, [(360, "For Shreeji Packaging Industries")],
                                                              [(360, "Authorised Signatory")], None]
    r = run(tmp_path, rows)
    read = r.stages["read"]["data"]
    assert read["file_kind"] == "digital" and [p["method"] for p in read["pages"]] == ["text", "text", "text"]
    assert "LOW_OCR_CONFIDENCE" not in r.codes and r.decision == "APPROVED"


def test_table_with_only_description_and_amount_columns(tmp_path):
    """Numbers inside descriptions ('12 staff', 'October 2026') used to be read as quantity and rate."""
    rows = [[(40, "GreenLeaf Facility Services Pvt. Ltd.")], [(40, "GSTIN: 29AAGCG6120H1Z9")], "",
            [(40, "Invoice No. : GLF/26-27/0520"), (330, "PO No. : PO-2026-0361")], [(40, "Invoice Date : 03-Nov-2026")], "",
            [(46, "Sl"), (70, "Description"), (R, "Amount", "r")],
            [(46, "1"), (70, "Housekeeping staff deployment, 12 staff - October 2026"), (R, "1,62,000.00", "r")],
            [(46, "2"), (70, "Cleaning consumables and supplies - October 2026"), (R, "28,000.00", "r")],
            [(46, "3"), (70, "Site supervisor charges - October 2026"), (R, "17,000.00", "r")], "",
            [(372, "Taxable Value"), (R, "2,07,000.00", "r")], [(372, "IGST @ 18%"), (R, "37,260.00", "r")],
            [(372, "Grand Total"), (R, "2,44,260.00", "r")]]
    r = run(tmp_path, rows)
    lines = r.stages["extract"]["data"]["line_items"]
    assert [(l["qty"], l["amount"]) for l in lines] == [(None, 162000.00), (None, 28000.00), (None, 17000.00)]
    assert lines[0]["description"].endswith("October 2026")  # the year is not stripped as if it were an HSN code
    assert r.decision == "APPROVED" and db.get_po("PO-2026-0361")["status"] == "fully_billed"


@pytest.mark.parametrize("number", ["INV 1047", "A 1234", "INV/2026-11/7", "INV-2026-11-07", "2026/000123", "7781"])
def test_invoice_number_shapes(tmp_path, number):
    rows = shreeji_head(number=number) + item_rows() + [""] + TOTALS_0412
    r = run(tmp_path, rows)
    assert r.stages["extract"]["data"]["fields"]["invoice_number"] == number
    assert r.decision == "APPROVED"


def test_total_tax_row_is_not_counted_as_another_tax_line(tmp_path):
    rows = shreeji_head() + item_rows() + ["", [(372, "Taxable Value"), (R, "1,42,400.00", "r")],
                                           [(372, "CGST @ 9%"), (R, "12,816.00", "r")],
                                           [(372, "SGST @ 9%"), (R, "12,816.00", "r")],
                                           [(372, "Total Tax"), (R, "25,632.00", "r")],
                                           [(372, "Grand Total"), (R, "1,68,032.00", "r")]]
    r = run(tmp_path, rows)
    assert r.stages["extract"]["data"]["fields"]["tax_amount"] == 25632.00
    assert r.decision == "APPROVED"


def test_amount_in_words_that_wraps_onto_a_second_line(tmp_path):
    rows = shreeji_head() + item_rows() + [""] + TOTALS_0412 + [
        "", [(40, "Amount in words: Rupees One Lakh Sixty Eight Thousand")], [(40, "Thirty Two Only")]]
    r = run(tmp_path, rows)
    assert "WORDS_MISMATCH" not in r.codes and r.decision == "APPROVED"
    assert r.stages["extract"]["data"]["fields"]["amount_in_words"].endswith("Thirty Two Only")


def test_amount_in_words_cut_short_is_not_treated_as_a_mismatch(tmp_path):
    rows = shreeji_head() + item_rows() + [""] + TOTALS_0412 + [
        "", [(40, "Amount in words: Rupees One Lakh Sixty Eight Thousand")], [(40, "(continued overleaf)")]]
    r = run(tmp_path, rows)
    assert "WORDS_MISMATCH" not in r.codes and r.decision == "APPROVED"


def test_two_page_invoice_with_page_footer_and_repeated_header(tmp_path):
    """'Page 1 of 2' used to become a ₹2.00 line item described by the repeated table header."""
    lines = PO_0412_LINES
    rows = shreeji_head() + item_rows(lines[:2]) + ["", [(40, "Continued on next page"), (R, "Page 1 of 2", "r")], None,
                                                    [(40, "Shreeji Packaging Industries")], ""]
    rows += item_rows(lines[2:])[:1] + [[(46, "3"), (70, lines[2][0]), (398, "40", "r"), (408, "rolls"),
                                         (480, "1,150.00", "r"), (R, "46,000.00", "r")], ""] + TOTALS_0412
    rows += ["", [(R, "Page 2 of 2", "r")]]
    r = run(tmp_path, rows)
    lines_read = r.stages["extract"]["data"]["line_items"]
    assert [l["amount"] for l in lines_read] == [85000.00, 11400.00, 46000.00]
    assert all("Page" not in l["description"] and "Shreeji" not in l["description"] for l in lines_read)
    assert r.decision == "APPROVED"


def test_unrelated_company_with_a_similar_name_is_not_matched(pdf):
    for name in ["Swiftline Couriers Pvt. Ltd.", "Orbit Printers", "Shreeji Packaging Solutions Pvt Ltd"]:
        seed.reset_demo()
        r = process(pdf(vendor=name, gstin=None, number="X/2026/1", po="PO-2026-0412", lines=PO_0412_LINES))
        assert r.decision == "NEEDS_REVIEW" and "VENDOR_UNKNOWN" in r.codes, name


def test_credit_note_is_not_paid_as_an_invoice(tmp_path):
    rows = shreeji_head() + item_rows() + [""] + TOTALS_0412
    rows.insert(0, [(260, "CREDIT NOTE")])
    r = run(tmp_path, rows)
    assert r.decision == "NEEDS_REVIEW" and r.check("NOT_AN_INVOICE")["route"] == "ap"
    assert db.get_po("PO-2026-0412")["billed_value"] == 0


def test_pages_beyond_the_limit_are_reported_not_silently_dropped(tmp_path):
    rows = shreeji_head() + item_rows() + [""] + TOTALS_0412
    for n in range(13):
        rows += [None, [(40, f"Annexure {n + 1}: shipment-wise delivery details for this invoice, listed by date")]]
    r = run(tmp_path, rows)
    assert r.decision == "NEEDS_REVIEW" and "PAGES_TRUNCATED" in r.codes


# ----------------------------------------------------------------- wrong sentences ---

def test_masked_variance_note_only_when_the_total_really_would_have_passed(pdf):
    """A small invoice with a price increase is not 'hidden by a short quantity'."""
    r = process(pdf(vendor="Orbit Print & Signage", gstin="29ABXPO5567L1Z2", number="OPS/26-27/0600",
                    po="PO-2026-0405", lines=[("Festive season product catalogue, A4, 24 pages, 4-colour", 1000, "copies", 20.00)]))
    assert "PRICE_VARIANCE" in r.codes and "MASKED_VARIANCE" not in r.codes
    assert "MASKED_VARIANCE" in process_sample("masked_variance").codes  # the real case still gets it


def test_single_open_po_is_described_as_the_only_one(tmp_path):
    rows = shreeji_head(po=None) + item_rows() + [""] + TOTALS_0412
    r = run(tmp_path, rows)
    detail = r.check("PO_INFERRED")["detail"]
    assert "only open PO for Shreeji Packaging Industries" in detail
    assert "next candidate" not in detail and "Industries's" not in detail
    assert r.decision == "APPROVED"


def test_excess_is_measured_from_zero_once_a_line_is_overbilled(pdf):
    """After a reviewer accepts an over-billing, the next invoice must not report a negative open quantity."""
    over = process_sample("split_overbill")
    assert approve(over.run["id"]).status_code == 200
    assert db.get_po("PO-2026-0405")["lines"][0]["remaining_qty"] == -500
    nxt = process(pdf(vendor="Orbit Print & Signage", gstin="29ABXPO5567L1Z2", number="OPS/26-27/0601",
                      po="PO-2026-0405", lines=[("Festive season product catalogue, A4, 24 pages, 4-colour", 100, "copies", 18.40)]))
    q = nxt.check("QTY_OVER_PO")
    assert q["data"]["excess_qty"] == 100 and q["data"]["excess_value"] == 1840.00
    assert "nothing is still open" in q["title"] and "-" not in q["title"]


def test_foreign_currency_invoice_is_not_compared_with_a_rupee_po(tmp_path):
    rows = shreeji_head() + item_rows() + ["", [(372, "Subtotal (USD)"), (R, "$142,400.00", "r")],
                                           [(372, "Tax 18%"), (R, "$25,632.00", "r")],
                                           [(372, "Total Due (USD)"), (R, "$168,032.00", "r")]]
    r = run(tmp_path, rows)
    assert r.stages["extract"]["data"]["fields"]["currency"] == "USD"
    assert r.decision == "NEEDS_REVIEW" and "FOREIGN_CURRENCY" in r.codes
    assert r.stages["reconcile"]["status"] == "skipped"
    po_text = " ".join(c["title"] + c["detail"] for c in r.stages["po_match"]["checks"])
    assert "₹1,42,400.00" in po_text and "USD 142,400" not in po_text  # the PO is quoted in rupees
    assert r.run["result"]["posting"] is None
    review = approve(r.run["id"]).json()["review"]
    assert review["posted"] is False and "USD" in review["note_system"]
    assert db.get_po("PO-2026-0412")["billed_value"] == 0


def test_price_far_below_the_po_is_questioned_not_welcomed(pdf):
    """A different, cheaper item that resembles a PO line must not quietly use up that line's quantity."""
    r = process(pdf(number="SPI/2026-27/6010", po="PO-2026-0412", **SHREEJI,
                    lines=[("3-ply corrugated shipping box 18 x 12 x 10 in", 2000, "pcs", 21.80)]))
    assert r.decision == "NEEDS_REVIEW" and r.check("PRICE_FAR_BELOW")["route"] == "buyer"


def test_tolerated_price_difference_does_not_fail_the_value_check(pdf):
    """+1.5% on every line of a full invoice is inside the 2% price tolerance and must be approved."""
    lines = [(d, q, u, round(p * 1.015, 2)) for d, q, u, p in PO_0412_LINES]
    r = process(pdf(number="SPI/2026-27/6011", po="PO-2026-0412", lines=lines, **SHREEJI))
    assert r.decision == "APPROVED" and "VALUE_OVER_PO" not in r.codes


def test_po_inference_margin_exactly_at_the_minimum_is_accepted():
    """0.95 - 0.85 is 0.0999… in floating point; it must still count as a 10-point lead."""
    db.set_policy({"po_inference_min_margin": 18})  # the scanned sample leads by exactly 18 points
    r = process_sample("scanned_no_po")
    assert r.check("PO_INFERRED")["data"]["margin"] == pytest.approx(0.18, abs=0.006)
    assert r.decision == "APPROVED"


# ------------------------------------------------------------------- crash and race ---

def test_ai_line_with_quantity_but_no_unit_price_does_not_crash(monkeypatch):
    monkeypatch.setenv("INVOICE_DESK_AI", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    out = extract_llm.InvoiceOut(
        vendor_name="Shreeji Packaging Industries", vendor_tax_id="29AAFCS8842K1Z6", invoice_number="SPI/2026-27/1047",
        invoice_date="2026-11-04", po_number="PO-2026-0412", currency="INR",
        line_items=[extract_llm.LineItemOut(description=d, quantity=q, unit=u, unit_price=None, amount=q * p)
                    for d, q, u, p in PO_0412_LINES],
        subtotal=142400.00, tax_lines=[extract_llm.TaxLineOut(label="IGST", rate_percent=18, amount=25632.00)],
        total=168032.00, confidence=0.9)
    monkeypatch.setattr(extract_llm, "_call_model", lambda *_: out)
    r = process_sample("happy")
    assert "SYSTEM_ERROR" not in r.codes
    assert r.decision == "APPROVED"  # the rate is implied by amount / quantity and matches the PO


def test_a_finished_run_always_carries_its_closing_event():
    r = process_sample("happy")
    assert r.run["status"] == "done" and r.events[-1]["type"] == "run_completed"
    assert r.events[-1]["payload"]["decision"]["decision"] == r.run["decision"]
