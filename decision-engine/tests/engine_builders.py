"""Input builders shared across the decision-engine tests.

Kept out of the rule layer on purpose: `test_tiering.TestPurity` walks every `*.py` beside the
rules and rejects impure imports, and test scaffolding has no business in that scan either way.

Both builders take keyword overrides on top of a deliberately clean baseline, so each test states
only the field it is exercising. That keeps a rule change from silently altering what a test
thought it was holding constant.
"""

from __future__ import annotations

from models import (
    FullInputs,
    PepStatus,
    PreliminaryInputs,
    RiskTier,
    Severity,
    VelocitySignals,
)

CLEAN_VELOCITY = VelocitySignals(critical=False)
CRITICAL_VELOCITY = VelocitySignals(critical=True, detail=("6 applications in 1h",))


def prelim(**over) -> PreliminaryInputs:
    """A clean Phase 1 input set — tier LOW, outcome PROVISIONAL_APPROVE_RESTRICTED."""
    base = dict(
        jurisdiction_risk=RiskTier.LOW,
        pre_screen_matched=False,
        document_confidence=0.95,
        velocity_signals=CLEAN_VELOCITY,
        product_applied="current_account",
    )
    base.update(over)
    return PreliminaryInputs(**base)


def full(**over) -> FullInputs:
    """A clean Phase 2 input set — tier LOW, outcome FULL_APPROVE_STANDARD."""
    base = dict(
        jurisdiction_risk=RiskTier.LOW,
        pep_status=PepStatus.NONE,
        adverse_media_severity=Severity.NONE,
        document_confidence=0.95,
        velocity_signals=CLEAN_VELOCITY,
        product_applied="current_account",
        screening_hits=(),
    )
    base.update(over)
    return FullInputs(**base)
