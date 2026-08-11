"""Thresholds and intervals — configuration, not code (design §8.2).

Design §8.2 is explicit: "Thresholds such as 0.8 are configuration, not code. They live in
`decision-engine/config`, and both the value in force and the rule that fired are recorded on
the case (R4.9) — so a past decision stays explainable after a threshold changes."

`CONFIG_VERSION` is stamped onto every `TierDecision`. Without it, re-running an old case after
a threshold change would silently produce a different answer with no way to tell why, which
defeats the audit obligation rather than merely inconveniencing it. **Bump it whenever any value
below changes.**
"""

from __future__ import annotations

from models import RiskTier

#: Bump on ANY change to the values in this module.
CONFIG_VERSION = "2026-08-09.1"

#: design §8.1 / §8.2 — below this, document confidence is treated as weak.
DOCUMENT_CONFIDENCE_THRESHOLD = 0.8

#: R29.6 — identity attributes shared across cases above this count suggest synthetic identity.
SHARED_ATTRIBUTE_THRESHOLD = 3

#: R30.2 / D-05 — liveness attempts before routing to a human. Explicitly NOT a lockout.
LIVENESS_FAILURE_LIMIT = 3

#: R29.1 — no provisional account below this age.
MINIMUM_AGE = 18

#: R4.7 — tier maps to a fixed action and re-KYC interval. `None` for PROHIBITED is deliberate:
#: there is no interval because there is no ongoing relationship to review.
TIER_ACTIONS: dict[RiskTier, dict[str, str | None]] = {
    RiskTier.LOW: {
        "action": "Lift all restrictions, standard monitoring",
        "re_kyc_interval": "annual",
    },
    RiskTier.MEDIUM: {
        "action": "Lift transfers, retain deposit ceiling",
        "re_kyc_interval": "6_months",
    },
    RiskTier.HIGH: {
        "action": "Mandatory manual review + EDD",
        "re_kyc_interval": "quarterly",
    },
    RiskTier.PROHIBITED: {
        "action": "Freeze and reject",
        "re_kyc_interval": None,
    },
}


def thresholds_in_force() -> dict[str, object]:
    """The snapshot recorded on every decision (R4.9)."""
    return {
        "document_confidence_threshold": DOCUMENT_CONFIDENCE_THRESHOLD,
        "shared_attribute_threshold": SHARED_ATTRIBUTE_THRESHOLD,
        "liveness_failure_limit": LIVENESS_FAILURE_LIMIT,
        "minimum_age": MINIMUM_AGE,
        "config_version": CONFIG_VERSION,
    }
