import json, pytest
from conftest import run, state, events
from app import clock, db, samples, security, states, workflow
from app.states import S


def submit(name, rng, **kw):
    p = samples.build(name, rng, **kw)
    return workflow.submit_application(p), p


def test_happy_path_straight_through(rng):
    aid, _ = submit("happy_path", rng)
    assert state(aid) == "VERIFYING"            # async: nothing is approved before callbacks arrive
    run(10)
    v = workflow.application_view(aid)
    assert v["state"] == "ACTIVE" and v["merchant"]["merchant_id"].startswith("MID")
    assert {p: c["status"] for p, c in v["checks"].items()} == {p: "SUCCEEDED" for p in ("pan", "gst", "bank", "risk")}
    assert events(aid, "CALLBACK_RECEIVED") == ["CALLBACK_RECEIVED"] * 4


def test_dependency_order_pan_before_gst_before_risk(rng):
    aid, _ = submit("happy_path", rng)
    reqs = [e for e in workflow.application_view(aid)["audit"] if e["event"] == "CHECK_REQUESTED"]
    assert {r["detail"]["provider"] for r in reqs} == {"pan", "bank"}   # gst/risk wait for their dependencies
    run(10)
    order = [e["detail"]["provider"] for e in workflow.application_view(aid)["audit"] if e["event"] == "CHECK_REQUESTED"]
    assert order.index("pan") < order.index("gst") < order.index("risk")


def test_pan_ok_gst_fails_then_merchant_fixes(rng):
    aid, p = submit("gst_not_found", rng)
    run(10)
    v = workflow.application_view(aid)
    assert v["state"] == "NEEDS_INFO"
    assert v["checks"]["pan"]["status"] == "SUCCEEDED" and v["checks"]["gst"]["status"] == "FAILED"
    assert "GSTIN" in v["issues"]["message"]
    pan_requests = len(v["checks"]["pan"]["attempts"])
    new_gstin = samples.gstin_for(p["pan"], rng, state="07")
    workflow.resubmit(aid, {"gstin": new_gstin}, [{"doc_type": "GST_CERT", "fields": {"gstin": new_gstin, "legal_name": p["legal_name"]}}])
    run(10)
    v = workflow.application_view(aid)
    assert v["state"] == "ACTIVE"
    assert len(v["checks"]["pan"]["attempts"]) == pan_requests      # unchanged inputs are NOT re-verified


def test_duplicate_callbacks_are_idempotent(rng):
    aid, _ = submit("duplicate_callbacks", rng)
    run(10)
    assert state(aid) == "ACTIVE"
    assert len(events(aid, "DUPLICATE_CALLBACK_IGNORED")) == 8           # 4 callbacks x 2 extra copies
    assert db.one("SELECT COUNT(*) n FROM merchants")["n"] == 1


def test_late_callback_after_timeout_is_accepted(rng):
    aid, _ = submit("bank_timeout_late_callback", rng)
    run(3)
    assert workflow.application_view(aid)["checks"]["bank"]["status"] == "PENDING"
    clock.advance(30); workflow.tick()                                    # past the 30s deadline
    v = workflow.application_view(aid)
    assert "CHECK_TIMED_OUT" in events(aid) and "RETRY_SCHEDULED" in events(aid)
    assert v["state"] == "VERIFYING"
    clock.advance(15); workflow.tick()                                    # late callback lands before the retry runs
    assert "LATE_CALLBACK_ACCEPTED" in events(aid) and "RETRY_SKIPPED" in events(aid)
    run(10)
    v = workflow.application_view(aid)
    assert v["state"] == "ACTIVE"
    assert any(a["late"] for a in v["checks"]["bank"]["attempts"])
    assert db.one("SELECT COUNT(*) n FROM merchants")["n"] == 1


def test_timeout_retry_then_success_when_late_callback_never_comes(rng):
    p = samples.build("happy_path", rng); p["simulate"] = {"bank": "timeout_late", "late_delay_s": 9999}
    aid = workflow.submit_application(p)
    run(60)                                                               # timeout -> backoff -> retry (normal) -> done
    v = workflow.application_view(aid)
    assert v["state"] == "ACTIVE" and len(v["checks"]["bank"]["attempts"]) == 2


def test_provider_outage_retries_with_backoff(rng):
    aid, _ = submit("provider_outage", rng)
    assert "CHECK_SUBMIT_FAILED" in events(aid) and "RETRY_SCHEDULED" in events(aid)
    run(20)
    assert state(aid) == "ACTIVE"


def test_provider_down_exhausts_retries_into_dlq_and_review(rng):
    aid, _ = submit("provider_down", rng)
    run(400)
    v = workflow.application_view(aid)
    assert v["state"] == "MANUAL_REVIEW"
    assert v["review_tasks"][0]["reasons"][0]["code"] == "PROVIDER_UNAVAILABLE"
    dl = workflow.dead_letters()
    assert dl and dl[0]["kind"] == "PROVIDER_EXHAUSTED"
    # reviewer can retry once the provider recovers
    db.x("UPDATE applications SET simulate=? WHERE id=?", (json.dumps({}), aid))   # provider recovers
    workflow.resolve_review(v["review_tasks"][0]["id"], "retry", "rita")
    run(10)
    assert state(aid) == "ACTIVE"


def test_blurred_document_requests_reupload(rng):
    aid, p = submit("blurred_document", rng)
    v = workflow.application_view(aid)
    assert v["state"] == "NEEDS_INFO" and "blurred" in v["issues"]["message"]
    assert not any(e == "CHECK_REQUESTED" for e in events(aid))           # no provider calls on a bad doc
    workflow.resubmit(aid, None, [samples.blurred_replacement(p)])
    run(10)
    assert state(aid) == "ACTIVE"


def test_missing_document_requests_info(rng):
    aid, _ = submit("missing_document", rng)
    v = workflow.application_view(aid)
    assert v["state"] == "NEEDS_INFO" and "GST_CERT" in v["issues"]["message"]


def test_document_conflict_goes_to_review_and_reviewer_can_proceed(rng):
    aid, _ = submit("document_conflict", rng)
    v = workflow.application_view(aid)
    assert v["state"] == "MANUAL_REVIEW"
    assert any(r["code"] == "DOC_FORM_CONFLICT" for r in v["review_tasks"][0]["reasons"])
    workflow.resolve_review(v["review_tasks"][0]["id"], "proceed", "rita", "holder is the proprietor")
    run(10)
    assert state(aid) == "ACTIVE"


def test_low_confidence_scan_goes_to_review(rng):
    aid, _ = submit("low_confidence_scan", rng)
    v = workflow.application_view(aid)
    assert v["state"] == "MANUAL_REVIEW" and v["review_tasks"][0]["reasons"][0]["code"] == "DOC_LOW_CONFIDENCE"
    assert v["documents"][0]["extraction"]["status"] == "LOW_CONFIDENCE"


def test_duplicate_application_detected(rng):
    a1, p1 = submit("happy_path", rng)
    a2, _ = submit("duplicate_application", rng, base_form=p1)
    v = workflow.application_view(a2)
    assert v["state"] == "MANUAL_REVIEW"
    codes = [r["code"] for r in v["review_tasks"][0]["reasons"]]
    assert "DUPLICATE_SUSPECTED" in codes
    workflow.resolve_review(v["review_tasks"][0]["id"], "reject", "rita", "same entity applying twice")
    assert state(a2) == "REJECTED"


def test_high_risk_needs_human_and_reviewer_approval_creates_merchant(rng):
    aid, _ = submit("high_risk", rng)
    run(10)
    v = workflow.application_view(aid)
    assert v["state"] == "MANUAL_REVIEW" and v["review_tasks"][0]["reasons"][0]["code"] == "RISK_REVIEW"
    workflow.resolve_review(v["review_tasks"][0]["id"], "approve", "rita", "known client")
    v = workflow.application_view(aid)
    assert v["state"] == "ACTIVE" and v["merchant"]
    assert "REVIEWER_OVERRIDE" in events(aid)


def test_reviewer_cannot_approve_over_failed_required_check(rng):
    p = samples.build("high_risk", rng); p["simulate"] = {"gst": "name_mismatch"}
    aid = workflow.submit_application(p)
    run(10)
    v = workflow.application_view(aid)
    assert v["state"] == "MANUAL_REVIEW" and v["checks"]["gst"]["status"] == "FAILED"
    with pytest.raises(workflow.Conflict):
        workflow.resolve_review(v["review_tasks"][0]["id"], "approve", "rita")
    with pytest.raises(workflow.Conflict):
        workflow.resolve_review(v["review_tasks"][0]["id"], "proceed", "rita")
    assert state(aid) == "MANUAL_REVIEW"
    assert not db.q("SELECT * FROM merchants")


def test_risk_decline_rejects(rng):
    aid, _ = submit("risk_decline", rng)
    run(10)
    assert state(aid) == "REJECTED" and not db.q("SELECT * FROM merchants")


def test_merchant_id_failure_is_retried_idempotently(rng):
    aid, _ = submit("merchant_id_fail_once", rng)
    run(5)
    assert state(aid) == "PROVISIONING_FAILED"                            # approved, but not active yet
    run(20)
    v = workflow.application_view(aid)
    assert v["state"] == "ACTIVE" and db.one("SELECT COUNT(*) n FROM merchants")["n"] == 1
    assert "RETRY_SCHEDULED" in events(aid)


def test_merchant_id_permanent_failure_dead_letters_then_replay(rng):
    aid, _ = submit("merchant_id_fail_always", rng)
    run(60)
    assert state(aid) == "PROVISIONING_FAILED"
    dl = [d for d in workflow.dead_letters() if d["kind"] == "PROVISION_EXHAUSTED"]
    assert dl and not db.q("SELECT * FROM merchants")
    db.x("UPDATE applications SET simulate='{}' WHERE id=?", (aid,))      # "service fixed"
    workflow.replay_dead_letter(dl[0]["id"])
    assert state(aid) == "ACTIVE"


def test_bank_change_after_verification_forces_reverification_and_review(rng):
    p = samples.build("happy_path", rng); p["simulate"] = {"callback_delay_s": 1.0}
    aid = workflow.submit_application(p)
    clock.advance(2); workflow.tick()                                     # pan + bank verified, gst now in flight
    v = workflow.application_view(aid)
    assert v["checks"]["bank"]["status"] == "SUCCEEDED" and v["state"] == "VERIFYING"
    old_bank_req = v["checks"]["bank"]["attempts"][0]["id"]
    workflow.resubmit(aid, {"bank": {"account_number": "998877665544"}}, None)
    v = workflow.application_view(aid)
    assert v["state"] == "NEEDS_INFO" and "BANK_PROOF" in v["issues"]["message"]   # old proof no longer valid
    assert v["flags"]["bank_changed"] is True
    new_proof = {"doc_type": "BANK_PROOF", "text": f"Account Holder: {p['legal_name']}\nAccount Number: 998877665544\nIFSC: {p['bank']['ifsc']}"}
    workflow.resubmit(aid, None, [new_proof])
    v = workflow.application_view(aid)
    assert v["state"] == "MANUAL_REVIEW" and v["review_tasks"][0]["reasons"][0]["code"] == "BANK_CHANGED_AFTER_VERIFICATION"
    assert v["checks"]["bank"]["status"] == "NOT_STARTED"                 # old verification no longer counts
    run(5)
    assert state(aid) == "MANUAL_REVIEW"                                  # nothing proceeds without the reviewer
    workflow.resolve_review(v["review_tasks"][0]["id"], "proceed", "rita")
    run(10)
    v = workflow.application_view(aid)
    assert v["state"] == "ACTIVE" and len(v["checks"]["bank"]["attempts"]) == 2
    assert len(v["checks"]["pan"]["attempts"]) == 1                       # PAN result reused
    assert v["merchant"]["payout_account_masked"].endswith("5544")


def test_stale_callback_for_old_bank_details_is_ignored(rng):
    aid, p = submit("happy_path", rng)                                    # bank request in flight, callback not yet due
    workflow.resubmit(aid, {"bank": {"account_number": "112233445566"}}, [
        {"doc_type": "BANK_PROOF", "text": f"Account Holder: {p['legal_name']}\nAccount Number: 112233445566\nIFSC: {p['bank']['ifsc']}"}])
    run(10)
    assert "CALLBACK_STALE_IGNORED" in events(aid)
    assert state(aid) == "ACTIVE"


def test_unknown_and_unsigned_callbacks(rng):
    with pytest.raises(workflow.BadSignature):
        workflow.handle_callback("pan", b"{}", "bad")
    raw = json.dumps({"event_id": "e1", "request_id": "nope", "provider": "pan", "status": "SUCCEEDED"}).encode()
    assert workflow.handle_callback("pan", raw, security.sign(raw))["outcome"] == "dead_lettered"
    assert workflow.dead_letters()[0]["kind"] == "UNKNOWN_CALLBACK"


def test_only_legal_transitions_ever_recorded(rng):
    for n in ("happy_path", "gst_not_found", "high_risk", "merchant_id_fail_once", "bank_timeout_late_callback"):
        submit(n, rng)
    run(100)
    for a in db.q("SELECT from_state,to_state FROM audit WHERE event='STATE_CHANGED'"):
        assert states.can(a["from_state"], a["to_state"]), a
    with pytest.raises(states.IllegalTransition):
        states.assert_can("ACTIVE", "VERIFYING")


def test_sensitive_values_never_in_audit_or_views(rng):
    aid, p = submit("happy_path", rng)
    run(10)
    blob = json.dumps(workflow.application_view(aid)) + json.dumps(workflow.list_applications())
    for raw in (p["pan"], p["gstin"], p["bank"]["account_number"]):
        assert raw not in blob
    assert not security.looks_sensitive(json.dumps([a["detail"] for a in workflow.application_view(aid)["audit"]]))
    # ...but are encrypted (not plaintext) at rest
    assert p["pan"] not in (db.one("SELECT form_enc FROM applications")["form_enc"])


def test_metrics(rng):
    submit("happy_path", rng); submit("high_risk", rng); submit("blurred_document", rng)
    run(10)
    m = workflow.metrics()
    assert m["applications"] == 3 and m["by_state"]["ACTIVE"] == 1
    assert m["exception_rate"] == 0.667 and m["straight_through_rate"] == 0.333
    assert "VERIFYING" in m["avg_time_in_state_s"]
