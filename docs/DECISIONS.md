# Design decisions

The choices behind the build, the reasoning for each, and the assumptions made where the brief was open. Written to be talked through.

## 1. Why this problem, and who it is for

I chose invoice processing because its failure modes are concrete and expensive: paying twice, paying a price nobody agreed, paying a fraudster. That makes "handles edge cases deliberately" testable rather than a matter of taste.

The user I built for is the AP analyst and the controller behind them. The analyst wants the routine invoices gone and the odd ones explained. The controller wants to know that nothing was paid on a guess and that every payment can be traced to a reason. Those two needs drive almost everything below.

## 2. AI reads. Rules decide. People handle exceptions.

This is the central decision.

- **Reading** an invoice is a language problem: layouts vary without limit, labels differ ("Bill No", "Invoice #", "Buyer's Order No."), scans are noisy. That is where a model earns its place.
- **Deciding** whether to pay is a control. It has to give the same answer every time, be explainable to an auditor, and be changeable by finance without retraining anything. That is what rules are for.

So the model (Claude, when a key is configured) is confined to one stage and one job: turn the page into typed fields. It never sees the PO and never proposes a decision. Everything after extraction is deterministic code reading thresholds from the Policy page.

Two consequences I wanted:

1. **A model error cannot turn silently into a payment.** Extracted values are cross-checked against each other (quantity × rate, lines to total, words against figures) and against the PO. There is a test where the reader returns a wrong unit price; the arithmetic check catches it and the invoice is held.
2. **The demo cannot die on an API call.** If Claude is unavailable, the run falls back to the built-in parser and says so in the trace.

## 3. A built-in parser as the default reader

With no API key, extraction is done by a deterministic parser. It does not use templates. It works from labels and column geometry: it finds the line-item table by its header, identifies the amount column by position, and tells quantity from HSN codes and box dimensions by requiring quantity × rate to reproduce the amount.

Why build it when a model exists:

- The brief requires the process to run live. A reader with no network dependency always runs.
- It makes the "AI reads" boundary real: there are two interchangeable readers behind one interface, and the rules do not care which one ran.
- It is honest about its limits. It reads the five demo layouts and three further common ones covered by tests (Tally-style with values under their labels, accounting-software style, per-line GST columns), and degrades safely on others: a field it cannot find is left empty and the invoice is returned or held. For arbitrary invoices, Claude is the better reader.

## 4. It refuses to guess

The Swiftline invoice has no invoice number and no date, but the page does contain dates: a billing period. The easy implementation takes the first date on the page. This one only accepts a date that is labelled as the invoice date, leaves the field empty otherwise, and returns the invoice to the vendor with a list of what is missing.

The same principle shows up elsewhere: all-numeric dates (03/11/2026) are read day-first, and the trace says so and shows the other reading; an inferred PO is never reported at more than 95% confidence; a tax rate that is not printed is assumed only with a flag for a person.

## 5. Line level, not header level

Edge case 1 exists to show why. The Nexa invoice is 0.23% **below** the PO value, so any total-based tolerance passes it. But eleven laptops are billed at ₹68,000 against an agreed ₹62,500, an overcharge of ₹60,500, hidden because the twelfth laptop was not shipped. Only a line-by-line comparison sees it. The process says so explicitly ("a total-only match would have approved this invoice") and gives the buyer the short-pay figure.

## 6. "Open on the PO", not "PO value"

A PO billed across several invoices cannot be checked against its original quantities. Every approved invoice writes its allocations to the ledger, and the next invoice is compared with what is still open. That is why the same over-billing sample reports 500 catalogues too many on a fresh database and 2,500 too many after the earlier part-shipment has been approved. The Purchase orders page shows this state changing.

"Open" is tracked by quantity and valued at the PO price. That keeps the remaining balance meaningful even after a reviewer approves an invoice at a different price.

## 7. Inferring a PO, and knowing when not to

The GreenLeaf scan carries no PO number, and the vendor has two open POs for exactly the same amount (October and November services). Amount alone cannot choose. The score combines amount fit (45%), description similarity (35%) and service period (20%); the invoice says "October 2026", so the October PO wins by 18 points.

The inference is accepted only above 80% **and** with a 10-point lead over the runner-up. There is a test with the same invoice minus the month: the two POs tie and the process declines to choose, sending both candidates to a person. An inferred match that cannot be explained is worse than no match.

## 8. Duplicates are recognised, not string-matched

The disguised duplicate is not byte-identical to the original (a file hash misses it) and its number is written differently (an exact-number check misses it). Invoice numbers are normalised: punctuation, case, leading zeros and trailing revision markers are ignored, so `SPI-2026-27-0982-R` is recognised as `SPI/2026-27/0982`.

The care is in the other direction. Consecutive invoices from one vendor look alike (`…/0982`, `…/0983`), series letters look like suffixes (`…/A1`, `…/A2`), and regular monthly invoices repeat the same amount. So numeric parts must match exactly, only explicit re-issue markers (`R`, `REV`, `COPY`) count as suffixes, and a suffix alone is never enough: the copy must also carry the same total and the same PO or lines. A suffix on different content, or the same content under a new number, is a question for a person, not a rejection.

## 9. Four outcomes, each with a next step

A decision that ends in "flagged" just moves the work. Each outcome here carries the action:

- Approved: posted, PO updated, payment scheduled to the account **on file**.
- Held: routed to a named person with the specific issue, the amounts, and two or three options.
- Returned: the reply to the vendor is drafted, listing exactly what is missing.
- Rejected: the notice is drafted, citing the original invoice and its payment.

Messages are drafted, not sent. A person stays in the loop for anything that leaves the building.

## 10. Payment goes to the account on file

The bank account printed on an invoice is compared with the vendor master but never used. A mismatch is the classic payment-redirection pattern, so it is held for treasury with an instruction to verify by phone on a known number. On a scan, a one- or two-digit difference is treated as an OCR misread and noted, precisely because the printed account is never where the money goes.

## 11. OCR is corroborated, not trusted

A scan read at 94% confidence still has a chance of a wrong digit. So the total read by OCR has to agree with two independent things before it is paid: the amount in words on the same page, and the open value on the PO. Three sources agreeing is a much stronger statement than one confidence score.

## 12. The live view replays an event log

Each stage writes events (started, log line, completed with its checks and data) to the database. The browser renders the run from those events over a server-sent event stream. Watching a run live and opening one from last week go through the same code, so the live view cannot drift from the record, and the event log doubles as the audit trail.

"Watchable" speed holds each stage on screen for about half a second. It changes nothing about the work, and the banner always reports the true compute time separately.

## 13. One run at a time

Runs are queued on a single worker. Two copies of the same invoice submitted together therefore cannot both pass the duplicate check. A reviewer's approval re-checks the ledger for the same reason: of two held copies, only one can be approved.

## 14. Price and quantity are judged separately

Each line's price is checked against its own tolerance. The value check is then made at PO prices, so it measures only quantity and scope: are more goods being billed than are open? Mixing the two would let a tolerated 1.5% price difference on a large invoice fail the value cap instead, which makes the price tolerance meaningless. A price far *below* the PO (25% or more) is also questioned: it usually means a different, cheaper item has been matched to a PO line and would use up that line's quantity.

## 15. It was attacked before it was submitted

After the build passed its own tests, I had it reviewed adversarially by a second, independent AI agent whose only job was to construct invoices that produce a wrong decision. It found real ones, and each is now a regression test (`tests/test_review_findings.py`):

- An invoice whose line table could not be read was approved on its total, because "value ₹0 is within the PO". Now: no readable lines, no payment.
- A lump sum a few hundred rupees over the PO was approved without drawing the PO down, so the same value could be billed again. Now the PO is consumed.
- A reviewer's approval of a part-billed lump sum posted nothing against the PO. Now it draws the PO down pro rata.
- `GLF/2026/A2` was rejected as a "revised copy" of `GLF/2026/A1`. Series letters are no longer treated as revision suffixes.
- "…challan no. 4471 dated 02-Oct-2026" in a sentence overrode the labelled invoice date.
- A vendor name that merely resembled an approved one ("Swiftline Couriers") was accepted when no tax ID was printed.

The lesson I took from it: the happy path and the designed edge cases were right from the start; the defects were all in inputs I had not imagined. That is the argument for failing safe by default, and for the rule that anything the process cannot verify goes to a person.

## Assumptions

Where the brief left a choice, this is what I assumed.

| Area | Assumption |
|---|---|
| Company | A mid-size Indian company: INR, GST, vendor GSTINs, day-first dates |
| Matching | Two-way (invoice to PO, cumulative). No goods-receipt data is available |
| Tax | The PO records pre-tax values and one tax rate; tax is checked against that rate |
| Tolerances | 2% on unit price, none on quantity, 1% capped at ₹500 on PO value. Editable |
| Payment terms | From the vendor master, counted from the invoice date. A past-due invoice goes into the next payment run rather than being back-dated |
| Non-PO invoices | Not auto-approved. They go to a person for budget-owner approval |
| Reference data | Vendor master and PO register are accurate; they stand in for the ERP |
| Volume | Hundreds of invoices a month, so a sequential queue is ample |
| Documents | One invoice per PDF, in English |

## What I would build next

1. **Three-way match.** Add goods-receipt notes so "billed" is checked against "received", not only "ordered".
2. **Email intake.** Read the AP mailbox directly, and send the drafted replies after approval.
3. **Learning from reviews.** When a buyer approves the same vendor's 3% variance three times, propose a vendor-specific tolerance for finance to accept.
4. **Credit notes and partial approvals.** Let a reviewer short-pay from the screen and generate the debit note.
5. **A measured comparison of the two readers** on a labelled set of real invoices, to decide where the parser is enough and where the model is needed.
6. **Roles and sign-off limits**, so approvals follow the delegation-of-authority matrix.
