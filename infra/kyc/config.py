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
    # Formerly the jurisdiction Router, which sat first on the 90-second path and classified the
    # applicant into US/UK/AU/JP from the passport country. Two problems, both measured in the
    # code rather than argued: its `CheckJurisdiction` edge read a field the harness envelope never
    # carries, so it could never fire; and the question it answered is not a judgement. Which law
    # applies is decided by which bank entity the customer applies to — a fact the API validates —
    # not by where their passport was issued. So the slot now does agent work that is genuinely
    # judgement: explaining a case to the human reviewer. It runs only on the review path, never
    # on the approval path, and it never decides.
    Agent("briefing", "kyc-review-briefing", Phase.ONE, "Briefing", 3,
          "Explain the recorded evidence to the human reviewer. Never decides."),
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
    # Screening runs in full here, exact and variant, before the account opens. It used to be
    # exact-only with variants deferred to Phase 2 — but by Phase 2 the account is already open,
    # sanctions breaches are strict liability, and the variant matcher is plain code that takes
    # milliseconds. Phase 3 re-runs the same agent when a list update is published.
    Agent("prescreen", "kyc-sanctions-screen", Phase.ONE, "PreScreen", 5,
          "Sanctions screening of the recorded identity, exact and variant. Fail closed.",
          ("sanctions_screen",)),
    Agent("compliance", "kyc-compliance-risk", Phase.TWO, "ComplianceRisk", 12,
          "PEP association and adverse media disambiguation.",
          ("pep_lookup", "adverse_media"), "high"),
    Agent("notify", "kyc-customer-experience", Phase.TWO, "Notify", 8,
          "Applicant notification copy."),
)

AGENT_BY_KEY = {a.key: a for a in AGENTS}
PHASE1_AGENTS = tuple(a for a in AGENTS if a.phase is Phase.ONE)
PHASE2_AGENTS = tuple(a for a in AGENTS if a.phase is Phase.TWO)

#: The four agents on the 90-second approval path, run in parallel. The Briefing agent is Phase 1
#: too, but only on the review path: it never stands between an applicant and an account.
PHASE1_EVIDENCE_AGENTS: tuple[str, ...] = ("documents", "identity", "fraud", "prescreen")

#: Where each recorded fact lives on the case record, keyed by the decision engine's fact names.
#: The tools write these (`tools/kyctools/case_record.py` FACT_ATTRIBUTES) and the state machines
#: pass them to the engine untouched; `infra/tests/test_asl.py` asserts the two tables agree, so a
#: renamed attribute cannot quietly turn every fact into "absent".
CASE_FACT_ATTRIBUTES: dict[str, str] = {
    "documentExtraction": "documentExtraction",
    "faceMatch": "factFaceMatch",
    "screening": "factScreening",
    "velocity": "factVelocity",
    "pep": "factPep",
    "adverseMedia": "factAdverseMedia",
}

#: Jurisdictions whose rules this deployment decides. Four Gateways exist (F-04) but every agent is
#: attached to the US one, so a case filed with another entity goes to a human rather than being
#: decided under the wrong law (R12.4).
DECIDED_JURISDICTIONS: tuple[str, ...] = ("us",)

#: The synthetic screening list's own version (`tools/kyctools/data/sanctions.py` LIST_VERSION). The
#: state machines compose the version in force from it and the published list update, exactly as
#: the screening tool does, so the rules can refuse a screening of an outdated list.
#: `infra/tests/test_jsonata_expressions.py` asserts the two compositions agree.
SCREENING_LIST_BASE_VERSION = "SYNTHETIC-2026-10-01"

#: R30.2 / D-05. Mirrors `decision-engine/config.py`: below it a failed liveness check is retried;
#: at it the checks run and the rules route the case to a reviewer, with no lockout.
LIVENESS_FAILURE_LIMIT = 3

# ---------------------------------------------------------------------------- constants
PRIMARY_REGION = "us-east-1"
PROJECT = "kyc90"

#: Runtime controls, in SSM Parameter Store and read per call. Parameters rather than environment
#: variables because the Gateway invokes the tools Lambda's published `live` alias: an environment
#: edit creates no new version, so it would never reach a Gateway call.
#:
#: Demo faults can only push a case towards a human — a tool reported unavailable, an agent given
#: less time — never towards an approval, so they fail open (absent means no faults). The list
#: overlay is a sanctions list update, so it fails closed: unreadable means screening is unavailable.
DEMO_FAULTS_PARAMETER = f"/{PROJECT}/demo/faults"
LIST_OVERLAY_PARAMETER = f"/{PROJECT}/screening/list-overlay"

# R11.14 / F-46: the Step Functions Task dies at 15 minutes but the harness keeps running
# and keeps billing (F-14). maxLifetime is the only control that stops it, and the service
# default is 8 hours.
HARNESS_MAX_LIFETIME_SECONDS = 900
HARNESS_IDLE_TIMEOUT_SECONDS = 120

# R17.2 / B-12 / B-13 / D-09: one floor of 7 years satisfies US, UK, EU, AU and JP.
OBJECT_LOCK_YEARS = 7


@dataclass(frozen=True)
class EvidenceLock:
    """How hard, and for how long, Object Lock holds every evidence object and decision record.

    Object Lock itself is enabled at bucket creation either way (it cannot be added later); this
    is the retention applied to what is written. `days` is both the buckets' default retention
    and the audit writer's retain-until for each decision record.
    """

    name: str
    mode: str  # "COMPLIANCE" | "GOVERNANCE", as S3 spells it
    days: int


#: A regulated deployment keeps COMPLIANCE for seven years: nobody, root included, can delete a
#: record or shorten its retention. A demo uploads documents with the presenter's real face on
#: them and decides no real person's application, so it locks in GOVERNANCE for one day (D-35):
#: a delete is still refused, so the lock is demonstrable, but an administrator can remove an
#: object at once with the governance bypass, and anyone can after a day.
EVIDENCE_LOCKS: dict[str, EvidenceLock] = {
    "COMPLIANCE": EvidenceLock("COMPLIANCE", "COMPLIANCE", 365 * OBJECT_LOCK_YEARS),
    "DEMO": EvidenceLock("DEMO", "GOVERNANCE", 1),
}

#: D-36 — the Face Liveness challenge each session asks for, as `CreateFaceLivenessSession` names
#: it (`api/liveness.py` holds the same pair). The light challenge is the service default and the
#: one AWS recommends for the highest accuracy: move toward the camera, then hold still through a
#: sequence of flashing colours. The movement challenge drops the flashes and is several seconds
#: quicker. A demo (demo controls on) uses the movement challenge; anything else the light one.
LIVENESS_LIGHT_CHALLENGE = "FaceMovementAndLightChallenge"
LIVENESS_MOVEMENT_CHALLENGE = "FaceMovementChallenge"
LIVENESS_CHALLENGES = (LIVENESS_LIGHT_CHALLENGE, LIVENESS_MOVEMENT_CHALLENGE)

#: The two modes a Gateway attaches its Cedar policy engine in (`-c policyEnforcement`). A closed
#: set, shared by the Gateways that are attached in it and the API that reports it to the
#: console's live view (D-40).
POLICY_ENFORCEMENT_MODES = ("LOG_ONLY", "ENFORCE")

# F-11: 5,000 active sessions in us-east-1 and us-west-2, 2,500 elsewhere. The approval path runs
# four agents per applicant, so divide before quoting a concurrency number — and R20.7 requires it
# measured, not calculated.
SESSION_QUOTA = {"us-east-1": 5000, "us-west-2": 5000}
SESSION_QUOTA_DEFAULT = 2500


def session_quota(region: str) -> int:
    return SESSION_QUOTA.get(region, SESSION_QUOTA_DEFAULT)


def max_concurrent_applicants(region: str,
                              agents_per_applicant: int = len(PHASE1_EVIDENCE_AGENTS)) -> int:
    """Calculated ceiling. R20.7 requires the real figure to be measured under burst load;
    this exists to size alarms, not to be quoted on a slide."""
    return session_quota(region) // agents_per_applicant
