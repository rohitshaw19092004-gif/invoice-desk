"""The small parsing functions that everything else leans on."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.money import (find_periods, fmt_inr, number_to_words_indian, parse_amount, parse_date, words_to_number)
from app.seed import normalize_invoice_number as norm


@pytest.mark.parametrize("text,value", [
    ("1,68,032.00", "168032.00"),      # Indian grouping
    ("1,041,232.00", "1041232.00"),    # western grouping
    ("42.50", "42.50"),
    ("2,000", "2000"),
    ("Rs.1,150.00", "1150.00"),
    ("₹85,000.00", "85000.00"),
    ("(1,200.00)", "-1200.00"),        # accounting negative
    ("500/-", "500"),
])
def test_parse_amount(text, value):
    assert parse_amount(text) == Decimal(value)


@pytest.mark.parametrize("text", ["18%", "pcs", "PO-2026-0412", "", "29AAFCS8842K1Z6", "4-colour", "A4,"])
def test_parse_amount_rejects_non_numbers(text):
    assert parse_amount(text) is None


def test_parse_amount_repairs_ocr_digit_confusions_only_when_asked():
    assert parse_amount("2,44,26O.OO") is None
    assert parse_amount("2,44,26O.OO", ocr=True) == Decimal("244260.00")


@pytest.mark.parametrize("text,expected,ambiguous", [
    ("04-Nov-2026", date(2026, 11, 4), False),
    ("November 6, 2026", date(2026, 11, 6), False),
    ("7 Nov 2026", date(2026, 11, 7), False),
    ("2026-11-04", date(2026, 11, 4), False),
    ("07.11.2026", date(2026, 11, 7), True),    # day-first, could be read as 11 July
    ("03/11/2026", date(2026, 11, 3), True),
    ("25/11/2026", date(2026, 11, 25), False),  # 25 cannot be a month
    ("11/25/2026", date(2026, 11, 25), False),  # must be month-first
    ("05/05/2026", date(2026, 5, 5), False),    # same either way
    ("3-11-26", date(2026, 11, 3), True),
])
def test_parse_date(text, expected, ambiguous):
    d, _raw, amb = parse_date(text)
    assert d == expected and amb is ambiguous


def test_parse_date_ignores_impossible_dates_and_plain_numbers():
    assert parse_date("31/02/2026")[0] is None
    assert parse_date("Invoice 2026 for 12 units")[0] is None


@pytest.mark.parametrize("n", [0, 7, 19, 40, 99, 100, 101, 999, 1000, 81184, 114460, 168032, 244260, 1041232,
                               99999999, 123456789])
def test_amount_in_words_round_trips(n):
    assert words_to_number(f"Rupees {number_to_words_indian(n)} Only") == n


def test_words_to_number_details():
    assert words_to_number("Rupees Two Lakh Forty Four Thousand Two Hundred Sixty Only") == 244260
    assert words_to_number("Rupees One Hundred Twenty and Fifty Paise Only") == Decimal("120.50")
    assert words_to_number("Rupees Twelve Lacs Only") == 1200000
    assert words_to_number("Rupees Two Crore Five Lakh Only") == 20500000
    assert words_to_number("Rupees One Lakh Forty-Two Thousand Only") == 142000
    assert words_to_number("Rupees Tvvo Lakh Only") is None  # an OCR-mangled word is never guessed at
    assert words_to_number("") is None


def test_fmt_inr_uses_indian_grouping():
    assert fmt_inr(168032) == "₹1,68,032.00"
    assert fmt_inr(1041232) == "₹10,41,232.00"
    assert fmt_inr(999.5) == "₹999.50"
    assert fmt_inr(-60500) == "-₹60,500.00"
    assert fmt_inr(None) == "—"


def test_find_periods_separates_service_months_from_calendar_dates():
    assert find_periods("services for the month of October 2026") == {(2026, 10)}
    assert find_periods("Housekeeping - Nov'26") == {(2026, 11)}
    assert find_periods("Invoice dated 04 Nov 2026") == set()
    assert find_periods("Billing period 16 Oct 2026 to 31 Oct 2026") == set()


def test_invoice_number_normalisation():
    # punctuation, case and leading zeros never matter
    assert norm("SPI/2026-27/0982") == norm("spi-2026-27-982") == norm("SPI 2026 27 0982")
    # the next invoice in the series is a different invoice
    assert norm("SPI/2026-27/0982") != norm("SPI/2026-27/0983")
    # a revision suffix is kept in the strict form...
    assert norm("SPI/2026-27/0982") != norm("SPI-2026-27-0982-R")
    # ...and dropped in the loose form used to spot re-issues
    assert norm("SPI/2026-27/0982", True) == norm("SPI-2026-27-0982-R", True) == norm("SPI/2026-27/0982 REV 2", True)
    # but a letter that is part of a short number is not treated as a suffix
    assert norm("INV/A", True) == "INV|A"
    assert norm("") == ""
