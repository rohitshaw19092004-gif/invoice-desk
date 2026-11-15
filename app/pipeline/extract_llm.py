"""Stage 3 (AI): Claude reads the invoice and returns structured fields.

Used when an Anthropic credential is configured. The model only *reads*: it turns a
messy document into typed fields. It never decides anything. Every rule that follows
is deterministic code working on what was extracted here, and any failure of this
call falls back to the built-in parser so a run never dies on the AI step.
"""
from __future__ import annotations

import base64
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .. import config
from ..money import D, find_periods
from .extract_rules import Extraction, FieldMeta, LineItem, _bbox
from .reader import Document


class AIUnavailable(Exception):
    """The AI extraction could not be completed; the caller falls back to the parser."""


class LineItemOut(BaseModel):
    description: str
    quantity: Optional[float] = Field(None, description="Null when the line is a lump sum with no quantity printed.")
    unit: Optional[str] = None
    unit_price: Optional[float] = Field(None, description="Price per unit before tax. Null for lump-sum lines.")
    amount: Optional[float] = Field(None, description="Line amount exactly as printed.")


class TaxLineOut(BaseModel):
    label: str = Field(description="As printed, e.g. CGST, SGST, IGST, VAT.")
    rate_percent: Optional[float] = None
    amount: float


class InvoiceOut(BaseModel):
    vendor_name: Optional[str] = Field(None, description="The seller issuing the invoice, never the customer.")
    vendor_tax_id: Optional[str] = Field(None, description="The seller's GSTIN / tax registration number.")
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = Field(None, description="ISO 8601 (YYYY-MM-DD).")
    invoice_date_ambiguous: bool = Field(False, description="True if the printed date is all-numeric and day/month could be swapped.")
    due_date: Optional[str] = Field(None, description="ISO 8601 (YYYY-MM-DD).")
    po_number: Optional[str] = Field(None, description="The customer's purchase order number if it appears anywhere.")
    po_reference_in_running_text: bool = False
    currency: Optional[str] = Field(None, description="ISO code such as INR or USD.")
    line_items: list[LineItemOut] = []
    subtotal: Optional[float] = Field(None, description="Total before tax, only if printed.")
    tax_lines: list[TaxLineOut] = []
    total: Optional[float] = Field(None, description="The final amount payable as printed.")
    tax_inclusive: bool = Field(False, description="True when tax is included in the total and not shown as its own amount.")
    tax_rate_percent: Optional[float] = None
    amount_in_words: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    document_type: Optional[str] = Field(None, description="'invoice' for an invoice or tax invoice. Otherwise what the "
                                                           "document says it is: 'credit note', 'proforma invoice', "
                                                           "'quotation', 'delivery challan', 'purchase order'.")
    confidence: float = Field(description="0 to 1: how sure you are of the critical fields (vendor, number, date, total).")
    notes: list[str] = []


SYSTEM_PROMPT = """You extract structured data from vendor invoices for an accounts-payable team.

The customer (the company being billed) is {buyer_name}, tax ID {buyer_gstin}. It appears on every invoice
as the bill-to party. It is never the vendor.

Read the attached invoice and return the fields exactly as the document states them. This output feeds
automated payment controls, so accuracy matters more than completeness:

- If a field is not on the document, return null. Never infer or invent an invoice number, a date or a total.
  A blank "Invoice No." line means null. A billing or service period is not an invoice date.
- Copy identifiers (invoice number, PO number, tax ID, bank account) character for character.
- Dates in ISO format. Indian invoices print day before month: read 03/11/2026 as 3 November 2026 and set
  invoice_date_ambiguous to true whenever an all-numeric date could be read either way.
- Amounts as plain numbers. Indian digit grouping (1,68,032.00) means 168032.00.
- List line items as printed. If the invoice shows one lump-sum line with no quantity or rate, return that single
  line with quantity and unit_price null. Do not split it and do not compute values that are not printed.
- If tax is included in the total and not shown as a separate amount, set tax_inclusive to true, leave tax_lines
  empty, and give the rate if one is stated.
- The purchase order number may be labelled (PO No., Buyer's Order No., Customer Ref) or only mentioned in a
  sentence. Capture it either way and set po_reference_in_running_text accordingly.
- Use notes for anything a reviewer should know, such as illegible regions or handwritten changes."""


def _client():
    import anthropic

    return anthropic.Anthropic(timeout=90.0, max_retries=1)


def _call_model(pdf_bytes: bytes, text_layer: str) -> InvoiceOut:
    import anthropic

    client = _client()
    system = SYSTEM_PROMPT.format(buyer_name=config.BUYER["name"], buyer_gstin=config.BUYER["gstin"])
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": base64.standard_b64encode(pdf_bytes).decode("ascii")}},
        {"type": "text", "text": "Extract this invoice. For reference, this is the text our reader pulled from it "
                                 "(it may contain OCR errors; trust the document itself where they differ):\n\n"
                                 f"<reader_text>\n{text_layer}\n</reader_text>"},
    ]
    common = dict(model=config.AI_MODEL, max_tokens=8000, system=system, output_config={"effort": "low"},
                  messages=[{"role": "user", "content": content}], output_format=InvoiceOut)
    try:
        try:
            # Server-side refusal fallback keeps a run alive if a safety classifier declines the request.
            response = client.beta.messages.parse(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                  **common)
        except anthropic.BadRequestError:
            response = client.messages.parse(**common)
    except anthropic.AuthenticationError as exc:
        raise AIUnavailable("the API key was rejected") from exc
    except anthropic.RateLimitError as exc:
        raise AIUnavailable("the AI service is rate-limiting requests") from exc
    except anthropic.APIStatusError as exc:
        raise AIUnavailable(f"the AI service returned HTTP {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIUnavailable("the AI service could not be reached") from exc
    if response.stop_reason == "refusal":
        raise AIUnavailable("the model declined to process this document")
    if response.stop_reason == "max_tokens":
        raise AIUnavailable("the model's answer was cut off")
    parsed = response.parsed_output
    if parsed is None:
        raise AIUnavailable("the model did not return structured output")
    return parsed


def _dec(x) -> Optional[Decimal]:
    return None if x is None else D(x)


def _iso(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s.strip())
    return m.group(0) if m else None


def _locate(doc: Document, needle: Optional[str], prefer_last: bool = False):
    """Find where a value sits on the page so the viewer can highlight it. Best effort."""
    if not needle:
        return None, None
    target = re.sub(r"[\s,]", "", str(needle)).lower()
    if len(target) < 2:
        return None, None
    hits = []
    for row in doc.rows:
        words = row.words
        for i in range(len(words)):
            acc = ""
            for j in range(i, min(i + 12, len(words))):
                acc += re.sub(r"[\s,]", "", words[j].text).lower()
                if acc.strip(".:;") == target or acc == target:
                    hits.append((row, words[i: j + 1]))
                    break
                if not target.startswith(acc):
                    break
    if not hits:
        return None, None
    row, words = hits[-1] if prefer_last else hits[0]
    return row.page, _bbox(words, doc, row.page)


def to_extraction(out: InvoiceOut, doc: Document) -> Extraction:
    conf = max(0.0, min(1.0, float(out.confidence)))
    ex = Extraction(method="ai", model=config.AI_MODEL)
    ex.vendor_name = (out.vendor_name or "").strip() or None
    ex.vendor_gstin = (out.vendor_tax_id or "").strip().upper() or None
    ex.invoice_number = (out.invoice_number or "").strip() or None
    ex.invoice_date = _iso(out.invoice_date)
    ex.date_ambiguous = bool(out.invoice_date_ambiguous)
    ex.due_date = _iso(out.due_date)
    ex.po_number = (out.po_number or "").strip().upper() or None
    if ex.po_number:
        ex.po_reference_style = "in running text" if out.po_reference_in_running_text else "labelled"
    ex.currency = (out.currency or config.BUYER["currency"]).upper()
    ex.subtotal = _dec(out.subtotal)
    ex.total = _dec(out.total)
    ex.tax_inclusive = bool(out.tax_inclusive)
    ex.tax_lines = [{"label": t.label.upper(), "rate": _dec(t.rate_percent), "amount": D(t.amount)} for t in out.tax_lines]
    if ex.tax_lines:
        ex.tax_amount = sum((t["amount"] for t in ex.tax_lines), Decimal("0"))
        rates = [t["rate"] for t in ex.tax_lines if t["rate"] is not None]
        if rates and len(rates) == len(ex.tax_lines):
            ex.tax_rate = sum(rates, Decimal("0"))
    if ex.tax_rate is None and out.tax_rate_percent is not None:
        ex.tax_rate = D(out.tax_rate_percent)
    ex.amount_in_words = (out.amount_in_words or "").strip() or None
    kind = (out.document_type or "").strip()
    if kind and not re.fullmatch(r"(?:tax\s+|service\s+|commercial\s+|retail\s+|gst\s+)?invoice", kind, re.I):
        ex.doc_type = kind.title()
    ex.bank_account = re.sub(r"\D", "", out.bank_account_number or "") or None
    ex.ifsc = (out.bank_ifsc or "").strip().upper() or None
    ex.notes = [n for n in out.notes if n]
    for li in out.line_items:
        page, bbox = _locate(doc, f"{li.amount:,.2f}" if li.amount is not None else None)
        ex.line_items.append(LineItem(description=li.description.strip(), qty=_dec(li.quantity), unit=li.unit,
                                      unit_price=_dec(li.unit_price), amount=_dec(li.amount), confidence=conf,
                                      page=page, bbox=bbox))
    ex.periods = sorted(find_periods(" ".join(li.description for li in ex.line_items)))
    if not ex.periods:
        for row in doc.rows:
            if re.search(r"\b(period|for\s+the\s+month|month\s+of)\b", row.flat, re.I):
                ex.periods = sorted(find_periods(row.flat))
                if ex.periods:
                    break
    for key, value, last in [("vendor_name", ex.vendor_name, False), ("vendor_gstin", ex.vendor_gstin, False),
                             ("invoice_number", ex.invoice_number, False), ("po_number", ex.po_number, False),
                             ("bank_account", ex.bank_account, False)]:
        if value:
            page, bbox = _locate(doc, value, prefer_last=last)
            ex.meta[key] = FieldMeta(confidence=conf, raw=str(value), page=page, bbox=bbox)
    for key, value in [("invoice_date", ex.invoice_date), ("due_date", ex.due_date), ("total", ex.total),
                       ("subtotal", ex.subtotal), ("tax_amount", ex.tax_amount)]:
        if value is not None:
            ex.meta[key] = FieldMeta(confidence=conf, raw=str(value))
    # Amounts are located by their printed value: the total is the last occurrence on the page
    # (it follows the lines), the subtotal the first, and tax is shown at its first tax line.
    if ex.total is not None:
        ex.meta["total"].page, ex.meta["total"].bbox = _locate(doc, f"{ex.total:.2f}", prefer_last=True)
    if ex.subtotal is not None and ex.subtotal != ex.total:
        ex.meta["subtotal"].page, ex.meta["subtotal"].bbox = _locate(doc, f"{ex.subtotal:.2f}")
    if ex.tax_lines:
        ex.meta["tax_amount"].page, ex.meta["tax_amount"].bbox = _locate(doc, f"{ex.tax_lines[0]['amount']:.2f}")
    return ex


def extract(path: Path, doc: Document) -> Extraction:
    """Run the AI extraction. Raises AIUnavailable on any failure so the caller can fall back."""
    try:
        out = _call_model(Path(path).read_bytes(), doc.text)
    except AIUnavailable:
        raise
    except Exception as exc:  # anything unexpected must not kill the run
        raise AIUnavailable(f"unexpected error ({exc.__class__.__name__})") from exc
    return to_extraction(out, doc)
