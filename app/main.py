"""Invoice Desk web app: API, live event stream and the single-page UI."""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import threading
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, config, db, seed
from .money import D, fmt_inr
from .pipeline import runner
from .pipeline.stages import DECISION_LABELS, STAGE_META, _po_summary, find_duplicate, po_progress, post_to_ledger


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    seed.ensure_seeded()
    # A run left "running" by a previous process will never finish: say so instead of spinning forever.
    for r in db.query("SELECT id FROM runs WHERE status IN ('queued', 'running')"):
        db.update_run(r["id"], status="error", finished_at=db.now_iso(),
                      error="Interrupted: the server restarted while this run was in progress.")
        db.add_event(r["id"], "run_failed", None, {"error": "Interrupted by a server restart. Run it again."})
    yield


app = FastAPI(title="Invoice Desk", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_for_app_shell(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# -------------------------------------------------------------------- meta ---

def _samples_with_last_run() -> list[dict]:
    last = {}
    for r in db.query("SELECT sample_key, id, decision, status, review_status FROM runs WHERE sample_key IS NOT NULL"
                      " ORDER BY seq"):
        last[r["sample_key"]] = {"run_id": r["id"], "decision": r["decision"], "status": r["status"],
                                 "review_status": r["review_status"]}
    out = []
    for s in seed.SAMPLES:
        item = dict(s)
        item["last_run"] = last.get(s["key"])
        item["available"] = (config.SAMPLES_DIR / s["file"]).exists()
        out.append(item)
    return out


@app.get("/api/meta")
def meta():
    return {
        "version": __version__,
        "buyer": config.BUYER,
        "stages": STAGE_META,
        "decision_labels": DECISION_LABELS,
        "ai": {"enabled": config.ai_enabled(), "model": config.AI_MODEL},
        "samples": _samples_with_last_run(),
        "today": runner.today().isoformat(),
    }


@app.get("/api/samples/{key}/file")
def sample_file(key: str):
    s = next((x for x in seed.SAMPLES if x["key"] == key), None)
    if not s:
        raise HTTPException(404, "Unknown sample")
    return FileResponse(config.SAMPLES_DIR / s["file"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{s["file"]}"'})


# -------------------------------------------------------------------- runs ---

def _clean_name(name: str) -> str:
    name = Path(name or "invoice.pdf").name
    name = re.sub(r"[^A-Za-z0-9._ \-()]+", "_", name).strip() or "invoice.pdf"
    return name[:120]


def _start_sample(key: str, pace_ms: int) -> dict:
    s = next((x for x in seed.SAMPLES if x["key"] == key), None)
    if not s:
        raise HTTPException(404, "Unknown sample")
    path = config.SAMPLES_DIR / s["file"]
    if not path.exists():
        raise HTTPException(500, "Sample file is missing. Run: python samples/generate_samples.py")
    run = db.create_run(s["file"], str(path), "sample", key, pace_ms)
    runner.submit(run["id"])
    return run


@app.post("/api/runs")
async def create_run(file: Optional[UploadFile] = File(None), sample_key: Optional[str] = Form(None),
                     pace_ms: int = Form(450)):
    pace_ms = max(0, min(int(pace_ms), 3000))
    if sample_key:
        run = _start_sample(sample_key, pace_ms)
        return {"run_id": run["id"]}
    if file is None:
        raise HTTPException(400, "Attach a PDF or choose a sample invoice.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"The file is larger than {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    name = _clean_name(file.filename)
    config.ensure_dirs()
    path = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:10]}_{name}"
    path.write_bytes(data)
    run = db.create_run(name, str(path), "upload", None, pace_ms)
    runner.submit(run["id"])
    return {"run_id": run["id"]}


@app.post("/api/runs/batch")
def run_inbox(pace_ms: int = Form(120)):
    """Queue the whole demo inbox, in arrival order."""
    pace_ms = max(0, min(int(pace_ms), 3000))
    ids = [_start_sample(s["key"], pace_ms)["id"] for s in seed.SAMPLES]
    return {"run_ids": ids}


@app.get("/api/runs")
def list_runs():
    return {"runs": db.list_runs()}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    run.pop("file_path", None)
    sample = next((s for s in seed.SAMPLES if s["key"] == run.get("sample_key")), None)
    return {"run": run, "events": db.get_events(run_id), "sample": sample}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = 0):
    """Server-sent events: replays everything after ``after`` and then follows the run live."""
    if not db.get_run(run_id):
        raise HTTPException(404, "Run not found")
    last_header = request.headers.get("last-event-id")
    if last_header and last_header.isdigit():
        after = max(after, int(last_header))

    async def stream():
        last = after
        idle = 0
        while True:
            events = db.get_events(run_id, last)
            for e in events:
                last = e["seq"]
                yield f"id: {e['seq']}\ndata: {json.dumps(e, default=str)}\n\n"
            if events:
                idle = 0
            else:
                run = db.get_run(run_id)
                if run is None or run["status"] in ("done", "error"):
                    # one last sweep so nothing emitted between the two reads is lost
                    for e in db.get_events(run_id, last):
                        last = e["seq"]
                        yield f"id: {e['seq']}\ndata: {json.dumps(e, default=str)}\n\n"
                    yield "event: end\ndata: {}\n\n"
                    return
                idle += 1
                if idle % 100 == 0:
                    yield ": keep-alive\n\n"
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.get("/api/runs/{run_id}/pages/{page}")
def run_page(run_id: str, page: int):
    if not re.fullmatch(r"RUN-\d{4,}", run_id):
        raise HTTPException(404, "Run not found")
    path = config.PREVIEW_DIR / run_id / f"page-{int(page)}.png"
    if not path.exists():
        raise HTTPException(404, "No preview for this page")
    # The URL carries a per-run version, so the image can be cached and never re-fetched mid-run.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    run = db.get_run(run_id)
    if not run or not Path(run["file_path"]).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(run["file_path"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{run["filename"]}"'})


_review_lock = threading.Lock()  # one reviewer decision at a time: check the ledger, then post


class ReviewIn(BaseModel):
    action: str  # approve | reject
    note: str = ""


@app.post("/api/runs/{run_id}/review")
def review_run(run_id: str, body: ReviewIn):
    """A person resolves an invoice the process held. Approval posts it; rejection closes it."""
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be 'approve' or 'reject'")
    with _review_lock:
        return _review(run_id, body)


def _review(run_id: str, body: ReviewIn):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run["decision"] != "NEEDS_REVIEW" or run["review_status"] != "pending":
        raise HTTPException(409, "This run is not waiting for a review.")
    route = (run["result"]["decision"].get("route") or {})
    reviewer = route.get("name") or "Reviewer"
    note = body.note.strip()[:500]
    event = {"action": body.action, "by": reviewer, "role": route.get("role"), "note": note, "posted": False}
    if body.action == "approve":
        posting = run["result"].get("posting")
        if posting:
            dup = find_duplicate(posting["vendor_id"], posting["invoice_number"], D(posting["total"]),
                                 posting["invoice_date"], posting["po_number"], posting["allocations"],
                                 posting["file_hash"], int(db.get_policy()["duplicate_window_days"]))
            if dup and dup["kind"] in ("file", "number", "reissue"):
                raise HTTPException(409, f"Invoice {dup['original']['invoice_number']} is already in the ledger, so "
                                         "this copy cannot be approved as well.")
            ledger_id = post_to_ledger(run_id, posting)
            event.update({"posted": True, "ledger_id": ledger_id, "pay_date": posting["pay_date"],
                          "amount": posting["total"]})
            if posting["po_number"]:
                po = db.get_po(posting["po_number"])
                event["po_after"] = _po_summary(po)
                event["po_progress"] = po_progress(po)
        else:
            why = run["result"].get("posting_blocked") or "it could not be prepared for posting"
            event["note_system"] = f"Approved, but not posted: {why}. Resolve that, then process the invoice again."
        db.update_run(run_id, review_status="approved", review_note=note, reviewed_at=db.now_iso())
    else:
        db.update_run(run_id, review_status="rejected", review_note=note, reviewed_at=db.now_iso())
    db.add_event(run_id, "review", None, event)
    return {"ok": True, "review": event}


# ------------------------------------------------------------- dashboards ---

@app.get("/api/stats")
def stats():
    runs = [r for r in db.list_runs(5000) if r["status"] == "done"]
    n = len(runs)
    by = {k: 0 for k in DECISION_LABELS}
    value = {k: 0.0 for k in DECISION_LABELS}
    for r in runs:
        by[r["decision"]] += 1
        value[r["decision"]] += r["total"] or 0
    pending = [r for r in runs if r["decision"] == "NEEDS_REVIEW" and r["review_status"] == "pending"]
    reviewer_approved = [r for r in runs if r["decision"] == "NEEDS_REVIEW" and r["review_status"] == "approved"]
    compute = [r["compute_ms"] for r in runs if r["compute_ms"] is not None]
    return {
        "runs": n,
        "by_decision": by,
        "value_by_decision": value,
        "straight_through_rate": round(by["APPROVED"] / n, 4) if n else None,
        "pending_review": len(pending),
        "pending_review_value": sum(r["total"] or 0 for r in pending),
        "approved_value": value["APPROVED"] + sum(r["total"] or 0 for r in reviewer_approved),
        "reviewer_approved": len(reviewer_approved),
        "stopped_value": value["REJECTED"] + value["RETURNED"] + sum(r["total"] or 0 for r in pending),
        "median_compute_ms": round(statistics.median(compute)) if compute else None,
        "in_progress": db.query_one("SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued', 'running')")["n"],
    }


@app.get("/api/pos")
def purchase_orders():
    vendors = {v["id"]: v for v in db.get_vendors()}
    out = []
    for po in db.list_pos():
        item = _po_summary(po)
        item["vendor_name"] = vendors[po["vendor_id"]]["name"]
        out.append(item)
    return {"purchase_orders": out}


@app.get("/api/vendors")
def vendors():
    out = []
    for v in db.get_vendors():
        v["bank_account"] = "••••" + (v["bank_account"] or "")[-4:]
        out.append(v)
    return {"vendors": out}


@app.get("/api/policy")
def get_policy():
    values = db.get_policy()
    return {"policy": [{**p, "value": values.get(p["key"], p["default"])} for p in seed.POLICY_DEFS]}


@app.put("/api/policy")
async def put_policy(request: Request):
    body = await request.json()
    defs = {p["key"]: p for p in seed.POLICY_DEFS}
    clean = {}
    for key, value in (body.get("values") or {}).items():
        if key not in defs:
            raise HTTPException(400, f"Unknown policy setting: {key}")
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{defs[key]['label']} must be a number.")
        if not (defs[key]["min"] <= v <= defs[key]["max"]):
            raise HTTPException(400, f"{defs[key]['label']} must be between {defs[key]['min']} and {defs[key]['max']}.")
        clean[key] = v
    db.set_policy(clean)
    return get_policy()


@app.post("/api/policy/reset")
def reset_policy():
    seed.reset_policy()
    return get_policy()


@app.post("/api/reset")
def reset_demo():
    """Clear all runs and restore vendors, POs, payment history and policy to their starting state."""
    busy = db.query_one("SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued', 'running')")["n"]
    if busy:
        raise HTTPException(409, "A run is still in progress. Wait for it to finish, then reset.")
    seed.reset_demo()
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"{exc.__class__.__name__}: {exc}"})
