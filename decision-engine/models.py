"""Types for the decision engine — Phase 6, task 6.1.

Pure data. No model, no network, no I/O anywhere in this package (design §8).

Every dataclass is `kw_only`. That is not style. An earlier positional-argument bug silently
corrupted a sanctions ground-truth record (MED-0004) by shifting fields one position, and the
data still validated because the types matched. Keyword-only construction makes that class of
error impossible rather than merely unlikely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RiskTier(Enum):
    """R4. Both tiers share this scale so preliminary and full are comparable (R4.4).

    `PROHIBITED` is a **risk tier**, not an outcome. Design §8.4 is explicit that a confirmed
    screening hit blocks the provisional account and routes for compliance handling rather than
    expressing a judgement about the person. The mapping from this tier to an outcome lives in
    `outcomes.py`, where the tension with R4.3 is resolved and recorded.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    PROHIBITED = "PROHIBITED"


class Phase1Outcome(Enum):
    """R3.1 — exactly these four, and never a `FULL_APPROVE_*` (R3.3)."""

    PROVISIONAL_APPROVE_RESTRICTED = "PROVISIONAL_APPROVE_RESTRICTED"
    STEP_UP = "STEP_UP"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECT = "REJECT"


class Phase2Outcome(Enum):
    """R3.2 — exactly these five."""

    FULL_APPROVE_STANDARD = "FULL_APPROVE_STANDARD"
    FULL_APPROVE_ENHANCED_MONITORING = "FULL_APPROVE_ENHANCED_MONITORING"
    RESTRICT_FURTHER = "RESTRICT_FURTHER"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECT = "REJECT"


class PepStatus(Enum):
    """R4.5."""

    SELF = "SELF"
    ASSOCIATE = "ASSOCIATE"
    NONE = "NONE"


class Severity(Enum):
    """Adverse-media severity. Ordered, so `>= HIGH` in design §8.2 is meaningful."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank >= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank > other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank <= other.rank

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank < other.rank


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.NONE: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class MatchStrength(Enum):
    """Screening hit strength. `CONFIRMED` is the only one that prohibits (design §8.2).

    `POSSIBLE` and `PROBABLE` both route to manual review (R27.5) — the name-alone-never-confirms
    rule that the live pre-screen agent was observed to honour in production.
    """

    CONFIRMED = "CONFIRMED"
    PROBABLE = "PROBABLE"
    POSSIBLE = "POSSIBLE"
    NO_MATCH = "NO_MATCH"


class ResultStatus(Enum):
    """Whether an agent actually produced a usable finding.

    This distinction is the whole reason the engine cannot simply read findings:

    * `OK` — the agent ran and returned content.
    * `INVALID` — the agent ran and the *input* was unusable in a permanent way. A document
      image that cannot be read is INVALID, not UNAVAILABLE: retrying will not help, and the
      applicant can fix it, so it routes to `STEP_UP`.
    * `UNAVAILABLE` — the agent or its dependency failed transiently. The applicant cannot fix
      this, so it must never become a `STEP_UP` demand; it routes to `MANUAL_REVIEW`.

    **An empty result is `UNAVAILABLE`, never a clean finding.** A harness invocation was
    observed returning `SUCCEEDED` with `Content: []`. Read naively, an empty screening result
    means "no sanctions hits found", which would approve exactly the applicant who should be
    stopped. `agent_result_status()` in `normalise.py` enforces this.
    """

    OK = "OK"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, kw_only=True)
class ScreeningHit:
    """One sanctions / PEP / adverse-media hit."""

    list_id: str
    strength: MatchStrength
    matched_on: tuple[str, ...] = ()
    score: float | None = None


@dataclass(frozen=True, kw_only=True)
class VelocitySignals:
    """Device and velocity signals (R4.5)."""

    critical: bool = False
    detail: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class PreliminaryInputs:
    """design §8.1. Every field is read by a rule — see the note in §8.2 about why that matters."""

    jurisdiction_risk: RiskTier
    pre_screen_matched: bool
    document_confidence: float
    velocity_signals: VelocitySignals
    product_applied: str


@dataclass(frozen=True, kw_only=True)
class FullInputs:
    """design §8.2 / R4.5. All seven inputs appear in the rule set."""

    jurisdiction_risk: RiskTier
    pep_status: PepStatus
    adverse_media_severity: Severity
    document_confidence: float
    velocity_signals: VelocitySignals
    product_applied: str
    screening_hits: tuple[ScreeningHit, ...] = ()


@dataclass(frozen=True, kw_only=True)
class TierDecision:
    """A tier plus the evidence for it.

    R4.9 requires recording every input value **and the rule that produced the tier**, and R4.10
    requires 1,000 identical results from identical inputs. Returning a bare enum would satisfy
    neither: a past decision must stay explainable after a threshold changes, so the threshold
    value in force is captured here rather than looked up later.
    """

    tier: RiskTier
    rule: str
    inputs: dict[str, object]
    thresholds: dict[str, object]
    config_version: str


@dataclass(frozen=True, kw_only=True)
class EdgeVerdict:
    """One matched edge case from design §8.4 (task 6.9).

    Carried separately from the tier because these are *mandated*: R28/R29/R30 name the result
    directly, so they are not inputs to a risk score. Design §8.4 also requires they live in this
    pure layer and never in a system prompt — an edge case handled by prompt instruction is an
    edge case handled probabilistically.

    Six of the fifteen rows mandate a **directive** rather than an outcome — "apply R12.5",
    "resumable for the window", "dedup admits exactly one", "show the existing decision", "link
    the prior case", "record all three countries". Forcing those into one of the outcome enums
    would misrepresent them: they instruct the surrounding machinery, they do not decide the
    application. So `outcome` is optional and `directive` carries the rest.
    """

    case_id: str
    requirement: str
    outcome: Phase1Outcome | Phase2Outcome | None = None
    directive: str | None = None
    reason_code: str | None = None
    explanation: str | None = None
    required_item: str | None = None
    notify_compliance: bool = False
    signals: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome is None and self.directive is None:
            raise ValueError(
                f"edge case {self.case_id} must carry an outcome or a directive")


@dataclass(frozen=True, kw_only=True)
class OutcomeDecision:
    """An outcome plus why. R3.6 and R3.7 both require more than the enum.

    `required_item` carries the specific thing a `STEP_UP` asks for — R3.6 requires naming it,
    and a step-up that does not say what it wants is indistinguishable from a rejection.
    `reason_code` and `explanation` are mandatory for `REJECT` (R3.7).
    """

    outcome: Phase1Outcome | Phase2Outcome
    rule: str
    reason_code: str | None = None
    explanation: str | None = None
    required_item: str | None = None
    notify_compliance: bool = False
    tier: TierDecision | None = None
    edge_cases: tuple[str, ...] = ()
    recorded_signals: dict[str, object] = field(default_factory=dict)
