"""SQLite persistence: the procurement system of record plus the run history.

Each thread keeps its own connection, so the web threads and the pipeline worker never
share one, and SQLite's WAL mode lets the dashboard read while a run is writing.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import config

_init_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    gstin TEXT,
    status TEXT NOT NULL DEFAULT 'approved',      -- approved | blocked
    status_note TEXT,
    payment_terms_days INTEGER NOT NULL DEFAULT 30,
    bank_name TEXT,
    bank_account TEXT,
    ifsc TEXT,
    email TEXT,
    category TEXT
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    po_date TEXT NOT NULL,
    description TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    tax_rate REAL NOT NULL DEFAULT 18.0,
    buyer_name TEXT,
    buyer_role TEXT,
    buyer_email TEXT,
    closed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS po_lines (
    po_number TEXT NOT NULL REFERENCES purchase_orders(po_number),
    line_no INTEGER NOT NULL,
    description TEXT NOT NULL,
    qty REAL NOT NULL,
    unit TEXT,
    unit_price REAL NOT NULL,
    PRIMARY KEY (po_number, line_no)
);

-- Invoices that have been accepted against a PO (seeded history + approved runs).
CREATE TABLE IF NOT EXISTS ledger_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    vendor_id TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    invoice_number_norm TEXT NOT NULL,
    invoice_date TEXT,
    po_number TEXT,
    subtotal REAL NOT NULL,
    tax REAL NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL,                         -- paid | scheduled
    pay_date TEXT,
    file_hash TEXT,
    source TEXT NOT NULL DEFAULT 'run',           -- seed | run
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_invoice_id INTEGER NOT NULL REFERENCES ledger_invoices(id) ON DELETE CASCADE,
    po_number TEXT,
    po_line_no INTEGER,
    description TEXT,
    qty REAL,
    unit_price REAL,
    amount REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT,
    source TEXT NOT NULL,                         -- upload | sample
    sample_key TEXT,
    pace_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,                         -- queued | running | done | error
    decision TEXT,                                -- APPROVED | NEEDS_REVIEW | RETURNED | REJECTED
    headline TEXT,
    top_reason TEXT,
    route_to TEXT,
    vendor_name TEXT,
    invoice_number TEXT,
    po_number TEXT,
    total REAL,
    currency TEXT,
    extractor TEXT,
    extraction_confidence REAL,
    duration_ms INTEGER,
    compute_ms INTEGER,
    result TEXT,
    review_status TEXT,                           -- pending | approved | rejected
    review_note TEXT,
    reviewed_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    stage TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS policy (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_local = threading.local()


def connect() -> sqlite3.Connection:
    """This thread's connection, opened once and kept.

    Reopening the database for every statement is slow in WAL mode (each close can
    checkpoint the log), which shows up as lag in the live view while the dashboard polls.
    """
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != str(config.DB_PATH):
        config.ensure_dirs()
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn, _local.path = conn, str(config.DB_PATH)
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with tx() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with tx() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid


def init_schema() -> None:
    with _init_lock, tx() as conn:
        conn.executescript(SCHEMA)


# ------------------------------------------------------------------ events ---

def add_event(run_id: str, type_: str, stage: Optional[str], payload: dict) -> int:
    with tx() as conn:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE run_id = ?", (run_id,)).fetchone()
        seq = row["n"]
        conn.execute(
            "INSERT INTO events (run_id, seq, ts, type, stage, payload) VALUES (?,?,?,?,?,?)",
            (run_id, seq, now_iso(), type_, stage, json.dumps(payload, default=str)),
        )
        return seq


def get_events(run_id: str, after: int = 0) -> list[dict]:
    rows = query("SELECT seq, ts, type, stage, payload FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                 (run_id, after))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


# -------------------------------------------------------------------- runs ---

def create_run(filename: str, file_path: str, source: str, sample_key: Optional[str], pace_ms: int) -> dict:
    with tx() as conn:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM runs").fetchone()
        seq = row["n"]
        run_id = f"RUN-{seq:04d}"
        conn.execute(
            "INSERT INTO runs (id, seq, created_at, filename, file_path, source, sample_key, pace_ms, status)"
            " VALUES (?,?,?,?,?,?,?,?, 'queued')",
            (run_id, seq, now_iso(), filename, file_path, source, sample_key, pace_ms),
        )
    return get_run(run_id)


def update_run(run_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in fields.values()]
    execute(f"UPDATE runs SET {cols} WHERE id = ?", vals + [run_id])


def finish_run(run_id: str, event_payload: dict, event_type: str = "run_completed", **fields) -> None:
    """Close a run: write its final fields and its closing event in one transaction."""
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in fields.values()]
    with tx() as conn:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE run_id = ?", (run_id,)).fetchone()["n"]
        conn.execute("INSERT INTO events (run_id, seq, ts, type, stage, payload) VALUES (?,?,?,?,NULL,?)",
                     (run_id, seq, now_iso(), event_type, json.dumps(event_payload, default=str)))
        conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", vals + [run_id])


def get_run(run_id: str) -> Optional[dict]:
    row = query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if row and row.get("result"):
        row["result"] = json.loads(row["result"])
    return row


def list_runs(limit: int = 500) -> list[dict]:
    return query(
        "SELECT id, seq, created_at, started_at, finished_at, filename, source, sample_key, status, decision,"
        " headline, top_reason, route_to, vendor_name, invoice_number, po_number, total, currency, extractor,"
        " extraction_confidence, duration_ms, compute_ms, review_status, review_note, reviewed_at, error"
        " FROM runs ORDER BY seq DESC LIMIT ?",
        (limit,),
    )


# ------------------------------------------------------------------ policy ---

def get_policy() -> dict[str, float]:
    return {r["key"]: r["value"] for r in query("SELECT key, value FROM policy")}


def set_policy(values: dict[str, float]) -> None:
    with tx() as conn:
        for k, v in values.items():
            conn.execute("INSERT INTO policy (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (k, float(v)))


# ------------------------------------------------------- procurement reads ---

def get_vendors() -> list[dict]:
    rows = query("SELECT * FROM vendors ORDER BY id")
    for r in rows:
        r["aliases"] = json.loads(r["aliases"] or "[]")
    return rows


def get_vendor(vendor_id: str) -> Optional[dict]:
    row = query_one("SELECT * FROM vendors WHERE id = ?", (vendor_id,))
    if row:
        row["aliases"] = json.loads(row["aliases"] or "[]")
    return row


def get_po(po_number: str) -> Optional[dict]:
    """A purchase order with its lines and how much of each line is already billed."""
    po = query_one("SELECT * FROM purchase_orders WHERE po_number = ?", (po_number,))
    if not po:
        return None
    lines = query("SELECT * FROM po_lines WHERE po_number = ? ORDER BY line_no", (po_number,))
    billed = {
        r["po_line_no"]: r
        for r in query(
            "SELECT po_line_no, COALESCE(SUM(qty), 0) AS qty, COALESCE(SUM(amount), 0) AS amount"
            " FROM ledger_lines WHERE po_number = ? GROUP BY po_line_no",
            (po_number,),
        )
    }
    total_value = 0.0
    open_value = 0.0
    for ln in lines:
        b = billed.get(ln["line_no"], {"qty": 0, "amount": 0})
        ln["value"] = round(ln["qty"] * ln["unit_price"], 2)
        ln["billed_qty"] = round(b["qty"] or 0, 8)
        ln["billed_amount"] = round(b["amount"] or 0, 2)
        ln["remaining_qty"] = round(ln["qty"] - ln["billed_qty"], 8)
        total_value += ln["value"]
        # what is still to be supplied, valued at the PO price
        open_value += max(0.0, ln["remaining_qty"]) * ln["unit_price"]
    billed_value = sum(r["amount"] or 0 for r in billed.values())
    po["lines"] = lines
    po["value"] = round(total_value, 2)
    po["billed_value"] = round(billed_value, 2)
    po["remaining_value"] = round(open_value, 2)
    po["invoices"] = query(
        "SELECT id, run_id, invoice_number, invoice_date, subtotal, tax, total, status, pay_date, source"
        " FROM ledger_invoices WHERE po_number = ? ORDER BY invoice_date, id",
        (po_number,),
    )
    if po["closed"]:
        po["status"] = "closed"
    elif all(ln["remaining_qty"] <= 0.000001 for ln in lines):
        po["status"] = "fully_billed"
    elif po["billed_value"] > 0.005:
        po["status"] = "partially_billed"
    else:
        po["status"] = "open"
    return po


def list_pos(vendor_id: Optional[str] = None) -> list[dict]:
    if vendor_id:
        nums = query("SELECT po_number FROM purchase_orders WHERE vendor_id = ? ORDER BY po_number", (vendor_id,))
    else:
        nums = query("SELECT po_number FROM purchase_orders ORDER BY po_number DESC")
    return [get_po(n["po_number"]) for n in nums]


def ledger_for_vendor(vendor_id: str) -> list[dict]:
    rows = query("SELECT * FROM ledger_invoices WHERE vendor_id = ? ORDER BY id", (vendor_id,))
    for r in rows:
        r["lines"] = query("SELECT * FROM ledger_lines WHERE ledger_invoice_id = ? ORDER BY id", (r["id"],))
    return rows


def ledger_by_hash(file_hash: str) -> Optional[dict]:
    return query_one("SELECT * FROM ledger_invoices WHERE file_hash = ? ORDER BY id LIMIT 1", (file_hash,))
