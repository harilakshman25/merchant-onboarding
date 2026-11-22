"""Explicit application states and the only legal transitions between them."""
from enum import Enum


class S(str, Enum):
    SUBMITTED = "SUBMITTED"
    DOCUMENT_PROCESSING = "DOCUMENT_PROCESSING"   # extraction + validation
    NEEDS_INFO = "NEEDS_INFO"                      # waiting on the merchant
    VERIFYING = "VERIFYING"                        # provider checks in flight / being evaluated
    MANUAL_REVIEW = "MANUAL_REVIEW"                # waiting on a human reviewer
    APPROVED = "APPROVED"                          # all configured checks passed (or reviewer approved)
    PROVISIONING = "PROVISIONING"                  # creating merchant ID / payment config
    PROVISIONING_FAILED = "PROVISIONING_FAILED"    # approved but internal system failed; retrying
    ACTIVE = "ACTIVE"                              # terminal: merchant ID created
    REJECTED = "REJECTED"                          # terminal


TRANSITIONS: dict[S, set[S]] = {
    S.SUBMITTED: {S.DOCUMENT_PROCESSING},
    S.DOCUMENT_PROCESSING: {S.NEEDS_INFO, S.MANUAL_REVIEW, S.VERIFYING},
    S.NEEDS_INFO: {S.DOCUMENT_PROCESSING, S.REJECTED},
    # any data change (e.g. new bank details) re-enters DOCUMENT_PROCESSING
    S.VERIFYING: {S.NEEDS_INFO, S.MANUAL_REVIEW, S.APPROVED, S.REJECTED, S.DOCUMENT_PROCESSING},
    S.MANUAL_REVIEW: {S.VERIFYING, S.NEEDS_INFO, S.APPROVED, S.REJECTED, S.DOCUMENT_PROCESSING},
    S.APPROVED: {S.PROVISIONING, S.VERIFYING, S.DOCUMENT_PROCESSING},
    S.PROVISIONING: {S.ACTIVE, S.PROVISIONING_FAILED},
    S.PROVISIONING_FAILED: {S.PROVISIONING, S.VERIFYING, S.DOCUMENT_PROCESSING},
    S.ACTIVE: set(),
    S.REJECTED: set(),
}

TERMINAL = {S.ACTIVE, S.REJECTED}


class IllegalTransition(Exception):
    pass


def can(frm: str, to: str) -> bool:
    return S(to) in TRANSITIONS[S(frm)]


def assert_can(frm: str, to: str) -> None:
    if not can(frm, to):
        raise IllegalTransition(f"{frm} -> {to} is not allowed")
