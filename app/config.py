"""Paths, environment and the identity of the company this desk works for."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:  # optional: a local .env file is a convenience, never a requirement
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover
    pass

DATA_DIR = Path(os.environ.get("INVOICE_DESK_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "invoice_desk.db"
UPLOAD_DIR = DATA_DIR / "uploads"
PREVIEW_DIR = DATA_DIR / "previews"
SAMPLES_DIR = BASE_DIR / "samples" / "invoices"
STATIC_DIR = BASE_DIR / "app" / "static"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PAGES = 12

# The buyer: the company whose AP team this process works for. Its own name and
# tax ID appear on every invoice ("Bill To"), so the extractor must know them in
# order not to mistake the customer for the vendor.
BUYER = {
    "name": "Aurelia Home & Living Pvt. Ltd.",
    "short_name": "Aurelia Home & Living",
    "gstin": "29AAKCA7421M1ZT",
    "address": "4th Floor, Prestige Tech Park, Outer Ring Road, Bengaluru 560103",
    "ap_email": "ap@aureliahome.example",
    "currency": "INR",
}

AI_MODEL = os.environ.get("INVOICE_DESK_MODEL", "claude-opus-5-5")

# Used only when an invoice says its total includes tax but prints no rate.
DEFAULT_TAX_RATE = 18.0

# Who exceptions are routed to. The PO's own buyer is looked up from the PO.
ROUTES = {
    "treasury": {"queue": "Treasury & Controls", "name": "Kavita Menon", "role": "Treasury & Controls",
                 "email": "treasury@aureliahome.example"},
    "procurement": {"queue": "Vendor Master desk", "name": "Deepak Shah", "role": "Procurement, Vendor Master",
                    "email": "vendor.master@aureliahome.example"},
    "ap": {"queue": "AP review", "name": "Meera Iyer", "role": "Accounts Payable Lead",
           "email": "meera.iyer@aureliahome.example"},
}


def ai_enabled() -> bool:
    """AI extraction is used only when a credential is present and not switched off."""
    if os.environ.get("INVOICE_DESK_AI", "auto").lower() in {"off", "0", "false", "no"}:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, PREVIEW_DIR):
        d.mkdir(parents=True, exist_ok=True)
