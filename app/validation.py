"""Completeness + consistency validation of the form against extracted documents.

Issues have a severity:
  BLOCKING  the merchant must fix / resubmit something  -> NEEDS_INFO
  REVIEW    a human should look (conflict, low confidence) -> MANUAL_REVIEW
"""
import difflib
import re

from . import config

PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
ACC_RE = re.compile(r"^\d{9,18}$")

REQUIRED_DOCS = ("PAN_CARD", "GST_CERT", "BANK_PROOF")
REQUIRED_FORM = [("legal_name",), ("pan",), ("gstin",), ("address",), ("contact", "email"),
                 ("bank", "account_number"), ("bank", "ifsc"), ("bank", "account_holder")]

# (doc field, form path, kind)
COMPARE = {
    "PAN_CARD": [("pan", ("pan",), "id"), ("name", ("legal_name",), "name")],
    "GST_CERT": [("gstin", ("gstin",), "id"), ("legal_name", ("legal_name",), "name")],
    "BANK_PROOF": [("account_number", ("bank", "account_number"), "id"), ("ifsc", ("bank", "ifsc"), "id"),
                   ("account_holder", ("bank", "account_holder"), "name")],
}


def get(d, path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def norm_name(s: str | None) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\bprivate\b", "pvt", s)
    s = re.sub(r"\blimited\b", "ltd", s)
    return re.sub(r"\s+", " ", s).strip()


def name_similarity(a: str | None, b: str | None) -> float:
    return difflib.SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


def _same_id(a, b) -> bool:
    return re.sub(r"\W", "", str(a or "")).upper() == re.sub(r"\W", "", str(b or "")).upper()


def _i(sev, code, field, msg, **extra):
    return {"severity": sev, "code": code, "field": field, "message": msg, **extra}


def validate(form: dict, extractions: list[dict]) -> list[dict]:
    issues: list[dict] = []
    by_type = {e["doc_type"]: e for e in extractions}

    for path in REQUIRED_FORM:
        if not get(form, path):
            issues.append(_i("BLOCKING", "MISSING_FIELD", ".".join(path), f"Missing required field '{'.'.join(path)}'"))
    for t in REQUIRED_DOCS:
        if t not in by_type:
            issues.append(_i("BLOCKING", "MISSING_DOCUMENT", t, f"Missing required document {t}"))

    pan, gstin = str(form.get("pan") or "").upper(), str(form.get("gstin") or "").upper()
    ifsc, acc = str(get(form, ("bank", "ifsc")) or "").upper(), str(get(form, ("bank", "account_number")) or "")
    if pan and not PAN_RE.match(pan):
        issues.append(_i("BLOCKING", "INVALID_FORMAT", "pan", "PAN must look like ABCDE1234F"))
    if gstin and not GSTIN_RE.match(gstin):
        issues.append(_i("BLOCKING", "INVALID_FORMAT", "gstin", "GSTIN must be 15 characters in the standard format"))
    if ifsc and not IFSC_RE.match(ifsc):
        issues.append(_i("BLOCKING", "INVALID_FORMAT", "bank.ifsc", "IFSC must look like ABCD0123456"))
    if acc and not ACC_RE.match(acc):
        issues.append(_i("BLOCKING", "INVALID_FORMAT", "bank.account_number", "Account number must be 9-18 digits"))
    if PAN_RE.match(pan) and GSTIN_RE.match(gstin) and gstin[2:12] != pan:
        issues.append(_i("BLOCKING", "GSTIN_PAN_MISMATCH", "gstin", "GSTIN does not embed the declared PAN"))

    holder, legal = get(form, ("bank", "account_holder")), form.get("legal_name")
    if holder and legal and name_similarity(holder, legal) < config.NAME_MATCH_OK:
        issues.append(_i("REVIEW", "ACCOUNT_HOLDER_DIFFERS", "bank.account_holder",
                         "Bank account holder differs from legal name", score=round(name_similarity(holder, legal), 2)))

    for t, e in by_type.items():
        if e["status"] == "UNREADABLE":
            why = "blurred / illegible" if any(i["code"] == "DOCUMENT_BLURRED" for i in e["issues"]) else "unreadable"
            issues.append(_i("BLOCKING", "DOCUMENT_UNREADABLE", t, f"{t} is {why}; please upload a clearer copy"))
            continue
        for k in [f for f in ("pan", "name", "gstin", "legal_name", "account_number", "ifsc", "account_holder") if f in e["fields"]]:
            c = e["fields"][k]["confidence"]
            if c < config.CONF_THRESHOLD:
                issues.append(_i("REVIEW", "DOC_LOW_CONFIDENCE", f"{t}.{k}", f"Low extraction confidence ({c:.2f}) for {t}.{k}", confidence=c))
        if e["status"] == "LOW_CONFIDENCE" and not e["fields"]:
            issues.append(_i("REVIEW", "DOC_LOW_CONFIDENCE", t, f"{t} could not be machine-read"))
        for dfield, path, kind in COMPARE.get(t, []):
            if dfield not in e["fields"]:
                continue
            dv, fv = e["fields"][dfield]["value"], get(form, path)
            if not fv:
                continue
            if kind == "id":
                ok, score = _same_id(dv, fv), None
            else:
                score = round(name_similarity(dv, fv), 2)
                ok = score >= config.NAME_MATCH_OK
            if not ok:
                issues.append(_i("REVIEW", "DOC_FORM_CONFLICT", f"{t}.{dfield}",
                                 f"{t} {dfield} does not match the application form", score=score))
    return issues
