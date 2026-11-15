"""The pipeline's stages, in the order they run.

  intake -> read -> extract -> validate -> vendor -> po_match -> duplicate -> reconcile -> decide -> act

Design rule: the AI (or the built-in parser) only *reads*. Everything from `validate`
onwards is deterministic code applying the policy, so the same invoice always gets the
same decision and every decision can be traced to a named check.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from .. import config, db
from ..money import (D, MONTH_NAMES, find_periods, fmt_date, fmt_inr, fmt_qty, fmt_rate, parse_date, q2,
                     set_currency, to_float, words_to_number)


def fmt_po(value) -> str:
    """Purchase-order money is always in the buyer's currency, whatever the invoice is in."""
    return fmt_inr(value, currency=config.BUYER["currency"])
from ..seed import normalize_invoice_number
from . import extract_llm, extract_rules, reader
from .extract_rules import Extraction
from .reader import Document, UnreadableDocument, read_pdf, render_previews

ZERO = Decimal("0")


# ------------------------------------------------------------ data model ---

@dataclass
class Check:
    status: str  # pass | info | warn | fail
    title: str
    detail: str = ""
    code: str = ""
    action: Optional[str] = None  # REVIEW | RETURN | REJECT  (None = does not block)
    route: Optional[str] = None  # treasury | procurement | buyer | ap
    stage: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StageResult:
    status: str  # ok | warn | fail | skipped
    summary: str
    checks: list[Check] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def _status(checks: list[Check]) -> str:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "ok"


@dataclass
class Ctx:
    run_id: str
    path: Path
    filename: str
    policy: dict
    today: date
    log: Callable[[str], None]
    file_hash: str = ""
    doc: Optional[Document] = None
    ex: Optional[Extraction] = None
    extract_note: Optional[str] = None
    amounts: dict = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    vendor: Optional[dict] = None
    po: Optional[dict] = None
    po_method: str = "none"
    po_confidence: Optional[float] = None
    duplicate: Optional[dict] = None
    recon: Optional[dict] = None
    allocations: list[dict] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    halted: Optional[str] = None
    decision: Optional[dict] = None

    def pol(self, key: str) -> Decimal:
        return D(self.policy[key])


# --------------------------------------------------------------- helpers ---

def _mask(account: Optional[str]) -> str:
    if not account:
        return "—"
    return "••••" + account[-4:]


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9. ]+", " ", (s or "").lower())).strip()


_SUFFIXES = r"\b(pvt|private|ltd|limited|llp|inc|co|company|corp|corporation|the|m/s|ms)\b\.?"


def _norm_company(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(_SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def _norm_po(s: str) -> str:
    return "|".join(str(int(t)) if t.isdigit() else t for t in re.findall(r"[A-Za-z]+|\d+", (s or "").upper()))


def _po_summary(po: dict) -> dict:
    return {
        "po_number": po["po_number"], "description": po["description"], "po_date": po["po_date"],
        "vendor_id": po["vendor_id"], "value": po["value"], "billed_value": po["billed_value"],
        "remaining_value": po["remaining_value"], "status": po["status"], "tax_rate": po["tax_rate"],
        "buyer_name": po["buyer_name"], "buyer_role": po["buyer_role"], "buyer_email": po["buyer_email"],
        "lines": [{"line_no": l["line_no"], "description": l["description"], "qty": l["qty"], "unit": l["unit"],
                   "unit_price": l["unit_price"], "value": l["value"], "billed_qty": l["billed_qty"],
                   "remaining_qty": l["remaining_qty"]} for l in po["lines"]],
        "invoices": po["invoices"],
    }


def _rate(x) -> Optional[float]:
    return None if x is None else round(float(x), 4)


_LOWER_FIRST = {"Line", "The", "Invoice", "No", "Required", "Duplicate", "Exact", "Possible", "Looks", "Lump", "Above",
                "Tax", "Amount", "Subtotal", "A", "An", "This", "Quoted", "Unexpected"}


def _mid_sentence(title: str) -> str:
    """Lower the first letter so a check title can follow a colon, unless it starts with a name."""
    first = title.split(" ", 1)[0]
    return title[0].lower() + title[1:] if first in _LOWER_FIRST else title


def _n(x) -> str:
    """A number without trailing zeros: 18, 2.5, 70 (never 7E+1)."""
    text = f"{D(x):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


_SINGULAR = {"units": "unit", "rolls": "roll", "copies": "copy", "shipments": "shipment", "months": "month",
             "lots": "lot", "sets": "set", "boxes": "box", "trips": "trip", "hours": "hour", "hrs": "hour",
             "days": "day", "pcs": "piece", "nos": "unit", "kgs": "kg", "reams": "ream", "sheets": "sheet"}


def _singular(unit: Optional[str]) -> str:
    return _SINGULAR.get((unit or "unit").lower(), unit or "unit")


def _qty_unit(qty, unit: Optional[str]) -> str:
    """'1 unit', '1,000 copies'."""
    unit = unit or "units"
    return f"{fmt_qty(qty)} {_singular(unit) if D(qty) == 1 and unit.lower() not in ('nos', 'pcs') else unit}"


def _plural(n, word: str, plural: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def extraction_to_dict(ex: Extraction) -> dict:
    return {
        "method": ex.method,
        "model": ex.model,
        "confidence": ex.confidence(),
        "fields": {
            "vendor_name": ex.vendor_name, "vendor_gstin": ex.vendor_gstin, "invoice_number": ex.invoice_number,
            "invoice_date": ex.invoice_date, "due_date": ex.due_date, "po_number": ex.po_number,
            "po_reference_style": ex.po_reference_style, "currency": ex.currency,
            "subtotal": to_float(ex.subtotal), "tax_amount": to_float(ex.tax_amount),
            "tax_rate": float(ex.tax_rate) if ex.tax_rate is not None else None, "total": to_float(ex.total),
            "tax_inclusive": ex.tax_inclusive, "amount_in_words": ex.amount_in_words,
            "bank_account": ex.bank_account, "ifsc": ex.ifsc,
            "service_periods": [f"{MONTH_NAMES[m]} {y}" for y, m in ex.periods],
        },
        "tax_lines": [{"label": t["label"], "rate": float(t["rate"]) if t.get("rate") is not None else None,
                       "amount": to_float(t["amount"])} for t in ex.tax_lines],
        "line_items": [{"description": li.description, "qty": float(li.qty) if li.qty is not None else None,
                        "unit": li.unit, "unit_price": _rate(li.unit_price), "amount": to_float(li.amount),
                        "confidence": li.confidence, "page": li.page, "bbox": li.bbox,
                        "derived_price": li.derived_price} for li in ex.line_items],
        "meta": {k: asdict(v) for k, v in ex.meta.items()},
        "notes": ex.notes,
    }


# ================================================================ 1 intake ===

def intake(ctx: Ctx) -> StageResult:
    data = ctx.path.read_bytes()
    ctx.file_hash = hashlib.sha256(data).hexdigest()
    db.update_run(ctx.run_id, file_hash=ctx.file_hash)
    size_kb = round(len(data) / 1024, 1)
    checks: list[Check] = []
    if b"%PDF" not in data[:1024]:
        ctx.halted = "not_pdf"
        checks.append(Check("fail", "The attachment is not a PDF",
                            f"{ctx.filename} does not have a PDF signature, so it cannot be read as an invoice. "
                            "Nothing further was attempted.", "NOT_A_PDF", action="RETURN"))
        return StageResult("fail", f"{ctx.filename} is not a PDF", checks,
                           {"filename": ctx.filename, "size_kb": size_kb, "fingerprint": ctx.file_hash[:12]})
    checks.append(Check("pass", "Valid PDF received", f"{ctx.filename}, {size_kb} KB."))
    prior = db.query_one(
        "SELECT id, decision FROM runs WHERE file_hash = ? AND id != ? AND status = 'done' ORDER BY seq DESC LIMIT 1",
        (ctx.file_hash, ctx.run_id),
    )
    if prior:
        label = DECISION_LABELS.get(prior["decision"], prior["decision"] or "no decision")
        checks.append(Check("info", f"This exact file has been through the desk before ({prior['id']})",
                            f"A byte-identical file was processed in {prior['id']} with the outcome “{label}”. "
                            "Whether that makes this a duplicate is decided at the duplicate check, against "
                            "invoices that were actually accepted.", "SEEN_BEFORE", data={"run_id": prior["id"]}))
    else:
        checks.append(Check("pass", "File fingerprinted", f"SHA-256 {ctx.file_hash[:12]}… recorded for the audit trail."))
    return StageResult(_status(checks), f"{ctx.filename} · {size_kb} KB · fingerprint {ctx.file_hash[:8]}", checks,
                       {"filename": ctx.filename, "size_kb": size_kb, "fingerprint": ctx.file_hash[:12]})


# ================================================================== 2 read ===

def read(ctx: Ctx) -> StageResult:
    def on_event(kind: str, info: dict) -> None:
        if kind == "page_text":
            ctx.log(f"Page {info['page']}: text layer found, {info['words']} words read directly.")
        elif kind == "ocr_start":
            ctx.log(f"Page {info['page']}: no text layer. The page is an image, so running OCR at "
                    f"{reader.OCR_DPI} dpi…")
        elif kind == "ocr_done":
            ctx.log(f"Page {info['page']}: OCR read {info['words']} words at {info['confidence']}% confidence.")

    checks: list[Check] = []
    try:
        ctx.doc = read_pdf(ctx.path, on_event)
    except UnreadableDocument as exc:
        ctx.halted = "unreadable"
        checks.append(Check("fail", "The document could not be opened", str(exc), "UNREADABLE", action="RETURN"))
        return StageResult("fail", "Unreadable document", checks, {"pages": []})
    doc = ctx.doc
    try:
        render_previews(ctx.path, config.PREVIEW_DIR / ctx.run_id)
    except Exception:  # previews are a convenience for the viewer, never a reason to fail
        pass
    pages = [{"number": p.number, "method": p.method, "words": p.word_count, "ocr_confidence": p.ocr_confidence,
              "width": p.width, "height": p.height} for p in doc.pages]
    data = {"pages": pages, "file_kind": doc.file_kind, "ocr_confidence": doc.ocr_confidence,
            "text_preview": [r.flat for r in doc.rows[:14]]}
    words = sum(p.word_count for p in doc.pages)
    if doc.file_kind == "empty":
        ctx.halted = "unreadable"
        checks.append(Check("fail", "No text could be read from the document",
                            "The pages contain no text layer and OCR found nothing legible.", "UNREADABLE",
                            action="RETURN"))
        return StageResult("fail", "Nothing legible on the page", checks, data)

    n_pages = _plural(len(doc.pages), "page")
    if doc.pages_in_file > len(doc.pages):
        checks.append(Check("fail", f"Only the first {len(doc.pages)} of {doc.pages_in_file} pages were read",
                            f"The file is longer than the {config.MAX_PAGES}-page limit. Lines or totals on the later "
                            "pages have not been seen, so a person has to check the invoice.", "PAGES_TRUNCATED",
                            action="REVIEW", route="ap"))
    if doc.file_kind == "digital":
        checks.append(Check("pass", "Digital PDF: text read directly from the file",
                            f"{words} words across {n_pages}. No OCR needed, so characters are exact."))
        summary = f"Digital PDF · {n_pages} · {words} words read from the text layer"
    else:
        floor = ctx.pol("ocr_min_confidence")
        conf = D(doc.ocr_confidence or 0)
        kind = "Scanned image" if doc.file_kind == "scanned" else "Partly scanned"
        if conf >= floor:
            checks.append(Check("pass", f"{kind}: read with OCR at {_n(conf)}% confidence",
                                f"{words} words recovered across {n_pages}. The minimum to proceed without a person "
                                f"is {_n(floor)}%."))
        else:
            checks.append(Check("fail", f"OCR confidence is low ({_n(conf)}%)",
                                f"The scan is too poor to trust: {_n(conf)}% against a minimum of {_n(floor)}%. "
                                "A person should check the extracted values against the image.",
                                "LOW_OCR_CONFIDENCE", action="REVIEW", route="ap"))
        checks.append(Check("info", "OCR values are corroborated later",
                            "Because OCR can misread a digit, the amounts read here are cross-checked against the "
                            "amount in words, the invoice arithmetic and the purchase order before anything is paid."))
        summary = f"{kind} · {n_pages} · OCR read {words} words at {_n(conf)}% confidence"
    return StageResult(_status(checks), summary, checks, data)


# =============================================================== 3 extract ===

def extract(ctx: Ctx) -> StageResult:
    doc = ctx.doc
    ex: Optional[Extraction] = None
    checks: list[Check] = []
    if config.ai_enabled():
        ctx.log(f"Sending the document to Claude ({config.AI_MODEL}) to read the fields…")
        try:
            ex = extract_llm.extract(ctx.path, doc)
            ctx.log("Claude returned structured fields.")
        except extract_llm.AIUnavailable as exc:
            ctx.extract_note = f"AI extraction was not available ({exc}); the built-in parser was used instead."
            ctx.log(ctx.extract_note)
    if ex is None:
        ctx.log("Locating labelled fields and the line-item table…")
        ex = extract_rules.extract(doc)
    ctx.ex = ex
    set_currency(ex.currency)
    method = f"Claude ({ex.model})" if ex.method == "ai" else "the built-in parser"
    if ctx.extract_note:
        checks.append(Check("info", "Fell back to the built-in parser", ctx.extract_note))

    def found(key: str, label: str, value, shown: Optional[str] = None, required: bool = True):
        meta = ex.meta.get(key)
        note = f" ({meta.note})" if meta and meta.note else ""
        if value is None or value == "":
            checks.append(Check("warn" if required else "info", f"{label}: not found on the document",
                                "Left empty rather than guessed." if required else ""))
        else:
            checks.append(Check("pass", f"{label}: {shown or value}{note}"))

    found("vendor_name", "Vendor", ex.vendor_name,
          f"{ex.vendor_name}" + (f" · tax ID {ex.vendor_gstin}" if ex.vendor_gstin else ""))
    found("invoice_number", "Invoice number", ex.invoice_number)
    found("invoice_date", "Invoice date", ex.invoice_date, fmt_date(ex.invoice_date) if ex.invoice_date else None)
    if ex.po_number:
        style = "mentioned inside a sentence, not in a labelled field" if ex.po_reference_style == "in running text" \
            else "labelled field"
        checks.append(Check("pass", f"PO reference: {ex.po_number} ({style})"))
    else:
        checks.append(Check("info", "PO reference: none on the document",
                            "The purchase order will have to be inferred from the vendor, the amount and the "
                            "description."))
    if ex.line_items:
        bundled = len(ex.line_items) == 1 and ex.line_items[0].qty is None
        checks.append(Check("pass", f"{_plural(len(ex.line_items), 'line item')} read" +
                            (" (one bundled lump-sum line, no quantity or rate)" if bundled else " with quantity, rate and amount")))
    else:
        checks.append(Check("warn", "Line items: no table found", "Only header-level amounts are available."))
    if ex.total is not None:
        tax_note = "tax included in the total, not shown separately" if ex.tax_inclusive else (
            " + ".join(f"{t['label']} {fmt_inr(t['amount'])}" for t in ex.tax_lines) if ex.tax_lines else "no tax lines")
        checks.append(Check("pass", f"Total: {fmt_inr(ex.total)} ({tax_note})"))
    else:
        checks.append(Check("warn", "Total: not found on the document", "Left empty rather than guessed."))

    n_fields = sum(1 for v in (ex.vendor_name, ex.vendor_gstin, ex.invoice_number, ex.invoice_date, ex.due_date,
                               ex.po_number, ex.subtotal, ex.tax_amount, ex.total, ex.bank_account, ex.ifsc,
                               ex.amount_in_words) if v is not None)
    conf = ex.confidence()
    summary = f"{n_fields} fields and {_plural(len(ex.line_items), 'line item')} read by {method}"
    if conf is not None:
        summary += f" · confidence {round(conf * 100)}%"
    return StageResult(_status(checks), summary, checks, extraction_to_dict(ex))


# ============================================================== 4 validate ===

def validate(ctx: Ctx) -> StageResult:
    ex = ctx.ex
    tol = ctx.pol("rounding_tolerance")
    checks: list[Check] = []
    missing: list[str] = []
    if not ex.invoice_number:
        missing.append("invoice number")
    if not ex.invoice_date:
        missing.append("invoice date")
    if not ex.vendor_name and not ex.vendor_gstin:
        missing.append("vendor name")

    subtotal, tax, total, rate = ex.subtotal, ex.tax_amount, ex.total, ex.tax_rate
    line_amounts = [li.amount for li in ex.line_items if li.amount is not None]
    lines_sum = sum(line_amounts, ZERO) if line_amounts else None
    gross_lines = False

    # ---- tax buried in the total: split it out so the invoice is comparable with a pre-tax PO
    if ex.tax_inclusive and total is not None and tax is None:
        assumed = rate is None
        if assumed:
            rate = D(config.DEFAULT_TAX_RATE)
        subtotal = q2(total / (1 + rate / 100))
        tax = q2(total - subtotal)
        gross_lines = True
        checks.append(Check(
            "warn" if assumed else "info", "Tax is included in the total, so it was split out",
            f"The invoice shows {fmt_inr(total)} inclusive of tax and no separate tax line. "
            f"At {_n(rate)}% that is {fmt_inr(subtotal)} before tax + {fmt_inr(tax)} tax. "
            "Purchase orders are recorded before tax, so this is the figure used for matching."
            + (f" No rate is printed; the standard {_n(rate)}% was assumed." if assumed else ""),
            "TAX_NORMALISED" if not assumed else "TAX_RATE_ASSUMED",
            action="REVIEW" if assumed else None, route="ap" if assumed else None))
    else:
        if subtotal is None and lines_sum is not None:
            subtotal = lines_sum
            checks.append(Check("info", "No subtotal printed; the sum of the lines is used",
                                f"Lines add up to {fmt_inr(subtotal)}."))
        elif subtotal is None and total is not None and tax is not None:
            subtotal = q2(total - tax)
            checks.append(Check("info", "No subtotal printed; total minus tax is used",
                                f"{fmt_inr(total)} less {fmt_inr(tax)} tax is {fmt_inr(subtotal)} before tax."))
        if tax is None and total is not None and subtotal is not None:
            tax = q2(total - subtotal)
            if abs(tax) > tol:
                checks.append(Check("info", "Tax is not itemised", f"{fmt_inr(tax)} is implied by total minus subtotal."))
            else:
                tax = ZERO
    if total is None:
        if subtotal is not None and tax is not None and subtotal > 0:
            total = q2(subtotal + tax)
            checks.append(Check("fail", "No total is printed on the invoice",
                                f"A total of {fmt_inr(total)} can be computed from the lines and tax, but a payable "
                                "invoice should state it. A person should confirm before it is paid.",
                                "TOTAL_DERIVED", action="REVIEW", route="ap"))
        else:
            missing.append("invoice total")

    if missing:
        detail = ("A payable invoice must carry these. Without them it cannot be posted, tracked or checked for "
                  "duplicates, so it goes back to the vendor rather than being guessed at.")
        if "invoice date" in missing and parse_date(ctx.doc.text)[0] is not None:
            detail += (" There are other dates on the page (for example a billing period or an order date), but none "
                       "is labelled as the invoice date, so none was used.")
        checks.append(Check("fail", "Required information is missing: " + ", ".join(missing), detail,
                            "MISSING_FIELDS", action="RETURN", data={"missing": missing}))
    else:
        checks.append(Check("pass", "All required fields are present",
                            "Vendor, invoice number, invoice date and total were all found."))

    if ex.doc_type:
        checks.append(Check("fail", f"The document is a {ex.doc_type.lower()}, not an invoice",
                            f"The page is titled “{ex.doc_type}”. Only invoices are paid through this process; "
                            "a credit note reduces what is owed, and a quotation, proforma or challan is not a "
                            "demand for payment. A person should book it the right way.", "NOT_AN_INVOICE",
                            action="REVIEW", route="ap"))
    if not ex.line_items and not missing:
        checks.append(Check("fail", "No line items could be read",
                            "The header and the totals were found, but not the table saying what is being billed. "
                            "Without lines the invoice cannot be compared with a purchase order, so it is not paid "
                            "on its total alone.", "NO_LINE_ITEMS", action="REVIEW", route="ap"))

    # ---- arithmetic
    bad_lines = []
    checked_lines = 0
    for i, li in enumerate(ex.line_items, start=1):
        if li.qty is not None and li.unit_price is not None and li.amount is not None and not li.derived_price:
            checked_lines += 1
            if abs(li.qty * li.unit_price - li.amount) > tol:
                bad_lines.append(f"line {i}: {fmt_qty(li.qty)} × {fmt_rate(li.unit_price)} = "
                                 f"{fmt_inr(li.qty * li.unit_price)}, but the invoice shows {fmt_inr(li.amount)}")
    if bad_lines:
        checks.append(Check("fail", "A line's quantity × rate does not equal its amount", "; ".join(bad_lines) + ".",
                            "LINE_ARITHMETIC", action="REVIEW", route="ap"))
    elif checked_lines:
        checks.append(Check("pass", f"Quantity × rate equals the amount on {_plural(checked_lines, 'line')}"))

    if lines_sum is not None:
        target, target_name = (total, "total") if gross_lines else (subtotal, "subtotal")
        if target is not None:
            if abs(lines_sum - target) <= tol:
                checks.append(Check("pass", f"Line items add up to the {target_name} ({fmt_inr(target)})"))
            else:
                checks.append(Check("fail", f"Line items do not add up to the {target_name}",
                                    f"Lines total {fmt_inr(lines_sum)} but the {target_name} says {fmt_inr(target)} "
                                    f"(difference {fmt_inr(lines_sum - target)}). Either the vendor made an error or a "
                                    "line was misread.", "LINES_VS_SUBTOTAL", action="REVIEW", route="ap"))
    if subtotal is not None and tax is not None and total is not None and not gross_lines:
        if abs(subtotal + tax - total) <= tol:
            checks.append(Check("pass", f"Subtotal + tax = total ({fmt_inr(subtotal)} + {fmt_inr(tax)} = {fmt_inr(total)})"))
        else:
            checks.append(Check("fail", "Subtotal + tax does not equal the total",
                                f"{fmt_inr(subtotal)} + {fmt_inr(tax)} = {fmt_inr(subtotal + tax)}, but the total says "
                                f"{fmt_inr(total)}.", "TOTAL_ARITHMETIC", action="REVIEW", route="ap"))

    # ---- amount in words: an independent reading of the total
    if ex.amount_in_words and total is not None:
        in_words = words_to_number(ex.amount_in_words)
        if in_words is not None:
            if abs(in_words - total) < 1:
                checks.append(Check("pass", "Amount in words agrees with the figure",
                                    f"“{ex.amount_in_words}” = {fmt_inr(total)}."
                                    + (" This independently confirms the OCR read the total correctly."
                                       if ctx.doc.is_ocr else "")))
            elif not ex.amount_in_words_complete:
                # The sentence does not end in "Only": it may run on in a way that was not captured,
                # so a mismatch here is not evidence of anything.
                checks.append(Check("info", "Amount in words could not be read to the end",
                                    f"“{ex.amount_in_words}…” was read but the sentence appears to continue, so it "
                                    "was not used as a cross-check."))
            else:
                checks.append(Check("fail", "Amount in words disagrees with the figure",
                                    f"The words say {fmt_inr(in_words)}; the figures say {fmt_inr(total)}. One of them "
                                    "is wrong.", "WORDS_MISMATCH", action="REVIEW", route="ap"))

    # ---- date sanity
    if ex.invoice_date:
        inv_date = date.fromisoformat(ex.invoice_date)
        age = (ctx.today - inv_date).days
        stale = int(ctx.pol("stale_invoice_days"))
        if age < 0:
            checks.append(Check("fail", f"Invoice is dated in the future ({fmt_date(inv_date)})",
                                f"Today is {fmt_date(ctx.today)}. Post-dated invoices are not paid without a check.",
                                "FUTURE_DATED", action="REVIEW", route="ap"))
        elif age > stale:
            checks.append(Check("fail", f"Invoice is {age} days old",
                                f"Dated {fmt_date(inv_date)}; anything older than {stale} days needs confirmation that "
                                "it is still payable and was not settled another way.", "STALE_INVOICE",
                                action="REVIEW", route="ap"))
        else:
            checks.append(Check("pass", f"Invoice date {fmt_date(inv_date)} is plausible ({_plural(age, 'day')} old)"))
        if ex.date_ambiguous:
            raw = ex.meta.get("invoice_date").raw if ex.meta.get("invoice_date") else ""
            alt = None
            try:
                alt = date(inv_date.year, inv_date.day, inv_date.month)
            except ValueError:
                pass
            other = ""
            if alt:
                other = (f" The other reading, {fmt_date(alt)}, is in the future, so day-first is the only one that fits."
                         if alt > ctx.today else
                         f" The other reading, {fmt_date(alt)}, would make the invoice {(ctx.today - alt).days} days old.")
            checks.append(Check("info", f"Date “{raw}” read day-first as {fmt_date(inv_date)}",
                                "All-numeric dates can be read two ways. Indian invoices print the day first." + other,
                                "DATE_DAY_FIRST"))

    if ex.currency and ex.currency != config.BUYER["currency"]:
        checks.append(Check("fail", f"Invoice is in {ex.currency}",
                            f"Purchase orders are raised in {config.BUYER['currency']}. A currency conversion needs a "
                            "person.", "FOREIGN_CURRENCY", action="REVIEW", route="ap"))

    # ---- hand normalised figures to the later stages
    ctx.amounts = {"subtotal": subtotal, "tax": tax, "total": total, "tax_rate": rate, "gross_lines": gross_lines}
    ctx.lines = []
    for li in ex.line_items:
        amount = li.amount
        price = li.unit_price
        if gross_lines and rate is not None:
            amount = q2(amount / (1 + rate / 100)) if amount is not None else None
            price = q2(price / (1 + rate / 100)) if price is not None else None
        ctx.lines.append({"description": li.description, "qty": li.qty, "unit": li.unit, "unit_price": price,
                          "amount": amount})
    passed = sum(1 for c in checks if c.status == "pass")
    problems = sum(1 for c in checks if c.status == "fail")
    summary = (f"{_plural(problems, 'check')} failed, {passed} passed" if problems
               else f"All {_plural(passed, 'check')} passed" + (" · tax split out of the total" if gross_lines else ""))
    return StageResult(_status(checks), summary, checks, {
        "subtotal": to_float(subtotal), "tax": to_float(tax), "total": to_float(total),
        "tax_rate": float(rate) if rate is not None else None, "tax_inclusive": gross_lines, "missing": missing})


# ================================================================ 5 vendor ===

def vendor(ctx: Ctx) -> StageResult:
    ex = ctx.ex
    vendors = db.get_vendors()
    checks: list[Check] = []
    match = None
    method = None
    name_norm = _norm_company(ex.vendor_name or "")
    gst = (ex.vendor_gstin or "").upper()

    def name_score(v: dict) -> float:
        # Plain edit similarity over the whole name. A partial match is not enough: "Swiftline
        # Couriers" is not "Swiftline Logistics", and a name is the only evidence when no tax ID is printed.
        if not name_norm:
            return 0.0
        return max(fuzz.ratio(name_norm, _norm_company(n)) for n in [v["name"]] + v["aliases"])

    if gst:
        match = next((v for v in vendors if v["gstin"] == gst), None)
        if match:
            method = f"tax ID {gst} matches the vendor master exactly"
    if not match and name_norm:
        best = max(vendors, key=name_score)
        score = name_score(best)
        if score >= 90:
            match = best
            method = f"name is a {round(score)}% match to the vendor master"
            if gst and best["gstin"] and gst != best["gstin"]:
                dist = Levenshtein.distance(gst, best["gstin"])
                if ctx.doc.is_ocr and dist <= 2:
                    checks.append(Check("info", "Tax ID differs from the master by an OCR-sized error",
                                        f"OCR read {gst}; the master has {best['gstin']} ({dist} character"
                                        f"{'s' if dist != 1 else ''} apart). With the name matching, this is treated as "
                                        "a misread, not a different company."))
                else:
                    checks.append(Check("fail", "Tax ID on the invoice is not the one on file",
                                        f"The name matches {best['name']}, but the invoice shows tax ID {gst} and the "
                                        f"vendor master has {best['gstin']}. This can mean a different legal entity or "
                                        "an impersonation.", "GSTIN_MISMATCH", action="REVIEW", route="procurement"))
    if not match and gst and ctx.doc.is_ocr:
        near = [v for v in vendors if v["gstin"] and Levenshtein.distance(gst, v["gstin"]) <= 1]
        if len(near) == 1:
            match = near[0]
            method = f"tax ID matches the vendor master except for one character (OCR read {gst})"

    data = {"extracted": {"name": ex.vendor_name, "gstin": ex.vendor_gstin}, "vendor": None, "method": method}
    if not match:
        closest = max(vendors, key=name_score) if name_norm else None
        hint = ""
        if closest and name_score(closest) >= 60:
            hint = f" The closest name on file is {closest['name']} ({round(name_score(closest))}% similar), which is not close enough to assume."
        who = ex.vendor_name or "the sender"
        checks.append(Check("fail", f"{who} is not an approved vendor",
                            f"No vendor in the master matches the name “{ex.vendor_name or 'unknown'}”"
                            + (f" or tax ID {gst}" if gst else "") + "." + hint +
                            " Payments are only made to vendors that have been onboarded and verified.",
                            "VENDOR_UNKNOWN", action="REVIEW", route="procurement"))
        return StageResult("fail", "Vendor not found in the approved vendor master", checks, data)

    ctx.vendor = match
    data["vendor"] = {k: match[k] for k in ("id", "name", "gstin", "status", "payment_terms_days", "category",
                                            "bank_name", "email")}
    data["vendor"]["bank_account_masked"] = _mask(match["bank_account"])
    if match["status"] == "blocked":
        checks.append(Check("fail", f"{match['name']} is blocked for payment",
                            (match.get("status_note") or "The vendor is on hold in the vendor master.") +
                            " Nothing can be paid until procurement lifts the block.", "VENDOR_BLOCKED",
                            action="REJECT", route="procurement"))
        return StageResult("fail", f"{match['name']} ({match['id']}) is blocked", checks, data)

    checks.insert(0, Check("pass", f"Approved vendor: {match['name']} ({match['id']})",
                           f"{method[0].upper() + method[1:]}. Category: {match['category']}. "
                           f"Agreed terms: net {match['payment_terms_days']} days."))
    if ex.bank_account:
        if ex.bank_account == match["bank_account"]:
            checks.append(Check("pass", f"Bank account on the invoice matches the one on file ({_mask(match['bank_account'])})"))
        else:
            dist = Levenshtein.distance(ex.bank_account, match["bank_account"] or "")
            if ctx.doc.is_ocr and dist <= 2:
                checks.append(Check("info", "Bank account differs from the master by an OCR-sized error",
                                    f"OCR read {_mask(ex.bank_account)}; the master has {_mask(match['bank_account'])}. "
                                    "Payment always goes to the account on file, never to one read off an invoice."))
            else:
                checks.append(Check("fail", "The invoice asks for payment to a different bank account",
                                    f"Invoice: account {_mask(ex.bank_account)}"
                                    + (f" ({ex.ifsc})" if ex.ifsc else "") +
                                    f". Vendor master: {_mask(match['bank_account'])} at {match['bank_name']}. "
                                    "A changed account on an invoice is the classic sign of payment-redirection fraud. "
                                    "Payment is only ever made to the account on file; treasury should confirm any "
                                    "change with the vendor on a known phone number.", "BANK_MISMATCH",
                                    action="REVIEW", route="treasury"))
    else:
        checks.append(Check("info", "No bank details on the invoice",
                            f"Payment goes to the account on file ({_mask(match['bank_account'])} at {match['bank_name']})."))
    return StageResult(_status(checks), f"{match['name']} ({match['id']}) · approved vendor", checks, data)


# ============================================================== 6 po_match ===

def _period_names(periods) -> str:
    return ", ".join(f"{MONTH_NAMES[m]} {y}" for y, m in sorted(periods))


def _score_candidates(ctx: Ctx, candidates: list[dict]) -> list[dict]:
    subtotal = ctx.amounts.get("subtotal")
    inv_text = _norm_text(" ".join(l["description"] for l in ctx.lines))
    inv_periods = set(ctx.ex.periods)
    scored = []
    for po in candidates:
        remaining = D(po["remaining_value"])
        if subtotal is None or remaining <= 0:
            amount_fit = 0.0
            amount_note = amount_short = "no amount to compare"
        else:
            diff = subtotal - remaining
            if abs(diff) <= max(Decimal("1"), remaining * Decimal("0.001")):
                amount_fit, amount_note = 1.0, f"invoice value equals the PO's open value ({fmt_po(remaining)})"
                amount_short = "equals the open value"
            elif diff < 0:
                amount_fit = float(Decimal("0.55") + Decimal("0.35") * (subtotal / remaining))
                amount_note = f"invoice value is {round(float(subtotal / remaining * 100))}% of the PO's open value"
                amount_short = f"{round(float(subtotal / remaining * 100))}% of the open value"
            else:
                amount_fit = max(0.0, 0.9 - 5 * float(diff / remaining))
                amount_note = f"invoice value is {fmt_po(diff)} more than the PO's open value"
                amount_short = f"{fmt_po(diff)} over the open value"
        po_texts = [_norm_text(po["description"])] + [_norm_text(l["description"]) for l in po["lines"]]
        if inv_text:
            per_line = []
            for l in ctx.lines:
                t = _norm_text(l["description"])
                per_line.append(max(fuzz.token_set_ratio(t, pt) for pt in po_texts))
            text_fit = (sum(per_line) / len(per_line)) / 100.0
        else:
            text_fit = 0.0
        po_periods = find_periods(po["description"] + " " + " ".join(l["description"] for l in po["lines"]))
        if inv_periods and po_periods:
            period_fit = 1.0 if inv_periods & po_periods else 0.0
            period_note = (f"same service period ({_period_names(inv_periods & po_periods)})" if period_fit
                           else f"PO is for {_period_names(po_periods)}, invoice is for {_period_names(inv_periods)}")
            period_short = _period_names(po_periods)
        else:
            period_fit, period_note, period_short = 0.5, "no service period to compare", "not stated"
        score = 0.45 * amount_fit + 0.35 * text_fit + 0.20 * period_fit
        scored.append({"po_number": po["po_number"], "description": po["description"],
                       "remaining_value": po["remaining_value"], "score": round(min(score, 0.95), 3),
                       "amount_fit": round(amount_fit, 2), "text_fit": round(text_fit, 2),
                       "period_fit": period_fit, "amount_note": amount_note, "period_note": period_note,
                       "amount_short": amount_short, "period_short": period_short})
    scored.sort(key=lambda c: -c["score"])
    return scored


def po_match(ctx: Ctx) -> StageResult:
    ex = ctx.ex
    checks: list[Check] = []
    data = {"reference": ex.po_number, "reference_style": ex.po_reference_style, "method": "none", "po": None,
            "confidence": None, "candidates": []}
    all_pos = {_norm_po(p["po_number"]): p["po_number"] for p in db.query("SELECT po_number FROM purchase_orders")}
    po = None
    ref_missing_in_register = False

    if ex.po_number:
        number = all_pos.get(_norm_po(ex.po_number))
        if number:
            po = db.get_po(number)
            ctx.po_method, ctx.po_confidence = "explicit", 1.0
            vend = db.get_vendor(po["vendor_id"])
            where = ("quoted inside a sentence rather than in a PO field"
                     if ex.po_reference_style == "in running text" else "printed on the invoice")
            if not ctx.vendor:
                checks.append(Check("warn", f"{po['po_number']} exists, but it was issued to {vend['name']}",
                                    "The invoice quotes a real PO, but its sender could not be matched to the vendor "
                                    "that PO was raised on. The invoice is not reconciled against it until the vendor "
                                    "is verified.", "PO_VENDOR_UNVERIFIED"))
            elif po["vendor_id"] != ctx.vendor["id"]:
                checks.append(Check("fail", f"{po['po_number']} was issued to a different vendor",
                                    f"The invoice is from {ctx.vendor['name']} but {po['po_number']} was raised on "
                                    f"{vend['name']}. An invoice can only be matched to a PO issued to the same vendor.",
                                    "PO_VENDOR_MISMATCH", action="REVIEW", route="ap"))
            else:
                checks.append(Check("pass", f"{po['po_number']} found in the PO register ({where})",
                                    f"“{po['description']}”, raised {fmt_date(po['po_date'])} by {po['buyer_name']} "
                                    f"({po['buyer_role']}). Value {fmt_po(po['value'])} before tax; "
                                    f"{fmt_po(po['billed_value'])} already billed, {fmt_po(po['remaining_value'])} open."))
        else:
            ref_missing_in_register = True
            checks.append(Check("warn", f"{ex.po_number} is not in the PO register",
                                "The invoice quotes a PO number that does not exist. It may be a typo, so the open "
                                "POs for this vendor are checked for a fit."))
    else:
        checks.append(Check("info", "The invoice carries no PO number",
                            "Looking for the purchase order it most plausibly belongs to."))

    if po is None:
        if not ctx.vendor:
            checks.append(Check("info", "PO inference skipped", "Without an identified vendor there are no candidate POs."))
        elif ex.currency != config.BUYER["currency"]:
            checks.append(Check("info", "PO inference skipped",
                                f"The invoice is in {ex.currency} and purchase orders are in {config.BUYER['currency']}, "
                                "so amounts cannot be compared to find a fit."))
        else:
            ctx.log(f"Scoring {ctx.vendor['name']}'s open purchase orders against the invoice…")
            open_pos = [p for p in db.list_pos(ctx.vendor["id"]) if p["status"] in ("open", "partially_billed")]
            scored = _score_candidates(ctx, open_pos)
            data["candidates"] = scored
            min_conf = float(ctx.pol("po_inference_min_confidence")) / 100
            min_margin = float(ctx.pol("po_inference_min_margin")) / 100
            if not scored:
                checks.append(Check("fail", f"No open purchase order for {ctx.vendor['name']}",
                                    "There is nothing to match this invoice against. An invoice without a PO needs the "
                                    "budget owner's approval before it can be paid.", "NO_OPEN_PO", action="REVIEW",
                                    route="ap"))
            else:
                top = scored[0]
                only = len(scored) == 1
                margin = round(top["score"] - scored[1]["score"], 4) if not only else 1.0
                clear = top["score"] >= min_conf - 1e-9 and margin >= min_margin - 1e-9
                if clear and not ref_missing_in_register:
                    po = db.get_po(top["po_number"])
                    ctx.po_method, ctx.po_confidence = "inferred", top["score"]
                    others = ""
                    if len(scored) > 1:
                        r = scored[1]
                        others = (f" The runner-up, {r['po_number']}, scores {round(r['score'] * 100)}%: "
                                  f"{r['period_note'] if r['period_fit'] == 0 else r['amount_note']}.")
                    lead_in = (f"It is the only open PO for {ctx.vendor['name']}, and it fits" if only else
                               f"Of the {len(scored)} open POs for {ctx.vendor['name']}, this is the one that fits")
                    accepted = (f" Accepted because it clears {round(min_conf * 100)}%." if only else
                                f" Accepted because it clears {round(min_conf * 100)}% and leads the next candidate "
                                f"by {round(margin * 100)} points (minimum {round(min_margin * 100)}).")
                    checks.append(Check("info", f"PO inferred: {top['po_number']} ({round(top['score'] * 100)}% confidence)",
                                        f"{lead_in}: {top['amount_note']}; {top['period_note']}; description "
                                        f"{round(top['text_fit'] * 100)}% similar." + others + accepted,
                                        "PO_INFERRED", data={"margin": round(margin, 3)}))
                elif ref_missing_in_register:
                    po = db.get_po(top["po_number"])
                    ctx.po_method, ctx.po_confidence = "inferred", top["score"]
                    checks.append(Check("fail", f"Quoted PO not found; closest fit is {top['po_number']}",
                                        f"The invoice quotes {ex.po_number}, which does not exist. {top['po_number']} "
                                        f"fits at {round(top['score'] * 100)}% ({top['amount_note']}). Because the vendor "
                                        "quoted a wrong number, a person should confirm the PO before payment.",
                                        "PO_NOT_FOUND", action="REVIEW", route="ap"))
                else:
                    shortlist = "; ".join(f"{c['po_number']} at {round(c['score'] * 100)}%" for c in scored[:3])
                    why = (f"the best candidate scores {round(top['score'] * 100)}%, below the {round(min_conf * 100)}% "
                           "needed" if top["score"] < min_conf - 1e-9 else
                           f"the top two are only {round(margin * 100)} points apart (minimum {round(min_margin * 100)})")
                    checks.append(Check("fail", "No purchase order fits clearly enough to choose automatically",
                                        f"The invoice has no PO number and {why}. Candidates: {shortlist}. "
                                        "A person should pick the PO.", "PO_AMBIGUOUS", action="REVIEW", route="ap"))

    if po:
        ctx.po = po
        data["po"] = _po_summary(po)
        data["method"] = ctx.po_method
        data["confidence"] = ctx.po_confidence
        if po["status"] in ("closed", "fully_billed"):
            state = "closed" if po["status"] == "closed" else "fully billed"
            by = f" (by {', '.join(i['invoice_number'] for i in po['invoices'])})" if po["invoices"] else ""
            if po["remaining_value"] > 0.005:  # closed by procurement with a balance left on it
                detail = (f"The PO was closed with {fmt_po(po['remaining_value'])} of its {fmt_po(po['value'])} "
                          f"unbilled{by}. A closed PO cannot take further invoices unless the buyer reopens it.")
            else:
                detail = (f"All {fmt_po(po['value'])} of this PO has been billed{by}. There is nothing left on it "
                          "for this invoice to be paid against.")
            checks.append(Check("fail", f"{po['po_number']} is already {state}", detail,
                                "PO_EXHAUSTED", action="REVIEW", route="buyer"))
        if ctx.po_method == "explicit":
            summary = f"{po['po_number']} · quoted on the invoice · {fmt_po(po['remaining_value'])} open"
        else:
            summary = f"{po['po_number']} · inferred at {round(ctx.po_confidence * 100)}% · no PO number on the invoice"
    else:
        summary = "No purchase order could be matched"
    return StageResult(_status(checks), summary, checks, data)


# ============================================================= 7 duplicate ===

def find_duplicate(vendor_id: str, invoice_number: Optional[str], total: Optional[Decimal], invoice_date: Optional[str],
                   po_number: Optional[str], lines: list[dict], file_hash: str, window_days: int,
                   exclude_run: Optional[str] = None) -> Optional[dict]:
    """Compare against every invoice already accepted from this vendor.

    Returns the strongest match as {"kind", "original", "signals"} or None.
    kind: file | number | reissue | revision | content
    """
    ledger = [l for l in db.ledger_for_vendor(vendor_id) if not exclude_run or l["run_id"] != exclude_run]
    strict = normalize_invoice_number(invoice_number or "")
    loose = normalize_invoice_number(invoice_number or "", strip_revision=True)
    inv_date = date.fromisoformat(invoice_date) if invoice_date else None
    fingerprint = sorted((float(l["qty"]), float(l["unit_price"])) for l in lines
                         if l.get("qty") is not None and l.get("unit_price") is not None)
    best = None
    rank = {"file": 5, "number": 4, "reissue": 3, "revision": 2, "content": 1}
    for inv in ledger:
        same_total = total is not None and abs(D(inv["total"]) - total) <= 1
        same_po = bool(po_number) and inv["po_number"] == po_number
        their_fp = sorted((float(l["qty"]), float(l["unit_price"])) for l in inv["lines"] if l["qty"] is not None)
        same_lines = bool(fingerprint) and fingerprint == their_fp
        orig_date = date.fromisoformat(inv["invoice_date"]) if inv["invoice_date"] else None
        gap = abs((inv_date - orig_date).days) if inv_date and orig_date else None
        kind = None
        if file_hash and inv["file_hash"] == file_hash:
            kind = "file"
        elif strict and strict == inv["invoice_number_norm"]:
            kind = "number"
        elif loose and loose == normalize_invoice_number(inv["invoice_number"], strip_revision=True):
            # Same number once a revision suffix is ignored. It is the same invoice only if the
            # content agrees too; a suffix on a different amount or order is a question, not a copy.
            kind = "reissue" if same_total and (same_po or same_lines) else "revision"
        elif same_total and same_po and (same_lines or not fingerprint) and gap is not None and gap <= window_days:
            kind = "content"
        if not kind:
            continue
        signals = [
            {"label": "Invoice number", "this": invoice_number or "—", "original": inv["invoice_number"],
             "match": kind in ("file", "number", "reissue", "revision"),
             "note": ("identical" if invoice_number == inv["invoice_number"] else
                      "same number once punctuation, leading zeros and the revision suffix are ignored"
                      if kind in ("number", "reissue", "revision", "file") else "different")},
            {"label": "Purchase order", "this": po_number or "—", "original": inv["po_number"] or "—",
             "match": same_po, "note": "same" if same_po else "different"},
            {"label": "Total", "this": fmt_inr(total), "original": fmt_inr(inv["total"]), "match": same_total,
             "note": "same" if same_total else "different"},
            {"label": "Line items", "this": _plural(len(lines), "line"), "original": _plural(len(inv["lines"]), "line"),
             "match": same_lines, "note": "same quantities and rates" if same_lines else "not comparable"
             if not fingerprint else "different"},
            {"label": "Invoice date", "this": fmt_date(invoice_date), "original": fmt_date(inv["invoice_date"]),
             "match": gap == 0, "note": "same day" if gap == 0 else (f"{gap} days apart" if gap is not None else "—")},
        ]
        cand = {"kind": kind, "original": {k: inv[k] for k in ("id", "run_id", "invoice_number", "invoice_date",
                                                               "po_number", "total", "status", "pay_date", "source")},
                "signals": signals}
        if best is None or rank[kind] > rank[best["kind"]]:
            best = cand
    return best


def duplicate(ctx: Ctx) -> StageResult:
    ex = ctx.ex
    checks: list[Check] = []
    if not ctx.vendor:
        checks.append(Check("info", "Limited check: the vendor is not identified",
                            "Duplicates are judged per vendor. With no vendor match, only the file fingerprint was "
                            "compared."))
        hit = db.ledger_by_hash(ctx.file_hash)
        if hit:
            ctx.duplicate = {"kind": "file", "original": hit, "signals": []}
            checks.append(Check("fail", f"This exact file was already accepted as {hit['invoice_number']}",
                                "A byte-identical file is in the ledger.", "DUPLICATE_FILE", action="REJECT"))
        return StageResult(_status(checks), "Vendor unknown · fingerprint compared only", checks,
                           {"match": ctx.duplicate, "compared": 0})

    n = len(db.ledger_for_vendor(ctx.vendor["id"]))
    ctx.log(f"Comparing against {_plural(n, 'earlier invoice')} accepted from {ctx.vendor['name']}…")
    hit = find_duplicate(ctx.vendor["id"], ex.invoice_number, ctx.amounts.get("total"), ex.invoice_date,
                         ctx.po["po_number"] if ctx.po else None, ctx.lines, ctx.file_hash,
                         int(ctx.pol("duplicate_window_days")))
    ctx.duplicate = hit
    data = {"match": hit, "compared": n}
    if hit:
        o = hit["original"]
        state = (f"paid on {fmt_date(o['pay_date'])}" if o["status"] == "paid"
                 else f"approved and scheduled for payment on {fmt_date(o['pay_date'])}")
        where = f" in {o['run_id']}" if o.get("run_id") else ""
        agree = [s["label"].lower() for s in hit["signals"] if s["match"] and s["label"] != "Invoice number"]
        if hit["kind"] == "file":
            checks.append(Check("fail", f"Exact duplicate of {o['invoice_number']}, already accepted{where}",
                                f"This is byte-for-byte the same file. The invoice was {state}.", "DUPLICATE_FILE",
                                action="REJECT"))
        elif hit["kind"] == "number":
            checks.append(Check("fail", f"Duplicate: invoice {o['invoice_number']} was already accepted{where}",
                                f"The same invoice number from the same vendor is in the ledger ({state}). "
                                + (f"Also identical: {', '.join(agree)}." if agree else
                                   "The amounts differ, so if this is a corrected invoice the vendor must cancel the "
                                   "original with a credit note first."),
                                "DUPLICATE_NUMBER", action="REJECT"))
        elif hit["kind"] == "reissue":
            checks.append(Check("fail", f"Duplicate in disguise: this is {o['invoice_number']}, already {state.split(' on ')[0]}",
                                f"The number {ex.invoice_number} is {o['invoice_number']} written differently "
                                "(formatting changed and a revision suffix added), so an exact-number check would miss "
                                f"it. It also has the same {', '.join(agree)}. The original was dated "
                                f"{fmt_date(o['invoice_date'])} and was {state}; this copy is dated "
                                f"{fmt_date(ex.invoice_date)}. Paying it would pay for the same goods twice.",
                                "DUPLICATE_REISSUE", action="REJECT"))
        elif hit["kind"] == "revision":
            differs = [s["label"].lower() for s in hit["signals"]
                       if not s["match"] and s["label"] in ("Purchase order", "Total", "Line items")]
            checks.append(Check("fail", f"Looks like a revision of {o['invoice_number']}, which is already in the ledger",
                                f"{ex.invoice_number} is the same number with a revision suffix, but it is not a plain "
                                f"copy: the {' and the '.join(differs) or 'content'} differ"
                                f"{'s' if len(differs) == 1 else ''} ({fmt_inr(ctx.amounts.get('total'))} here against "
                                f"{fmt_inr(o['total'])} on the original). If it replaces the original, the original "
                                "has to be reversed before this can be paid.",
                                "POSSIBLE_REVISION", action="REVIEW", route="ap"))
        else:
            checks.append(Check("fail", f"Possible duplicate of {o['invoice_number']} under a new number",
                                f"Different invoice number, but the same {', '.join(agree)} as {o['invoice_number']} "
                                f"({state}), dated {hit['signals'][-1]['note']}. It may be a genuine repeat delivery, "
                                "so a person should confirm before it is paid.", "POSSIBLE_DUPLICATE", action="REVIEW",
                                route="ap"))
        summary = f"Matches {o['invoice_number']} already in the ledger"
    else:
        if n:
            checks.append(Check("pass", "No duplicate found",
                                f"{ex.invoice_number or 'This invoice'} was compared with {_plural(n, 'invoice')} "
                                f"already accepted from {ctx.vendor['name']}: by number (ignoring punctuation, leading "
                                "zeros and revision suffixes), and by amount, PO and line content within "
                                f"{int(ctx.pol('duplicate_window_days'))} days."))
            summary = f"No duplicate among {_plural(n, 'earlier invoice')} from this vendor"
        else:
            checks.append(Check("pass", "No duplicate found",
                                f"No invoice from {ctx.vendor['name']} has been accepted before, so there is nothing "
                                "it could duplicate."))
            summary = "No duplicate: first invoice accepted from this vendor"
    pending = db.query(
        "SELECT id FROM runs WHERE id != ? AND decision = 'NEEDS_REVIEW' AND review_status = 'pending'"
        " AND invoice_number = ? AND vendor_name = ?",
        (ctx.run_id, ex.invoice_number or "", ctx.vendor["name"]),
    )
    if pending and not hit:
        checks.append(Check("info", f"The same invoice is already waiting for review ({pending[0]['id']})",
                            "It has not been accepted, so this is not a duplicate payment risk yet. Only one of the "
                            "two can be approved: approval re-checks the ledger."))
    return StageResult(_status(checks), summary, checks, data)


# ============================================================= 8 reconcile ===

def _match_lines(inv_lines: list[dict], po_lines: list[dict]) -> dict[int, Optional[dict]]:
    """Pair each invoice line with the PO line it most resembles (description first, price as tie-break)."""
    pairs = []
    for i, il in enumerate(inv_lines):
        for pl in po_lines:
            score = fuzz.token_set_ratio(_norm_text(il["description"]), _norm_text(pl["description"]))
            if il.get("unit_price") is not None and pl["unit_price"]:
                gap = abs(float(il["unit_price"]) - pl["unit_price"]) / pl["unit_price"]
                score += 8 * (1 - min(1.0, gap))
            pairs.append((score, i, pl))
    pairs.sort(key=lambda p: -p[0])
    result: dict[int, Optional[dict]] = {i: None for i in range(len(inv_lines))}
    used = set()
    for score, i, pl in pairs:
        if result[i] is None and pl["line_no"] not in used and score >= 60:
            result[i] = pl
            used.add(pl["line_no"])
    return result


def _short(desc: str, n: int = 48) -> str:
    desc = re.split(r"[,(]", desc)[0].strip() if len(desc) > n else desc
    return desc if len(desc) <= n else desc[: n - 1].rstrip() + "…"


def pro_rata_allocations(po: dict, value: Decimal) -> list[dict]:
    """Spread a value that cannot be tied to individual lines across what is open on the PO.

    Used when a bundled invoice is accepted without line detail, so that the PO is still
    drawn down by what was billed and a later invoice cannot bill the same value again.
    """
    if value is None or value <= 0:
        return []
    open_lines = [l for l in po["lines"] if l["remaining_qty"] > 0]
    open_value = sum((D(l["remaining_qty"]) * D(l["unit_price"]) for l in open_lines), ZERO)
    out = []
    if open_value > 0:
        share = min(Decimal(1), value / open_value)
        target = min(value, open_value)
        for l in open_lines:
            # fine-grained quantities: a line of "1 month" at a large price must still add up to the rupee
            qty = (D(l["remaining_qty"]) * share).quantize(Decimal("0.00000001"))
            out.append({"po_line_no": l["line_no"], "description": l["description"], "qty": float(qty),
                        "unit_price": l["unit_price"], "amount": to_float(qty * D(l["unit_price"]))})
        drift = target - sum((D(a["amount"]) for a in out), ZERO)
        out[-1]["amount"] = to_float(D(out[-1]["amount"]) + drift)  # paise of rounding go to the last line
    beyond = value - open_value
    if beyond > Decimal("0.005"):
        out.append({"po_line_no": None, "description": "Billed beyond what was open on the PO", "qty": None,
                    "unit_price": None, "amount": to_float(beyond)})
    return out


def reconcile(ctx: Ctx) -> StageResult:
    po = ctx.po
    checks: list[Check] = []
    price_tol = ctx.pol("price_tolerance_pct")
    qty_tol = ctx.pol("qty_tolerance_pct")
    exact_tol = max(ctx.pol("rounding_tolerance"), Decimal("1"))
    subtotal = ctx.amounts.get("subtotal")
    if subtotal is None:
        subtotal = sum((l["amount"] for l in ctx.lines if l["amount"] is not None), ZERO)
    tax = ctx.amounts.get("tax")
    remaining_value = D(po["remaining_value"])
    value_tol = min(remaining_value * ctx.pol("total_tolerance_pct") / 100, ctx.pol("total_tolerance_abs"))
    rows: list[dict] = []
    allocations: list[dict] = []
    po_lines = po["lines"]
    lump = len(ctx.lines) == 1 and ctx.lines[0]["qty"] is None and len(po_lines) > 1
    billed_at_po = ZERO  # what this invoice bills, valued at PO prices (lines not on the PO at their own price)
    short_billed = False

    if lump:
        il = ctx.lines[0]
        open_lines = [l for l in po_lines if l["remaining_qty"] > 0]
        diff = subtotal - remaining_value
        billed_at_po = subtotal
        if remaining_value > 0 and diff < -exact_tol:
            state, note = "unknown", "cannot be tied to a line"
            checks.append(Check("fail", "Lump-sum invoice covers only part of the PO",
                                f"The invoice is one bundled line of {fmt_inr(subtotal)} before tax, but "
                                f"{fmt_inr(remaining_value)} is open on {po['po_number']} across {len(open_lines)} "
                                f"{'line' if len(open_lines) == 1 else 'lines'}. With no line detail there is no way to "
                                "tell which lines are being billed. If it is accepted, the PO is drawn down pro rata.",
                                "BUNDLED_PARTIAL", action="REVIEW", route="buyer"))
            allocations = pro_rata_allocations(po, subtotal)
        elif remaining_value > 0 and diff <= value_tol:
            state, note = "lump", "covered by the lump sum"
            exact = abs(diff) <= exact_tol
            checks.append(Check("pass", "Lump-sum invoice equals everything still open on the PO" if exact else
                                f"Lump-sum invoice covers everything open on the PO, {fmt_inr(diff)} over but within the allowance",
                                f"The invoice bundles the work into one line of {fmt_inr(subtotal)} before tax. "
                                f"{po['po_number']} has {len(open_lines)} {'line' if len(open_lines) == 1 else 'lines'} with "
                                f"{'exactly ' if exact else ''}{fmt_inr(remaining_value)} still open, so all of them are "
                                "treated as billed in full. A line-by-line price check is not possible on a bundled "
                                "invoice; the match rests on the total."
                                + ("" if exact else f" The allowance over the open value is {fmt_inr(value_tol)}.")))
            allocations = pro_rata_allocations(po, subtotal)
        else:
            # more than the allowance over what is open (or nothing open at all): the value check below reports it
            state, note = "unknown", "more than is open"
            allocations = pro_rata_allocations(po, subtotal)
        for n, pl in enumerate(po_lines):
            open_qty = max(0.0, pl["remaining_qty"])
            rows.append({"description": il["description"] if n == 0 else "", "po_line_no": pl["line_no"],
                         "po_description": pl["description"], "unit": pl["unit"], "po_qty": pl["qty"],
                         "billed_before": pl["billed_qty"], "remaining_before": open_qty, "inv_qty": None,
                         "po_price": pl["unit_price"], "inv_price": None, "price_var_pct": None,
                         "amount": to_float(D(open_qty) * D(pl["unit_price"])) if state == "lump" else None,
                         "price_status": "na", "qty_status": state if open_qty > 0 else "not_billed",
                         "note": note if open_qty > 0 else "already fully billed"})
    else:
        matched = _match_lines(ctx.lines, po_lines)
        for i, il in enumerate(ctx.lines):
            pl = matched[i]
            n = i + 1
            label = f"Line {n} ({_short((pl or il)['description'])})"
            if pl is None:
                billed_at_po += il["amount"] or ZERO
                rows.append({"description": il["description"], "po_line_no": None, "po_description": None,
                             "unit": il["unit"], "po_qty": None, "billed_before": None, "remaining_before": None,
                             "inv_qty": float(il["qty"]) if il["qty"] is not None else None, "po_price": None,
                             "inv_price": _rate(il["unit_price"]), "price_var_pct": None,
                             "amount": to_float(il["amount"]), "price_status": "fail", "qty_status": "unknown",
                             "note": "not on the PO"})
                allocations.append({"po_line_no": None, "description": il["description"],
                                    "qty": float(il["qty"]) if il["qty"] is not None else None,
                                    "unit_price": _rate(il["unit_price"]), "amount": to_float(il["amount"])})
                checks.append(Check("fail", f"{label} is not on the purchase order",
                                    f"{fmt_inr(il['amount'])} is billed for something {po['po_number']} does not "
                                    "cover. The buyer has to confirm it was ordered.", "LINE_NOT_ON_PO",
                                    action="REVIEW", route="buyer"))
                continue
            po_price = D(pl["unit_price"])
            qty, price, amount = il["qty"], il["unit_price"], il["amount"]
            implied = False
            # Fill in whichever of quantity and rate the invoice did not print, from the amount.
            if qty is None and amount is not None and po_price:
                qty, price, implied = (amount / po_price).quantize(Decimal("0.0001")), po_price, True
            elif qty is not None and price is None:
                price = (amount / qty).quantize(Decimal("0.0001")) if amount is not None and qty else po_price
            if qty is None or price is None:
                rows.append({"description": il["description"], "po_line_no": pl["line_no"],
                             "po_description": pl["description"], "unit": pl["unit"], "po_qty": pl["qty"],
                             "billed_before": pl["billed_qty"], "remaining_before": max(0.0, pl["remaining_qty"]),
                             "inv_qty": None, "po_price": pl["unit_price"], "inv_price": None, "price_var_pct": None,
                             "amount": to_float(amount), "price_status": "na", "qty_status": "unknown",
                             "note": "no quantity or amount"})
                checks.append(Check("fail", f"{label} has no quantity or amount that can be checked",
                                    "The line was read without enough numbers to compare it with the PO.",
                                    "LINE_INCOMPLETE", action="REVIEW", route="ap"))
                continue
            if amount is None:
                amount = q2(qty * price)
            remaining = max(ZERO, D(pl["remaining_qty"]))
            unit = pl["unit"] or il["unit"] or "units"
            var_pct = (price - po_price) / po_price * 100 if po_price else ZERO
            billed_at_po += qty * po_price
            row = {"description": il["description"], "po_line_no": pl["line_no"], "po_description": pl["description"],
                   "unit": unit, "po_qty": pl["qty"], "billed_before": pl["billed_qty"],
                   "remaining_before": float(remaining), "inv_qty": float(qty), "po_price": pl["unit_price"],
                   "inv_price": _rate(price), "price_var_pct": round(float(var_pct), 2),
                   "amount": to_float(amount), "note": ""}
            # ---- price
            if var_pct > price_tol:
                over = (price - po_price) * qty
                row["price_status"] = "fail"
                checks.append(Check("fail", f"{label} is billed at {fmt_rate(price)}; the PO price is "
                                    f"{fmt_rate(po_price)} ({var_pct:+.1f}%)",
                                    f"That is {fmt_rate(price - po_price)} more per {_singular(unit)} on "
                                    f"{_qty_unit(qty, unit)}: {fmt_inr(over)} above the agreed price before tax. "
                                    f"The tolerance is {_n(price_tol)}%.", "PRICE_VARIANCE", action="REVIEW",
                                    route="buyer", data={"line": n, "over_amount": to_float(over),
                                                         "variance_pct": round(float(var_pct), 2)}))
            elif var_pct <= -25:
                # Far below the agreed price is not a bargain; it usually means this is a different item.
                row["price_status"] = "low"
                checks.append(Check("fail", f"{label} is billed at {fmt_rate(price)}, far below the PO price of "
                                    f"{fmt_rate(po_price)} ({var_pct:+.1f}%)",
                                    f"A price this far under the PO usually means the line is a different item from "
                                    f"PO line {pl['line_no']} (“{_short(pl['description'], 40)}”), which would use up "
                                    "that line's open quantity wrongly. The buyer should confirm what was supplied.",
                                    "PRICE_FAR_BELOW", action="REVIEW", route="buyer"))
            elif var_pct < -price_tol:
                row["price_status"] = "info"
                checks.append(Check("info", f"{label} is billed below the PO price ({var_pct:+.1f}%)",
                                    f"{fmt_rate(price)} against {fmt_rate(po_price)}. In our favour, so it does not block."))
            elif var_pct != 0:
                row["price_status"] = "pass"
                checks.append(Check("pass", f"{label}: price within tolerance ({var_pct:+.2f}%)",
                                    f"{fmt_rate(price)} against a PO price of {fmt_rate(po_price)}; up to "
                                    f"{_n(price_tol)}% is accepted."))
            else:
                row["price_status"] = "pass"
            # ---- quantity against what is still open
            allowed = remaining * (1 + qty_tol / 100)
            if qty > allowed + Decimal("0.0001"):
                excess = qty - remaining
                row["qty_status"] = "over"
                row["note"] = f"{fmt_qty(excess)} more than is open"
                left_open = f"only {fmt_qty(remaining)} are still open" if remaining > 0 else "nothing is still open"
                already = (f" {fmt_qty(pl['billed_qty'])} have already been billed on earlier invoices."
                           if pl["billed_qty"] else "")
                checks.append(Check("fail", f"{label} bills {fmt_qty(qty)} {unit}, but {left_open} on the PO",
                                    f"{po['po_number']} line {pl['line_no']} is for {fmt_qty(pl['qty'])} {unit}.{already} "
                                    f"This invoice bills {fmt_qty(excess)} more than remain, worth "
                                    f"{fmt_inr(excess * po_price)} before tax at the PO price.", "QTY_OVER_PO",
                                    action="REVIEW", route="buyer",
                                    data={"line": n, "excess_qty": float(excess),
                                          "excess_value": to_float(excess * po_price)}))
            elif qty < remaining:
                short_billed = True
                row["qty_status"] = "partial"
                row["note"] = f"{_qty_unit(remaining - qty, unit)} still open"
            else:
                row["qty_status"] = "final"
                row["note"] = "completes this line"
            if row["price_status"] == "pass" and row["qty_status"] in ("partial", "final"):
                tail = (f"; {_qty_unit(remaining - qty, unit)} still open on the PO" if row["qty_status"] == "partial"
                        else "; this completes the PO line")
                prior = f" ({fmt_qty(pl['billed_qty'])} billed earlier)" if pl["billed_qty"] else ""
                checks.append(Check("pass", f"{label}: {fmt_qty(qty)} {unit} at {fmt_rate(price)} matches PO line "
                                    f"{pl['line_no']}",
                                    f"Ordered {fmt_qty(pl['qty'])} at {fmt_rate(po_price)}{prior}{tail}."
                                    + (" Quantity was not printed; it is implied by the amount at the PO price."
                                       if implied else "")))
            rows.append(row)
            allocations.append({"po_line_no": pl["line_no"], "description": pl["description"], "qty": float(qty),
                                "unit_price": _rate(price), "amount": to_float(amount)})
        billed_line_nos = {a["po_line_no"] for a in allocations}
        for pl in po_lines:
            if pl["line_no"] not in billed_line_nos and pl["remaining_qty"] > 0:
                rows.append({"description": None, "po_line_no": pl["line_no"], "po_description": pl["description"],
                             "unit": pl["unit"], "po_qty": pl["qty"], "billed_before": pl["billed_qty"],
                             "remaining_before": pl["remaining_qty"], "inv_qty": None, "po_price": pl["unit_price"],
                             "inv_price": None, "price_var_pct": None, "amount": None, "price_status": "na",
                             "qty_status": "not_billed", "note": "not on this invoice"})

    # ---- what is billed against what is open on the PO
    # Valued at PO prices, so that this check is about quantity and scope. Price is judged line by line
    # above, against its own tolerance; otherwise a tolerated price difference would fail here instead.
    over_value = billed_at_po - remaining_value
    value_ok = over_value <= value_tol
    basis = "before tax" if lump else "at PO prices"
    if not value_ok:
        checks.append(Check("fail", f"The invoice bills {fmt_inr(over_value)} more than is open on {po['po_number']}",
                            f"{fmt_inr(billed_at_po)} {basis} is billed against {fmt_inr(remaining_value)} still open "
                            f"(PO value {fmt_inr(po['value'])}, {fmt_inr(po['billed_value'])} already billed). "
                            f"The allowance is {fmt_inr(value_tol)}.", "VALUE_OVER_PO", action="REVIEW", route="buyer"))
    elif not lump:
        if billed_at_po < remaining_value - exact_tol:
            checks.append(Check("pass", f"{fmt_inr(billed_at_po)} billed {basis}, within the {fmt_inr(remaining_value)} "
                                "open on the PO", "A part-billing: the PO stays open for the balance."))
        else:
            checks.append(Check("pass", f"{fmt_inr(billed_at_po)} billed {basis} matches what is open on the PO "
                                f"({fmt_inr(remaining_value)})"))

    # ---- the case a total-only match gets wrong: a price increase hidden by a short quantity
    header_gap = abs(subtotal - remaining_value)
    header_pct = max(ctx.pol("total_tolerance_pct"), Decimal(1))
    if (any(c.code == "PRICE_VARIANCE" for c in checks) and short_billed and remaining_value > 0
            and header_gap <= remaining_value * header_pct / 100):
        gap_pct = (subtotal - remaining_value) / remaining_value * 100
        checks.append(Check("warn", "A total-only match would have approved this invoice",
                            f"Invoice value {fmt_inr(subtotal)} is {abs(gap_pct):.2f}% "
                            f"{'below' if gap_pct < 0 else 'above'} the {fmt_inr(remaining_value)} open on the PO, "
                            f"inside the {_n(header_pct)}% a header-level check allows. The price increase is hidden "
                            "because a quantity was billed short. Only the line-by-line comparison exposes it.",
                            "MASKED_VARIANCE"))

    # ---- tax
    if tax is not None and subtotal > 0:
        expected = q2(subtotal * D(po["tax_rate"]) / 100)
        tax_tol = max(ctx.pol("rounding_tolerance"), expected * ctx.pol("tax_tolerance_pct") / 100)
        if abs(tax - expected) <= tax_tol:
            checks.append(Check("pass", f"Tax is right: {fmt_inr(tax)} is {_n(po['tax_rate'])}% of {fmt_inr(subtotal)}"))
        else:
            checks.append(Check("fail", f"Tax charged ({fmt_inr(tax)}) is not {_n(po['tax_rate'])}% of the invoice value",
                                f"Expected {fmt_inr(expected)} on {fmt_inr(subtotal)}; the invoice charges "
                                f"{fmt_inr(tax)}, a difference of {fmt_inr(tax - expected)}.", "TAX_MISMATCH",
                                action="REVIEW", route="ap"))

    ctx.allocations = allocations
    ctx.recon = {
        "mode": "lump" if lump else "line", "rows": rows, "invoice_subtotal": to_float(subtotal),
        "po_value": po["value"], "po_billed_before": po["billed_value"], "po_remaining_before": po["remaining_value"],
        "value_at_po_prices": to_float(billed_at_po), "tax_rate": po["tax_rate"],
    }
    fails = [c for c in checks if c.status == "fail"]
    if fails:
        summary = f"{_plural(len(fails), 'discrepancy', 'discrepancies')} against {po['po_number']}"
    elif lump:
        summary = f"Lump sum covers the open value of {po['po_number']}"
    else:
        partial = any(r["qty_status"] in ("partial", "not_billed") for r in rows)
        summary = (f"{_plural(len(ctx.lines), 'line')} match {po['po_number']} on price and quantity"
                   + (" · part-billing, PO stays open" if partial else " · PO fully billed"))
    return StageResult(_status(checks), summary, checks, ctx.recon)


# ================================================================ 9 decide ===

_VENDOR_FIXABLE = {"LINE_ARITHMETIC", "LINES_VS_SUBTOTAL", "TOTAL_ARITHMETIC", "WORDS_MISMATCH", "TAX_MISMATCH",
                   "PRICE_VARIANCE", "QTY_OVER_PO", "VALUE_OVER_PO", "LINE_NOT_ON_PO", "FUTURE_DATED"}

DECISION_LABELS = {
    "APPROVED": "Approved for payment",
    "NEEDS_REVIEW": "Held for review",
    "RETURNED": "Returned to vendor",
    "REJECTED": "Rejected",
}
_ROUTE_PRIORITY = ["treasury", "procurement", "buyer", "ap"]


def _route_for(ctx: Ctx, key: str) -> dict:
    if key == "buyer" and ctx.po:
        return {"key": "buyer", "queue": "PO owner", "name": ctx.po["buyer_name"], "role": ctx.po["buyer_role"],
                "email": ctx.po["buyer_email"]}
    if key == "buyer":
        key = "ap"
    return {"key": key, **config.ROUTES[key]}


def _due_date(ctx: Ctx) -> Optional[date]:
    """When the invoice falls due on the terms agreed with the vendor."""
    if not ctx.ex.invoice_date or not ctx.vendor:
        return None
    return date.fromisoformat(ctx.ex.invoice_date) + timedelta(days=int(ctx.vendor["payment_terms_days"]))


def _pay_date(ctx: Ctx) -> Optional[date]:
    """Pay on the due date, or in the next payment run if that date has already passed."""
    due = _due_date(ctx)
    return max(due, ctx.today) if due else None


def _signature() -> str:
    return f"Accounts Payable\n{config.BUYER['name']}\n{config.BUYER['ap_email']}"


def _vendor_contact(ctx: Ctx) -> tuple[str, str]:
    if ctx.vendor:
        return ctx.vendor["name"], ctx.vendor["email"]
    return ctx.ex.vendor_name if ctx.ex and ctx.ex.vendor_name else "Sender", "(reply to the sender of the invoice email)"


def _doc_ref(ctx: Ctx) -> str:
    ex = ctx.ex
    if ex and ex.invoice_number:
        return f"invoice {ex.invoice_number}"
    return f"the document “{ctx.filename}”"


def decide(ctx: Ctx) -> StageResult:
    ex = ctx.ex
    checks: list[Check] = []
    total = ctx.amounts.get("total") if ctx.amounts else None

    # ---- approval-level rules that only matter if nothing else has stopped the invoice
    blocking = [c for c in ctx.checks if c.action]
    if not blocking and ex is not None:
        limit = ctx.pol("auto_approve_limit")
        if total is not None and total > limit:
            c = Check("fail", f"Above the straight-through limit of {fmt_inr(limit)}",
                      f"Everything matches, but at {fmt_inr(total)} this invoice needs a second pair of eyes before "
                      "payment.", "ABOVE_AUTO_LIMIT", action="REVIEW", route="ap", stage="decide")
            checks.append(c)
            blocking.append(c)
        elif ex.method == "ai" and (ex.confidence() or 1) < 0.7:
            c = Check("fail", f"The AI reader was not confident ({round((ex.confidence() or 0) * 100)}%)",
                      "The model flagged low confidence in the fields it read. A person should compare the extracted "
                      "values with the document.", "LOW_EXTRACTION_CONFIDENCE", action="REVIEW", route="ap",
                      stage="decide")
            checks.append(c)
            blocking.append(c)

    rejects = [c for c in blocking if c.action == "REJECT"]
    returns = [c for c in blocking if c.action == "RETURN"]
    reviews = [c for c in blocking if c.action == "REVIEW"]
    if rejects:
        decision, decisive = "REJECTED", rejects
    elif returns:
        decision, decisive = "RETURNED", returns
    elif reviews:
        decision, decisive = "NEEDS_REVIEW", reviews
    else:
        decision, decisive = "APPROVED", []

    vendor_name, vendor_email = _vendor_contact(ctx)
    insights: list[Check] = []
    route = None
    message = None
    payment = None
    next_steps: list[str] = []
    options: list[str] = []
    ref = _doc_ref(ctx)
    po_no = ctx.po["po_number"] if ctx.po else None

    if decision == "APPROVED":
        pay_date = _pay_date(ctx)
        terms = ctx.vendor["payment_terms_days"]
        headline = (f"Approved for payment: {fmt_inr(total)} to {ctx.vendor['name']}, scheduled for "
                    f"{fmt_date(pay_date)}.")
        payment = {"payee": ctx.vendor["name"], "vendor_id": ctx.vendor["id"], "amount": to_float(total),
                   "currency": ex.currency, "pay_date": pay_date.isoformat() if pay_date else None,
                   "terms": (f"Net {terms} days from invoice date" if pay_date == _due_date(ctx)
                             else f"Next payment run (due {fmt_date(_due_date(ctx))})"),
                   "bank_name": ctx.vendor["bank_name"],
                   "bank_account_masked": _mask(ctx.vendor["bank_account"]), "ifsc": ctx.vendor["ifsc"],
                   "reference": ex.invoice_number, "po_number": po_no}
        notes = [c for c in ctx.checks if c.code in ("PO_INFERRED", "TAX_NORMALISED", "DATE_DAY_FIRST")]
        if ctx.doc and ctx.doc.is_ocr:
            notes.insert(0, Check("info", f"Read from a scan by OCR at {ctx.doc.ocr_confidence}% confidence",
                                  "The total was independently confirmed by the amount in words and by the purchase "
                                  "order.", "OCR_USED"))
        due = _due_date(ctx)
        if due and due < ctx.today:
            notes.append(Check("info", f"Already past its due date ({fmt_date(due)})",
                               f"On net {terms} days this invoice fell due on {fmt_date(due)}, so it goes into the "
                               f"next payment run ({fmt_date(pay_date)}) rather than being back-dated.", "PAST_DUE"))
        elif ex.due_date and due and ex.due_date != due.isoformat():
            notes.append(Check("info", f"Invoice asks for payment by {fmt_date(ex.due_date)}",
                               f"Payment is scheduled on the agreed terms instead (net {terms} days: "
                               f"{fmt_date(pay_date)}).", "TERMS_DIFFER"))
        decisive = notes
        next_steps = [f"Invoice posted to the ledger against {po_no}.",
                      f"Payment of {fmt_inr(total)} scheduled for {fmt_date(pay_date)} "
                      f"({'net ' + str(terms) + ' days' if pay_date == _due_date(ctx) else 'next payment run'}) to the "
                      f"account on file, {_mask(ctx.vendor['bank_account'])} at {ctx.vendor['bank_name']}."]
    elif decision == "NEEDS_REVIEW":
        keys = [c.route or "ap" for c in reviews]
        primary_key = next(k for k in _ROUTE_PRIORITY if k in keys)
        route = _route_for(ctx, primary_key)
        decisive = sorted(reviews, key=lambda c: _ROUTE_PRIORITY.index(c.route or "ap"))
        lead = decisive[0]
        insights = [c for c in ctx.checks if c.code == "MASKED_VARIANCE"]
        headline = f"Held for {route['name']} ({route['role']}): {_mid_sentence(lead.title)}."
        codes = {c.code for c in reviews}
        if "PRICE_VARIANCE" in codes and ctx.recon:
            at_po = D(ctx.recon["value_at_po_prices"])
            short_total = q2(at_po * (1 + D(ctx.po["tax_rate"]) / 100))
            options = [f"Approve as billed ({fmt_inr(total)}) and amend {po_no} to the new price.",
                       f"Short-pay at the PO price: {fmt_inr(at_po)} + tax = {fmt_inr(short_total)}, and ask the "
                       "vendor for a credit note for the difference.",
                       "Reject and ask for a corrected invoice."]
        elif "QTY_OVER_PO" in codes or "VALUE_OVER_PO" in codes:
            q = next((c for c in reviews if c.code == "QTY_OVER_PO"), None)
            extra = f" ({fmt_inr(D(q.data['excess_value']))} before tax)" if q else ""
            options = [f"Approve as billed and raise a PO amendment for the excess{extra}.",
                       "Pay only what is open on the PO and ask the vendor to credit the rest.",
                       "Reject if the extra quantity was never requested."]
        elif "BANK_MISMATCH" in codes:
            options = ["Call the vendor on the number in the vendor master (not the one on the invoice) to confirm.",
                       "If confirmed, update the vendor master through the bank-change process, then release.",
                       "If not confirmed, reject and report as attempted fraud."]
        elif "VENDOR_UNKNOWN" in codes:
            options = ["Onboard the vendor (KYC, tax registration, bank verification), then re-run the invoice.",
                       "Reject if the purchase was not authorised."]
        elif "PO_AMBIGUOUS" in codes or "PO_NOT_FOUND" in codes or "NO_OPEN_PO" in codes:
            options = ["Confirm which purchase order this invoice belongs to, then re-run.",
                       "If there is no PO, obtain the budget owner's approval as a non-PO invoice."]
        else:
            options = ["Approve as billed if the document supports it.", "Reject and ask the vendor to correct it."]
        issues = "\n".join(f"  {i}. {c.title}\n     {c.detail}" for i, c in enumerate(decisive, start=1))
        opts = "\n".join(f"  {chr(96 + i)}) {o}" for i, o in enumerate(options, start=1))
        message = {
            "kind": "review_request", "to_name": route["name"], "to": route["email"],
            "subject": f"Decision needed: {vendor_name} {ref}" + (f" ({fmt_inr(total)})" if total is not None else ""),
            "body": (f"Hi {route['name'].split()[0]},\n\n"
                     f"{vendor_name}'s {ref}" + (f" for {fmt_inr(total)}" if total is not None else "")
                     + (f" against {po_no}" if po_no else "") + " was not paid automatically. It needs your call:\n\n"
                     f"{issues}\n\nYour options:\n{opts}\n\n"
                     f"Everything else on the invoice checked out. The full trail is in run {ctx.run_id}.\n\n"
                     f"{_signature()}"),
        }
        next_steps = [f"Routed to {route['name']} ({route['role']}) with the question and the options.",
                      "Nothing has been posted or scheduled for payment.",
                      "On approval the invoice is posted against the PO; on rejection the vendor is notified."]
    elif decision == "RETURNED":
        lead = returns[0]
        missing = lead.data.get("missing", [])
        # only things the vendor can correct belong in a message to the vendor
        vendor_fixable = [c for c in reviews if c.code in _VENDOR_FIXABLE]
        if lead.code == "MISSING_FIELDS":
            headline = f"Returned to {vendor_name}: the invoice has no {' and no '.join(missing)}."
            bullets = "\n".join(f"  - {m[0].upper() + m[1:]}" for m in missing)
            identify = []
            if po_no:
                identify.append(f"purchase order {po_no}")
            if total is not None:
                identify.append(f"amount {fmt_inr(total)}")
            body = (f"Dear {vendor_name} accounts team,\n\n"
                    f"Thank you for the document “{ctx.filename}”"
                    + (f" ({', '.join(identify)})" if identify else "") + ". We are unable to process it because it "
                    f"is missing:\n\n{bullets}\n\n"
                    "A tax invoice needs its own serial number and date of issue (CGST Rules, rule 46) before we can "
                    "book it and claim input credit. Please send a complete invoice and we will process it straight "
                    "away." + (" Nothing else on the document needs to change." if not vendor_fixable else
                               " When you re-issue it, please also look at the following, which we noticed on this "
                               "copy:\n\n" + "\n".join(f"  - {c.title}" for c in vendor_fixable)) + "\n\n"
                    f"Regards,\n{_signature()}")
        else:
            headline = f"Returned to sender: {_mid_sentence(lead.title)}."
            body = (f"Dear {vendor_name},\n\nWe received “{ctx.filename}” but could not read it as an invoice: "
                    f"{lead.detail}\n\nPlease resend it as a readable PDF.\n\nRegards,\n{_signature()}")
        message = {"kind": "vendor_email", "to_name": vendor_name, "to": vendor_email,
                   "subject": f"Invoice returned: information missing"
                              + (f" (your bill against {po_no})" if po_no else f" ({ctx.filename})"),
                   "body": body}
        next_steps = [f"Reply drafted to {vendor_email} listing exactly what is needed.",
                      "Nothing has been posted. The invoice restarts from the top when a corrected copy arrives."]
    else:  # REJECTED
        lead = rejects[0]
        headline = f"Rejected: {_mid_sentence(lead.title)}."
        if ctx.duplicate:
            o = ctx.duplicate["original"]
            paid = (f"was paid on {fmt_date(o['pay_date'])}" if o["status"] == "paid"
                    else f"is approved and scheduled for payment on {fmt_date(o['pay_date'])}")
            body = (f"Dear {vendor_name} accounts team,\n\n"
                    f"We received {ref} dated {fmt_date(ex.invoice_date)} for {fmt_inr(total)}. Our records show it "
                    f"is the same supply as your invoice {o['invoice_number']} dated {fmt_date(o['invoice_date'])}"
                    + (f" against {o['po_number']}" if o["po_number"] else "") + f", which {paid}.\n\n"
                    "We have therefore not processed this copy. No action is needed from you. If you believe it "
                    "covers a separate delivery, please send the delivery challan or proof of delivery and we will "
                    f"look again.\n\nRegards,\n{_signature()}")
            subject = f"{ref[0].upper() + ref[1:]} not processed: already settled as {o['invoice_number']}"
            next_steps = [f"Vendor notice drafted to {vendor_email} with the original invoice and payment reference.",
                          "Nothing has been posted. The original payment stands."]
        else:
            why = ("Your vendor account with us is currently on hold, so we cannot accept invoices against it. "
                   "Please contact our procurement team to resolve this, then send the invoice again."
                   if lead.code == "VENDOR_BLOCKED" else lead.detail)
            body = (f"Dear {vendor_name},\n\nWe are unable to process {ref}. {why}\n\n"
                    f"Regards,\n{_signature()}")
            subject = f"{ref[0].upper() + ref[1:]} could not be processed"
            route = _route_for(ctx, lead.route) if lead.route else None
            next_steps = ["Nothing has been posted or scheduled.",
                          f"{route['name']} ({route['role']}) is notified." if route else "Vendor notice drafted."]
        message = {"kind": "vendor_notice", "to_name": vendor_name, "to": vendor_email, "subject": subject, "body": body}

    passed = [c for c in ctx.checks if c.status == "pass"]
    checks.insert(0, Check("pass" if decision == "APPROVED" else "info",
                           f"{len(passed)} checks passed, {_plural(len(blocking), 'blocking issue')}",
                           "Policy: any duplicate or blocked vendor rejects; missing required fields return the "
                           "invoice to the vendor; any other failed check holds it for a named person; otherwise it "
                           "is approved."))
    ctx.decision = {
        "decision": decision, "label": DECISION_LABELS[decision], "headline": headline,
        "reasons": [c.to_dict() for c in decisive + insights], "route": route, "options": options,
        "message": message,
        "payment": payment, "next_steps": next_steps, "passed_count": len(passed),
        "passed": [c.title for c in passed],
        "confidence": {"extraction": ex.confidence() if ex else None, "po_match": ctx.po_confidence,
                       "po_method": ctx.po_method},
    }
    summary = DECISION_LABELS[decision] + (f" · {route['name']}" if route and decision == "NEEDS_REVIEW" else "")
    status = {"APPROVED": "ok", "NEEDS_REVIEW": "warn", "RETURNED": "warn", "REJECTED": "fail"}[decision]
    return StageResult(status, summary, checks, {"decision": decision, "headline": headline})


# =================================================================== 10 act ===

def why_not_postable(ctx: Ctx) -> Optional[str]:
    """If this invoice could not be posted even with a reviewer's approval, say why."""
    if not ctx.ex or ctx.amounts.get("total") is None:
        return "no amounts could be read from the document"
    if not ctx.vendor:
        return "the vendor is not in the vendor master, so there is no account to pay"
    if ctx.ex.currency != config.BUYER["currency"]:
        return (f"the invoice is in {ctx.ex.currency}; it has to be converted and booked by a person")
    return None


def posting_payload(ctx: Ctx) -> Optional[dict]:
    """Everything needed to post this invoice to the ledger, now or after a reviewer approves it."""
    if why_not_postable(ctx):
        return None
    pay = _pay_date(ctx)
    total = ctx.amounts["total"]
    subtotal = ctx.amounts.get("subtotal")
    tax = ctx.amounts.get("tax")
    # An invoice is only ever posted against a PO raised on the same vendor.
    po = ctx.po if ctx.po and ctx.po["vendor_id"] == ctx.vendor["id"] else None
    if subtotal is None:
        subtotal = q2(total - tax) if tax is not None else (
            q2(total / (1 + D(po["tax_rate"]) / 100)) if po else total)
    allocations = ctx.allocations
    if po and not allocations:
        # Reconciliation did not run or could not tie the value to lines. If a reviewer accepts the
        # invoice anyway, the PO must still be drawn down, or the same value could be billed again.
        allocations = pro_rata_allocations(po, subtotal)
    return {
        "vendor_id": ctx.vendor["id"], "invoice_number": ctx.ex.invoice_number or f"UNNUMBERED-{ctx.run_id}",
        "invoice_date": ctx.ex.invoice_date, "po_number": po["po_number"] if po else None,
        "subtotal": to_float(subtotal), "tax": to_float(tax if tax is not None else total - subtotal),
        "total": to_float(total), "pay_date": pay.isoformat() if pay else None,
        "file_hash": ctx.file_hash, "allocations": allocations,
    }


def post_to_ledger(run_id: str, p: dict) -> int:
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ledger_invoices (run_id, vendor_id, invoice_number, invoice_number_norm, invoice_date,"
            " po_number, subtotal, tax, total, status, pay_date, file_hash, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,'scheduled',?,?,'run',?)",
            (run_id, p["vendor_id"], p["invoice_number"], normalize_invoice_number(p["invoice_number"]),
             p["invoice_date"], p["po_number"], p["subtotal"], p["tax"], p["total"], p["pay_date"], p["file_hash"],
             db.now_iso()),
        )
        ledger_id = cur.lastrowid
        for a in p["allocations"]:
            conn.execute(
                "INSERT INTO ledger_lines (ledger_invoice_id, po_number, po_line_no, description, qty, unit_price,"
                " amount) VALUES (?,?,?,?,?,?,?)",
                (ledger_id, p["po_number"], a.get("po_line_no"), a.get("description"), a.get("qty"),
                 a.get("unit_price"), a.get("amount") or 0),
            )
    return ledger_id


def po_progress(po: dict) -> str:
    if po["status"] in ("fully_billed", "closed"):
        if abs(po["billed_value"] - po["value"]) < 0.01:
            return f"{po['po_number']} is now fully billed ({fmt_inr(po['value'])})."
        return (f"{po['po_number']} is now fully billed: {fmt_inr(po['billed_value'])} against a PO value of "
                f"{fmt_inr(po['value'])}.")
    supplied = po["value"] - po["remaining_value"]
    pct = min(99, int(supplied / po["value"] * 100)) if po["value"] else 0
    still_open = [l for l in po["lines"] if l["remaining_qty"] > 0]
    # After a pro-rata draw-down the open quantities are fractions; the value says it better.
    whole = all(abs(l["remaining_qty"] - round(l["remaining_qty"])) < 1e-6 for l in still_open)
    open_lines = [f"{_qty_unit(round(l['remaining_qty']), l['unit'])} of “{_short(l['description'], 36)}”"
                  for l in still_open] if whole else []
    return (f"{po['po_number']} is now {pct}% supplied and billed; {fmt_inr(po['remaining_value'])} stays open"
            + (f" ({'; '.join(open_lines)})." if open_lines else "."))


def act(ctx: Ctx) -> StageResult:
    d = ctx.decision
    checks: list[Check] = []
    data: dict = {"posted": False, "po_after": None}
    decision = d["decision"]
    if decision == "APPROVED":
        ledger_id = post_to_ledger(ctx.run_id, posting_payload(ctx))
        po_after = db.get_po(ctx.po["po_number"])
        data.update({"posted": True, "ledger_id": ledger_id, "po_after": _po_summary(po_after)})
        progress = po_progress(po_after)
        d["next_steps"].append(progress)
        d["po_after"] = data["po_after"]
        checks.append(Check("pass", "Invoice posted to the ledger", f"Ledger entry #{ledger_id} against {ctx.po['po_number']}."))
        checks.append(Check("pass", f"Payment scheduled for {fmt_date(d['payment']['pay_date'])}",
                            f"{fmt_inr(d['payment']['amount'])} to {d['payment']['payee']}, account on file "
                            f"{d['payment']['bank_account_masked']}."))
        checks.append(Check("pass", "Purchase order updated", progress))
        summary = f"Posted · payment scheduled {fmt_date(d['payment']['pay_date'])} · PO updated"
    elif decision == "NEEDS_REVIEW":
        r = d["route"]
        checks.append(Check("info", f"Sent to {r['name']} ({r['role']})",
                            "The review request states the issue, the numbers and the options. It is waiting in the "
                            "review queue."))
        checks.append(Check("info", "Nothing posted, nothing scheduled", "The ledger and the PO are unchanged until a person decides."))
        summary = f"Waiting on {r['name']} · nothing posted"
    elif decision == "RETURNED":
        checks.append(Check("info", f"Reply drafted to {d['message']['to']}", "Lists exactly what is missing. A person sends it."))
        checks.append(Check("info", "Nothing posted, nothing scheduled"))
        summary = "Reply to vendor drafted · nothing posted"
    else:
        checks.append(Check("info", f"Notice drafted to {d['message']['to']}", "Explains why the invoice was not processed."))
        checks.append(Check("info", "Nothing posted, nothing scheduled", "The ledger and the PO are unchanged."))
        summary = "Vendor notice drafted · nothing posted"
    return StageResult("ok", summary, checks, data)


STAGES = [
    ("intake", "Intake", "Receive the file and fingerprint it", intake),
    ("read", "Read document", "Pull the text out; OCR if it is a scan", read),
    ("extract", "Extract fields", "Turn the page into structured invoice data", extract),
    ("validate", "Validate invoice", "Completeness, arithmetic, tax, dates", validate),
    ("vendor", "Verify vendor", "Approved vendor master, tax ID, bank account", vendor),
    ("po_match", "Find purchase order", "Use the PO quoted, or infer it", po_match),
    ("duplicate", "Duplicate check", "Has this invoice been accepted before?", duplicate),
    ("reconcile", "Reconcile with PO", "Line by line: price, quantity, what is still open", reconcile),
    ("decide", "Decide", "Apply the policy to everything found", decide),
    ("act", "Act", "Post, route or reply", act),
]
STAGE_META = [{"id": s[0], "title": s[1], "blurb": s[2]} for s in STAGES]
