"""Central configuration. Everything is overridable via environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ONBOARDING_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("ONBOARDING_DB", DATA_DIR / "onboarding.db"))

# Shared secret used to HMAC-sign provider callbacks (simulated webhooks).
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-webhook-secret")
# Operator / admin endpoints require this key in the X-Operator-Key header.
OPERATOR_API_KEY = os.environ.get("OPERATOR_API_KEY", "dev-operator-key")

# --- Checks that must pass before approval (configurable) -------------------
REQUIRED_CHECKS = tuple(
    c.strip() for c in os.environ.get("REQUIRED_CHECKS", "pan,gst,bank,risk").split(",") if c.strip()
)
# A check is only launched once its dependencies SUCCEEDED.
# PAN first (GSTIN embeds it); bank is independent; risk is the final gate.
DEPENDENCIES = {"pan": [], "bank": [], "gst": ["pan"], "risk": ["pan", "gst", "bank"]}

# --- Resilience policy -------------------------------------------------------
CHECK_TIMEOUT_S = 30          # a request with no callback by then is TIMED_OUT
MAX_ATTEMPTS = 3              # per input version of a check
RETRY_BACKOFF_S = 5           # delay before retry n = BACKOFF * 2**(n-1)
PROVISION_MAX_ATTEMPTS = 3

# --- Document intelligence thresholds ---------------------------------------
CONF_THRESHOLD = 0.80         # field confidence below this -> human review
BLUR_THRESHOLD = 0.50         # scan quality below this -> unreadable, re-upload
NAME_MATCH_OK = 0.85          # similarity >= this counts as the same name
DUPLICATE_NAME_SIMILARITY = 0.92

# Provider failure reasons a human may look at (everything else = merchant must fix input)
REVIEW_FAILURE_REASONS = {"NAME_MISMATCH"}

SIM_CALLBACK_DELAY_S = 1.0    # default simulated provider latency
