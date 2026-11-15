"""Run every sample invoice through the pipeline and print what was decided.

    python scripts/run_samples.py            # summary table
    python scripts/run_samples.py -v         # every check of every stage
    python scripts/run_samples.py --check    # exit non-zero unless every decision is the expected one

Uses a throwaway database, so it never touches the demo data. Useful before a demo,
and essential after switching on AI extraction, to confirm the outcomes are unchanged.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXPECTED = {
    "happy": "APPROVED",
    "masked_variance": "NEEDS_REVIEW",
    "split_partial": "APPROVED",
    "split_overbill": "NEEDS_REVIEW",
    "scanned_no_po": "APPROVED",
    "disguised_duplicate": "REJECTED",
    "missing_fields": "RETURNED",
}


def main() -> int:
    verbose = "-v" in sys.argv
    check = "--check" in sys.argv
    tmp = tempfile.mkdtemp(prefix="invoice-desk-")
    os.environ["INVOICE_DESK_DATA"] = tmp
    os.environ.setdefault("INVOICE_DESK_TODAY", "2026-11-16")

    from app import config, db, seed
    from app.pipeline.runner import run_pipeline

    seed.reset_demo()
    failures = 0
    print(f"\nExtractor: {'Claude (' + config.AI_MODEL + ')' if config.ai_enabled() else 'built-in parser'}\n")
    for s in seed.SAMPLES:
        run = db.create_run(s["file"], str(config.SAMPLES_DIR / s["file"]), "sample", s["key"], 0)
        run_pipeline(run["id"])
        r = db.get_run(run["id"])
        want = EXPECTED[s["key"]]
        ok = r["decision"] == want
        failures += 0 if ok else 1
        mark = "ok " if ok else "FAIL"
        print(f"[{mark}] {s['tag']:<13} {r['decision'] or r['status']:<13} {r['compute_ms']:>5} ms  {s['title']}")
        print(f"       {r['headline']}")
        if not ok:
            print(f"       expected {want}; error: {r.get('error')}")
        if verbose:
            for e in db.get_events(run["id"]):
                if e["type"] == "stage_completed":
                    p = e["payload"]
                    print(f"         [{p['status']:<7}] {e['stage']:<10} {p['summary']}")
                    for c in p["checks"]:
                        act = f" -> {c['action']}:{c['route'] or ''}" if c["action"] else ""
                        print(f"             {c['status']:<5} {c['title']}{act}")
                        if c["detail"]:
                            print(f"                   {c['detail']}")
            msg = r["result"]["decision"].get("message")
            if msg:
                print("       ---- drafted message ----")
                print(f"       To: {msg['to']}\n       Subject: {msg['subject']}")
                for line in msg["body"].splitlines():
                    print("       | " + line)
            print()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(seed.SAMPLES) - failures}/{len(seed.SAMPLES)} decisions as expected.")
    return 1 if (check and failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
