"""Workflow orchestration: state machine, verification orchestration, callbacks, retries,
human review, provisioning, duplicate detection, audit trail and metrics.

Concurrency model: every public function takes the global DB lock via `db.tx()`, so state
changes are serialised and callback handling is naturally idempotent (see README).
"""
import json
import logging
import uuid

from . import clock, config, db, extraction, providers, security, states, validation
from .states import S

log = logging.getLogger("workflow")


class NotFound(Exception): ...
class Conflict(Exception): ...
class BadRequest(Exception): ...
class BadSignature(Exception): ...


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _v(x):
    return x.value if hasattr(x, "value") else x


# =============================================================================
# audit + state transitions
# =============================================================================
def audit(app_id, event, actor="system", frm=None, to=None, detail=None):
    db.x("INSERT INTO audit(app_id,ts,actor,event,from_state,to_state,detail) VALUES (?,?,?,?,?,?,?)",
         (app_id, clock.now(), actor, event, _v(frm), _v(to),
          json.dumps(security.mask_obj(detail or {}), default=str)))


def get_app(app_id: str) -> dict:
    r = db.one("SELECT * FROM applications WHERE id=?", (app_id,))
    if not r:
        raise NotFound(f"application {app_id} not found")
    r["form"] = security.unseal(r.pop("form_enc"))
    for k in ("flags", "issues", "simulate"):
        r[k] = json.loads(r[k])
    return r


def _save_form(app_id: str, form: dict, flags: dict | None = None):
    pan, gstin = form.get("pan"), form.get("gstin")
    acc = (form.get("bank") or {}).get("account_number")
    db.x("UPDATE applications SET form_enc=?, pan_fp=?, gstin_fp=?, bank_fp=?, name_norm=?, updated_at=? WHERE id=?",
         (security.seal(form), security.fingerprint(str(pan).upper(), "pan") if pan else None,
          security.fingerprint(str(gstin).upper(), "gstin") if gstin else None,
          security.fingerprint(acc, "bank") if acc else None,
          validation.norm_name(form.get("legal_name")), clock.now(), app_id))
    if flags is not None:
        db.x("UPDATE applications SET flags=? WHERE id=?", (json.dumps(flags), app_id))


def transition(app_id: str, to, actor: str = "system", reason: str | None = None, detail: dict | None = None):
    app = db.one("SELECT state FROM applications WHERE id=?", (app_id,))
    if not app:
        raise NotFound(app_id)
    frm, to = app["state"], _v(to)
    states.assert_can(frm, to)
    now = clock.now()
    db.x("UPDATE applications SET state=?, state_entered_at=?, updated_at=? WHERE id=?", (to, now, now, app_id))
    audit(app_id, "STATE_CHANGED", actor, frm, to, {"reason": reason, **(detail or {})})
    if frm == S.MANUAL_REVIEW.value:  # leaving review by any path closes still-open tasks
        db.x("UPDATE review_tasks SET status='RESOLVED', decision='superseded', resolved_at=? WHERE app_id=? AND status='OPEN'",
             (now, app_id))


def _set_issues(app_id: str, items: list[dict], message: str = ""):
    db.x("UPDATE applications SET issues=? WHERE id=?",
         (json.dumps({"items": items, "message": message, "at": clock.now()}), app_id))


def _clear_issues(app_id: str):
    db.x("UPDATE applications SET issues='{}' WHERE id=?", (app_id,))


# =============================================================================
# submission, documents, resubmission
# =============================================================================
def _add_documents(app_id: str, docs: list[dict]):
    for d in docs:
        t = d.get("doc_type")
        if t not in extraction.DOC_TYPES:
            raise BadRequest(f"unknown doc_type {t!r}; expected one of {extraction.DOC_TYPES}")
        ex = extraction.extract(d)
        db.x("UPDATE documents SET status='SUPERSEDED' WHERE app_id=? AND doc_type=? AND status='ACTIVE'", (app_id, t))
        did = _id("doc")
        db.x("INSERT INTO documents(id,app_id,doc_type,filename,content_type,sha256,extraction_enc,extraction_view,created_at)"
             " VALUES (?,?,?,?,?,?,?,?,?)",
             (did, app_id, t, d.get("filename"), d.get("content_type"), ex["sha256"],
              security.seal(ex), json.dumps(security.mask_extraction(ex)), clock.now()))
        audit(app_id, "DOCUMENT_EXTRACTED", "system", detail={
            "doc_id": did, "doc_type": t, "method": ex["method"], "status": ex["status"],
            "issues": [i["code"] for i in ex["issues"]]})


def submit_application(payload: dict) -> str:
    docs = payload.get("documents") or []
    form = {k: v for k, v in payload.items() if k not in ("documents", "simulate")}
    form.setdefault("bank", {})
    form.setdefault("contact", {})
    if not isinstance(form["bank"], dict) or not isinstance(form["contact"], dict):
        raise BadRequest("'bank' and 'contact' must be objects")
    with db.tx():
        app_id, now = _id("app"), clock.now()
        db.x("INSERT INTO applications(id,state,created_at,updated_at,state_entered_at,form_enc,simulate)"
             " VALUES (?,?,?,?,?,?,?)",
             (app_id, S.SUBMITTED.value, now, now, now, security.seal(form), json.dumps(payload.get("simulate") or {})))
        _save_form(app_id, form, flags={})
        audit(app_id, "APPLICATION_SUBMITTED", "merchant", None, S.SUBMITTED,
              {"form": form, "documents": [d.get("doc_type") for d in docs]})
        _add_documents(app_id, docs)
        transition(app_id, S.DOCUMENT_PROCESSING, "system")
        process_documents(app_id)
    return app_id


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        out[k] = _deep_merge(out.get(k) or {}, v) if isinstance(v, dict) and isinstance(out.get(k) or {}, dict) else v
    return out


def resubmit(app_id: str, patch: dict | None, documents: list[dict] | None, actor: str = "merchant"):
    """Merchant supplies corrections / new documents. Any data change re-enters DOCUMENT_PROCESSING;
    checks whose inputs did not change keep their results (input-hash versioning)."""
    patch, documents = patch or {}, documents or []
    patch = {k: v for k, v in patch.items() if k not in ("documents",)}
    sim_patch = patch.pop("simulate", None)
    if not patch and not documents:
        raise BadRequest("nothing to update")
    with db.tx():
        app = get_app(app_id)
        if app["state"] not in (S.NEEDS_INFO, S.VERIFYING, S.MANUAL_REVIEW, S.APPROVED, S.PROVISIONING_FAILED):
            raise Conflict(f"application in state {app['state']} cannot be modified")
        old = app["form"]
        new = _deep_merge(old, patch)
        flags = app["flags"]
        changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        if sim_patch:
            sim = {**app["simulate"], **sim_patch}
            db.x("UPDATE applications SET simulate=? WHERE id=?", (json.dumps(sim), app_id))
        bank_keys = ("account_number", "ifsc", "account_holder")
        bank_changed = any(old.get("bank", {}).get(k) != new.get("bank", {}).get(k) for k in bank_keys)
        if bank_changed:
            audit(app_id, "BANK_DETAILS_CHANGED", actor, detail={"old": old.get("bank"), "new": new.get("bank")})
            if db.one("SELECT 1 x FROM verifications WHERE app_id=? AND provider='bank' AND status='SUCCEEDED'", (app_id,)):
                flags["bank_changed"] = True  # changed AFTER a successful verification: a human must look
            if not any(d.get("doc_type") == "BANK_PROOF" for d in documents):
                # the old proof describes the old account, so it can no longer count
                db.x("UPDATE documents SET status='SUPERSEDED' WHERE app_id=? AND doc_type='BANK_PROOF' AND status='ACTIVE'", (app_id,))
        if documents:  # document-derived review decisions no longer apply to new documents
            flags["reviewed"] = [c for c in flags.get("reviewed", []) if not c.startswith("DOC_")]
        _save_form(app_id, new, flags)
        _add_documents(app_id, documents)
        audit(app_id, "APPLICATION_UPDATED", actor, detail={"fields_changed": changed,
              "documents": [d.get("doc_type") for d in documents]})
        transition(app_id, S.DOCUMENT_PROCESSING, actor, "merchant data changed")
        process_documents(app_id)


# =============================================================================
# document processing -> routing
# =============================================================================
def detect_duplicates(app: dict) -> list[dict]:
    out = []
    for r in db.q("SELECT id,state,pan_fp,gstin_fp,bank_fp,name_norm FROM applications WHERE id!=?", (app["id"],)):
        why = []
        if app["pan_fp"] and r["pan_fp"] == app["pan_fp"]: why.append("SAME_PAN")
        if app["gstin_fp"] and r["gstin_fp"] == app["gstin_fp"]: why.append("SAME_GSTIN")
        if app["bank_fp"] and r["bank_fp"] == app["bank_fp"]: why.append("SAME_BANK_ACCOUNT")
        if not why and app["name_norm"] and r["name_norm"]:
            import difflib
            if difflib.SequenceMatcher(None, app["name_norm"], r["name_norm"]).ratio() >= config.DUPLICATE_NAME_SIMILARITY:
                why.append("SIMILAR_NAME")
        if why:
            out.append({"application_id": r["id"], "state": r["state"], "match_on": why})
    return out


def request_info(app_id: str, items: list[dict], actor: str = "system"):
    msg = "; ".join(i["message"] for i in items)
    _set_issues(app_id, items, msg)
    transition(app_id, S.NEEDS_INFO, actor, "information requested", {"items": [i.get("code") for i in items]})


def enter_manual_review(app_id: str, reasons: list[dict]):
    transition(app_id, S.MANUAL_REVIEW, "system", "escalated", {"reasons": [r["code"] for r in reasons]})
    tid = _id("rev")
    db.x("INSERT INTO review_tasks(id,app_id,reasons,created_at) VALUES (?,?,?,?)",
         (tid, app_id, json.dumps(security.mask_obj(reasons)), clock.now()))
    audit(app_id, "REVIEW_TASK_CREATED", "system", detail={"task_id": tid, "reasons": reasons})


def process_documents(app_id: str):
    with db.tx():
        app = get_app(app_id)
        docs = db.q("SELECT extraction_enc FROM documents WHERE app_id=? AND status='ACTIVE'", (app_id,))
        exts = [security.unseal(d["extraction_enc"]) for d in docs]
        issues = validation.validate(app["form"], exts)
        blocking = [i for i in issues if i["severity"] == "BLOCKING"]
        audit(app_id, "VALIDATION_COMPLETED", detail={"blocking": [i["code"] for i in blocking],
              "review": [i["code"] for i in issues if i["severity"] == "REVIEW"]})
        if blocking:
            return request_info(app_id, blocking)

        reviewed = set(app["flags"].get("reviewed", []))
        reasons = [{"code": i["code"], "field": i["field"], "message": i["message"]}
                   for i in issues if i["severity"] == "REVIEW"]
        dups = detect_duplicates(app)
        if dups:
            reasons.append({"code": "DUPLICATE_SUSPECTED", "message": "Possible duplicate of existing application(s)", "matches": dups})
        if app["flags"].get("bank_changed"):
            reasons.append({"code": "BANK_CHANGED_AFTER_VERIFICATION",
                            "message": "Bank details were changed after a successful bank verification"})
        reasons = [r for r in reasons if r["code"] not in reviewed]
        if reasons:
            return enter_manual_review(app_id, reasons)
        _clear_issues(app_id)
        transition(app_id, S.VERIFYING, "system", "documents validated")
        advance(app_id)


# =============================================================================
# verification orchestration
# =============================================================================
def _inputs(form: dict, provider: str) -> dict:
    b = form.get("bank") or {}
    return {
        "pan": {"pan": form.get("pan"), "name": form.get("legal_name")},
        "gst": {"gstin": form.get("gstin"), "pan": form.get("pan"), "legal_name": form.get("legal_name")},
        "bank": {"account_number": b.get("account_number"), "ifsc": b.get("ifsc"), "account_holder": b.get("account_holder")},
        "risk": {"pan": form.get("pan"), "gstin": form.get("gstin"), "account_number": b.get("account_number"),
                 "business_type": form.get("business_type"), "volume": form.get("expected_monthly_volume_inr")},
    }[provider]


def input_hash(form: dict, provider: str) -> str:
    return security.fingerprint(json.dumps(_inputs(form, provider), sort_keys=True), "ver:" + provider)


def check_state(app: dict, provider: str):
    """Status of a check for the application's CURRENT inputs.
    -> (NOT_STARTED|PENDING|SUCCEEDED|FAILED|EXHAUSTED, result)"""
    ih = input_hash(app["form"], provider)
    rows = db.q("SELECT rowid seq,* FROM verifications WHERE app_id=? AND provider=? AND input_hash=?", (app["id"], provider, ih))
    resolved = [r for r in rows if r["status"] in ("SUCCEEDED", "FAILED")]
    if resolved:
        last = max(resolved, key=lambda r: (r["resolved_at"], r["seq"]))
        return last["status"], json.loads(last["result"] or "{}")
    if any(r["status"] == "PENDING" for r in rows) or db.one(
            "SELECT 1 x FROM jobs WHERE app_id=? AND key=? AND status='PENDING'", (app["id"], f"{provider}|{ih}")):
        return "PENDING", {}
    return ("EXHAUSTED", {}) if rows else ("NOT_STARTED", {})


def _schedule_retry(app: dict, provider: str, ih: str, attempt: int) -> bool:
    if attempt >= config.MAX_ATTEMPTS:
        db.x("INSERT INTO dead_letters(kind,app_id,payload,reason,created_at) VALUES (?,?,?,?,?)",
             ("PROVIDER_EXHAUSTED", app["id"], json.dumps({"provider": provider}),
              f"{provider}: {attempt} attempts without a result", clock.now()))
        audit(app["id"], "DEAD_LETTERED", detail={"kind": "PROVIDER_EXHAUSTED", "provider": provider, "attempts": attempt})
        return False
    delay = config.RETRY_BACKOFF_S * 2 ** (attempt - 1)
    db.x("INSERT INTO jobs(app_id,kind,key,payload,run_at) VALUES (?,?,?,?,?)",
         (app["id"], "DISPATCH_CHECK", f"{provider}|{ih}",
          json.dumps({"provider": provider, "ih": ih, "attempt": attempt + 1}), clock.now() + delay))
    audit(app["id"], "RETRY_SCHEDULED", detail={"provider": provider, "next_attempt": attempt + 1, "delay_s": delay})
    return True


def _dispatch(app: dict, provider: str, attempt: int):
    rid, now, ih = _id("req"), clock.now(), input_hash(app["form"], provider)
    seq = db.one("SELECT COUNT(*) n FROM verifications WHERE app_id=? AND provider=?", (app["id"], provider))["n"] + 1
    db.x("INSERT INTO verifications(id,app_id,provider,input_hash,attempt,seq,status,deadline,created_at)"
         " VALUES (?,?,?,?,?,?,'PENDING',?,?)", (rid, app["id"], provider, ih, attempt, seq, now + config.CHECK_TIMEOUT_S, now))
    audit(app["id"], "CHECK_REQUESTED", detail={"provider": provider, "request_id": rid, "attempt": attempt})
    try:
        providers.submit(provider, rid, app["id"], _inputs(app["form"], provider), app["simulate"], seq)
    except providers.ProviderUnavailable as e:
        db.x("UPDATE verifications SET status='ERROR', resolved_at=?, result=? WHERE id=?",
             (clock.now(), json.dumps({"error": str(e)}), rid))
        audit(app["id"], "CHECK_SUBMIT_FAILED", detail={"provider": provider, "request_id": rid, "error": str(e)})
        _schedule_retry(app, provider, ih, attempt)


FAILURE_FIELD = {"PAN_INVALID": "pan", "GSTIN_NOT_FOUND": "gstin", "ACCOUNT_INVALID": "bank"}
FAILURE_TEXT = {
    "PAN_INVALID": "PAN verification failed: please check the PAN",
    "GSTIN_NOT_FOUND": "GST verification failed: GSTIN not found, please provide the correct GSTIN",
    "ACCOUNT_INVALID": "Bank verification failed: account could not be validated",
    "NAME_MISMATCH": "Name registered with the authority does not match the application",
}


def can_approve(app: dict, waive_risk: bool = False) -> tuple[bool, str]:
    """The single gate for approval and for merchant-ID creation."""
    for p in config.REQUIRED_CHECKS:
        s, res = check_state(app, p)
        if s != "SUCCEEDED":
            return False, f"{p} check is {s}"
        if p == "risk":
            band = res.get("band")
            if band == "DECLINE":
                return False, "risk declined"
            if band != "LOW" and not waive_risk:
                return False, f"risk band {band} needs human approval"
    return True, ""


def advance(app_id: str):
    """Launch any check whose dependencies passed, then decide what to do once nothing is pending."""
    with db.tx():
        app = get_app(app_id)
        if app["state"] != S.VERIFYING.value:
            return
        req = list(config.REQUIRED_CHECKS)
        st = {p: check_state(app, p) for p in req}
        for p in req:
            deps = [d for d in config.DEPENDENCIES.get(p, []) if d in req]
            if st[p][0] == "NOT_STARTED" and all(st[d][0] == "SUCCEEDED" for d in deps):
                _dispatch(app, p, 1)
                st[p] = check_state(app, p)
        status = {p: s for p, (s, _) in st.items()}
        if "PENDING" in status.values():
            return  # wait for callbacks / timeouts / retries

        failed = [(p, st[p][1]) for p in req if status[p] == "FAILED"]
        exhausted = [p for p in req if status[p] == "EXHAUSTED"]
        if failed or exhausted:
            manual, fix = [], []
            for p, res in failed:
                r = res.get("reason")
                item = {"code": "CHECK_FAILED", "check": p, "reason": r, "field": FAILURE_FIELD.get(r),
                        "message": FAILURE_TEXT.get(r, f"{p} check failed ({r})")}
                (manual if r in config.REVIEW_FAILURE_REASONS else fix).append(item)
            for p in exhausted:
                manual.append({"code": "PROVIDER_UNAVAILABLE", "check": p,
                               "message": f"{p} provider did not respond after {config.MAX_ATTEMPTS} attempts"})
            return enter_manual_review(app_id, manual + fix) if manual else request_info(app_id, fix)

        if not all(s == "SUCCEEDED" for s in status.values()):
            return
        band = st["risk"][1].get("band") if "risk" in req else "LOW"
        if band == "DECLINE":
            audit(app_id, "RISK_DECLINED", detail=st["risk"][1])
            return transition(app_id, S.REJECTED, "system", "risk provider declined")
        if band != "LOW" and not app["flags"].get("risk_waived"):
            return enter_manual_review(app_id, [{"code": "RISK_REVIEW", "message": f"Risk band {band}", "result": st["risk"][1]}])
        ok, why = can_approve(app, waive_risk=bool(app["flags"].get("risk_waived")))
        assert ok, why
        transition(app_id, S.APPROVED, "system", "all required checks passed")
        _provision(app_id, 1)


# =============================================================================
# callbacks (async provider results)
# =============================================================================
def handle_callback(provider: str, raw: bytes, signature: str) -> dict:
    if not security.verify(raw, signature):
        log.warning("rejected callback with bad signature for provider=%s", provider)
        raise BadSignature("invalid signature")
    try:
        body = json.loads(raw)
        event_id, rid, status = body["event_id"], body["request_id"], body["status"]
    except (ValueError, KeyError) as e:
        raise BadRequest(f"malformed callback: {e}")
    if status not in ("SUCCEEDED", "FAILED"):
        raise BadRequest("status must be SUCCEEDED or FAILED")
    result = body.get("result") or {}

    with db.tx():
        now = clock.now()
        v = db.one("SELECT * FROM verifications WHERE id=?", (rid,))
        seen = db.one("SELECT 1 x FROM callback_events WHERE event_id=?", (event_id,))
        if seen:  # exactly-once effect for at-least-once delivery
            db.x("UPDATE callback_events SET dup_count=dup_count+1 WHERE event_id=?", (event_id,))
            if v:
                audit(v["app_id"], "DUPLICATE_CALLBACK_IGNORED", "provider", detail={"event_id": event_id, "provider": provider})
            return {"outcome": "duplicate"}
        if not v or v["provider"] != provider:
            db.x("INSERT INTO dead_letters(kind,app_id,payload,reason,created_at) VALUES (?,?,?,?,?)",
                 ("UNKNOWN_CALLBACK", v["app_id"] if v else None, json.dumps({"event_id": event_id, "request_id": rid, "provider": provider}),
                  "callback does not match any request", now))
            db.x("INSERT INTO callback_events(event_id,request_id,provider,received_at,outcome) VALUES (?,?,?,?,?)",
                 (event_id, rid, provider, now, "UNKNOWN_REQUEST"))
            return {"outcome": "dead_lettered"}

        app = get_app(v["app_id"])
        outcome = "APPLIED"
        if v["input_hash"] != input_hash(app["form"], provider):
            db.x("UPDATE verifications SET status='STALE' WHERE id=?", (rid,))
            outcome = "STALE_IGNORED"
            audit(app["id"], "CALLBACK_STALE_IGNORED", "provider",
                  detail={"provider": provider, "request_id": rid, "why": "inputs changed after request was sent"})
        elif v["status"] not in ("PENDING", "TIMED_OUT"):
            outcome = "CONFLICT_IGNORED"
            audit(app["id"], "CALLBACK_IGNORED", "provider", detail={"request_id": rid, "request_status": v["status"]})
        else:
            late = v["status"] == "TIMED_OUT"
            db.x("UPDATE verifications SET status=?, result=?, resolved_at=?, late=? WHERE id=?",
                 (status, json.dumps(result), now, int(late), rid))
            audit(app["id"], "LATE_CALLBACK_ACCEPTED" if late else "CALLBACK_RECEIVED", "provider",
                  detail={"provider": provider, "request_id": rid, "status": status, "result": result,
                          **({"note": "arrived after the request had timed out"} if late else {})})
            outcome = "LATE_APPLIED" if late else "APPLIED"
        db.x("INSERT INTO callback_events(event_id,request_id,provider,received_at,outcome) VALUES (?,?,?,?,?)",
             (event_id, rid, provider, now, outcome))
        advance(app["id"])
    return {"outcome": outcome}


# =============================================================================
# timers: timeouts, retries (called by the background worker / tick())
# =============================================================================
def sweep_timeouts() -> int:
    n = 0
    for v in db.q("SELECT id FROM verifications WHERE status='PENDING' AND deadline<=?", (clock.now(),)):
        with db.tx():
            v = db.one("SELECT * FROM verifications WHERE id=?", (v["id"],))
            if v["status"] != "PENDING":
                continue
            db.x("UPDATE verifications SET status='TIMED_OUT' WHERE id=?", (v["id"],))
            app = get_app(v["app_id"])
            audit(app["id"], "CHECK_TIMED_OUT", detail={"provider": v["provider"], "request_id": v["id"], "attempt": v["attempt"]})
            n += 1
            if app["state"] == S.VERIFYING.value and v["input_hash"] == input_hash(app["form"], v["provider"]):
                _schedule_retry(app, v["provider"], v["input_hash"], v["attempt"])
                advance(app["id"])
    return n


def run_jobs() -> int:
    n = 0
    for j in db.q("SELECT id FROM jobs WHERE status='PENDING' AND run_at<=? ORDER BY run_at,id", (clock.now(),)):
        with db.tx():
            j = db.one("SELECT * FROM jobs WHERE id=?", (j["id"],))
            if j["status"] != "PENDING":
                continue
            db.x("UPDATE jobs SET status='DONE' WHERE id=?", (j["id"],))
            p, app = json.loads(j["payload"]), get_app(j["app_id"])
            n += 1
            if j["kind"] == "DISPATCH_CHECK":
                prov = p["provider"]
                st = check_state(app, prov)[0] if input_hash(app["form"], prov) == p["ih"] else "STALE"
                if app["state"] != S.VERIFYING.value or st in ("SUCCEEDED", "FAILED", "PENDING", "STALE"):
                    audit(app["id"], "RETRY_SKIPPED", detail={"provider": prov, "why": f"state={app['state']} check={st}"})
                    continue
                _dispatch(app, prov, p["attempt"])
                advance(app["id"])
            elif j["kind"] == "PROVISION":
                _provision(app["id"], p["attempt"])
    return n


def tick() -> dict:
    """One scheduler pass. Order matters: timeouts first so late callbacks are recognised as late."""
    t = sweep_timeouts()
    d = providers.deliver_due(handle_callback)
    j = run_jobs()
    return {"timeouts": t, "callbacks_delivered": d, "jobs_run": j}


# =============================================================================
# provisioning (merchant ID creation)
# =============================================================================
def _provision(app_id: str, attempt: int = 1):
    with db.tx():
        app = get_app(app_id)
        if app["state"] not in (S.APPROVED.value, S.PROVISIONING_FAILED.value):
            return
        ok, why = can_approve(app, waive_risk=bool(app["flags"].get("risk_waived")))
        if not ok:  # e.g. bank details changed after approval: never create an ID on stale verification
            audit(app_id, "PROVISIONING_BLOCKED", detail={"why": why})
            transition(app_id, S.VERIFYING, "system", "checks no longer valid")
            return advance(app_id)
        transition(app_id, S.PROVISIONING, "system", detail={"attempt": attempt})
        try:
            mid = providers.create_merchant(app_id, app["simulate"], attempt)
        except providers.MerchantServiceError as e:
            transition(app_id, S.PROVISIONING_FAILED, "system", str(e), {"attempt": attempt})
            if attempt < config.PROVISION_MAX_ATTEMPTS:
                delay = config.RETRY_BACKOFF_S * 2 ** (attempt - 1)
                db.x("INSERT INTO jobs(app_id,kind,key,payload,run_at) VALUES (?,?,?,?,?)",
                     (app_id, "PROVISION", f"provision|{app_id}", json.dumps({"attempt": attempt + 1}), clock.now() + delay))
                audit(app_id, "RETRY_SCHEDULED", detail={"what": "merchant_id_creation", "next_attempt": attempt + 1, "delay_s": delay})
            else:
                db.x("INSERT INTO dead_letters(kind,app_id,payload,reason,created_at) VALUES (?,?,?,?,?)",
                     ("PROVISION_EXHAUSTED", app_id, "{}", str(e), clock.now()))
                audit(app_id, "DEAD_LETTERED", detail={"kind": "PROVISION_EXHAUSTED", "attempts": attempt})
            return
        db.x("INSERT OR IGNORE INTO merchants(merchant_id,app_id,created_at,payout_account_masked) VALUES (?,?,?,?)",
             (mid, app_id, clock.now(), security.mask_value("account_number", (app["form"].get("bank") or {}).get("account_number"))))
        transition(app_id, S.ACTIVE, "system", "merchant ID created", {"merchant_id": mid})


# =============================================================================
# human-in-the-loop review + dead letters
# =============================================================================
DOC_STAGE_CODES = {"DOC_LOW_CONFIDENCE", "DOC_FORM_CONFLICT", "DUPLICATE_SUSPECTED",
                   "BANK_CHANGED_AFTER_VERIFICATION", "ACCOUNT_HOLDER_DIFFERS"}


def resolve_review(task_id: str, decision: str, reviewer: str, notes: str = "") -> dict:
    """decision: proceed | approve | reject | request_info | retry"""
    with db.tx():
        t = db.one("SELECT * FROM review_tasks WHERE id=?", (task_id,))
        if not t:
            raise NotFound(task_id)
        if t["status"] != "OPEN":
            raise Conflict("review task already resolved")
        app = get_app(t["app_id"])
        codes = {r["code"] for r in json.loads(t["reasons"])}
        if decision not in ("proceed", "approve", "reject", "request_info", "retry"):
            raise BadRequest("decision must be proceed|approve|reject|request_info|retry")
        if decision in ("reject", "request_info") and not notes:
            raise BadRequest("notes are required for this decision")

        def close():
            db.x("UPDATE review_tasks SET status='RESOLVED', decision=?, reviewer=?, notes=?, resolved_at=? WHERE id=?",
                 (decision, reviewer, notes, clock.now(), task_id))

        audit(app["id"], "REVIEW_DECISION", reviewer, detail={"task_id": task_id, "decision": decision, "notes": notes, "reasons": sorted(codes)})
        if decision == "proceed":
            if not codes <= DOC_STAGE_CODES:
                raise Conflict("'proceed' is only valid for document-stage reasons; failed checks cannot be overridden "
                               "(use request_info / reject, or retry for provider outages)")
            flags = app["flags"]
            flags["reviewed"] = sorted(set(flags.get("reviewed", [])) | codes)
            flags["bank_changed"] = False
            db.x("UPDATE applications SET flags=? WHERE id=?", (json.dumps(flags), app["id"]))
            close()
            _clear_issues(app["id"])
            transition(app["id"], S.VERIFYING, reviewer, "reviewer cleared document-stage flags")
            advance(app["id"])
        elif decision == "approve":
            ok, why = can_approve(app, waive_risk=True)
            if not ok:
                raise Conflict(f"cannot approve: {why}. Required checks must pass; only a MEDIUM/HIGH risk band can be waived")
            flags = app["flags"]
            flags["risk_waived"] = True
            db.x("UPDATE applications SET flags=? WHERE id=?", (json.dumps(flags), app["id"]))
            close()
            audit(app["id"], "REVIEWER_OVERRIDE", reviewer, detail={"waived": "risk_band", "notes": notes})
            transition(app["id"], S.APPROVED, reviewer, "approved by reviewer")
            _provision(app["id"], 1)
        elif decision == "reject":
            close()
            transition(app["id"], S.REJECTED, reviewer, notes)
        elif decision == "request_info":
            close()
            request_info(app["id"], [{"code": "REVIEWER_REQUEST", "message": notes}], actor=reviewer)
        elif decision == "retry":
            if "PROVIDER_UNAVAILABLE" not in codes:
                raise Conflict("'retry' is only valid for provider outages")
            close()
            _retry_exhausted(app["id"], reviewer)
        return {"task_id": task_id, "decision": decision}


def _retry_exhausted(app_id: str, actor: str):
    if get_app(app_id)["state"] == S.MANUAL_REVIEW.value:
        transition(app_id, S.VERIFYING, actor, "retrying unavailable providers")
    app = get_app(app_id)
    for p in config.REQUIRED_CHECKS:
        if check_state(app, p)[0] == "EXHAUSTED":
            _dispatch(app, p, 1)
    db.x("UPDATE dead_letters SET status='REPLAYED' WHERE app_id=? AND kind='PROVIDER_EXHAUSTED' AND status='OPEN'", (app_id,))
    advance(app_id)


def reject_application(app_id: str, actor: str, notes: str):
    with db.tx():
        transition(app_id, S.REJECTED, actor, notes or "rejected by operator")


def replay_dead_letter(dl_id: int, actor: str = "operator") -> dict:
    with db.tx():
        d = db.one("SELECT * FROM dead_letters WHERE id=?", (dl_id,))
        if not d:
            raise NotFound(str(dl_id))
        if d["status"] != "OPEN":
            raise Conflict("already handled")
        if d["kind"] == "PROVISION_EXHAUSTED":
            db.x("UPDATE dead_letters SET status='REPLAYED' WHERE id=?", (dl_id,))
            audit(d["app_id"], "DEAD_LETTER_REPLAYED", actor, detail={"kind": d["kind"]})
            _provision(d["app_id"], 1)
        elif d["kind"] == "PROVIDER_EXHAUSTED":
            audit(d["app_id"], "DEAD_LETTER_REPLAYED", actor, detail={"kind": d["kind"]})
            _retry_exhausted(d["app_id"], actor)
        else:
            db.x("UPDATE dead_letters SET status='DISMISSED' WHERE id=?", (dl_id,))
        return {"id": dl_id, "kind": d["kind"]}


# =============================================================================
# read models
# =============================================================================
def _mask_form(form: dict) -> dict:
    return security.mask_obj(form)


def application_view(app_id: str) -> dict:
    app = get_app(app_id)
    checks = {}
    for p in config.REQUIRED_CHECKS:
        s, res = check_state(app, p)
        rows = db.q("SELECT id,attempt,status,late,created_at,resolved_at FROM verifications WHERE app_id=? AND provider=? ORDER BY created_at",
                    (app_id, p))
        checks[p] = {"status": s, "result": res, "attempts": rows,
                     "depends_on": [d for d in config.DEPENDENCIES.get(p, []) if d in config.REQUIRED_CHECKS]}
    docs = [{"id": d["id"], "doc_type": d["doc_type"], "filename": d["filename"], "status": d["status"],
             "extraction": json.loads(d["extraction_view"])}
            for d in db.q("SELECT * FROM documents WHERE app_id=? ORDER BY created_at", (app_id,))]
    reviews = [{**r, "reasons": json.loads(r["reasons"])} for r in
               db.q("SELECT * FROM review_tasks WHERE app_id=? ORDER BY created_at", (app_id,))]
    trail = [{**a, "detail": json.loads(a["detail"]), "time": clock.iso(a["ts"])}
             for a in db.q("SELECT * FROM audit WHERE app_id=? ORDER BY id", (app_id,))]
    m = db.one("SELECT * FROM merchants WHERE app_id=?", (app_id,))
    return {
        "id": app_id, "state": app["state"], "created_at": clock.iso(app["created_at"]),
        "time_in_state_s": round(clock.now() - app["state_entered_at"], 1),
        "form": _mask_form(app["form"]), "issues": app["issues"], "flags": app["flags"],
        "checks": checks, "documents": docs, "review_tasks": reviews, "merchant": m, "audit": trail,
        "duplicates": detect_duplicates(app),
    }


def applicant_status(app_id: str) -> dict:
    app = get_app(app_id)
    m = db.one("SELECT merchant_id FROM merchants WHERE app_id=?", (app_id,))
    return {"id": app_id, "state": app["state"], "action_required": app["issues"].get("items", []) if app["state"] == S.NEEDS_INFO.value else [],
            "merchant_id": m["merchant_id"] if m else None}


def list_applications() -> list[dict]:
    now = clock.now()
    out = []
    for a in db.q("SELECT id,state,created_at,state_entered_at,form_enc,issues FROM applications ORDER BY created_at DESC"):
        form = security.unseal(a["form_enc"])
        m = db.one("SELECT merchant_id FROM merchants WHERE app_id=?", (a["id"],))
        out.append({"id": a["id"], "legal_name": form.get("legal_name"), "state": a["state"],
                    "created_at": clock.iso(a["created_at"]), "time_in_state_s": round(now - a["state_entered_at"], 1),
                    "open_review": bool(db.one("SELECT 1 x FROM review_tasks WHERE app_id=? AND status='OPEN'", (a["id"],))),
                    "missing": [i["message"] for i in json.loads(a["issues"]).get("items", [])],
                    "merchant_id": m["merchant_id"] if m else None})
    return out


def review_queue() -> list[dict]:
    out = []
    for t in db.q("SELECT * FROM review_tasks WHERE status='OPEN' ORDER BY created_at"):
        app = get_app(t["app_id"])
        out.append({**t, "reasons": json.loads(t["reasons"]), "legal_name": app["form"].get("legal_name"),
                    "age_s": round(clock.now() - t["created_at"], 1)})
    return out


def dead_letters() -> list[dict]:
    return [{**d, "payload": json.loads(d["payload"] or "{}")} for d in db.q("SELECT * FROM dead_letters ORDER BY id DESC")]


def metrics() -> dict:
    now = clock.now()
    apps = db.q("SELECT id,state,created_at,state_entered_at FROM applications")
    by_state: dict[str, int] = {}
    for a in apps:
        by_state[a["state"]] = by_state.get(a["state"], 0) + 1
    durations: dict[str, list[float]] = {}
    exception_apps: set[str] = set()
    trans: dict[str, list] = {}
    for r in db.q("SELECT app_id,ts,to_state FROM audit WHERE event='STATE_CHANGED' ORDER BY id"):
        trans.setdefault(r["app_id"], []).append((r["ts"], r["to_state"]))
        if r["to_state"] in ("NEEDS_INFO", "MANUAL_REVIEW"):
            exception_apps.add(r["app_id"])
    for a in apps:
        prev_ts, prev_state = a["created_at"], S.SUBMITTED.value
        for ts, to in trans.get(a["id"], []):
            durations.setdefault(prev_state, []).append(ts - prev_ts)
            prev_ts, prev_state = ts, to
        if prev_state not in (S.ACTIVE.value, S.REJECTED.value):
            durations.setdefault(prev_state, []).append(now - prev_ts)
    for d in db.q("SELECT app_id FROM dead_letters WHERE app_id IS NOT NULL"):
        exception_apps.add(d["app_id"])
    n = len(apps)
    active = [a["id"] for a in apps if a["state"] == S.ACTIVE.value]
    ev = {r["event"]: r["n"] for r in db.q("SELECT event, COUNT(*) n FROM audit GROUP BY event")}
    return {
        "applications": n, "by_state": by_state,
        "avg_time_in_state_s": {s: round(sum(v) / len(v), 2) for s, v in durations.items()},
        "max_time_in_state_s": {s: round(max(v), 2) for s, v in durations.items()},
        "exception_rate": round(len(exception_apps) / n, 3) if n else 0.0,
        "straight_through_rate": round(len([i for i in active if i not in exception_apps]) / n, 3) if n else 0.0,
        "open_review_tasks": db.one("SELECT COUNT(*) n FROM review_tasks WHERE status='OPEN'")["n"],
        "open_dead_letters": db.one("SELECT COUNT(*) n FROM dead_letters WHERE status='OPEN'")["n"],
        "duplicate_callbacks_ignored": ev.get("DUPLICATE_CALLBACK_IGNORED", 0),
        "late_callbacks_accepted": ev.get("LATE_CALLBACK_ACCEPTED", 0),
        "stale_callbacks_ignored": ev.get("CALLBACK_STALE_IGNORED", 0),
        "check_timeouts": ev.get("CHECK_TIMED_OUT", 0),
        "retries_scheduled": ev.get("RETRY_SCHEDULED", 0),
    }
