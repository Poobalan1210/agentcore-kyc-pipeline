"""Single source of truth for jurisdiction and agent configuration.

Everything downstream — KMS keys, buckets, Gateways, harnesses, the router matrix — is
generated from these tables rather than restated. R12.8 requires the jurisdiction matrix to
be generated from code so it cannot silently go stale as Region and model availability shift.

Every model and Region choice here traces to a measured fact in
`.kiro/specs/kyc-90-seconds/requirements.md`. The AU row in particular is not a preference:
`apac.amazon.nova-2-lite-v1:0` does not exist (F-69), and `global.` carries no residency
guarantee (F-49), so AU cannot use Nova 2 Lite at all (R12.10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class InferenceOption(str, Enum):
    """The three options Bedrock actually offers (F-49). R12.11 requires naming which one
    each jurisdiction uses; "cross-region" on its own is not an answer a regulator accepts."""

    IN_REGION = "In-Region"
    GEO = "Geo Cross-Region"
    GLOBAL = "Global Cross-Region"


@dataclass(frozen=True)
class Jurisdiction:
    code: str
    name: str
    home_region: str
    liveness_region: str
    retention_years: int
    # Reasoning-capable model for the Compliance Risk agent, or None where no
    # residency-respecting option exists.
    reasoning_model: str | None
    reasoning_inference: InferenceOption | None
    # Text/multimodal workhorse used by the Phase 1 agents.
    workhorse_model: str
    workhorse_inference: InferenceOption
    fast_model: str
    notes: str = ""

    @property
    def liveness_is_cross_border(self) -> bool:
        return self.liveness_region != self.home_region

    @property
    def object_lock_years(self) -> int:
        """A single floor of 7 covers all four markets (B-12, B-13, D-09)."""
        return 7


#: R18.7/R18.9 define the reasoning-trace retention period as "case closure plus appeal
#: window" but the spec does not pin a number of days anywhere (checked requirements.md,
#: design.md; R7 the appeal loop itself is silent on a filing deadline). This is the same
#: category of gap as A-05/A-06/A-17 — an unmeasured assumption, not a decided requirement.
#:
#: 90 days is chosen as a conservative default: it comfortably exceeds every reviewed AML
#: appeal/dispute-style window (e.g. Reg E's 60-day error-resolution window, many bank
#: complaint-handling SLAs of 30-45 days) so a trace does not expire while an appeal
#: (R7) is still open, while remaining short enough that R18.9's "delete while preserving
#: the decision record" is a real data-minimisation act rather than a formality.
#:
#: Recorded here rather than silently hardcoded in the stack, so it is visible, traceable to
#: this comment, and trivially adjustable if Compliance sets a real number later.
REASONING_TRACE_RETENTION_DAYS = 90


JURISDICTIONS: tuple[Jurisdiction, ...] = (
    Jurisdiction(
        code="us",
        name="United States",
        home_region="us-east-1",
        liveness_region="us-east-1",
        retention_years=5,
        reasoning_model="us.amazon.nova-2-lite-v1:0",
        reasoning_inference=InferenceOption.GEO,
        workhorse_model="us.amazon.nova-lite-v1:0",
        workhorse_inference=InferenceOption.GEO,
        fast_model="us.amazon.nova-micro-v1:0",
        notes="Fully served by the US geo. Liveness in region.",
    ),
    Jurisdiction(
        code="uk",
        name="United Kingdom",
        home_region="eu-west-2",
        liveness_region="eu-west-1",
        retention_years=5,
        # No eu. profile is resolvable from eu-west-2 and there is no UK-only geo (F-21).
        # Rather than route UK identity data through global inference, drop to Nova Lite V1
        # In-Region and lose extended thinking. Same trade as AU, different cause.
        reasoning_model=None,
        reasoning_inference=None,
        workhorse_model="amazon.nova-lite-v1:0",
        workhorse_inference=InferenceOption.IN_REGION,
        fast_model="amazon.nova-micro-v1:0",
        notes=(
            "Face Liveness is not available in London, so a UK face is processed in "
            "Ireland (F-27). No UK-only geo profile exists for Nova 2 Lite."
        ),
    ),
    Jurisdiction(
        code="au",
        name="Australia",
        home_region="ap-southeast-2",
        liveness_region="ap-southeast-5",
        retention_years=7,
        # apac.amazon.nova-2-lite-v1:0 does not exist — "The provided model identifier is
        # invalid" (F-69). global. would work but gives no residency guarantee (F-49).
        reasoning_model=None,
        reasoning_inference=None,
        workhorse_model="apac.amazon.nova-lite-v1:0",
        workhorse_inference=InferenceOption.GEO,
        fast_model="apac.amazon.nova-micro-v1:0",
        notes=(
            "No APAC geo exists for Nova 2 Lite, so AU forgoes extended thinking rather "
            "than route through global inference (R12.10). Liveness runs in Malaysia, the "
            "nearest supported Region, not Sydney (F-27, D-02)."
        ),
    ),
    Jurisdiction(
        code="jp",
        name="Japan",
        home_region="ap-northeast-1",
        liveness_region="ap-northeast-1",
        retention_years=7,
        reasoning_model="jp.amazon.nova-2-lite-v1:0",
        reasoning_inference=InferenceOption.GEO,
        workhorse_model="apac.amazon.nova-lite-v1:0",
        workhorse_inference=InferenceOption.GEO,
        fast_model="apac.amazon.nova-micro-v1:0",
        notes="A jp. geo profile exists for Nova 2 Lite. Liveness in region. APTCP: 7 years.",
    ),
)

JURISDICTION_BY_CODE = {j.code: j for j in JURISDICTIONS}


class Phase(int, Enum):
    ONE = 1
    TWO = 2


@dataclass(frozen=True)
class Agent:
    key: str
    name: str
    phase: Phase
    role_suffix: str
    max_iterations: int
    purpose: str
    tools: tuple[str, ...] = field(default_factory=tuple)
    # Reasoning effort, or None. F-62: at "high" the service rejects the call unless
    # temperature and maxTokens are unset, so a high-effort agent cannot pin sampling.
    reasoning_effort: str | None = None

    @property
    def can_pin_sampling(self) -> bool:
        """False for **any** reasoning-enabled agent, not just `high` (F-86).

        F-62 established that at `high` effort the service rejects the call outright unless
        `temperature` and `maxTokens` are unset. `medium` is *accepted* — and then fails in a much
        worse way. Measured live: the Fraud agent at `medium` with `MaxTokens: 2048` produced
        **8,525 output tokens** and returned `Content: []` with `StopReason: timeout_exceeded`. It
        spent the entire output allowance on `<thinking>` and had nothing left for the answer.

        A hard rejection is a good failure — it stops the build. An empty successful response is
        the bad one: no error, no Catch, and a naive reader treats "no findings" as "no risk".

        AWS's own Nova troubleshooting guidance says to unset temperature, topP and maxToken for
        reasoning, and the same failure is documented for other reasoning models, where the advice
        is to lower the effort a step rather than raise the ceiling. So sampling is unpinned for
        every reasoning agent here.

        The cost is real and worth stating: R26.3 wants a pinned temperature for reproducibility,
        and a reasoning agent cannot have one. That is why no reasoning agent's output is ever the
        sole basis for an outcome — the pure decision engine is.
        """
        return self.reasoning_effort is None


AGENTS: tuple[Agent, ...] = (
    Agent("router", "kyc-jurisdiction-router", Phase.ONE, "Router", 5,
          "Classify the applicant into US/UK/AU/JP from document country."),
    Agent("documents", "kyc-document-analysis", Phase.ONE, "Documents", 10,
          "Extract and validate document fields.", ("doc_extract",)),
    Agent("identity", "kyc-identity", Phase.ONE, "Identity", 10,
          "Face match against the liveness reference frame.", ("face_compare",)),
    # Reasoning REMOVED after two measured failures (F-88). At `medium` this agent returned
    # `Content: []` twice: once with MaxTokens pinned at 2048 (8,525 output tokens, 55.7s) and
    # again with sampling unpinned (16,629 output tokens, **132 seconds**). Unpinning made it
    # worse — given a larger budget it simply thought for longer and still never answered.
    #
    # The cause is a mismatch between the task and the tool. Velocity and synthetic-identity
    # signals are pattern lookups the `velocity_check` tool performs; there is no chain of
    # reasoning to build, so the effort budget bought nothing and consumed the entire Phase 1
    # allowance. Compliance Risk keeps `high` effort in Phase 2, where deep screening genuinely
    # needs it and no clock is running (D-08).
    #
    # This sharpens DIV-5 rather than contradicting it: reasoning effort has to be justified per
    # agent, and applied to a mechanical task it produces a confident-looking empty result — the
    # most dangerous shape of failure in this system.
    Agent("fraud", "kyc-fraud", Phase.ONE, "Fraud", 12,
          "Velocity and synthetic-identity signals.", ("velocity_check",)),
    Agent("prescreen", "kyc-sanctions-prescreen", Phase.ONE, "PreScreen", 5,
          "Exact-match sanctions pre-screen only. Fail closed.", ("sanctions_exact",)),
    Agent("compliance", "kyc-compliance-risk", Phase.TWO, "ComplianceRisk", 12,
          "Fuzzy sanctions, PEP association, adverse media.",
          ("sanctions_fuzzy", "pep_lookup", "adverse_media"), "high"),
    Agent("notify", "kyc-customer-experience", Phase.TWO, "Notify", 8,
          "Applicant notification copy."),
)

AGENT_BY_KEY = {a.key: a for a in AGENTS}
PHASE1_AGENTS = tuple(a for a in AGENTS if a.phase is Phase.ONE)
PHASE2_AGENTS = tuple(a for a in AGENTS if a.phase is Phase.TWO)

# ---------------------------------------------------------------------------- constants
PRIMARY_REGION = "us-east-1"
PROJECT = "kyc90"

# R11.14 / F-46: the Step Functions Task dies at 15 minutes but the harness keeps running
# and keeps billing (F-14). maxLifetime is the only control that stops it, and the service
# default is 8 hours.
HARNESS_MAX_LIFETIME_SECONDS = 900
HARNESS_IDLE_TIMEOUT_SECONDS = 120

# R17.2 / B-12 / B-13 / D-09: one floor of 7 years satisfies US, UK, EU, AU and JP.
OBJECT_LOCK_YEARS = 7

# F-11: 5,000 active sessions in us-east-1 and us-west-2, 2,500 elsewhere. Phase 1 uses
# five agents per applicant, so divide before quoting a concurrency number — and R20.7
# requires it measured, not calculated.
SESSION_QUOTA = {"us-east-1": 5000, "us-west-2": 5000}
SESSION_QUOTA_DEFAULT = 2500


def session_quota(region: str) -> int:
    return SESSION_QUOTA.get(region, SESSION_QUOTA_DEFAULT)


def max_concurrent_applicants(region: str, agents_per_applicant: int = 5) -> int:
    """Calculated ceiling. R20.7 requires the real figure to be measured under burst load;
    this exists to size alarms, not to be quoted on a slide."""
    return session_quota(region) // agents_per_applicant
