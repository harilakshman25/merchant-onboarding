"""Document intelligence: turn a document payload into fields with per-field confidence.

Supported inputs (in order of preference):
  * `fields`          structured payload (e.g. from an upstream OCR/DigiLocker-style service)
  * `text`            OCR'd text; parsed with label-anchored regexes
  * `content_base64`  + content_type: text-layer PDFs via pypdf; images / scans via an optional
                      multimodal LLM adapter (needs ANTHROPIC_API_KEY; UNTESTED in this repo).
                      With no extractor available the doc is routed to human review.

`scan_quality` (0..1) simulates a blur / legibility detector. Below BLUR_THRESHOLD the doc is
UNREADABLE and the merchant is asked to re-upload; between BLUR and 0.8 confidences are scaled
down so borderline scans fall below CONF_THRESHOLD and go to a human.
"""
import base64
import hashlib
import io
import json
import os
import re

from . import config

DOC_TYPES = ("PAN_CARD", "GST_CERT", "BANK_PROOF")

EXPECTED = {
    "PAN_CARD": ["pan", "name"],
    "GST_CERT": ["gstin", "legal_name"],
    "BANK_PROOF": ["account_number", "ifsc", "account_holder"],
}

_PATTERNS = {
    "PAN_CARD": {
        "pan": r"\b([A-Z]{5}[0-9]{4}[A-Z])\b",
        "name": r"(?<![A-Za-z])Name\s*[:\-]\s*([^\n]+)",
    },
    "GST_CERT": {
        "gstin": r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b",
        "legal_name": r"Legal Name[^:\n]*[:\-]\s*([^\n]+)",
    },
    "BANK_PROOF": {
        "account_number": r"A/?c(?:count)?\.?\s*(?:No\.?|Number)\s*[:\-]\s*(\d{9,18})",
        "ifsc": r"IFSC(?:\s*Code)?\s*[:\-]\s*([A-Z]{4}0[A-Z0-9]{6})",
        "account_holder": r"(?:Account Holder|Beneficiary)(?:\s*Name)?\s*[:\-]\s*([^\n]+)",
    },
}
_BASE_TEXT_CONF = 0.95
_STRUCTURED_CONF = 0.98


def _norm(key: str, val):
    if val is None:
        return None
    s = str(val).strip()
    if key in ("pan", "gstin", "ifsc"):
        return s.upper().replace(" ", "")
    if key == "account_number":
        return re.sub(r"\D", "", s)
    return re.sub(r"\s+", " ", s)


def _from_text(doc_type: str, text: str) -> dict:
    out = {}
    for field, pat in _PATTERNS[doc_type].items():
        m = re.search(pat, text, re.I if field not in ("pan", "gstin") else 0)
        if m:
            out[field] = {"value": _norm(field, m.group(1)), "confidence": _BASE_TEXT_CONF}
    return out


def _pdf_text(raw: bytes) -> str | None:
    try:
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages) or None
    except Exception:
        return None


def _llm_extract(doc_type: str, content_type: str, b64: str) -> dict | None:
    """Optional multimodal path. Returns None when not configured / on any failure."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic

        block = "document" if content_type == "application/pdf" else "image"
        prompt = (
            f"This is a {doc_type}. Extract these fields: {EXPECTED[doc_type]}. Return ONLY JSON of the form "
            '{"field": {"value": "...", "confidence": 0.0-1.0}}. Use null for unreadable fields and lower '
            "confidence for blurry or partially legible values. Do not guess."
        )
        resp = anthropic.Anthropic().messages.create(
            model=os.environ.get("EXTRACTION_MODEL", "claude-sonnet-5-5"),
            max_tokens=600,
            messages=[{"role": "user", "content": [
                {"type": block, "source": {"type": "base64", "media_type": content_type, "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        data = json.loads(re.search(r"\{.*\}", resp.content[0].text, re.S).group(0))
        return {k: {"value": _norm(k, v["value"]), "confidence": float(v.get("confidence", 0.5))}
                for k, v in data.items() if isinstance(v, dict) and v.get("value")}
    except Exception:
        return None


def extract(doc: dict) -> dict:
    """Return {doc_type, method, status, quality, fields, issues, sha256}.
    status: OK | LOW_CONFIDENCE | UNREADABLE"""
    dtype = doc.get("doc_type")
    quality = float(doc.get("scan_quality", 1.0))
    issues: list[dict] = []
    fields: dict = {}
    method = "none"
    sha_src = b""

    if doc.get("fields"):
        method = "structured"
        sha_src = json.dumps(doc["fields"], sort_keys=True).encode()
        for k, v in doc["fields"].items():
            val, conf = (v["value"], float(v.get("confidence", _STRUCTURED_CONF))) if isinstance(v, dict) else (v, _STRUCTURED_CONF)
            fields[k] = {"value": _norm(k, val), "confidence": conf}
    elif doc.get("text") is not None:
        method = "text_regex"
        sha_src = doc["text"].encode()
        fields = _from_text(dtype, doc["text"])
    elif doc.get("content_base64"):
        raw = base64.b64decode(doc["content_base64"])
        sha_src = raw
        ctype = doc.get("content_type", "")
        text = _pdf_text(raw) if ctype == "application/pdf" else None
        if text:
            method = "pdf_text_layer"
            fields = _from_text(dtype, text)
        else:
            fields = _llm_extract(dtype, ctype, doc["content_base64"]) or {}
            method = "multimodal_llm" if fields else "none"
            if not fields:
                issues.append({"code": "NO_EXTRACTOR", "message": "No extractor available for this file; needs human review"})

    # simulated legibility: scale confidence for soft scans
    if quality < 1.0:
        scale = min(1.0, quality / 0.8)
        for f in fields.values():
            f["confidence"] = round(f["confidence"] * scale, 3)

    expected = EXPECTED.get(dtype, [])
    for k in expected:
        if k not in fields:
            issues.append({"code": "FIELD_NOT_FOUND", "field": k, "message": f"Could not find '{k}'"})

    if quality < config.BLUR_THRESHOLD:
        status = "UNREADABLE"
        issues.insert(0, {"code": "DOCUMENT_BLURRED", "message": f"Scan quality {quality:.2f} is below {config.BLUR_THRESHOLD}"})
    elif not any(k in fields for k in expected) and method != "none":
        status = "UNREADABLE"
        issues.insert(0, {"code": "NO_FIELDS_EXTRACTED", "message": "Nothing recognisable could be extracted"})
    elif any(i["code"] == "NO_EXTRACTOR" for i in issues) or any(
        fields.get(k, {}).get("confidence", 0) < config.CONF_THRESHOLD for k in expected
    ):
        status = "LOW_CONFIDENCE"
    else:
        status = "OK"

    return {
        "doc_type": dtype, "method": method, "status": status, "quality": quality,
        "fields": fields, "issues": issues, "sha256": hashlib.sha256(sha_src).hexdigest(),
    }
