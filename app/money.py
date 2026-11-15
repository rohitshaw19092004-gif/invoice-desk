"""Exact money arithmetic and the number/date parsing that messy invoices need."""
from __future__ import annotations

import contextvars
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

CENT = Decimal("0.01")


def D(value) -> Decimal:
    """Decimal from anything, without inheriting float noise."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    if isinstance(value, float):
        return Decimal(repr(round(value, 6)))
    return Decimal(str(value))


def q2(value) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def to_float(value) -> Optional[float]:
    return None if value is None else float(q2(value))


# The currency of the invoice being processed. Runs execute one at a time on a single worker
# thread, so a context variable is enough to make every message print the right currency.
_currency: contextvars.ContextVar[str] = contextvars.ContextVar("invoice_currency", default="INR")


def set_currency(code: Optional[str]) -> None:
    _currency.set((code or "INR").upper())


def fmt_inr(value, places: int = 2, currency: Optional[str] = None) -> str:
    """Money in the invoice's currency (or ``currency``). Rupees use Indian grouping: ₹1,68,032.00"""
    if value is None:
        return "—"
    v = D(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if v < 0 else ""
    code = currency or _currency.get()
    if code != "INR":
        return f"{sign}{code} {abs(v):,.{places}f}"
    symbol = "₹"
    whole, frac = f"{abs(v):.{places}f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{sign}{symbol}{whole}.{frac}"


def fmt_rate(value, currency: Optional[str] = None) -> str:
    """A unit price: two decimals normally, up to four when the rate needs them (0.023)."""
    if value is None:
        return "—"
    v = D(value)
    return fmt_inr(v, 2 if v == v.quantize(CENT) else 4, currency)


def invoice_currency() -> str:
    return _currency.get()


def fmt_qty(value) -> str:
    if value is None:
        return "—"
    v = D(value)
    if v == v.to_integral_value():
        return f"{int(v):,}"
    return f"{v.normalize():,}"


def fmt_pct(value, places: int = 1) -> str:
    v = D(value)
    return f"{v:+.{places}f}%"


# ---------------------------------------------------------------- numbers ---

_NUM_RE = re.compile(r"^[\(\-–−]?\s*(?:₹|rs\.?|inr|usd|\$|€|eur)?\s*[\-–−]?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)\s*\)?(?:/-)?$", re.I)

# Characters OCR commonly confuses inside numbers.
_OCR_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})


def parse_amount(token: str, ocr: bool = False) -> Optional[Decimal]:
    """Parse '1,68,032.00', 'Rs. 42.50', '(1,200.00)', '748,000.00'. None if not a number."""
    if token is None:
        return None
    t = token.strip().replace(" ", " ")
    if not t:
        return None
    if ocr and re.fullmatch(r"[\dOolI|SB,.\-]+", t) and re.search(r"\d", t):
        t = t.translate(_OCR_DIGIT_FIX)
    m = _NUM_RE.match(t)
    if not m:
        return None
    body = m.group(1).replace(",", "")
    try:
        val = Decimal(body)
    except InvalidOperation:
        return None
    negative = t.startswith("(") or bool(re.match(r"^[\-–−]", t)) or bool(re.search(r"[₹$€.a-z]\s*[\-–−]\s*\d", t, re.I))
    return -val if negative else val


def looks_like_money(token: str) -> bool:
    """A token formatted the way invoices print amounts (decimals or grouping)."""
    t = token.strip()
    return bool(re.fullmatch(r"[\(\-]?(?:₹|rs\.?|inr|\$)?\s*\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?\)?", t, re.I)) and (
        "." in t or "," in t
    )


# ------------------------------------------------------------------ dates ---

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]

_DATE_PATTERNS = [
    # 2026-11-04
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    # 04-Nov-2026, 4 Nov 2026, 04 November, 2026
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.]*([A-Za-z]{3,9})\.?[\s\-/.,]*(\d{2,4})\b"), "dMy"),
    # November 6, 2026
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"), "Mdy"),
    # 04/11/2026, 07.11.2026, 4-11-26
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b"), "num"),
]


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def parse_date(text: str, day_first: bool = True) -> tuple[Optional[date], Optional[str], bool]:
    """Find the first date in ``text``.

    Returns (date, matched_text, ambiguous). ``ambiguous`` is True for all-numeric
    dates such as 03/11/2026 where day and month could be swapped; the caller decides
    how to disclose the day-first assumption.
    """
    if not text:
        return None, None, False
    best = None
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    amb = False
                elif kind == "dMy":
                    mon = _MONTHS.get(m.group(2).lower())
                    if not mon:
                        continue
                    d = date(_year(int(m.group(3))), mon, int(m.group(1)))
                    amb = False
                elif kind == "Mdy":
                    mon = _MONTHS.get(m.group(1).lower())
                    if not mon:
                        continue
                    d = date(int(m.group(3)), mon, int(m.group(2)))
                    amb = False
                else:
                    a, b, y = int(m.group(1)), int(m.group(2)), _year(int(m.group(3)))
                    amb = a <= 12 and b <= 12 and a != b
                    if day_first:
                        if b > 12 and a <= 12:
                            a, b = b, a  # must be month-first
                        d = date(y, b, a)
                    else:
                        if a > 12 and b <= 12:
                            a, b = b, a
                        d = date(y, a, b)
            except ValueError:
                continue
            if not (1990 <= d.year <= 2100):
                continue
            if best is None or m.start() < best[3]:
                best = (d, m.group(0), amb, m.start())
    if best is None:
        return None, None, False
    return best[0], best[1], best[2]


def fmt_date(d) -> str:
    if d is None:
        return "—"
    if isinstance(d, str):
        try:
            d = datetime.strptime(d[:10], "%Y-%m-%d").date()
        except ValueError:
            return d
    return f"{d.day} {MONTH_NAMES[d.month][:3]} {d.year}"


_PERIOD_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?[\s,'\-]*((?:20)?\d{2})\b",
    re.I,
)


def find_periods(text: str) -> set[tuple[int, int]]:
    """(year, month) pairs mentioned as a service period, e.g. 'October 2026', "Oct'26"."""
    out: set[tuple[int, int]] = set()
    for m in _PERIOD_RE.finditer(text or ""):
        # '04 Nov 2026' is a calendar date, not a service period.
        before = (text[: m.start()]).rstrip(" -/.,")
        if re.search(r"\d{1,2}(?:st|nd|rd|th)?$", before) and not re.search(r"\d{3,}$", before):
            continue
        mon = _MONTHS.get(m.group(1).lower().rstrip("."))
        if mon:
            out.add((_year(int(m.group(2))), mon))
    return out


# --------------------------------------------------- amount in words (IN) ---

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000,
           "lacs": 100_000, "million": 1_000_000, "crore": 10_000_000, "crores": 10_000_000}


def _words_int(tokens: list[str]) -> Optional[int]:
    total = 0
    current = 0
    seen = False
    for tok in tokens:
        if tok in _UNITS:
            current += _UNITS[tok]
            seen = True
        elif tok == "hundred":
            current = (current or 1) * 100
            seen = True
        elif tok in _SCALES:
            total += (current or 1) * _SCALES[tok]
            current = 0
            seen = True
        else:
            return None  # an unknown word means we cannot vouch for the reading
    return total + current if seen else None


_NOISE_WORDS = r"\b(rupees?|inr|rs|indian|only|and|in|words|amount|total|chargeable|payable)\b"


def is_number_words(text: str) -> bool:
    """True if the text is made only of number words (used to join an amount in words that wraps)."""
    tokens = re.findall(r"[a-z]+", (text or "").lower().replace("-", " "))
    allowed = set(_UNITS) | set(_SCALES) | {"hundred", "only", "and", "paise", "paisa"}
    return bool(tokens) and all(t in allowed for t in tokens)


def words_to_number(text: str) -> Optional[Decimal]:
    """'Rupees One Lakh Sixty Eight Thousand Thirty Two Only' -> 168032. None if unparseable."""
    if not text:
        return None
    t = text.lower().replace("-", " ")
    paise = 0
    # "... and Fifty Paise Only"  /  "... Paise Fifty Only"
    m = re.search(r"\band\s+((?:[a-z]+\s+){1,4})pais[ea]\b", t) or re.search(
        r"\bpais[ea]\s+((?:[a-z]+\s*){1,4}?)(?:only\b|$)", t
    )
    if m:
        p = _words_int(re.findall(r"[a-z]+", m.group(1)))
        if p is None or p > 99:
            return None
        paise = p
        t = t[: m.start()]
    t = re.sub(_NOISE_WORDS, " ", t)
    rupees = _words_int(re.findall(r"[a-z]+", t))
    if rupees is None:
        return None
    return Decimal(rupees) + Decimal(paise) / 100


def number_to_words_indian(n: int) -> str:
    """168032 -> 'One Lakh Sixty Eight Thousand Thirty Two' (used to print sample invoices)."""
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven",
            "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def two(x: int) -> str:
        if x < 20:
            return ones[x]
        return (tens[x // 10] + (" " + ones[x % 10] if x % 10 else "")).strip()

    def three(x: int) -> str:
        h, r = divmod(x, 100)
        parts = []
        if h:
            parts.append(ones[h] + " Hundred")
        if r:
            parts.append(two(r))
        return " ".join(parts)

    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    if crore:
        parts.append(two(crore) if crore < 100 else three(crore))
        parts[-1] += " Crore"
    if lakh:
        parts.append(two(lakh) + " Lakh")
    if thousand:
        parts.append(two(thousand) + " Thousand")
    if n:
        parts.append(three(n))
    return " ".join(parts)
