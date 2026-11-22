"""Sample application payloads for every scenario in the brief. Used by the UI, demo and tests."""
import random
import string

_A = ["Sunrise", "Blue Lotus", "Kaveri", "Northwind", "Orchid", "Zenith", "Maple", "Coastal", "Falcon", "Indigo",
      "Heritage", "Pioneer", "Lakeview", "Saffron", "Crescent", "Evergreen", "Meridian", "Vanguard", "Aurora", "Banyan"]
_B = ["Foods", "Traders", "Textiles", "Logistics", "Electronics", "Agro", "Pharma", "Retail", "Ventures", "Handicrafts"]

SCENARIOS = {
    "happy_path": "Everything passes -> auto-approved -> merchant ID (straight-through)",
    "gst_not_found": "PAN verifies, GST fails (GSTIN not found) -> NEEDS_INFO; fix via PATCH -> continues",
    "bank_timeout_late_callback": "Bank callback arrives AFTER the timeout (+ every callback delivered 3x)",
    "duplicate_callbacks": "Every provider callback is delivered 3 times -> idempotent handling",
    "blurred_document": "PAN card scan is blurred -> NEEDS_INFO (re-upload)",
    "document_conflict": "Bank proof holder name conflicts with the form -> MANUAL_REVIEW",
    "low_confidence_scan": "Soft scan -> low field confidence -> MANUAL_REVIEW",
    "missing_document": "GST certificate not supplied -> NEEDS_INFO",
    "high_risk": "Risk provider returns HIGH -> MANUAL_REVIEW (reviewer may approve)",
    "risk_decline": "Risk provider declines -> REJECTED",
    "provider_outage": "PAN provider returns 503 on first call -> retry with backoff -> continues",
    "provider_down": "Bank provider never answers -> retries exhausted -> dead letter + MANUAL_REVIEW",
    "merchant_id_fail_once": "Approved, merchant-ID creation fails once -> retried -> ACTIVE (one merchant only)",
    "merchant_id_fail_always": "Approved, merchant-ID creation keeps failing -> dead letter",
    "duplicate_application": "Re-uses PAN/GSTIN of the most recent application -> MANUAL_REVIEW",
}


def _pan(r):
    return "".join(r.choices(string.ascii_uppercase, k=5)) + "".join(r.choices(string.digits, k=4)) + r.choice(string.ascii_uppercase)


def gstin_for(pan: str, r=random, state="29") -> str:
    """Structurally valid GSTIN (embeds the PAN). The real checksum character is NOT computed."""
    return f"{state}{pan}1Z{r.choice(string.digits)}"


def build(name: str, rng: random.Random | None = None, base_form: dict | None = None) -> dict:
    if name not in SCENARIOS:
        raise KeyError(name)
    r = rng or random.Random()
    pan = _pan(r)
    legal = f"{r.choice(_A)} {r.choice(_B)} Private Limited"
    gstin = gstin_for(pan, r)
    acc = "".join(r.choices(string.digits, k=12))
    ifsc = "HDFC0" + "".join(r.choices(string.digits, k=6))
    if name == "duplicate_application" and base_form:
        pan, gstin, legal = base_form["pan"], base_form["gstin"], base_form["legal_name"]
    holder = legal

    form = {
        "legal_name": legal, "business_type": "PRIVATE_LIMITED", "pan": pan, "gstin": gstin,
        "address": "12 MG Road, Bengaluru, Karnataka 560001",
        "contact": {"email": f"finance@{legal.split()[0].lower()}.example", "phone": "9" + "".join(r.choices(string.digits, k=9))},
        "bank": {"account_number": acc, "ifsc": ifsc, "account_holder": holder},
        "business_category": "Retail", "expected_monthly_volume_inr": 2_500_000,
    }
    docs = [
        {"doc_type": "PAN_CARD", "filename": "pan.jpg", "content_type": "text/plain",
         "text": f"INCOME TAX DEPARTMENT\nPermanent Account Number: {pan}\nName: {legal.upper()}\n"},
        # structured payload, e.g. from an upstream OCR service
        {"doc_type": "GST_CERT", "filename": "gst.pdf", "content_type": "application/json",
         "fields": {"gstin": gstin, "legal_name": legal}},
        {"doc_type": "BANK_PROOF", "filename": "cheque.png", "content_type": "text/plain",
         "text": f"HDFC Bank - Cancelled Cheque\nAccount Holder: {holder}\nAccount Number: {acc}\nIFSC: {ifsc}\n"},
    ]
    sim: dict = {}

    if name == "gst_not_found":
        sim = {"gst": "not_found_once"}
    elif name == "bank_timeout_late_callback":
        sim = {"bank": "timeout_late", "duplicate_callbacks": 3}
    elif name == "duplicate_callbacks":
        sim = {"duplicate_callbacks": 3}
    elif name == "blurred_document":
        docs[0]["scan_quality"] = 0.3
    elif name == "document_conflict":
        docs[2]["text"] = docs[2]["text"].replace(holder, "Globex Holdings LLP")
    elif name == "low_confidence_scan":
        docs[0]["scan_quality"] = 0.62
    elif name == "missing_document":
        docs = [d for d in docs if d["doc_type"] != "GST_CERT"]
    elif name == "high_risk":
        sim = {"risk": "high"}
    elif name == "risk_decline":
        sim = {"risk": "decline"}
    elif name == "provider_outage":
        sim = {"pan": "outage_once"}
    elif name == "provider_down":
        sim = {"bank": "timeout_always"}
    elif name == "merchant_id_fail_once":
        sim = {"merchant_id": "fail_once"}
    elif name == "merchant_id_fail_always":
        sim = {"merchant_id": "fail_always"}
    return {**form, "documents": docs, "simulate": sim}


def blurred_replacement(payload: dict) -> dict:
    """A clear re-upload of the PAN card for a `blurred_document` application."""
    return {"doc_type": "PAN_CARD", "filename": "pan_rescan.jpg", "content_type": "text/plain",
            "text": f"INCOME TAX DEPARTMENT\nPermanent Account Number: {payload['pan']}\nName: {payload['legal_name'].upper()}\n"}
