"""The fifteen edge cases — design §8.4, task 6.9.

R28, R29 and R30 name fifteen concrete situations with a mandated result. Until this module they
were exercised by the golden set but implemented nowhere.

They live here, in the pure layer, and **not in any system prompt**. Design §8.4 is direct about
why: an edge case handled by prompt instruction is an edge case handled probabilistically. A
deceased-identity indicator that a model usually notices is not a control.

## Nine outcomes, six directives

Only nine rows decide an application. The other six instruct the surrounding machinery — pick the
stricter jurisdiction, keep the case resumable, admit one of two duplicates, show the decision
that already exists, link the prior case, record three countries. Those are returned as
`directive` rather than being forced into an outcome enum, because calling them outcomes would
claim the engine decides things it does not.

## The rejection ratio, stated accurately

Design §8.4 says "exactly one of the fifteen ends in rejection" — the deceased-identity indicator
(R29.5) — and calls the ratio deliberate. That is true **of this table**, and the tests below pin
it. It is not true of the applicant's whole journey, and the difference matters if the claim is
ever made from a stage: R5.5 sends a case to `REJECT` with reason `ABANDONED` once a step-up
deadline passes. Abandonment appears in this table (R30.1) as *resumable*, which is the mandated
behaviour **inside** the window; the rejection is a separate lifecycle transition on expiry, not
an edge-case routing decision.

So the honest form of the claim is: of the fifteen edge cases, one rejects. Of all the ways an
application can end, two do — and the second is recoverable, because R30.5 gives an abandoned
applicant a linked case and an appeal path on resubmission.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config
from models import EdgeVerdict, Phase1Outcome, RiskTier

#: R12.5 tie-break. Strictness is risk order; where two candidates rate equal, this fixed order
#: decides, so the selection is reproducible rather than dependent on input ordering.
_STRICTNESS_TIEBREAK = ("jp", "au", "uk", "us")

_RISK_RANK = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2, RiskTier.PROHIBITED: 3}


@dataclass(frozen=True, kw_only=True)
class EdgeFacts:
    """Everything the fifteen rules read. All optional, because a given phase knows only some.

    `None` means "not established", which is never treated as "false". A rule fires only on a
    positive fact — an unknown age must not read as an adult.
    """

    # R28 — documents
    document_expired: bool | None = None
    issuing_country_supported: bool | None = None
    is_screen_photograph: bool | None = None
    document_quality: float | None = None
    quality_defects: tuple[str, ...] = ()

    # R29 — person
    age: int | None = None
    nationalities: tuple[str, ...] = ()
    jurisdiction_risk_by_code: dict[str, RiskTier] = field(default_factory=dict)
    passport_country: str | None = None
    address_country: str | None = None
    ip_country: str | None = None
    is_thin_file: bool | None = None
    deceased_indicator: bool | None = None
    shared_attribute_count: int | None = None
    shared_with_case_id: str | None = None

    # R30 — process
    abandoned_mid_flow: bool | None = None
    within_resume_window: bool | None = None
    pending_step_up_item: str | None = None
    liveness_failure_count: int | None = None
    is_duplicate_submission: bool | None = None
    canonical_case_id: str | None = None
    case_already_decided: bool | None = None
    existing_outcome: str | None = None
    resubmitted_after_final_decision: bool | None = None
    prior_outcome: str | None = None
    prior_case_id: str | None = None


def evaluate_edge_cases(facts: EdgeFacts) -> tuple[EdgeVerdict, ...]:
    """Every matched edge case, in table order.

    All fifteen are evaluated — none short-circuits — because several can hold at once and the
    audit record must show every one that matched. Severity resolution happens later, in
    `outcomes.select_*`, which is the only place that decides which one governs.
    """
    verdicts: list[EdgeVerdict] = []
    for rule in _RULES:
        v = rule(facts)
        if v is not None:
            verdicts.append(v)
    return tuple(verdicts)


# --- R28 documents ------------------------------------------------------------------------


def _r28_1_expired(f: EdgeFacts) -> EdgeVerdict | None:
    if f.document_expired is not True:
        return None
    return EdgeVerdict(
        case_id="document_expired", requirement="R28.1",
        outcome=Phase1Outcome.STEP_UP,
        required_item="A current, unexpired identity document",
        explanation="The document supplied has expired. This is a continuation, not a rejection.",
    )


def _r28_2_unsupported_country(f: EdgeFacts) -> EdgeVerdict | None:
    if f.issuing_country_supported is not False:
        return None
    return EdgeVerdict(
        case_id="issuing_country_unsupported", requirement="R28.2",
        outcome=Phase1Outcome.MANUAL_REVIEW,
        explanation=(
            "The issuing country is outside the supported set. Routed to a reviewer and "
            "explicitly NOT auto-rejected — an unsupported country is our coverage gap, not "
            "the applicant's failing."
        ),
    )


def _r28_3_screen_photograph(f: EdgeFacts) -> EdgeVerdict | None:
    if f.is_screen_photograph is not True:
        return None
    return EdgeVerdict(
        case_id="photograph_of_screen", requirement="R28.3",
        outcome=Phase1Outcome.STEP_UP,
        required_item="A direct capture of the physical document, not a photograph of a screen",
        explanation="The submission appears to be a photograph of a display.",
    )


def _r28_4_low_quality(f: EdgeFacts) -> EdgeVerdict | None:
    if f.document_quality is None:
        return None
    if f.document_quality >= config.DOCUMENT_CONFIDENCE_THRESHOLD:
        return None
    defects = ", ".join(f.quality_defects) if f.quality_defects else "quality below threshold"
    return EdgeVerdict(
        case_id="document_quality_below_threshold", requirement="R28.4",
        outcome=Phase1Outcome.STEP_UP,
        # R28.4 requires naming the defect. "Try again" without a reason is the difference
        # between a step-up an applicant can act on and one they experience as a dead end.
        required_item=f"A clearer document image. Defect: {defects}",
        explanation=f"Document quality {f.document_quality} is below "
                    f"{config.DOCUMENT_CONFIDENCE_THRESHOLD}. Defect: {defects}.",
        signals={"document_quality": f.document_quality,
                 "quality_defects": list(f.quality_defects)},
    )


# --- R29 person ---------------------------------------------------------------------------


def _r29_1_minor(f: EdgeFacts) -> EdgeVerdict | None:
    if f.age is None or f.age >= config.MINIMUM_AGE:
        return None
    return EdgeVerdict(
        case_id="applicant_under_18", requirement="R29.1",
        outcome=Phase1Outcome.MANUAL_REVIEW,
        explanation=(
            f"Applicant age {f.age} is below {config.MINIMUM_AGE}. No provisional account is "
            "opened. Routed to a reviewer rather than step-up, because no document resolves age."
        ),
        signals={"age": f.age, "provisional_account_blocked": True},
    )


def _r29_2_dual_nationality(f: EdgeFacts) -> EdgeVerdict | None:
    if len(f.nationalities) < 2:
        return None
    chosen, rule = _stricter_jurisdiction(f.nationalities, f.jurisdiction_risk_by_code)
    return EdgeVerdict(
        case_id="dual_nationality", requirement="R29.2",
        directive="APPLY_STRICTER_JURISDICTION",
        explanation=(
            f"Dual nationality {list(f.nationalities)}. Applying the stricter jurisdiction "
            f"{chosen!r} per R12.5."
        ),
        # R12.5 requires recording both candidates AND the selection rule, not just the winner —
        # otherwise a later reviewer cannot tell whether the stricter one was actually chosen.
        signals={"nationality_candidates": list(f.nationalities),
                 "jurisdiction_selected": chosen,
                 "jurisdiction_selection_rule": rule},
    )


def _r29_3_country_divergence(f: EdgeFacts) -> EdgeVerdict | None:
    trio = (f.passport_country, f.address_country, f.ip_country)
    if any(c is None for c in trio):
        return None
    if len({c.lower() for c in trio if c}) != 3:
        return None
    return EdgeVerdict(
        case_id="passport_address_ip_all_differ", requirement="R29.3",
        directive="RAISE_RISK_SIGNAL",
        explanation=(
            "Passport, address and IP countries all differ. Recorded as a risk signal; not an "
            "outcome on its own, because legitimate applicants travel and relocate."
        ),
        signals={"passport_country": f.passport_country,
                 "address_country": f.address_country,
                 "ip_country": f.ip_country,
                 "country_divergence": True},
    )


def _r29_4_thin_file(f: EdgeFacts) -> EdgeVerdict | None:
    if f.is_thin_file is not True:
        return None
    return EdgeVerdict(
        case_id="thin_file", requirement="R29.4",
        outcome=Phase1Outcome.STEP_UP,
        required_item="Alternative verification, for example a utility bill or bank statement",
        explanation=(
            "Insufficient credit-bureau history. Step-up for alternative verification rather "
            "than rejection — a thin file is common among young, recently arrived and unbanked "
            "applicants, and rejecting on it would fail the fair-access argument."
        ),
        signals={"thin_file": True},
    )


def _r29_5_deceased(f: EdgeFacts) -> EdgeVerdict | None:
    if f.deceased_indicator is not True:
        return None
    return EdgeVerdict(
        case_id="deceased_identity", requirement="R29.5",
        outcome=Phase1Outcome.REJECT,
        reason_code="DECEASED_IDENTITY",
        explanation=(
            "The identity matches a deceased-person indicator. This is the one edge case in the "
            "table that ends in rejection: it signals identity theft of a dead person, and there "
            "is no step-up a legitimate applicant could supply to resolve it."
        ),
        notify_compliance=True,
        signals={"deceased_indicator": True},
    )


def _r29_6_shared_attributes(f: EdgeFacts) -> EdgeVerdict | None:
    if f.shared_attribute_count is None:
        return None
    if f.shared_attribute_count < config.SHARED_ATTRIBUTE_THRESHOLD:
        return None
    return EdgeVerdict(
        case_id="shared_identity_attributes", requirement="R29.6",
        outcome=Phase1Outcome.MANUAL_REVIEW,
        explanation=(
            f"{f.shared_attribute_count} identity attributes shared with case "
            f"{f.shared_with_case_id!r}, at or above the threshold of "
            f"{config.SHARED_ATTRIBUTE_THRESHOLD}. Possible synthetic identity fraud. **Both** "
            "cases route to review — reviewing only the second would leave the first approved on "
            "the same evidence."
        ),
        signals={"shared_attribute_count": f.shared_attribute_count,
                 "shared_with_case_id": f.shared_with_case_id,
                 "possible_synthetic_identity": True,
                 "route_both_cases": True},
    )


# --- R30 process --------------------------------------------------------------------------


def _r30_1_abandonment(f: EdgeFacts) -> EdgeVerdict | None:
    if f.abandoned_mid_flow is not True:
        return None
    if f.within_resume_window is False:
        # Outside the window this is no longer an edge case: R5.5 takes over and decides
        # REJECT / ABANDONED as a lifecycle transition. Returning a verdict here would put a
        # second rejection into a table that design §8.4 says contains exactly one.
        return None
    return EdgeVerdict(
        case_id="mid_flow_abandonment", requirement="R30.1",
        directive="KEEP_RESUMABLE",
        required_item=f.pending_step_up_item,
        explanation=(
            "Abandoned mid-flow and still inside the resume window. The case stays resumable "
            "with all prior results preserved (R5.3), and holds no harness session while "
            "waiting so it costs nothing (R5.4)."
        ),
        signals={"abandoned_mid_flow": True, "within_resume_window": True},
    )


def _r30_2_liveness_failures(f: EdgeFacts) -> EdgeVerdict | None:
    if f.liveness_failure_count is None:
        return None
    if f.liveness_failure_count < config.LIVENESS_FAILURE_LIMIT:
        return None
    return EdgeVerdict(
        case_id="liveness_failed_three_times", requirement="R30.2",
        outcome=Phase1Outcome.MANUAL_REVIEW,
        explanation=(
            f"Liveness failed {f.liveness_failure_count} times. Routed to a reviewer instead of "
            "asking again, with **no lockout** (D-05). Liveness fails for poor lighting, low-end "
            "cameras and some disabilities, so a permanent lock would exclude exactly the "
            "applicants least able to complain about it. The reviewer decides on the other "
            "evidence; this build offers the applicant no other way to verify."
        ),
        # `alternative_path_offered` stays in the record, false, so a reader can see the gap
        # rather than infer from its absence that an alternative was offered.
        signals={"liveness_failure_count": f.liveness_failure_count,
                 "routed_to_reviewer": True,
                 "alternative_path_offered": False,
                 "permanent_lockout": False},
    )


def _r30_3_duplicate(f: EdgeFacts) -> EdgeVerdict | None:
    if f.is_duplicate_submission is not True:
        return None
    return EdgeVerdict(
        case_id="simultaneous_submissions", requirement="R30.3",
        directive="DEDUP_DISCARD_DUPLICATE",
        explanation=(
            "A concurrent submission for the same applicant already holds the case. Exactly one "
            f"is admitted ({f.canonical_case_id!r}); this one is discarded rather than decided, "
            "so no second decision can exist to conflict with the first (R21.1)."
        ),
        signals={"duplicate_submission": True, "canonical_case_id": f.canonical_case_id},
    )


def _r30_4_already_decided(f: EdgeFacts) -> EdgeVerdict | None:
    if f.case_already_decided is not True:
        return None
    return EdgeVerdict(
        case_id="reviewer_opened_decided_case", requirement="R30.4",
        directive="SHOW_EXISTING_DECISION_BLOCK_SECOND",
        explanation=(
            f"The case is already decided ({f.existing_outcome!r}). The existing decision is "
            "displayed and a second is blocked (R6.5) — first writer wins, and the second "
            "reviewer sees a conflict rather than silently overwriting."
        ),
        signals={"case_already_decided": True, "existing_outcome": f.existing_outcome},
    )


def _r30_5_resubmission(f: EdgeFacts) -> EdgeVerdict | None:
    if f.resubmitted_after_final_decision is not True:
        return None
    appeal = f.prior_outcome == "REJECT"
    return EdgeVerdict(
        case_id="resubmission_after_final_decision", requirement="R30.5",
        directive="LINK_PRIOR_CASE_APPEAL" if appeal else "LINK_PRIOR_CASE",
        explanation=(
            f"Resubmission after a final decision of {f.prior_outcome!r}. Linked to prior case "
            f"{f.prior_case_id!r}"
            + (", and routed via the appeal path because the prior outcome was REJECT (R7.1)."
               if appeal else ".")
        ),
        signals={"prior_case_id": f.prior_case_id, "prior_outcome": f.prior_outcome,
                 "appeal_path": appeal},
    )


#: Table order matches design §8.4 so the two can be diffed by eye.
_RULES = (
    _r28_1_expired,
    _r28_2_unsupported_country,
    _r28_3_screen_photograph,
    _r28_4_low_quality,
    _r29_1_minor,
    _r29_2_dual_nationality,
    _r29_3_country_divergence,
    _r29_4_thin_file,
    _r29_5_deceased,
    _r29_6_shared_attributes,
    _r30_1_abandonment,
    _r30_2_liveness_failures,
    _r30_3_duplicate,
    _r30_4_already_decided,
    _r30_5_resubmission,
)

#: Every requirement clause the table implements, for the coverage test.
COVERED_REQUIREMENTS = (
    "R28.1", "R28.2", "R28.3", "R28.4",
    "R29.1", "R29.2", "R29.3", "R29.4", "R29.5", "R29.6",
    "R30.1", "R30.2", "R30.3", "R30.4", "R30.5",
)


def _stricter_jurisdiction(
    candidates: tuple[str, ...], risk_by_code: dict[str, RiskTier]
) -> tuple[str, str]:
    """R12.5 — pick the stricter jurisdiction deterministically, and say how.

    Ranks by configured risk, then by a fixed tie-break order. Without the tie-break, two equally
    rated jurisdictions would resolve by input order, so the same applicant could be assessed
    under different rules depending on which passport was read first.
    """
    normalised = [c.lower() for c in candidates]
    ranked = sorted(
        normalised,
        key=lambda c: (
            -_RISK_RANK.get(risk_by_code.get(c, RiskTier.LOW), 0),
            _STRICTNESS_TIEBREAK.index(c) if c in _STRICTNESS_TIEBREAK
            else len(_STRICTNESS_TIEBREAK),
            c,
        ),
    )
    chosen = ranked[0]
    if risk_by_code:
        rule = "highest_configured_jurisdiction_risk_then_fixed_tiebreak"
    else:
        rule = "fixed_tiebreak_only_no_risk_config_supplied"
    return chosen, rule
