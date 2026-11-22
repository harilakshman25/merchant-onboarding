#!/usr/bin/env python3
"""End-to-end demo against a RUNNING server (uvicorn app.main:app). Uses only the public HTTP API.

  python scripts/demo.py [--base http://127.0.0.1:8000]

Shows: (A) a clean application with async callbacks, duplicate/replayed callbacks, a callback
that arrives after the timeout; (B) an exception path: PAN ok / GST fails -> merchant fixes it;
(C) escalation to a human reviewer. The server's simulated clock is advanced so nothing waits.
"""
import argparse
import json
import sys
import time

import httpx

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])
from app import samples, security  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
args = ap.parse_args()
c = httpx.Client(base_url=args.base, headers={"X-Operator-Key": "dev-operator-key"}, timeout=30)


def show(title):
    print(f"\n=== {title} " + "=" * max(3, 70 - len(title)))


def view(app_id):
    return c.get(f"/applications/{app_id}").json()


def step(app_id, seconds=2, until=None, limit=60):
    for _ in range(limit):
        c.post(f"/admin/clock/advance?seconds={seconds}")
        v = view(app_id)
        if v["state"] in (until or ("ACTIVE", "REJECTED", "NEEDS_INFO", "MANUAL_REVIEW")):
            return v
        time.sleep(0.05)
    return v


def trail(v, only=None):
    for e in v["audit"]:
        if only and e["event"] not in only:
            continue
        arrow = f"{e['from_state']} -> {e['to_state']}" if e["to_state"] else ""
        d = e["detail"]
        extra = d.get("provider") or d.get("reason") or d.get("note") or ""
        print(f"  {e['time'][11:23]}  {e['event']:<26} {arrow:<34} {extra}")


# ---------------------------------------------------------------- A
show("A. Late bank callback + duplicate callbacks (the async path)")
p = samples.build("bank_timeout_late_callback")
r = c.post("/applications", json=p).json()
print("submitted:", r["id"], "->", r["state"])
v = step(r["id"], seconds=16, limit=5)          # advance past the 30s timeout and the 45s late callback
trail(v, {"CHECK_REQUESTED", "CALLBACK_RECEIVED", "DUPLICATE_CALLBACK_IGNORED", "CHECK_TIMED_OUT",
          "RETRY_SCHEDULED", "LATE_CALLBACK_ACCEPTED", "RETRY_SKIPPED", "STATE_CHANGED"})
print("final:", v["state"], v["merchant"] and v["merchant"]["merchant_id"])

# replaying a callback by hand over real HTTP: signature required, duplicates are no-ops
req = v["checks"]["pan"]["attempts"][0]["id"]
body = json.dumps({"event_id": f"evt_{req}", "request_id": req, "provider": "pan", "status": "SUCCEEDED", "result": {}}, sort_keys=True)
print("replay w/ bad signature ->", c.post("/callbacks/pan", content=body, headers={"X-Signature": "nope"}).status_code)
print("replay w/ good signature ->", c.post("/callbacks/pan", content=body, headers={"X-Signature": security.sign(body.encode())}).json())

# ---------------------------------------------------------------- B
show("B. PAN verified, GST fails -> request info -> merchant corrects")
p = samples.build("gst_not_found")
r = c.post("/applications", json=p).json()
v = step(r["id"], until=("NEEDS_INFO",))
print("state:", v["state"], "| asked for:", v["issues"]["message"])
print("checks:", {k: x["status"] for k, x in v["checks"].items()})
fixed = samples.gstin_for(p["pan"], state="07")
c.patch(f"/applications/{r['id']}", json={"gstin": fixed, "documents": [
    {"doc_type": "GST_CERT", "fields": {"gstin": fixed, "legal_name": p["legal_name"]}}]})
v = step(r["id"], until=("ACTIVE", "MANUAL_REVIEW", "REJECTED"))
trail(v, {"STATE_CHANGED", "APPLICATION_UPDATED"})
print("final:", v["state"], v["merchant"] and v["merchant"]["merchant_id"], "| PAN requests:", len(v["checks"]["pan"]["attempts"]), "(reused, not re-run)")

# ---------------------------------------------------------------- C
show("C. High risk -> human review queue -> reviewer approves -> merchant ID")
r = c.post("/applications", json=samples.build("high_risk")).json()
v = step(r["id"], until=("MANUAL_REVIEW",))
task = v["review_tasks"][-1]
print("escalated:", [x["code"] for x in task["reasons"]])
print("approve:", c.post(f"/review-queue/{task['id']}/resolve", json={"decision": "approve", "reviewer": "rita", "notes": "long-standing client"}).json())
v = view(r["id"])
print("final:", v["state"], v["merchant"]["merchant_id"])

show("Metrics")
print(json.dumps(c.get("/metrics").json(), indent=2))
