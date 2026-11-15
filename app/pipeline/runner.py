"""Runs an invoice through the stages, emitting an event for everything that happens.

The event log is the single source of truth for the live view: the browser replays
the same events whether it is watching a run as it executes or opening one from last
week. Runs execute one at a time on a worker thread, so two copies of the same
invoice submitted together cannot both be approved.
"""
from __future__ import annotations

import logging
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Optional

from .. import config, db
from ..money import set_currency
from .stages import (DECISION_LABELS, STAGES, Check, Ctx, StageResult, _route_for, posting_payload,
                     why_not_postable)

log = logging.getLogger("invoice_desk")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")


def today() -> date:
    override = os.environ.get("INVOICE_DESK_TODAY")
    return date.fromisoformat(override) if override else date.today()


def submit(run_id: str) -> None:
    _executor.submit(_safe_run, run_id)


def _safe_run(run_id: str) -> None:
    try:
        run_pipeline(run_id)
    except Exception as exc:  # last line of defence: a run must never stay "running" forever
        log.error("run %s crashed: %s\n%s", run_id, exc, traceback.format_exc())
        message = f"{exc.__class__.__name__}: {exc}"
        db.finish_run(run_id, {"error": message}, event_type="run_failed", status="error",
                      finished_at=db.now_iso(), error=message)


def _skip_reason(ctx: Ctx, stage_id: str) -> Optional[str]:
    if stage_id in ("intake", "decide", "act"):
        return None
    if ctx.halted == "not_pdf":
        return "Skipped: the file is not a PDF."
    if stage_id == "read":
        return None
    if ctx.halted == "unreadable":
        return "Skipped: the document could not be read."
    if stage_id == "extract":
        return None
    if ctx.ex is None:
        return "Skipped: the fields could not be extracted."
    if stage_id == "reconcile":
        if ctx.duplicate and ctx.duplicate["kind"] in ("file", "number", "reissue"):
            return "Skipped: the invoice is a duplicate, so there is nothing to reconcile."
        if ctx.po is None:
            return "Skipped: there is no purchase order to reconcile against."
        if any(c.code == "PO_VENDOR_MISMATCH" for c in ctx.checks):
            return "Skipped: the quoted PO belongs to a different vendor."
        if ctx.vendor is None:
            return "Skipped: the vendor is not verified, so the invoice is not matched against a PO."
        if ctx.amounts.get("total") is None:
            return "Skipped: the invoice has no readable amounts."
        if not ctx.lines:
            return "Skipped: no line items were read, so there is nothing to compare with the PO's lines."
        if ctx.ex.currency != ctx.po["currency"]:
            return (f"Skipped: the invoice is in {ctx.ex.currency} and the PO is in {ctx.po['currency']}. "
                    "The amounts cannot be compared without a conversion.")
    return None


def _fallback_decision(ctx: Ctx, error: str) -> dict:
    """If the decision step itself fails, hold the invoice for a person. Never approve by accident."""
    route = _route_for(ctx, "ap")
    return {
        "decision": "NEEDS_REVIEW", "label": DECISION_LABELS["NEEDS_REVIEW"],
        "headline": f"Held for {route['name']} ({route['role']}): the process hit an unexpected error.",
        "reasons": [Check("fail", "The process hit an unexpected error", error, "SYSTEM_ERROR", action="REVIEW",
                          route="ap", stage="decide").to_dict()],
        "route": route, "options": ["Review the invoice manually.", "Re-run it once the error is fixed."],
        "message": None, "payment": None,
        "next_steps": ["Nothing has been posted or scheduled.", f"Routed to {route['name']} for manual handling."],
        "passed_count": sum(1 for c in ctx.checks if c.status == "pass"),
        "passed": [c.title for c in ctx.checks if c.status == "pass"],
        "confidence": {"extraction": None, "po_match": ctx.po_confidence, "po_method": ctx.po_method},
    }


def run_pipeline(run_id: str) -> None:
    run = db.get_run(run_id)
    if not run:
        return
    wall_start = time.perf_counter()
    set_currency(config.BUYER["currency"])  # until the invoice says otherwise
    pace_s = max(0, int(run.get("pace_ms") or 0)) / 1000.0
    db.update_run(run_id, status="running", started_at=db.now_iso())
    db.add_event(run_id, "run_started", None, {"filename": run["filename"], "source": run["source"],
                                               "ai_enabled": config.ai_enabled(), "pace_ms": run.get("pace_ms") or 0})

    current_stage = {"id": None}

    def emit_log(text: str) -> None:
        db.add_event(run_id, "stage_log", current_stage["id"], {"text": text})

    ctx = Ctx(run_id=run_id, path=Path(run["file_path"]), filename=run["filename"], policy=db.get_policy(),
              today=today(), log=emit_log)
    compute_total = 0.0

    for stage_id, title, _blurb, fn in STAGES:
        current_stage["id"] = stage_id
        db.add_event(run_id, "stage_started", stage_id, {"title": title})
        t0 = time.perf_counter()
        skip = _skip_reason(ctx, stage_id)
        if skip:
            result = StageResult("skipped", skip)
        else:
            try:
                result = fn(ctx)
            except Exception as exc:
                log.error("stage %s failed in %s: %s\n%s", stage_id, run_id, exc, traceback.format_exc())
                detail = (f"{exc.__class__.__name__}: {exc}. The invoice is held for a person rather than decided on "
                          "incomplete information.")
                if stage_id == "decide":
                    ctx.decision = _fallback_decision(ctx, detail)
                    result = StageResult("fail", "Held for review after an unexpected error",
                                         [Check("fail", "The decision step failed", detail, "SYSTEM_ERROR")])
                elif stage_id == "act":
                    ctx.decision = _fallback_decision(ctx, detail)
                    result = StageResult("fail", "Nothing posted: the action step failed",
                                         [Check("fail", "The action step failed", detail, "SYSTEM_ERROR")])
                else:
                    result = StageResult("fail", "This step failed unexpectedly",
                                         [Check("fail", "Unexpected error in this step", detail, "SYSTEM_ERROR",
                                                action="REVIEW", route="ap")])
        compute = time.perf_counter() - t0
        compute_total += compute
        for c in result.checks:
            c.stage = stage_id
            ctx.checks.append(c)
        if pace_s and compute < pace_s:
            time.sleep(pace_s - compute)  # readability only: lets a viewer follow each stage as it lands
        db.add_event(run_id, "stage_completed", stage_id, {
            "status": result.status, "summary": result.summary, "checks": [c.to_dict() for c in result.checks],
            "data": result.data, "ms": round(compute * 1000, 1),
        })

    d = ctx.decision
    ex = ctx.ex
    duration_ms = round((time.perf_counter() - wall_start) * 1000)
    compute_ms = round(compute_total * 1000)
    reasons = d.get("reasons") or []
    blocking = [r for r in reasons if r.get("action")]
    if d["decision"] == "APPROVED":
        # what made this approval notable, in a few words
        tags = {"OCR_USED": "scanned, read by OCR", "TAX_NORMALISED": "tax split out of the total",
                "PO_INFERRED": "PO inferred"}
        notable = [tags[r["code"]] for r in reasons if r.get("code") in tags]
        if ctx.recon and any(row.get("qty_status") in ("partial", "not_billed") for row in ctx.recon["rows"]):
            notable.append("part-billing, PO stays open")
        top_reason = f"All {d['passed_count']} checks passed" + (" · " + ", ".join(notable) if notable else "")
    else:
        top_reason = (blocking or reasons or [{"title": d["headline"]}])[0]["title"]
    result = {"decision": d, "posting": posting_payload(ctx), "posting_blocked": why_not_postable(ctx)}
    # The final status and the closing event are committed together, so nothing reading the run
    # can ever see it as finished without its decision.
    db.finish_run(
        run_id, {"decision": d, "duration_ms": duration_ms, "compute_ms": compute_ms},
        status="done", finished_at=db.now_iso(), decision=d["decision"], headline=d["headline"],
        top_reason=top_reason, route_to=(d["route"] or {}).get("name") if d.get("route") else None,
        vendor_name=ctx.vendor["name"] if ctx.vendor else (ex.vendor_name if ex else None),
        invoice_number=ex.invoice_number if ex else None, po_number=ctx.po["po_number"] if ctx.po else None,
        total=float(ctx.amounts["total"]) if ctx.amounts.get("total") is not None else None,
        currency=ex.currency if ex else None, extractor=ex.method if ex else None,
        extraction_confidence=ex.confidence() if ex else None, duration_ms=duration_ms, compute_ms=compute_ms,
        result=result, review_status="pending" if d["decision"] == "NEEDS_REVIEW" else None,
    )
