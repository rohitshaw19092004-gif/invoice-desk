# Process map

The process on paper: what goes in, what happens at each stage, where the decisions are, what comes out. This was written before the build and kept in step with it.

## Input and output

- **Input:** one invoice PDF (digital or scanned), from an upload or the AP inbox.
- **Reference data:** approved vendor master, purchase order register (with lines), the ledger of invoices already accepted, and the policy thresholds.
- **Output:** one decision, the checks behind it, and the action taken: a ledger posting and payment instruction, a routed review request, or a drafted reply to the vendor.

## Flow

```
                    ┌──────────────┐
  invoice PDF  ───▶ │ 1  Intake    │  fingerprint the file
                    └──────┬───────┘
                           ▼            not a PDF ───────────────────────────┐
                    ┌──────────────┐                                         │
                    │ 2  Read      │  text layer, or OCR if it is an image   │
                    └──────┬───────┘                                         │
                           ▼            nothing legible ─────────────────────┤
                    ┌──────────────┐                                         │
                    │ 3  Extract   │  fields + line items (Claude or parser) │
                    └──────┬───────┘                                         │
                           ▼                                                 │
                    ┌──────────────┐                                         │
                    │ 4  Validate  │  required fields · arithmetic · tax     │
                    └──────┬───────┘     · amount in words · dates           │
                           ▼                                                 │
                    ┌──────────────┐                                         │
                    │ 5  Vendor    │  vendor master · tax ID · bank account  │
                    └──────┬───────┘                                         │
                           ▼                                                 │
                    ┌──────────────┐  PO quoted? ── yes ─▶ look it up        │
                    │ 6  Find PO   │       └────── no ──▶ infer from open POs│
                    └──────┬───────┘                                         │
                           ▼                                                 │
                    ┌──────────────┐                                         │
                    │ 7  Duplicate │  file · number · re-issue · content     │
                    └──────┬───────┘                                         │
                           ▼            duplicate / no PO ─▶ skip 8          │
                    ┌──────────────┐                                         │
                    │ 8  Reconcile │  price · quantity open · value · tax    │
                    └──────┬───────┘                                         │
                           ▼                                                 │
                    ┌──────────────┐ ◀───────────────────────────────────────┘
                    │ 9  Decide    │  first rule that applies wins
                    └──────┬───────┘
        ┌──────────────────┼──────────────────┬───────────────────┐
        ▼                  ▼                  ▼                   ▼
   REJECTED           RETURNED           HELD FOR REVIEW       APPROVED
   duplicate,         required field     any other failed      everything
   blocked vendor     missing,           check                 passed
                      unreadable
        │                  │                  │                   │
        ▼                  ▼                  ▼                   ▼
                    ┌──────────────┐
                    │ 10  Act      │
                    └──────────────┘
   notice to          reply to vendor    routed to a named     post to ledger,
   vendor drafted     drafted            person with options   update PO,
                                         │                     schedule payment
                                         ▼
                              reviewer approves ─▶ post, update PO, schedule
                              reviewer rejects  ─▶ nothing posted
```

Stages 4 to 8 do not stop at the first problem. They collect every finding, so a held invoice shows the reviewer everything that is wrong with it at once, and a returned invoice tells the vendor everything to fix in one message.

## Stage by stage

| # | Stage | Input | What happens | Decision points | Output |
|---|---|---|---|---|---|
| 1 | Intake | File bytes | SHA-256 fingerprint; check for a PDF signature; note if this exact file was seen before | Not a PDF → return | Fingerprint |
| 2 | Read | PDF | Per page: use the text layer if there is one; if the page is a picture (an image covers it), render at 300 dpi (200 on the hosted demo) and OCR. Build words → cells → rows with positions | No legible text → return. OCR confidence below the floor, or more pages than the limit → hold (AP) | Positioned text, page previews |
| 3 | Extract | Positioned text (and the PDF, for Claude) | Vendor, tax ID, invoice number, dates, PO reference, line items, subtotal, tax lines, total, amount in words, bank details. Each with a confidence and its location on the page | None. Missing values stay empty | Structured invoice |
| 4 | Validate | Structured invoice | Required fields present; qty × rate = amount; lines = subtotal; subtotal + tax = total; words = figure; date not in the future or stale; tax split out of an inclusive total; document is actually an invoice | Missing required field → return. Arithmetic, words or date problem, no readable line items, or a credit note / proforma / quotation → hold (AP) | Normalised pre-tax, tax and total |
| 5 | Vendor | Name, tax ID, bank account | Exact tax-ID match, else near-exact name match (≥ 90% edit similarity over the whole name); status; bank account against the one on file | Unknown or tax-ID conflict → hold (Vendor Master). Blocked → reject. Different bank account → hold (Treasury) | Vendor record, terms |
| 6 | Find PO | PO reference, vendor, amounts, descriptions | Quoted PO: look up (tolerant of formatting). None: score the vendor's open POs = 45% amount fit + 35% description similarity + 20% service period | Quoted PO missing or issued to someone else → hold (AP). No candidate ≥ 80%, or lead over runner-up < 10 points → hold (AP). PO already fully billed → hold (buyer) | PO with open quantities |
| 7 | Duplicate | Vendor's accepted invoices | Same file hash; same normalised number; same number with a revision suffix; same PO + total + lines under a new number within 90 days | Same file or number → reject. Revision suffix **and** the same content (total plus PO or lines) → reject as a re-issue. Revision suffix with different content, or same content under a new number → hold (AP) | Match and comparison |
| 8 | Reconcile | Invoice lines, PO lines, billed-to-date | Pair lines by description; unit price vs PO price; quantity vs quantity still open; what is billed, valued at PO prices, vs open value; tax vs PO rate. A single bundled line is matched on value against everything open, and draws the PO down pro rata if it covers only part | Price over tolerance, price far below the PO (probably a different item), quantity or value over what is open, line not on PO, part-billed lump sum → hold (buyer). Tax mismatch → hold (AP) | Line comparison, allocations |
| 9 | Decide | All findings | Reject > return > hold > approve. Clean invoices above the straight-through limit are held (AP). Review goes to the highest-priority route: Treasury > Vendor Master > buyer > AP | — | Decision, reasons, route, options, drafted message |
| 10 | Act | Decision | Approve: post invoice and allocations, update the PO, schedule payment. Otherwise: queue for the reviewer or draft the vendor message; post nothing | — | Ledger entry / review task / draft |

## Who gets what

| Route | Person (demo data) | Gets |
|---|---|---|
| PO owner | The buyer named on the PO | Price variance, quantity or value beyond what is open, lines not on the PO, exhausted PO, bundled part-billing |
| AP lead | Meera Iyer | Arithmetic, words vs figures, dates, PO not found or ambiguous, possible duplicate, tax mismatch, poor scan, above the straight-through limit |
| Vendor Master desk | Deepak Shah | Unknown vendor, tax ID that does not match the name |
| Treasury & Controls | Kavita Menon | Bank account on the invoice differs from the one on file |

## Policy thresholds (defaults)

| Setting | Default | Used in |
|---|---|---|
| Unit price tolerance | 2% | Reconcile |
| Quantity tolerance | 0% | Reconcile |
| PO value tolerance (billed at PO prices vs open value) | 1%, capped at ₹500 (stricter wins) | Reconcile |
| Rounding tolerance | ₹1 | Validate |
| Tax tolerance | 0.5% | Reconcile |
| Stale invoice age | 180 days | Validate |
| Duplicate look-back (content match) | 90 days | Duplicate |
| Inferred PO: minimum confidence / lead | 80% / 10 points | Find PO |
| Minimum OCR confidence | 70% | Read |
| Straight-through approval limit | ₹25,00,000 | Decide |

All editable on the Policy page; changes apply from the next run.
