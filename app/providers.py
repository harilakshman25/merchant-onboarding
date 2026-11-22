"""Simulated external verification providers and the internal merchant-ID service.

Providers are asynchronous: `submit()` only *accepts* a request. The result arrives later as a
signed callback placed in `sim_outbox` and delivered by `deliver_due()` (the background worker
in the API process, or `workflow.tick()` in tests/demos). Delivery goes through exactly the same
`workflow.handle_callback` code path as the real HTTP endpoint.

Scenario control (application payload `simulate`, a test-only channel):
  pan | gst | bank | risk : mode string. Suffix `_once` = misbehave only on the first request
                            for that check, behave normally afterwards (models "merchant fixed it").
    pan:  ok | fail
    gst:  ok | not_found | name_mismatch
    bank: ok | fail | timeout_late (callback arrives after our timeout) | timeout_always | outage_once
    risk: low | medium | high | decline
    any:  outage_once | outage_always  -> provider rejects the submit call (HTTP 503 equivalent)
  duplicate_callbacks: N      deliver every callback N times (same event_id)
  callback_delay_s / late_delay_s
  merchant_id: ok | fail_once | fail_always
"""
import hashlib
import json

from . import clock, config, db, security


class ProviderUnavailable(Exception):
    pass


class MerchantServiceError(Exception):
    pass


DEFAULT = {"pan": "ok", "gst": "ok", "bank": "ok", "risk": "low"}


def _result(provider: str, mode: str, inp: dict):
    if provider == "pan":
        if mode == "ok":
            return "SUCCEEDED", {"status": "VALID", "name_on_record": inp["name"]}
        if mode == "fail":
            return "FAILED", {"reason": "PAN_INVALID"}
    elif provider == "gst":
        if mode == "ok":
            return "SUCCEEDED", {"status": "ACTIVE", "trade_name": inp["legal_name"]}
        if mode == "not_found":
            return "FAILED", {"reason": "GSTIN_NOT_FOUND"}
        if mode == "name_mismatch":
            return "FAILED", {"reason": "NAME_MISMATCH"}
    elif provider == "bank":
        if mode == "ok":
            return "SUCCEEDED", {"penny_drop": "CREDITED", "name_at_bank": inp["account_holder"]}
        if mode == "fail":
            return "FAILED", {"reason": "ACCOUNT_INVALID"}
    elif provider == "risk":
        if mode in ("low", "medium", "high", "decline"):
            score = {"low": 12, "medium": 48, "high": 81, "decline": 97}[mode]
            return "SUCCEEDED", {"band": mode.upper(), "score": score}
    raise ValueError(f"unknown simulator mode {provider}={mode}")


def submit(provider: str, request_id: str, app_id: str, inputs: dict, scenario: dict, seq: int) -> None:
    """Accept a verification request. `seq` = how many requests this app has made to this provider."""
    mode = scenario.get(provider, DEFAULT[provider])
    if mode.endswith("_once") and mode != "outage_once":
        mode = mode[:-5] if seq == 1 else DEFAULT[provider]
    if mode == "outage_always" or (mode == "outage_once" and seq == 1):
        raise ProviderUnavailable(f"{provider} provider returned 503")

    delay = float(scenario.get("callback_delay_s", config.SIM_CALLBACK_DELAY_S))
    if mode == "timeout_always":
        return  # request accepted, but no callback ever
    if mode == "timeout_late":
        if seq == 1:
            delay = float(scenario.get("late_delay_s", config.CHECK_TIMEOUT_S + 15))
        mode = DEFAULT[provider]
    if mode == "outage_once":  # second request onward
        mode = DEFAULT[provider]

    status, result = _result(provider, mode, inputs)
    body = {"event_id": f"evt_{request_id}", "request_id": request_id, "provider": provider,
            "status": status, "result": result}
    raw = json.dumps(body, sort_keys=True)
    sig = security.sign(raw.encode())
    now = clock.now()
    for i in range(max(1, int(scenario.get("duplicate_callbacks", 1)))):
        db.x("INSERT INTO sim_outbox(provider,request_id,body,signature,due_at) VALUES (?,?,?,?,?)",
             (provider, request_id, raw, sig, now + delay + i * 0.1))


def deliver_due(handler) -> int:
    """Deliver due callbacks to `handler(provider, raw_bytes, signature)`."""
    rows = db.q("SELECT * FROM sim_outbox WHERE delivered=0 AND due_at<=? ORDER BY due_at,id", (clock.now(),))
    for r in rows:
        db.x("UPDATE sim_outbox SET delivered=1 WHERE id=?", (r["id"],))
        try:
            handler(r["provider"], r["body"].encode(), r["signature"])
        except Exception:  # a real webhook sender would see a 4xx/5xx; we just keep going
            import logging
            logging.getLogger("simulator").exception("callback delivery failed")
    return len(rows)


def create_merchant(app_id: str, scenario: dict, attempt: int) -> str:
    """Internal merchant-ID / payment-config service. Idempotent per application."""
    mode = scenario.get("merchant_id", "ok")
    if mode == "fail_always" or (mode == "fail_once" and attempt == 1):
        raise MerchantServiceError("merchant-config service unavailable (simulated)")
    return "MID" + str(int(hashlib.sha256(app_id.encode()).hexdigest(), 16))[:10]
