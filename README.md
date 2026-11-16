# Invoice Desk

**PS-1 · Invoice processing, from PDF to decision.** A vendor invoice goes in; a reasoned accounts-payable decision comes out, with every step in between visible.

Built for the Zamp AI Solutions Analyst case study.

**Live demo: <https://invoice-desk-fvtf.onrender.com>**. It runs on a free instance that sleeps when idle, so the first visit can take up to a minute to wake, and the scanned invoice takes about 9 seconds there (under 2 locally). Use "Reset demo data" in the sidebar for a clean start.

To run it yourself:

```bash
./run.sh
```

Then open <http://localhost:8000>. No API key and no configuration are needed. (Scanned invoices need Tesseract: `brew install tesseract`.)

---

## What it does

An AP clerk opens each invoice, finds the purchase order, checks the numbers and decides what to do. Invoice Desk does the same thing in about 50 milliseconds (two seconds for a scan), and writes down why.

Every invoice ends in exactly one of four decisions:

| Decision | When | What the process does |
|---|---|---|
| **Approved for payment** | Everything matches | Posts the invoice against the PO, reduces what is open on the PO, schedules payment on the agreed terms to the bank account on file |
| **Held for review** | A check failed that a person has to judge | Routes it to a *named* person (the PO's buyer, AP lead, treasury or the vendor master desk) with the issue, the numbers and the options |
| **Returned to vendor** | Required information is missing or the file is unreadable | Drafts a reply saying exactly what is needed. Never guesses a missing value |
| **Rejected** | It duplicates an invoice already accepted, or the vendor is blocked | Drafts a notice pointing to the original invoice and its payment |

## The two screens

- **Live run view.** Ten stages execute one after another. Each shows what it did, the checks it ran and how long it took. Alongside: the document with every extracted field outlined on the page, the extracted data, the PO comparison, and the outcome with its drafted message.
- **Dashboard.** Every run with its outcome and reason, straight-through rate, what is waiting on a person, value approved and value stopped before payment. Plus the purchase order register (which changes as invoices are approved) and the policy page.

## The demo inbox

Seven invoices from five vendors, each in a different layout. Each one gives the same result whatever was run before it.

| # | Invoice | What it tests | Decision |
|---|---|---|---|
| Happy path | Shreeji Packaging, SPI/2026-27/1047 | Digital PDF, explicit PO, itemised lines, CGST + SGST | Approved |
| Edge 1 | Nexa IT, NXT/2026/3381 | **Totals agree, lines don't.** Invoice is 0.23% *under* the PO, but one line is billed 8.8% over the agreed price; a short shipment hides it | Held for the PO's buyer |
| Edge 2a | Orbit Print, OPS/26-27/0231 | **Split PO.** Third part-shipment; PO number buried in a sentence; must know what earlier invoices already billed | Approved, PO stays open |
| Edge 2b | Orbit Print, OPS/26-27/0246 | **Split PO, over-billed.** Final invoice bills more than is left on the PO | Held for the PO's buyer |
| Edge 3 | GreenLeaf (scan), GLF/26-27/0456 | **Scanned image, no PO number, one bundled line, tax inside the total**, and two open POs for the identical amount | Approved, PO inferred |
| Edge 4 | Shreeji Packaging, SPI-2026-27-0982-R | **Duplicate in disguise.** A paid invoice re-sent with a new date and a reformatted number | Rejected |
| Guardrail | Swiftline Logistics, unnumbered | **No invoice number, no invoice date.** The only dates on the page are a billing period | Returned to vendor |

Anything else can be uploaded too. An unknown vendor, a foreign-currency invoice, a blank page or a file that is not a PDF each get a sensible, explained outcome rather than an error.

## How a decision is reached

```
intake → read → extract → validate → vendor → find PO → duplicate → reconcile → decide → act
```

1. **Intake.** Fingerprint the file (SHA-256) for the audit trail.
2. **Read.** Digital PDFs: text is read directly. Scans: each page is rendered and read by OCR.
3. **Extract.** Turn positioned text into fields and line items. Claude does this when an API key is set; otherwise a built-in parser does. Either way, nothing is guessed: an unlabelled field stays empty.
4. **Validate.** Required fields; quantity × rate; lines to subtotal; subtotal + tax to total; amount in words against the figure; date sanity. Tax buried in a total is split out so the invoice can be compared with a pre-tax PO. A document with no readable line items, or one titled as a credit note or quotation, is never paid on its total alone.
5. **Verify vendor.** Match to the approved vendor master by tax ID, then by name. Compare the bank account printed on the invoice with the one on file.
6. **Find the PO.** Use the PO quoted on the invoice. If there is none, score the vendor's open POs on amount, description and service period, and accept the best only if it is clearly ahead.
7. **Duplicate check.** Against every invoice already accepted from that vendor: same file, same number (ignoring punctuation, leading zeros and revision suffixes), or same content under a new number.
8. **Reconcile.** Line by line: unit price against the PO price, quantity against what is *still open* on the PO after earlier invoices, what is billed (valued at PO prices) against the open value, tax against the PO's rate.
9. **Decide.** Duplicate or blocked vendor: reject. Missing required field: return. Any other failed check: hold for the right person. Otherwise: approve.
10. **Act.** Post and schedule, or route, or draft the reply.

**The design rule: AI reads, rules decide, people handle exceptions.** The reader's only job is to turn a messy page into typed fields. Every decision is made by deterministic rules with thresholds that are visible and editable on the Policy page, so the same invoice always gets the same answer and every answer traces to a named check. See [docs/DECISIONS.md](docs/DECISIONS.md) for the reasoning and [docs/PROCESS_MAP.md](docs/PROCESS_MAP.md) for the full map.

## Running it

```bash
./run.sh                                   # http://localhost:8000
PORT=9000 ./run.sh                         # a different port

.venv/bin/python scripts/run_samples.py -v       # every check of every sample, in the terminal
.venv/bin/python scripts/run_samples.py --check  # exits non-zero unless all 7 decisions are as expected
.venv/bin/python -m pytest tests -q              # 149 tests
.venv/bin/python samples/generate_samples.py     # regenerate the sample PDFs (re-run --check after: the scan's noise is random)
```

**Reset before a demo.** "Reset demo data" in the sidebar clears all runs and restores the purchase orders, payment history and policy. Approved invoices really are posted, so running the happy path a second time without a reset rejects it as a duplicate. That is correct behaviour, and worth showing on purpose.

### Reading with Claude (optional)

```bash
cp .env.example .env        # then set ANTHROPIC_API_KEY
.venv/bin/python scripts/run_samples.py --check   # confirm the outcomes are unchanged
```

With a key, the extract stage sends the PDF to Claude (`claude-opus-5-5` by default) and receives schema-validated fields. If the call fails for any reason the run falls back to the built-in parser and says so in the trace. The sidebar shows which reader is active.

### Deploying

The `Dockerfile` installs Tesseract and runs the seven sample invoices as a build step, so a build only succeeds if every demo decision is correct on that platform. `render.yaml` is a ready blueprint for Render; any Docker host works.

```bash
docker build -t invoice-desk . && docker run -p 8000:8000 invoice-desk
```

Data lives in SQLite under `data/` and is recreated from seed on first start.

## Project layout

```
app/
  main.py                 API, server-sent event stream, static UI
  db.py  seed.py          SQLite schema; vendors, POs, payment history, policy defaults
  money.py                exact decimal money, Indian number formats, date and amount-in-words parsing
  pipeline/
    reader.py             PDF → positioned words, cells and rows (text layer or OCR)
    extract_rules.py      built-in field extractor (labels + column geometry, no templates)
    extract_llm.py        Claude extractor (structured output), with fallback
    stages.py             the ten stages and every rule
    runner.py             runs the stages, emits events, records the result
  static/                 the single-page interface (no build step)
samples/                  generator and the seven invoice PDFs
scripts/run_samples.py    run the inbox from the terminal
tests/                    149 tests: the samples, 70 further scenarios and layouts, parsers, API, AI path
docs/                     process map and design decisions
```

## Assumptions and limits

Stated plainly, so they can be discussed rather than discovered:

- **Two-way match.** Invoices are matched to POs and to earlier invoices. There is no goods-receipt data, so "was it delivered?" is not checked. A three-way match is the first thing to add.
- **Indian conventions.** INR, GST at the PO's rate, day-first dates (ambiguous numeric dates are flagged, not silently assumed). Other currencies are read and held for a person.
- **Nothing is sent.** Emails and review requests are drafted for a person to send. The "reviewer" buttons let you act as the reviewer to show the loop closing.
- **Procurement data is seeded** in SQLite to stand in for an ERP or PO spreadsheet.
- **The built-in parser is heuristic.** It reads the five demo layouts and, in the test suite, three more that are common in practice: Tally-style (values printed under their labels, tax rows inside the table), accounting-software style ("Invoice#", "Balance Due") and invoices with per-line GST columns. On layouts it has not seen it degrades safely: missing fields are returned, never invented. For arbitrary real-world invoices Claude is the better reader; that is what the optional key is for.
- **One invoice per PDF**, up to 12 pages. A longer file is held for a person rather than read in part.
- **Credit notes are recognised, not processed.** A document titled as a credit note, proforma or quotation is held for AP.
