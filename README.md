# Autonomous Merchant Onboarding Platform — working vertical slice

A payments-company style onboarding pipeline: documents in → field extraction + validation → simulated
PAN / GST / bank / risk checks (asynchronous, signed callbacks) → explicit state machine → human review queue
→ approval → simulated merchant-ID creation, with a full audit trail and an operator console.

Python 3.11+ · FastAPI · SQLite · no external services (everything external is simulated and clearly marked).

## Getting started

Use Python 3.11 or newer. From the project root:

On Linux or macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run the tests

The tests use an isolated in-memory SQLite database and do not need a running server:

```bash
python -m pytest -q
```

The suite currently contains 24 tests and completes in about 1 second. To run one test or include output:

```bash
python -m pytest tests/test_flow.py::test_happy_path_straight_through
python -m pytest tests/test_flow.py -vv
```

## Run the application

Start the API and operator console in the first terminal:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

- Operator console: http://127.0.0.1:8000
- API documentation: http://127.0.0.1:8000/docs

Operator and admin endpoints use `X-Operator-Key: dev-operator-key`; the console adds this header automatically.

With the server running, open a second activated terminal and run the scripted end-to-end demo:

```bash
python scripts/demo.py
```

For a server on a different address:

```bash
python scripts/demo.py --base http://127.0.0.1:8000
```

Operator/admin endpoints need `X-Operator-Key: dev-operator-key` (the console sends it automatically).
In the console, pick a scenario → **Submit sample**; use **+35s** to jump past provider timeouts instead of waiting.

## The minimum demonstration (what `scripts/demo.py` prints)
* **A** – bank callback arrives *after* our 30 s timeout while every callback is delivered 3×: timeout → retry
  scheduled → late callback accepted → retry skipped → duplicates ignored → merchant ID created once.
* **B** – PAN verifies, GST fails → `NEEDS_INFO` with a precise ask → merchant corrects via `PATCH` → only GST is
  re-verified (PAN result reused) → `ACTIVE`.
* **C** – HIGH risk → review queue → reviewer approves → merchant ID.

## Architecture

```
 merchant ──POST /applications──► submit ─► DOCUMENT_PROCESSING ─► extraction ─► validation ─► duplicate check
                                                                                    │
            ┌───────────── NEEDS_INFO ◄── blocking issues (missing/blurred/invalid) ┤
            │              MANUAL_REVIEW ◄─ conflicts / low confidence / duplicate ─┤
            │                                                                       ▼
            │                                     VERIFYING: pan ─► gst ─┐
            │                                                 bank ──────┼─► risk (final gate)
            │            signed callbacks ◄── provider simulator          │
            ▼                                                             ▼
        resubmit/PATCH                    all required checks pass ─► APPROVED ─► PROVISIONING ─► ACTIVE (merchant ID)
                                                                            └─ failure ► PROVISIONING_FAILED ─ retry/backoff ─► DLQ
```
| Module | Responsibility |
|---|---|
| `states.py` | The 10 states and the *only* legal transitions; anything else raises `IllegalTransition`. |
| `workflow.py` | Orchestrator: routing, dependency-ordered check dispatch, callbacks, timeouts, retries, review, provisioning, audit, metrics. |
| `extraction.py` | Fields + per-field confidence from structured payloads, OCR text, text-layer PDFs; optional multimodal LLM adapter. |
| `validation.py` | Completeness, formats (PAN/GSTIN/IFSC/account), GSTIN-embeds-PAN, doc↔form consistency, fuzzy name match. |
| `providers.py` | Simulated PAN/GST/bank/risk providers (async, signed callbacks, fault injection) + internal merchant-ID service. |
| `security.py` | Encryption at rest (Fernet), masking, keyed fingerprints for dedupe, HMAC webhook signing. |
| `main.py`, `static/index.html` | REST API, background worker (timeouts/retries/callback delivery), operator console. |

### Key design decisions
* **Input-hash versioning of checks.** Every verification request records a keyed hash of exactly the inputs it
  verified. A check result only counts if its hash equals the *current* inputs. This one mechanism gives: selective
  re-verification (fix GSTIN → PAN not re-run), stale-callback rejection (old bank details), and safe
  invalidation after a bank change.
* **Idempotent callbacks.** `event_id` is a primary key; replays are no-ops (audited, counted). Callbacks for timed-out
  requests are *accepted* (status `TIMED_OUT`→result, flagged `late`) because the provider's answer is still true
  for those inputs; a later retry is skipped if the check is already resolved. Unknown request IDs → dead letter;
  bad signature → 401.
* **Single approval gate.** `can_approve()` guards both approval and merchant-ID creation, so even a reviewer cannot
  approve over a failed PAN/GST/bank check; a reviewer may only waive a MEDIUM/HIGH *risk band*. Provisioning
  re-checks the gate, so a bank change after approval sends the application back to verification instead of
  creating an ID on stale data.
* **Escalation vs. asking the merchant.** Things the merchant can fix (missing/blurred docs, bad formats, GSTIN not
  found) → `NEEDS_INFO`. Things a human must judge (doc/form conflict, low confidence, duplicates, name mismatch,
  high risk, provider outages, bank change after verification) → `MANUAL_REVIEW`.
* **Failure/retry policy.** Per check: 30 s timeout, up to 3 attempts, exponential backoff (5 s, 10 s); exhaustion →
  dead letter + review (reviewer can `retry`, or replay the DLQ item). Merchant-ID creation: 3 attempts with backoff
  → dead letter; creation is idempotent per application (deterministic ID + unique constraint).
* **Sensitive data.** PAN / GSTIN / account numbers are encrypted at rest, masked in every API response, audit entry
  and extraction view, and only fingerprinted (keyed HMAC) for duplicate detection. A test asserts raw values never
  appear in views/audit. Operator endpoints require an API key; callbacks require a valid HMAC.

### Failure scenarios → where they're handled (all covered by `tests/test_flow.py`)
| Scenario | Behaviour | Sample (`simulate`) |
|---|---|---|
| PAN ok, GST fails | `NEEDS_INFO`, PAN result kept, only GST re-run after fix | `gst_not_found` |
| Same callback many times | exactly-once effect, duplicates audited | `duplicate_callbacks` |
| Callback after timeout | accepted as late, retry skipped | `bank_timeout_late_callback` |
| Bank changed after verification | old proof superseded → `NEEDS_INFO` → human review → bank+risk re-verified; stale callbacks ignored | test `bank_change…` |
| Approved but merchant ID fails | `PROVISIONING_FAILED`, backoff retries, DLQ + replay; never two merchants | `merchant_id_fail_once/always` |
| Blurred / conflicting document | blurred → re-upload; conflict → review | `blurred_document`, `document_conflict` |
| Provider outage / silence | retry w/ backoff → DLQ + review | `provider_outage`, `provider_down` |
| Duplicate applicant | same PAN/GSTIN/account or near-identical name → review | `duplicate_application` |

## API (see `/docs`)
`POST /applications` · `PATCH /applications/{id}` · `GET /applications/{id}/status` (applicant-safe) ·
`POST /callbacks/{provider}` (HMAC `X-Signature`) · operator: `GET /applications[/{id}]`, `GET /review-queue`,
`POST /review-queue/{id}/resolve` (`proceed|approve|reject|request_info|retry`), `GET /dead-letters`,
`POST /dead-letters/{id}/replay`, `GET /metrics`, `POST /admin/clock/advance`, `POST /admin/samples/{name}`.

Application payload: `legal_name, pan, gstin, address, contact{email,phone}, bank{account_number,ifsc,account_holder},
business_type, expected_monthly_volume_inr, documents[], simulate{}`. A document is
`{doc_type: PAN_CARD|GST_CERT|BANK_PROOF, text | fields | content_base64+content_type, scan_quality?}`.

## Assumptions, mocks and limitations (please read)
* **All verification providers are simulated.** `simulate` in the payload is a test-only control channel and would be
  removed/ignored in a real deployment. GSTIN structure is validated but the GSTIN checksum character is not.
* **Extraction is real for text / structured / text-layer-PDF input.** Scanned images and image-only PDFs need the
  optional multimodal adapter (`ANTHROPIC_API_KEY` + `pip install anthropic`); that code path is **untested** here
  (no key in the build environment). Without it such documents are routed to human review. Blur detection is
  *simulated* by the `scan_quality` hint — there is no real image-sharpness measurement.
* **Single process.** One SQLite connection + one global lock serialise all mutations (this is what makes callback
  handling race-free). Not horizontally scalable; production would use Postgres + row locks/outbox and a real
  queue/Temporal-style workflow engine. Extraction runs synchronously inside `POST /applications`.
* **Timers** are a 0.5 s polling worker, not a durable scheduler; they survive restarts because jobs and deadlines
  are in the DB, but there is no leader election. The simulated provider outbox is also DB-backed.
* **No applicant authentication**: `POST/PATCH /applications` are open for the demo; a real system needs applicant
  identity, rate limits, upload scanning and size limits. Encryption key lives in `data/.enc_key` unless
  `ONBOARDING_ENC_KEY` is set (use a KMS in production). No retention/erasure policy implemented.
* **Post-activation changes** (e.g. bank change for an already ACTIVE merchant) are out of scope: `PATCH` returns 409.
* While one required check is still pending, a failure in another is held until all running checks settle so the
  merchant gets one complete list of problems (trade-off: slower feedback if a provider is slow).
* Review UI is deliberately plain. Reviewer identity is a free-text field; no RBAC / four-eyes rule.

## Configuration (env)
`REQUIRED_CHECKS` (default `pan,gst,bank,risk`), `OPERATOR_API_KEY`, `WEBHOOK_SECRET`, `ONBOARDING_DATA_DIR`,
`ONBOARDING_ENC_KEY`, `DISABLE_WORKER=1`. Thresholds/timeouts are in `app/config.py`.
