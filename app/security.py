"""Sensitive-data handling: encryption at rest, masking, blind-index fingerprints, webhook signing."""
import copy
import hashlib
import hmac
import json
import os
import re

from cryptography.fernet import Fernet

from . import config

_fernet: Fernet | None = None
_key: bytes | None = None


def _load_key() -> bytes:
    global _key
    if _key:
        return _key
    env = os.environ.get("ONBOARDING_ENC_KEY")
    if env:
        _key = env.encode()
        return _key
    p = config.DATA_DIR / ".enc_key"
    if p.exists():
        _key = p.read_bytes()
    else:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _key = Fernet.generate_key()
        p.write_bytes(_key)
        os.chmod(p, 0o600)
    return _key


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def seal(obj) -> str:
    return _f().encrypt(json.dumps(obj, sort_keys=True, default=str).encode()).decode()


def unseal(token: str):
    return json.loads(_f().decrypt(token.encode()))


def fingerprint(value, namespace: str = "") -> str | None:
    """Keyed hash used for duplicate detection / input versioning without storing raw values."""
    if value in (None, ""):
        return None
    k = hashlib.sha256(b"fp:" + _load_key()).digest()
    return hmac.new(k, f"{namespace}:{value}".encode(), hashlib.sha256).hexdigest()[:32]


# ---- masking ----------------------------------------------------------------
def mask_value(key: str, v):
    if not isinstance(v, str) or not v:
        return v
    if key in ("pan", "gstin", "account_number"):
        return "*" * max(len(v) - 4, 0) + v[-4:]
    if key == "phone":
        return "*" * max(len(v) - 3, 0) + v[-3:]
    if key == "email" and "@" in v:
        local, dom = v.split("@", 1)
        return local[:1] + "***@" + dom
    return v


def mask_obj(obj):
    """Deep-mask known sensitive keys. Safe to apply to anything leaving the trust boundary."""
    obj = copy.deepcopy(obj)

    def walk(o):
        if isinstance(o, dict):
            for k, v in list(o.items()):
                if isinstance(v, (dict, list)):
                    walk(v)
                else:
                    o[k] = mask_value(k, v)
        elif isinstance(o, list):
            for i in o:
                walk(i)

    walk(obj)
    return obj


def mask_extraction(ex: dict) -> dict:
    """Extraction results keep values under fields[name]["value"], so mask by field name."""
    ex = copy.deepcopy(ex)
    for k, f in (ex.get("fields") or {}).items():
        if isinstance(f, dict):
            f["value"] = mask_value(k, f.get("value"))
    return ex


# ---- webhook signing --------------------------------------------------------
def sign(raw: bytes) -> str:
    return hmac.new(config.WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def verify(raw: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(raw), signature or "")


_SENSITIVE_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b|\b\d{9,18}\b")


def looks_sensitive(text: str) -> bool:
    """Used by tests to assert audit/logs never contain raw PAN / account numbers."""
    return bool(_SENSITIVE_RE.search(text))
