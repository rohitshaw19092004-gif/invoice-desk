"""The web layer: uploads, the live event stream, reviewer actions, policy, and the AI reader."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from app.pipeline import extract_llm
from tests.conftest import PO_0412_LINES, SHREEJI, make_invoice, process_sample


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def wait_done(client, run_id, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["run"]["status"] in ("done", "error"):
            return run
        time.sleep(0.05)
    raise AssertionError(f"{run_id} did not finish")


def test_meta_lists_stages_and_samples(client):
    meta = client.get("/api/meta").json()
    assert [s["id"] for s in meta["stages"]] == ["intake", "read", "extract", "validate", "vendor", "po_match",
                                                 "duplicate", "reconcile", "decide", "act"]
    assert len(meta["samples"]) == 7 and all(s["available"] for s in meta["samples"])
    assert meta["ai"]["enabled"] is False


def test_upload_runs_end_to_end_and_serves_the_document(client, tmp_path):
    path = tmp_path / "upload.pdf"
    make_invoice(path, number="SPI/2026-27/3001", po="PO-2026-0412", lines=PO_0412_LINES, **SHREEJI)
    res = client.post("/api/runs", files={"file": ("my invoice (final).pdf", path.read_bytes(), "application/pdf")},
                      data={"pace_ms": "0"})
    assert res.status_code == 200
    run = wait_done(client, res.json()["run_id"])
    assert run["run"]["decision"] == "APPROVED" and run["run"]["source"] == "upload"
    assert "file_path" not in run["run"]  # server paths are not exposed
    rid = run["run"]["id"]
    page = client.get(f"/api/runs/{rid}/pages/1")
    assert page.status_code == 200 and page.headers["content-type"] == "image/png"
    assert client.get(f"/api/runs/{rid}/file").headers["content-type"] == "application/pdf"
    assert client.get(f"/api/runs/{rid}/pages/9").status_code == 404


def test_upload_validation(client):
    assert client.post("/api/runs").status_code == 400
    assert client.post("/api/runs", files={"file": ("empty.pdf", b"", "application/pdf")}).status_code == 400
    assert client.post("/api/runs", data={"sample_key": "nope"}).status_code == 404
    assert client.get("/api/runs/RUN-9999").status_code == 404


def test_event_stream_replays_every_stage_in_order(client):
    rid = client.post("/api/runs", data={"sample_key": "happy", "pace_ms": "0"}).json()["run_id"]
    wait_done(client, rid)
    with client.stream("GET", f"/api/runs/{rid}/events") as s:
        body = "".join(s.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {\"seq\"")]
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_completed"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    done = [e["stage"] for e in events if e["type"] == "stage_completed"]
    assert done == ["intake", "read", "extract", "validate", "vendor", "po_match", "duplicate", "reconcile",
                    "decide", "act"]
    assert "event: end" in body
    # resuming from the middle only sends what is new
    with client.stream("GET", f"/api/runs/{rid}/events?after={events[-3]['seq']}") as s:
        tail = "".join(s.iter_text())
    assert tail.count("data: {\"seq\"") == 2


def test_reviewer_approval_posts_once_and_only_once(client):
    rid = client.post("/api/runs", data={"sample_key": "masked_variance", "pace_ms": "0"}).json()["run_id"]
    run = wait_done(client, rid)
    assert run["run"]["decision"] == "NEEDS_REVIEW" and run["run"]["review_status"] == "pending"
    assert client.get("/api/stats").json()["pending_review"] == 1

    res = client.post(f"/api/runs/{rid}/review", json={"action": "approve", "note": "Revised quote accepted"})
    assert res.status_code == 200
    review = res.json()["review"]
    assert review["posted"] and review["by"] == "Arjun Mehta" and review["amount"] == 1041232.00
    po = db.get_po("PO-2026-0398")
    assert po["billed_value"] == 882400.00 and po["remaining_value"] == 62500.00
    assert client.post(f"/api/runs/{rid}/review", json={"action": "approve"}).status_code == 409

    stats = client.get("/api/stats").json()
    assert stats["pending_review"] == 0 and stats["reviewer_approved"] == 1
    events = client.get(f"/api/runs/{rid}").json()["events"]
    assert events[-1]["type"] == "review" and events[-1]["payload"]["note"] == "Revised quote accepted"

    # the same invoice submitted again is now a duplicate of the approved one
    again = wait_done(client, client.post("/api/runs", data={"sample_key": "masked_variance", "pace_ms": "0"}).json()["run_id"])
    assert again["run"]["decision"] == "REJECTED"


def test_two_held_copies_cannot_both_be_approved(client):
    a = wait_done(client, client.post("/api/runs", data={"sample_key": "masked_variance", "pace_ms": "0"}).json()["run_id"])
    b = wait_done(client, client.post("/api/runs", data={"sample_key": "masked_variance", "pace_ms": "0"}).json()["run_id"])
    assert a["run"]["decision"] == b["run"]["decision"] == "NEEDS_REVIEW"
    assert client.post(f"/api/runs/{a['run']['id']}/review", json={"action": "approve"}).status_code == 200
    second = client.post(f"/api/runs/{b['run']['id']}/review", json={"action": "approve"})
    assert second.status_code == 409 and "already in the ledger" in second.json()["detail"]
    assert len(db.get_po("PO-2026-0398")["invoices"]) == 1


def test_reviewer_rejection_posts_nothing(client):
    rid = client.post("/api/runs", data={"sample_key": "split_overbill", "pace_ms": "0"}).json()["run_id"]
    wait_done(client, rid)
    before = db.get_po("PO-2026-0405")["billed_value"]
    assert client.post(f"/api/runs/{rid}/review", json={"action": "reject", "note": "overrun not ordered"}).status_code == 200
    assert db.get_po("PO-2026-0405")["billed_value"] == before
    assert client.get(f"/api/runs/{rid}").json()["run"]["review_status"] == "rejected"
    assert client.post(f"/api/runs/{rid}/review", json={"action": "bogus"}).status_code == 400
    assert client.post(f"/api/runs/{rid}/review", json={"action": "approve"}).status_code == 409  # already decided


def test_review_is_refused_for_runs_that_are_not_held(client):
    rid = client.post("/api/runs", data={"sample_key": "happy", "pace_ms": "0"}).json()["run_id"]
    wait_done(client, rid)
    assert client.post(f"/api/runs/{rid}/review", json={"action": "approve"}).status_code == 409


def test_batch_processes_the_whole_inbox(client):
    ids = client.post("/api/runs/batch", data={"pace_ms": "0"}).json()["run_ids"]
    assert len(ids) == 7
    decisions = [wait_done(client, rid, timeout=60)["run"]["decision"] for rid in ids]
    assert decisions == ["APPROVED", "NEEDS_REVIEW", "APPROVED", "NEEDS_REVIEW", "APPROVED", "REJECTED", "RETURNED"]
    stats = client.get("/api/stats").json()
    assert stats["runs"] == 7 and stats["by_decision"]["APPROVED"] == 3
    assert stats["straight_through_rate"] == round(3 / 7, 4)
    samples = client.get("/api/meta").json()["samples"]
    assert all(s["last_run"]["status"] == "done" for s in samples)


def test_policy_can_be_changed_validated_and_restored(client):
    res = client.put("/api/policy", json={"values": {"price_tolerance_pct": 10}})
    assert res.status_code == 200
    assert next(p for p in res.json()["policy"] if p["key"] == "price_tolerance_pct")["value"] == 10
    assert client.put("/api/policy", json={"values": {"price_tolerance_pct": 999}}).status_code == 400
    assert client.put("/api/policy", json={"values": {"price_tolerance_pct": "abc"}}).status_code == 400
    assert client.put("/api/policy", json={"values": {"made_up": 1}}).status_code == 400
    restored = client.post("/api/policy/reset").json()["policy"]
    assert all(p["value"] == p["default"] for p in restored)


def test_reset_restores_the_starting_state(client):
    wait_done(client, client.post("/api/runs", data={"sample_key": "happy", "pace_ms": "0"}).json()["run_id"])
    assert db.get_po("PO-2026-0412")["status"] == "fully_billed"
    assert client.post("/api/reset").json() == {"ok": True}
    assert client.get("/api/runs").json()["runs"] == []
    assert db.get_po("PO-2026-0412")["status"] == "open"
    assert len(db.query("SELECT id FROM ledger_invoices")) == 4  # the seeded payment history


def test_purchase_orders_and_vendors_endpoints(client):
    pos = client.get("/api/pos").json()["purchase_orders"]
    assert len(pos) == 8 and {p["status"] for p in pos} == {"open", "partially_billed", "closed"}
    vendors = client.get("/api/vendors").json()["vendors"]
    assert all(v["bank_account"].startswith("••••") and len(v["bank_account"]) == 8 for v in vendors)


# ------------------------------------------------------------- the AI reader ---

def _happy_as_model_output() -> extract_llm.InvoiceOut:
    return extract_llm.InvoiceOut(
        vendor_name="Shreeji Packaging Industries", vendor_tax_id="29AAFCS8842K1Z6",
        invoice_number="SPI/2026-27/1047", invoice_date="2026-11-04", due_date="2026-12-04",
        po_number="PO-2026-0412", currency="INR",
        line_items=[extract_llm.LineItemOut(description=d, quantity=q, unit=u, unit_price=p, amount=q * p)
                    for d, q, u, p in PO_0412_LINES],
        subtotal=142400.00,
        tax_lines=[extract_llm.TaxLineOut(label="CGST", rate_percent=9, amount=12816.00),
                   extract_llm.TaxLineOut(label="SGST", rate_percent=9, amount=12816.00)],
        total=168032.00, amount_in_words="Rupees One Lakh Sixty Eight Thousand Thirty Two Only",
        bank_account_number="50200048213377", bank_ifsc="HDFC0001234", confidence=0.97,
    )


def test_ai_reader_is_used_when_configured_and_rules_still_decide(monkeypatch):
    monkeypatch.setenv("INVOICE_DESK_AI", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    seen = {}

    def fake_call(pdf_bytes, text_layer):
        seen["bytes"] = len(pdf_bytes)
        seen["text"] = text_layer
        return _happy_as_model_output()

    monkeypatch.setattr(extract_llm, "_call_model", fake_call)
    assert config.ai_enabled()
    r = process_sample("happy")
    assert seen["bytes"] > 1000 and "SPI/2026-27/1047" in seen["text"]
    assert r.run["extractor"] == "ai" and r.stages["extract"]["data"]["method"] == "ai"
    assert r.decision == "APPROVED" and r.run["total"] == 168032.00
    meta = r.stages["extract"]["data"]["meta"]
    assert meta["invoice_number"]["bbox"] and meta["total"]["bbox"]  # values were located on the page


def test_ai_reader_output_goes_through_the_same_controls(monkeypatch):
    """If the model misreads a price, the deterministic checks catch the inconsistency."""
    monkeypatch.setenv("INVOICE_DESK_AI", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    out = _happy_as_model_output()
    out.line_items[0].unit_price = 45.20  # hallucinated rate; amount left as printed
    monkeypatch.setattr(extract_llm, "_call_model", lambda *_: out)
    r = process_sample("happy")
    assert r.decision == "NEEDS_REVIEW" and "LINE_ARITHMETIC" in r.codes


def test_ai_failure_falls_back_to_the_built_in_parser(monkeypatch):
    monkeypatch.setenv("INVOICE_DESK_AI", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    def boom(*_):
        raise extract_llm.AIUnavailable("the API key was rejected")

    monkeypatch.setattr(extract_llm, "_call_model", boom)
    r = process_sample("happy")
    assert r.run["extractor"] == "rules" and r.decision == "APPROVED"
    assert any("Fell back" in c["title"] for c in r.checks)


def test_unexpected_ai_error_is_contained(monkeypatch):
    monkeypatch.setenv("INVOICE_DESK_AI", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    def boom(*_):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(extract_llm, "_call_model", boom)
    r = process_sample("happy")
    assert r.run["status"] == "done" and r.run["extractor"] == "rules" and r.decision == "APPROVED"
