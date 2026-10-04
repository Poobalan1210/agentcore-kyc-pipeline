"""State machine graph properties for all three phases.

These tests walk the ASL graph rather than checking that particular strings are present. The
requirements that matter most here are *negative* — a degraded pipeline cannot open an account
without a person, no failure path lifts a restriction, a frozen account never reaches MONITORING —
and a negative property about reachability cannot be verified by reading one state in isolation.

That is the whole reason the definitions are plain dicts (design §12.3): a synthesised construct
tree cannot be traversed legibly, so the invariant would only be checkable by eye in the console,
which is to say not checkable at all.
"""

from __future__ import annotations

import json
import re

import pytest

import account_stub
from kyc.asl import (
    AGENT_TIMEOUT_SECONDS,
    COMPLIANCE_TIMEOUT_SECONDS,
    INVOKE_HARNESS,
    PIN_FIELDS,
    REVIEW_UNRESUMABLE,
    RUNTIME_SESSION_ID,
    phase1_definition,
    phase2_definition,
    phase3_definition,
    timeout_argument,
)
from kyc.config import CASE_FACT_ATTRIBUTES, LIVENESS_FAILURE_LIMIT, PHASE1_EVIDENCE_AGENTS

AGENT_KEYS = ("briefing", "documents", "identity", "fraud", "prescreen", "compliance", "notify")
# F-85: plain harness ARNs. An endpoint-suffixed ARN is rejected by the invokeHarness integration
# with "Invalid harness ARN format", so the pinned endpoint travels in `Qualifier` instead.
HARNESS_ARNS = {
    k: f"arn:aws:bedrock-agentcore:us-east-1:633898796092:harness/kyc90_{k}-x"
    for k in AGENT_KEYS
}
ENDPOINTS = HARNESS_ARNS
MODELS = {k: "us.amazon.nova-lite-v1:0" for k in AGENT_KEYS}
PINS = {
    "endpointName": "live",
    "harnessVersion": "briefing=1,documents=1,identity=1,fraud=1,prescreen=1",
    "policyEngineVersion": "arn:aws:bedrock-agentcore:us-east-1:633898796092:policy-engine/pe#LOG_ONLY",
    "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:633898796092:gateway/kycgateway-us-abc",
}
ACCOUNT_FN = "arn:aws:lambda:us-east-1:633898796092:function:kyc90-account-stub"
COMMON = dict(
    case_table="kyc90-cases",
    harness_arns=HARNESS_ARNS,
    model_ids=MODELS,
    decision_engine_arn="arn:aws:lambda:us-east-1:633898796092:function:kyc90-decision",
    audit_writer_arn="arn:aws:lambda:us-east-1:633898796092:function:kyc90-audit",
    review_queue_url="https://sqs.us-east-1.amazonaws.com/633898796092/kyc90-review",
    account_function_arn=ACCOUNT_FN,
    audit_pins=PINS,
)
PHASE1_EXTRA = dict(phase2_state_machine_arn="arn:aws:states:::sm/phase2",
                    controls_parameter="/kyc90/demo/faults")
PHASE3_EXTRA = dict(history_table="kyc90-decision-history")


@pytest.fixture(scope="module")
def p1():
    return phase1_definition(**COMMON, **PHASE1_EXTRA)


@pytest.fixture(scope="module")
def p2():
    return phase2_definition(**COMMON)


@pytest.fixture(scope="module")
def p3():
    return phase3_definition(**COMMON, **PHASE3_EXTRA)


@pytest.fixture(scope="module")
def phases(p1, p2, p3):
    return {"phase1": p1, "phase2": p2, "phase3": p3}


# ------------------------------------------------------------------------------------ helpers


def successors(state: dict) -> set[str]:
    """Every state this one can transition to at its own graph level.

    Deliberately does not descend into `Branches`: a Parallel branch is a self-contained graph whose
    names resolve only within it, so folding its targets in would report false missing states and
    make a branch-internal terminal look like a parent transition.
    """
    out: set[str] = set()
    if "Next" in state:
        out.add(state["Next"])
    for choice in state.get("Choices", []):
        if "Next" in choice:
            out.add(choice["Next"])
    if "Default" in state:
        out.add(state["Default"])
    for catcher in state.get("Catch", []):
        if "Next" in catcher:
            out.add(catcher["Next"])
    return out


def reachable_from(definition: dict, start: str, *, blocked: frozenset[str] | set[str] = frozenset()
                   ) -> set[str]:
    """Transitive closure, not passing through `blocked` states (they are reached, not crossed).

    Blocking `AwaitReviewDecision` asks "what can this reach without a person deciding?".
    """
    states = definition["States"]
    seen: set[str] = set()
    frontier = [start]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in blocked and name != start:
            continue
        state = states.get(name)
        if state is None:
            continue
        frontier.extend(successors(state))
    return seen - {start}


WITHOUT_A_PERSON = frozenset({"AwaitReviewDecision"})


def _all_states(definition: dict) -> dict[str, dict]:
    """Every state at every level, including inside Parallel branches."""
    found: dict[str, dict] = {}

    def walk(states: dict, prefix: str = "") -> None:
        for name, state in states.items():
            found[prefix + name] = state
            for index, branch in enumerate(state.get("Branches", [])):
                walk(branch.get("States", {}), f"{name}.branch{index}.")

    walk(definition["States"])
    return found


def _agent_tasks(definition: dict) -> dict[str, dict]:
    """Every `invokeHarness` Task, including those nested inside Parallel branches."""
    found: dict[str, dict] = {}

    def walk(states: dict) -> None:
        for name, state in states.items():
            if state.get("Resource") == INVOKE_HARNESS:
                found[name] = state
            for branch in state.get("Branches", []):
                walk(branch.get("States", {}))

    walk(definition["States"])
    return found


def _harness_arns(definition: dict) -> set[str]:
    return {s["Arguments"]["HarnessArn"] for s in _agent_tasks(definition).values()}


def _default_timeout(state: dict) -> int:
    """The configured timeout: the literal, or the default inside a demo-fault override."""
    value = state["Arguments"]["TimeoutSeconds"]
    if isinstance(value, int):
        return value
    match = re.search(r": (\d+) %}$", value)
    assert match, value
    return int(match.group(1))


def _account_ops(definition: dict) -> dict[str, str]:
    return {name: s["Arguments"]["Payload"]["operation"]
            for name, s in definition["States"].items()
            if s.get("Resource") == "arn:aws:states:::lambda:invoke"
            and s["Arguments"].get("FunctionName") == ACCOUNT_FN}


def _audit_states(definition: dict) -> dict[str, dict]:
    return {name: s for name, s in definition["States"].items()
            if s.get("Resource") == "arn:aws:states:::lambda:invoke"
            and s["Arguments"].get("FunctionName") == COMMON["audit_writer_arn"]}


def _state_writers(definition: dict) -> dict[str, dict]:
    """States whose UpdateExpression assigns the case's `state` attribute."""
    writers = {}
    for name, state in definition["States"].items():
        args = state.get("Arguments") or {}
        expression = args.get("UpdateExpression")
        if not isinstance(expression, str):
            continue
        aliases = [a for a, real in (args.get("ExpressionAttributeNames") or {}).items()
                   if real == "state"]
        set_clause = expression.split(" REMOVE ")[0]
        if any(f"{a} = " in set_clause for a in aliases):
            writers[name] = state
    return writers


def _writes_state_value(state: dict, value: str) -> bool:
    values = state["Arguments"].get("ExpressionAttributeValues") or {}
    return values.get(":state") == {"S": value}


# ============================================================================== structure


class TestStructure:
    def test_phase1_starts_by_normalising_the_input(self, p1):
        """The Pipe (no InputTemplate) delivers a raw SQS batch; a direct start sends the bare
        object. NormalizeInput reconciles both and captures the ingress timestamp."""
        assert p1["StartAt"] == "NormalizeInput"
        assert p1["States"]["NormalizeInput"]["Next"] == "LoadCase"

    def test_jsonata_everywhere(self, phases):
        """F-71 — `Arguments` and `{% %}` do not exist under JSONPath."""
        for phase, definition in phases.items():
            assert definition["QueryLanguage"] == "JSONata"
            for name, state in _all_states(definition).items():
                if state["Type"] == "Fail":
                    continue
                assert state.get("QueryLanguage") == "JSONata", f"{phase}.{name}"

    def test_every_transition_target_exists(self, phases):
        for phase, definition in phases.items():
            names = set(definition["States"])
            for name, state in definition["States"].items():
                for target in successors(state):
                    assert target in names, f"{phase}: {name} -> missing state {target}"

    def test_every_state_is_reachable(self, phases):
        """An unreachable state is dead code in the one place dead code hides a missing edge."""
        for phase, definition in phases.items():
            reached = reachable_from(definition, definition["StartAt"]) | {definition["StartAt"]}
            assert set(definition["States"]) == reached, (
                f"{phase}: unreachable {sorted(set(definition['States']) - reached)}")

    def test_every_branch_internal_target_exists(self, p1):
        for index, branch in enumerate(p1["States"]["Phase1Parallel"]["Branches"]):
            inner = branch["States"]
            assert branch["StartAt"] in inner, f"branch {index} StartAt is dangling"
            for name, state in inner.items():
                for target in successors(state):
                    assert target in inner, f"branch {index}: {name} -> missing {target}"

    def test_every_branch_reaches_a_terminal(self, p1):
        """Each branch must be able to arrive at an End, or the Parallel never returns."""
        for index, branch in enumerate(p1["States"]["Phase1Parallel"]["Branches"]):
            inner = branch["States"]
            seen, frontier = set(), [branch["StartAt"]]
            while frontier:
                name = frontier.pop()
                if name in seen:
                    continue
                seen.add(name)
                frontier += [t for t in successors(inner[name]) if t in inner]
            assert any(inner[n].get("End") for n in seen), f"branch {index} has no terminal"

    def test_an_empty_agent_result_degrades_the_branch(self, p1):
        """Measured live: Fraud finished with StopReason `timeout_exceeded` and `Content: []`."""
        for branch in p1["States"]["Phase1Parallel"]["Branches"]:
            check = next(s for n, s in branch["States"].items() if n.endswith("Check"))
            condition = check["Choices"][0]["Condition"]
            assert "count($states.input.Output.Message.Content) = 0" in condition
            assert "timeout_exceeded" in condition
            assert check["Choices"][0]["Next"].endswith("Degraded")

    def test_branch_output_is_keyed_by_agent(self, p1):
        for branch in p1["States"]["Phase1Parallel"]["Branches"]:
            for name, state in branch["States"].items():
                if name.endswith(("Ok", "Degraded")):
                    assert "agent" in state["Output"]
                    assert state["Output"]["status"] in ("OK", "DEGRADED")

    def test_the_four_evidence_agents_run_in_parallel(self, p1):
        """The approval path runs Documents, Identity, Fraud and Pre-Screen, nothing else."""
        in_parallel = {branch["StartAt"]
                       for branch in p1["States"]["Phase1Parallel"]["Branches"]}
        assert in_parallel == {"Documents", "Identity", "Fraud", "PreScreen"}
        assert len(PHASE1_EVIDENCE_AGENTS) == 4

    def test_the_briefing_is_never_on_the_approval_path(self, p1):
        """The Briefing agent explains a case to a reviewer. It must not stand between an
        applicant and an account, so no path from it reaches the account without a person."""
        assert ENDPOINTS["briefing"] in _harness_arns(p1)
        reached = reachable_from(p1, "Briefing", blocked=WITHOUT_A_PERSON)
        for state in ("OpenAccount", "OpenProvisional", "TriggerPhase2"):
            assert state not in reached

    def test_no_phase2_agent_runs_in_phase1(self, p1):
        """D-08 — Compliance Risk runs at high effort and cannot fit the 90s budget."""
        invoked = _harness_arns(p1)
        assert ENDPOINTS["compliance"] not in invoked
        assert ENDPOINTS["notify"] not in invoked

    def test_every_phase1_evidence_agent_fits_the_phase_budget(self, p1):
        for branch in p1["States"]["Phase1Parallel"]["Branches"]:
            name = branch["StartAt"]
            assert _default_timeout(branch["States"][name]) == AGENT_TIMEOUT_SECONDS, name

    def test_the_high_effort_agent_gets_the_longer_budget(self, p2):
        """Phase 2 has no clock pressure, which is the whole reason D-08 puts A6 there."""
        assert _default_timeout(p2["States"]["ComplianceRisk"]) == COMPLIANCE_TIMEOUT_SECONDS

    def test_missing_version_pins_are_refused_at_build_time(self):
        """R18.6 — the audit writer refuses a record without all four pins, so a definition
        without them would fail every decision at run time instead of failing the build."""
        for field in PIN_FIELDS:
            pins = {**PINS, field: ""}
            with pytest.raises(ValueError):
                phase1_definition(**{**COMMON, "audit_pins": pins}, **PHASE1_EXTRA)


# ============================================================ the rules decide on facts


class TestFactsAreTheDecisionInput:
    """The engine decides on what the tools recorded, never on what an agent said."""

    def _tier_payloads(self, p1, p2, p3):
        return {"phase1": p1["States"]["PreliminaryTier"]["Arguments"]["Payload"],
                "phase2": p2["States"]["FullTier"]["Arguments"]["Payload"],
                "phase3": p3["States"]["Reassess"]["Arguments"]["Payload"]}

    def test_every_phase_passes_all_six_recorded_facts(self, p1, p2, p3):
        for phase, payload in self._tier_payloads(p1, p2, p3).items():
            assert set(payload["facts"]) == set(CASE_FACT_ATTRIBUTES), phase
            for name, attribute in CASE_FACT_ATTRIBUTES.items():
                read = payload["facts"][name]
                # As recorded — the tool's JSON string, not a re-derivation of it.
                assert f"$case.{attribute}.S" in read, (phase, name)
                # Null, not {}: absent must read as "the check did not run".
                assert f"$exists($case.{attribute})" in read and ": null" in read, (phase, name)

    def test_the_attribute_names_agree_with_the_tools_and_the_engine(self):
        """A renamed attribute would quietly turn every fact into "absent"."""
        from facts import FACT_OWNER
        from kyctools.case_record import FACT_ATTRIBUTES

        assert set(CASE_FACT_ATTRIBUTES.values()) == set(FACT_ATTRIBUTES.values())
        assert set(CASE_FACT_ATTRIBUTES) == set(FACT_OWNER)

    def test_no_agent_reply_reaches_a_tier_input(self, p1, p2, p3):
        """Agent results are an availability signal only. Nothing else in the payload may read
        the branches, the agent envelopes or their content."""
        for phase, payload in self._tier_payloads(p1, p2, p3).items():
            for key, value in payload.items():
                if key == "agentResults":
                    continue
                rendered = json.dumps(value)
                for forbidden in ("$branchResults", "Result.", "Message.Content",
                                  "$complianceBranch", "$rescreenBranch"):
                    assert forbidden not in rendered, (phase, key, forbidden)

    def test_the_fail_open_defaults_are_gone(self, p1, p2, p3):
        """`jurisdictionRisk` defaulted to LOW and `pepStatus` to NONE when absent. Both are now
        derived from recorded facts inside the engine, which marks an absent fact undetermined."""
        for phase, payload in self._tier_payloads(p1, p2, p3).items():
            for stale in ("jurisdictionRisk", "edgeFacts", "findings", "pepStatus",
                          "adverseMediaSeverity", "documentConfidence", "velocitySignals",
                          "preScreenMatched", "customerAge"):
                assert stale not in payload, (phase, stale)

    def test_freshness_is_measured_from_the_phase_start(self, p1, p2, p3):
        """A fact from an earlier attempt must not decide this one."""
        for phase, payload in self._tier_payloads(p1, p2, p3).items():
            assert payload["phaseStartedAt"] == "{% $phaseStartedAt %}", phase
        assert "Execution.StartTime" in p1["States"]["NormalizeInput"]["Assign"]["phaseStartedAt"]

    def test_the_rules_get_a_date_to_judge_expiry_and_age_against(self, p1):
        assert p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]["asOf"] == "{% $now() %}"

    def test_reviewer_dispositions_reach_every_tier(self, p1, p2, p3):
        for phase, payload in self._tier_payloads(p1, p2, p3).items():
            assert "$case.screeningDispositions" in payload["screeningDispositions"], phase

    def test_the_declared_identity_is_unwrapped_field_by_field(self, p1):
        declared = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]["declaredIdentity"]
        for field in ("surname", "givenNames", "dateOfBirth", "nationality"):
            assert f"'{field}'" in declared, field
        assert ".M." in declared and "$exists(" in declared and ": null" in declared

    def test_only_the_current_attempt_counts_as_liveness_failures(self, p1):
        payload = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]
        assert "'FAILED'" in payload["livenessFailures"]

    def test_the_tier_task_invokes_a_lambda_not_a_model(self, p1, p2, p3):
        """R4.6 — tier computation must not invoke a model."""
        for definition, state_name, phase in ((p1, "PreliminaryTier", 1), (p2, "FullTier", 2),
                                              (p3, "Reassess", 3)):
            state = definition["States"][state_name]
            assert state["Resource"] == "arn:aws:states:::lambda:invoke"
            assert "Model" not in state["Arguments"]
            assert state["Arguments"]["Payload"]["phase"] == phase


# ================================================================= precondition guard


class TestPreconditionGuard:
    """R38 — the guard that makes the expensive part of the graph unreachable."""

    STEP_UPS = ("StepUpLivenessRequired", "StepUpLivenessFailed", "StepUpDocumentsRequired")

    def test_the_claim_and_the_guard_sit_before_any_agent(self, p1):
        assert p1["States"]["CheckIdempotency"]["Default"] == "ClaimRun"
        assert p1["States"]["ClaimRun"]["Next"] == "CheckPreconditions"
        assert p1["States"]["CheckPreconditions"]["Default"] == "LoadControls"
        assert p1["States"]["LoadControls"]["Next"] == "Phase1Parallel"

    def test_no_agent_is_reachable_from_a_step_up(self, p1):
        for target in self.STEP_UPS:
            reached = reachable_from(p1, target)
            assert "Phase1Parallel" not in reached
            assert "Briefing" not in reached

    def test_a_missing_precondition_steps_up_rather_than_rejects(self, p1):
        for name in self.STEP_UPS:
            record = p1["States"][name]["Assign"]["outcomeRecord"]
            assert record["outcome"] == "STEP_UP"
            assert record["agentsInvoked"] == 0

    def test_all_three_precondition_reasons_are_distinct(self, p1):
        reasons = {p1["States"][n]["Assign"]["outcomeRecord"]["reasonCode"]
                   for n in self.STEP_UPS}
        assert reasons == {"LIVENESS_REQUIRED", "LIVENESS_FAILED", "DOCUMENTS_REQUIRED"}

    def test_liveness_is_retried_below_the_limit_and_reviewed_at_it(self, p1):
        """R30.2 / D-05: no lockout. Below the limit a failure steps up; at it the checks run and
        the rules route the case to a reviewer with an alternative path."""
        choices = p1["States"]["CheckPreconditions"]["Choices"]
        failed = next(c for c in choices if c["Next"] == "StepUpLivenessFailed")
        assert f"$livenessFailures < {LIVENESS_FAILURE_LIMIT}" in failed["Condition"]

    def test_an_undecided_jurisdiction_goes_to_a_person(self, p1):
        """R12.4. The Router used to own this and its edge could never fire."""
        choices = p1["States"]["CheckPreconditions"]["Choices"]
        guard = next(c for c in choices if c["Next"] == "JurisdictionNotDecided")
        assert "$case.jurisdiction.S in ['us']" in guard["Condition"]
        state = p1["States"]["JurisdictionNotDecided"]
        assert state["Next"] == "ReviewGate"
        assert state["Assign"]["reviewReason"] == "JURISDICTION_NOT_DECIDED"


# ======================================================== no account without a decision


class TestNoAccountWithoutAnExplicitDecision:
    """R13.5 and the degraded-path demo — the most important structural property in Phase 1."""

    def test_the_only_rules_approval_edge_is_the_approving_outcome(self, p1):
        """It used to be the Default, so an outcome with no branch of its own opened an account."""
        decision = p1["States"]["Phase1Decision"]
        approving = [c for c in decision["Choices"] if c["Next"] == "WriteDecisionRecord"]
        assert len(approving) == 1
        assert "PROVISIONAL_APPROVE_RESTRICTED" in approving[0]["Condition"]
        assert decision["Default"] == "EngineReferral"
        assert "OpenAccount" not in reachable_from(p1, "EngineReferral", blocked=WITHOUT_A_PERSON)

    def test_a_degraded_branch_is_checked_before_any_approval(self, p1):
        first = p1["States"]["Phase1Decision"]["Choices"][0]
        assert "DEGRADED" in first["Condition"]
        assert first["Next"] == "DegradedPath"

    def test_the_degraded_path_cannot_open_an_account_without_a_person(self, p1):
        reached = reachable_from(p1, "DegradedPath", blocked=WITHOUT_A_PERSON)
        for state in ("WriteDecisionRecord", "OpenProvisional", "OpenAccount", "TriggerPhase2"):
            assert state not in reached, state
        assert "ManualReview" in reached

    def test_every_account_opening_is_the_rules_approval_or_a_person(self, p1):
        """Block both and the account platform is unreachable from the start."""
        reached = reachable_from(p1, p1["StartAt"],
                                 blocked={"WriteDecisionRecord", "AwaitReviewDecision"})
        assert "OpenAccount" not in reached
        assert "TriggerPhase2" not in reached

    def test_every_agent_task_catches_to_a_state_that_cannot_open_an_account(self, p1):
        for name, state in _agent_tasks(p1).items():
            catchers = state.get("Catch", [])
            assert catchers, f"{name} has no Catch"
            for catcher in catchers:
                target = catcher["Next"]
                if target not in p1["States"]:
                    continue  # branch-internal: the branch's own degraded terminal
                reached = reachable_from(p1, target, blocked=WITHOUT_A_PERSON)
                assert "OpenAccount" not in reached, name

    def test_every_approval_path_failure_goes_to_a_person(self, p1):
        """A failed record, a lost write: the account is not opened and a person looks."""
        assert p1["States"]["WriteDecisionRecord"]["Catch"][0]["Next"] == "AuditWriteFailed"
        assert p1["States"]["AuditWriteFailed"]["Assign"]["reviewReason"] == "AUDIT_RECORD_MISSING"
        catchers = {c["Next"] for c in p1["States"]["OpenProvisional"]["Catch"]}
        assert catchers == {"ProvisionalConflict"}
        for name in ("AuditWriteFailed", "ProvisionalConflict"):
            assert p1["States"][name]["Next"] == "ReviewGate"


# ================================================================================ idempotency


class TestIdempotency:
    """R21 — task 6.3."""

    def test_a_replay_returns_before_any_agent(self, p1):
        assert p1["States"]["LoadCase"]["Next"] == "CheckIdempotency"
        assert reachable_from(p1, "ReturnCached") == set()
        assert p1["States"]["ReturnCached"]["Type"] == "Succeed"

    def test_the_key_is_read_from_where_it_is_written(self, p1):
        """It was read from inside `phase1`, a JSON string, so the check could never match and a
        redelivery during a review wait ran every agent again."""
        condition = p1["States"]["CheckIdempotency"]["Choices"][0]["Condition"]
        assert "$case.idempotencyKey.S = $applicantId & '#' & $eventId" in condition
        assert "phase1.idempotencyKey" not in condition

    def test_the_claim_is_written_before_any_work(self, p1):
        claim = p1["States"]["ClaimRun"]
        assert "idempotencyKey = :k" in claim["Arguments"]["UpdateExpression"]
        assert "#s = :received" in claim["Arguments"]["ConditionExpression"]
        assert claim["Catch"][0]["Next"] == "ReturnCached"
        assert "ConditionalCheckFailedException" in claim["Catch"][0]["ErrorEquals"][0]

    def test_opening_is_conditional_so_two_executions_cannot_both_open(self, p1):
        """R21.4."""
        condition = p1["States"]["OpenProvisional"]["Arguments"]["ConditionExpression"]
        assert "attribute_not_exists(provisionalDecidedAt)" in condition

    def test_phase2_start_is_named_for_idempotency_and_fits_the_limit(self, p1):
        """R21.7. The decision id alone: a real Cognito-derived applicant id is 76 characters, and
        with it prefixed the name broke the 80-character limit for every real applicant."""
        args = p1["States"]["TriggerPhase2"]["Arguments"]
        assert "$provisionalDecisionId" in args["Name"]
        assert "$applicantId" not in args["Name"]
        assert "$decision.decisionId" not in json.dumps(p1["States"]["TriggerPhase2"])

    def test_the_decision_id_comes_from_the_audit_writer(self, p1):
        for name in ("WriteDecisionRecord", "ReviewApproved"):
            assign = p1["States"][name]["Assign"]["provisionalDecisionId"]
            assert "$states.result.Payload.decisionId" in assign, name

    def test_phase2_is_idempotent_on_the_case(self, p2):
        choices = p2["States"]["CheckPhase2Idempotency"]["Choices"]
        assert choices[0]["Condition"] == "{% $exists($case.phase2) %}"
        assert choices[0]["Next"] == "AlreadyComplete"


class TestPhase2IsNotAwaited:
    def test_start_execution_is_not_sync(self, p1):
        """R13.11. `.sync` would put a multi-minute screening job inside the 90-second budget."""
        resource = p1["States"]["TriggerPhase2"]["Resource"]
        assert resource == "arn:aws:states:::states:startExecution"

    def test_phase1_ends_at_trigger_phase2(self, p1):
        assert p1["States"]["TriggerPhase2"].get("End") is True

    def test_the_account_is_open_before_phase2_starts(self, p1):
        assert p1["States"]["OpenAccount"]["Next"] == "TriggerPhase2"
        assert _account_ops(p1)["OpenAccount"] == "openProvisional"


# ============================================================================ agent tasks


class TestAgentTaskShape:
    """design §7.3, and the facts that make it work."""

    def test_resource_uri_has_no_hyphen(self):
        assert INVOKE_HARNESS.endswith("bedrockagentcore:invokeHarness")
        assert "bedrock-agentcore:invokeHarness" not in INVOKE_HARNESS

    def test_the_harness_arn_is_plain_and_the_endpoint_is_pinned_by_qualifier(self, phases):
        """F-85, found by gate G9. Every suffix form is rejected."""
        for phase, definition in phases.items():
            for name, state in _agent_tasks(definition).items():
                arn = state["Arguments"]["HarnessArn"]
                assert arn.startswith("arn:aws:bedrock-agentcore:"), (phase, name)
                assert arn.count(":harness/") == 1 and arn.count("/") == 1, (phase, name)
                assert state["Arguments"]["Qualifier"] == "live", (phase, name)

    def test_runtime_session_id_meets_the_minimum_length(self, phases):
        """F-36 — 33 characters minimum; `$uuid()` yields 36."""
        assert RUNTIME_SESSION_ID == "{% $uuid() %}"
        for definition in phases.values():
            for state in _agent_tasks(definition).values():
                assert state["Arguments"]["RuntimeSessionId"] == RUNTIME_SESSION_ID

    def test_every_agent_task_is_below_the_task_ceiling(self, phases):
        """F-14 — the hard cap is 15 minutes."""
        for definition in phases.values():
            for name, state in _agent_tasks(definition).items():
                assert _default_timeout(state) < 900, name

    def test_throttling_is_retried_three_times_with_backoff(self, phases):
        for definition in phases.values():
            for name, state in _agent_tasks(definition).items():
                retry, = state["Retry"]
                assert retry["ErrorEquals"] == ["BedrockAgentCore.ThrottlingException"], name
                assert retry["MaxAttempts"] == 3 and retry["BackoffRate"] == 2.0

    def test_reasoning_config_nests_under_additional_model_request_fields(self, p2):
        """F-61 — a bare `reasoningConfig` is rejected."""
        model = p2["States"]["ComplianceRisk"]["Arguments"]["Model"]["BedrockModelConfig"]
        nested = model["AdditionalParams"]["additionalModelRequestFields"]["reasoningConfig"]
        assert nested["maxReasoningEffort"] == "high"

    def test_the_high_effort_agent_does_not_pin_sampling(self, p2):
        """F-62 — at high effort the call fails if Temperature or MaxTokens are set."""
        model = p2["States"]["ComplianceRisk"]["Arguments"]["Model"]["BedrockModelConfig"]
        assert "Temperature" not in model and "MaxTokens" not in model

    def test_no_phase1_agent_reasons_and_every_one_pins_sampling(self, p1):
        """F-88 removed reasoning from Phase 1; R26.3's pinned temperature is satisfiable again."""
        for name, state in _agent_tasks(p1).items():
            model = state["Arguments"]["Model"]["BedrockModelConfig"]
            assert "AdditionalParams" not in model, name
            assert model["Temperature"] == 0.0, name


class TestDemoFaults:
    """A presenter can make an agent run out of time on stage. That may only ever send a case to
    a person: the override can shorten an agent's time, never extend it, and a missing or broken
    parameter means no faults."""

    def test_the_override_can_only_shorten(self):
        expression = timeout_argument("fraud", 90)
        assert "$min([$number($lookup($agentTimeouts, 'fraud')), 90])" in expression
        assert expression.endswith(": 90 %}")

    def test_only_the_evidence_branches_take_the_override(self, phases):
        for phase, definition in phases.items():
            for name, state in _agent_tasks(definition).items():
                timeout = state["Arguments"]["TimeoutSeconds"]
                overridable = isinstance(timeout, str)
                in_branch = phase == "phase1" and name in {"Documents", "Identity", "Fraud",
                                                           "PreScreen"}
                assert overridable == in_branch, (phase, name)

    def test_the_parameter_is_read_fail_open(self, p1):
        state = p1["States"]["LoadControls"]
        assert state["Resource"] == "arn:aws:states:::aws-sdk:ssm:getParameter"
        assert state["Arguments"]["Name"] == "/kyc90/demo/faults"
        assert state["Catch"] == [{"ErrorEquals": ["States.ALL"], "Next": "Phase1Parallel"}]
        assert p1["States"]["NormalizeInput"]["Assign"]["agentTimeouts"] == "{% {} %}"


# ====================================================== Phase 2: restrictions never lapse


class TestPhase2RestrictionsNeverLapse:
    """R31.6 / Property 3 — the negative invariant. Silence is not permission."""

    LIFT_STATES = ("LiftAllRestrictions", "LiftTransfersKeepCeiling",
                   "ReviewLiftTransfersKeepCeiling")
    RESTRICTIVE_ENTRIES = ("Tighten", "FreezeAccount", "ReviewTighten", "ReviewFreezeAccount")

    def test_the_lift_states_are_the_only_lifts(self, p2):
        ops = _account_ops(p2)
        assert {n for n, op in ops.items() if op == "lift"} == set(self.LIFT_STATES)

    #: Catchers that are not failures of a check, each closed some other way the graph walk cannot
    #: see because it does not evaluate conditions:
    #:   * a failed applicant message skips the message and carries on to the checks;
    #:   * a failed agent becomes a DEGRADED result, which the first choice in Phase2Decision sends
    #:     to review (asserted below);
    #:   * an unreadable list version becomes UNREADABLE, which no screening matches, so the rules
    #:     refuse to decide (asserted below and in decision-engine/tests/test_facts.py).
    MESSAGE_SKIPS = {"LoadListVersion", "Phase2Complete"}
    DEGRADED_RESULT = {"ComplianceDegraded", "ReScreenDegraded"}
    VALUE_FAIL_CLOSED = {"ListVersionUnreadable"}
    ALLOWED = MESSAGE_SKIPS | DEGRADED_RESULT | VALUE_FAIL_CLOSED

    def test_no_failure_path_reaches_a_lift_without_a_person(self, p2):
        checked = 0
        for name, state in p2["States"].items():
            for catcher in state.get("Catch", []):
                if catcher["Next"] in self.ALLOWED:
                    continue
                checked += 1
                reached = reachable_from(p2, catcher["Next"], blocked=WITHOUT_A_PERSON)
                for lift in self.LIFT_STATES:
                    assert lift not in reached, f"{name} catches into a path that reaches {lift}"
        assert checked > 10, "the walk must not pass vacuously"

    def test_the_allowance_is_narrow(self, p2):
        """Only these states may catch into the checks, and each allowance is closed by value."""
        skipping = {name for name, state in p2["States"].items()
                    for catcher in state.get("Catch", [])
                    if catcher["Next"] in self.ALLOWED}
        assert skipping == {"ReadAccount", "NotifyProvisional", "PersistProvisionalNotice",
                            "LoadListVersion", "ReScreen", "ComplianceRisk", "NotifyOutcome",
                            "PersistOutcomeNotice"}
        for name, branch in (("ComplianceDegraded", "complianceBranch"),
                             ("ReScreenDegraded", "rescreenBranch")):
            assert p2["States"][name]["Assign"][branch]["status"] == "DEGRADED"
        assert p2["States"]["ListVersionUnreadable"]["Assign"]["currentListVersion"] == \
            "UNREADABLE"
        degraded_guard = p2["States"]["Phase2Decision"]["Choices"][0]["Condition"]
        assert "$complianceBranch.status = 'DEGRADED'" in degraded_guard
        assert "$rescreenBranch.status = 'DEGRADED'" in degraded_guard

    def test_the_failure_state_only_fails(self, p2):
        assert reachable_from(p2, "Phase2Failed") == {"Phase2FailedTerminal"}
        assert p2["States"]["Phase2FailedTerminal"]["Type"] == "Fail"
        rendered = json.dumps(p2["States"]["Phase2Failed"])
        assert "restrictionChange" not in rendered and "phase2Pending" not in rendered

    def test_the_failure_state_raises_an_alarm_metric(self, p2):
        args = p2["States"]["Phase2Failed"]["Arguments"]
        assert args["MetricData"][0]["MetricName"] == "Phase2Failed"

    def test_a_lift_happens_only_after_its_decision_record(self, p2):
        """Accounts open, and restrictions lift, only once the record exists."""
        records = {"RecordStandardApproval", "RecordEnhancedApproval", "RecordReviewedApproval"}
        reached = reachable_from(p2, p2["StartAt"], blocked=records)
        for lift in self.LIFT_STATES:
            assert lift not in reached, lift

    def test_restrictions_tighten_before_the_paperwork(self, p2):
        """A failed record write can never leave an account less restricted than decided."""
        assert p2["States"]["FreezeAccount"]["Next"] == "RecordFrozen"
        assert p2["States"]["RecordFrozen"]["Next"] == "WriteFrozenRecord"
        assert p2["States"]["Tighten"]["Next"] == "RecordTightened"
        assert p2["States"]["RecordTightened"]["Next"] == "WriteTightenedRecord"

    def test_a_frozen_account_never_reaches_monitoring(self, p2):
        monitoring = {n for n, s in _state_writers(p2).items()
                      if _writes_state_value(s, "MONITORING")}
        assert monitoring == {"EnrolStandardMonitoring", "EnrolEnhancedMonitoring"}
        for entry in self.RESTRICTIVE_ENTRIES:
            reached = reachable_from(p2, entry)
            assert not reached & monitoring, entry
            assert not reached & set(self.LIFT_STATES), entry

    def test_a_lift_cannot_overwrite_a_freeze_that_landed_meanwhile(self, p2):
        """Phase 3 can freeze while Phase 2 runs. The platform refuses to lift a frozen account,
        and the case write requires the state this execution expects."""
        for name in ("EnrolStandardMonitoring", "EnrolEnhancedMonitoring"):
            args = p2["States"][name]["Arguments"]
            assert args["ConditionExpression"] == "#s = :required"
            assert args["ExpressionAttributeValues"][":required"] == {"S": "PROVISIONAL"}

    def test_an_unrecognised_outcome_does_not_lift_anything(self, p2):
        assert p2["States"]["Phase2Decision"]["Default"] == "Phase2Referral"

    def test_a_degraded_compliance_result_is_checked_before_any_lift(self, p2):
        first = p2["States"]["Phase2Decision"]["Choices"][0]
        assert "DEGRADED" in first["Condition"] and first["Next"] == "ComplianceReferral"

    def test_a_compliance_failure_is_a_degraded_result_not_a_dead_end(self, p2):
        """The facts its tools recorded still count; the rules send an unavailable agent to a
        person, where a Phase2Failed would need an operator instead."""
        assert p2["States"]["ComplianceRisk"]["Catch"][0]["Next"] == "ComplianceDegraded"
        assert p2["States"]["ComplianceDegraded"]["Next"] == "PersistPhase2Evidence"

    def test_all_four_restriction_changes_exist(self, p2):
        """R31.3 through R31.5."""
        changes = {s["Arguments"]["ExpressionAttributeValues"][":r"]["S"]
                   for s in p2["States"].values()
                   if ":r" in (s.get("Arguments") or {}).get("ExpressionAttributeValues", {})}
        assert changes == {"LIFT_ALL", "LIFT_TRANSFERS_KEEP_CEILING", "TIGHTEN", "FREEZE"}

    def test_the_pending_flag_is_cleared_only_by_an_outcome(self, p2):
        clearing = {n for n, s in p2["States"].items()
                    if "REMOVE phase2Pending" in json.dumps(s.get("Arguments") or {})}
        for name in clearing:
            assert "restrictionChange = :r" in p2["States"][name]["Arguments"]["UpdateExpression"]


class TestApplicantMessages:
    def test_the_message_is_written_from_the_account_and_nothing_else(self, p2):
        """Restrictions-only input: the Notify agent cannot reveal what it never sees."""
        for name in ("NotifyProvisional", "NotifyOutcome"):
            prompt = p2["States"][name]["Arguments"]["Messages"][0]["Content"][0]["Text"]
            assert "$account" in prompt
            for forbidden in ("$decision", "$case", "$caseView", "$reviewReason"):
                assert forbidden not in prompt, (name, forbidden)

    def test_a_failed_message_never_undoes_or_blocks_an_outcome(self, p2):
        assert p2["States"]["NotifyProvisional"]["Catch"][0]["Next"] == "LoadListVersion"
        assert p2["States"]["NotifyOutcome"]["Catch"][0]["Next"] == "Phase2Complete"
        assert p2["States"]["ReadAccount"]["Catch"][0]["Next"] == "LoadListVersion"

    def test_messages_are_persisted_for_the_applicant_app(self, p2):
        for name in ("PersistProvisionalNotice", "PersistOutcomeNotice"):
            update = p2["States"][name]["Arguments"]["UpdateExpression"]
            assert update.startswith("SET notifications = list_append(")


# ============================================================================ human review


class TestHumanReview:
    """F-12 / design §7.5 — a separate state, never an agent call."""

    def test_review_uses_sqs_wait_for_task_token(self, phases):
        for definition in phases.values():
            resource = definition["States"]["AwaitReviewDecision"]["Resource"]
            assert resource == "arn:aws:states:::sqs:sendMessage.waitForTaskToken"

    def test_no_agent_task_uses_wait_for_task_token(self, phases):
        for definition in phases.values():
            for name, state in _agent_tasks(definition).items():
                assert ".waitForTaskToken" not in state["Resource"], name

    def test_the_task_token_and_the_phase_travel_in_the_message(self, phases):
        """Nothing downstream can infer the phase: a Phase 2 review sits on a case that already
        has a `phase1` decision, so the record alone is ambiguous."""
        labels = set()
        for phase, definition in phases.items():
            body = definition["States"]["AwaitReviewDecision"]["Arguments"]["MessageBody"]
            assert body["taskToken"] == "{% $states.context.Task.Token %}"
            assert body["phase"] == phase
            labels.add(body["phase"])
        assert len(labels) == 3

    def test_an_unresumable_review_is_caught_explicitly(self, phases):
        for definition in phases.values():
            errors = definition["States"]["AwaitReviewDecision"]["Catch"][0]["ErrorEquals"]
            assert REVIEW_UNRESUMABLE in errors

    def test_the_case_is_marked_and_its_phase_written_before_the_wait(self, phases):
        """The console finds cases through the `state` GSI, and the review handler refuses a case
        that is not AWAITING_REVIEW or whose phase attribute does not exist yet. The phase used to
        be written only after the review, so every Phase 1 review was refused as unresumable."""
        for phase, definition in phases.items():
            mark = definition["States"]["ManualReview"]
            args = mark["Arguments"]
            assert mark["Resource"] == "arn:aws:states:::aws-sdk:dynamodb:updateItem"
            assert args["ExpressionAttributeValues"][":awaiting"] == {"S": "AWAITING_REVIEW"}
            assert args["ExpressionAttributeNames"]["#s"] == "state"
            assert "#s = :awaiting" in args["UpdateExpression"]
            assert f"{phase} = :pending" in args["UpdateExpression"]
            assert "REMOVE reviewTaskToken" in args["UpdateExpression"]
            assert mark["Next"] == "AwaitReviewDecision"
            assert "Catch" not in mark

    def test_the_pending_record_keeps_the_tier(self, phases):
        """The review handler refuses APPROVE on a PROHIBITED tier; it reads it from here."""
        for phase, definition in phases.items():
            pending = definition["States"]["ManualReview"]["Arguments"][
                "ExpressionAttributeValues"][":pending"]["S"]
            assert "$decision" in pending and "'outcome': 'MANUAL_REVIEW'" in pending
        pending3 = phases["phase3"]["States"]["ManualReview"]["Arguments"][
            "ExpressionAttributeValues"][":pending"]["S"]
        assert "'tier': $decision != null ? $decision.newTier : null" in pending3

    def test_every_entry_to_the_wait_goes_through_the_mark(self, phases):
        for phase, definition in phases.items():
            for name, state in definition["States"].items():
                if name == "ManualReview":
                    continue
                assert "AwaitReviewDecision" not in successors(state), (
                    f"{phase}.{name} reaches the wait without marking the case")

    def test_the_transition_is_recorded_with_system_as_actor(self, p1):
        values = p1["States"]["ManualReview"]["Arguments"]["ExpressionAttributeValues"]
        transition = values[":tr"]["L"][0]["M"]
        assert transition["to"] == {"S": "AWAITING_REVIEW"}
        assert transition["actor"] == {"S": "system"}

    def test_the_briefing_runs_first_and_cannot_block_the_review(self, phases):
        for phase, definition in phases.items():
            states = definition["States"]
            assert states["Briefing"]["Next"] == "PersistBriefing", phase
            assert states["Briefing"]["Catch"][0]["Next"] == "ManualReview", phase
            assert states["PersistBriefing"]["Catch"][0]["Next"] == "ManualReview", phase

    def test_the_briefing_never_sees_document_text(self, phases):
        """Printed text is the one place an applicant writes straight into a model's context."""
        for definition in phases.values():
            prompt = definition["States"]["Briefing"]["Arguments"]["Messages"][0][
                "Content"][0]["Text"]
            assert "$k != 'textLines'" in prompt

    def test_every_review_entry_names_its_reason(self, phases):
        for phase, definition in phases.items():
            entries = [(name, state) for name, state in definition["States"].items()
                       if "ReviewGate" in successors(state) and name != "WaitForOpenReview"]
            assert entries, phase
            for name, state in entries:
                assert state["Type"] == "Pass", f"{phase}.{name} enters review without a reason"
                assert "reviewReason" in state.get("Assign", {}), f"{phase}.{name}"

    def test_the_briefing_is_entered_only_through_the_gate(self, phases):
        for phase, definition in phases.items():
            into = {name for name, state in definition["States"].items()
                    if "Briefing" in successors(state)}
            assert into == {"ReviewGate", "IsReviewOpen"}, (phase, into)


class TestReviewGate:
    """A second review on a case waits for the first rather than replacing its task token."""

    def test_an_open_review_is_waited_for_with_a_bound(self, phases):
        for phase, definition in phases.items():
            states = definition["States"]
            choices = states["IsReviewOpen"]["Choices"]
            assert "'AWAITING_REVIEW'" in choices[0]["Condition"]
            assert "$reviewWaits <" in choices[0]["Condition"]
            assert choices[0]["Next"] == "CountReviewWait"
            assert states["CountReviewWait"]["Next"] == "WaitForOpenReview"
            assert states["WaitForOpenReview"]["Type"] == "Wait"
            assert states["WaitForOpenReview"]["Next"] == "ReviewGate"
            assert choices[1]["Next"] == "ReviewStillOpen"
            assert states["ReviewStillOpen"]["Type"] == "Fail"

    def test_the_wait_counter_starts_at_zero_in_every_phase(self, p1, p2, p3):
        for definition, first in ((p1, "NormalizeInput"), (p2, "LoadCase"),
                                  (p3, "LoadDayZero")):
            assert definition["States"][first]["Assign"]["reviewWaits"] == "{% 0 %}"

    def test_a_case_frozen_or_closed_meanwhile_needs_no_review(self, phases):
        expected = {"phase1": "ReviewNotNeeded", "phase2": "Phase2Failed", "phase3": "Done"}
        for phase, definition in phases.items():
            choice = definition["States"]["IsReviewOpen"]["Choices"][2]
            assert "['FROZEN', 'DECIDED']" in choice["Condition"]
            assert choice["Next"] == expected[phase]

    def test_the_wait_stays_inside_the_execution_history_limit(self):
        from kyc.asl import REVIEW_GATE_MAX_WAITS, REVIEW_GATE_WAIT_SECONDS

        # Nine history events per round trip; a Standard execution allows 25,000.
        assert REVIEW_GATE_MAX_WAITS * 9 < 20_000
        assert REVIEW_GATE_MAX_WAITS * REVIEW_GATE_WAIT_SECONDS <= 7 * 24 * 3600


class TestListCurrency:
    """No phase decides on a screening of an outdated list."""

    TIERS = {"phase1": "PreliminaryTier", "phase2": "FullTier", "phase3": "Reassess"}

    def test_every_tier_is_told_the_list_in_force(self, phases):
        for phase, definition in phases.items():
            payload = definition["States"][self.TIERS[phase]]["Arguments"]["Payload"]
            assert payload["currentListVersion"] == "{% $currentListVersion %}", phase
            reached = reachable_from(definition, "LoadListVersion")
            assert self.TIERS[phase] in reached, phase

    def test_the_version_is_read_from_the_list_update_parameter_and_fails_closed(self, phases):
        for phase, definition in phases.items():
            load = definition["States"]["LoadListVersion"]
            assert load["Resource"] == "arn:aws:states:::aws-sdk:ssm:getParameter"
            assert load["Arguments"]["Name"] == "/kyc90/screening/list-overlay"
            assert load["Catch"] == [{"ErrorEquals": ["States.ALL"],
                                      "Next": "ListVersionUnreadable"}]

    def test_phase2_rescreens_only_when_the_list_moved(self, p2):
        check = p2["States"]["CheckListCurrent"]
        condition = check["Choices"][0]["Condition"]
        assert "$parse($case.factScreening.S).listVersion = $currentListVersion" in condition
        assert check["Choices"][0]["Next"] == "ComplianceRisk"
        assert check["Default"] == "ReScreen"
        assert p2["States"]["ReScreen"]["Arguments"]["HarnessArn"] == HARNESS_ARNS["prescreen"]

    def test_the_rescreen_is_evidence_for_the_record_and_the_rules(self, p2):
        evidence = p2["States"]["PersistPhase2Evidence"]["Arguments"][
            "ExpressionAttributeValues"][":e"]["S"]
        assert "$rescreenBranch" in evidence and "$append(" in evidence
        results = p2["States"]["FullTier"]["Arguments"]["Payload"]["agentResults"]
        assert "'prescreen': $rescreenBranch" in results


class TestHolds:
    """A Phase 3 restriction cannot be undone by a decision made on older evidence."""

    def test_phase3_restrictions_are_held_by_the_event(self, p3):
        for name in ("RestrictForReview", "ReapplyRestrictions"):
            args = p3["States"][name]["Arguments"]["Payload"]["arguments"]
            assert args["hold"] == "{% $riskEvent.eventId %}", name

    def test_only_the_reviewers_clearance_releases_the_hold(self, p3):
        releasing = {name for name, state in p3["States"].items()
                     if "releaseHolds" in json.dumps(state.get("Arguments") or {})}
        assert releasing == {"RestoreStandard", "RestoreEnhanced", "RestoreTightened",
                             "RestoreProvisional"}
        for name in releasing:
            args = p3["States"][name]["Arguments"]["Payload"]["arguments"]
            assert args["releaseHolds"] == "{% [$riskEvent.eventId] %}"
            assert name in reachable_from(p3, "AwaitReviewDecision")

    def test_no_phase2_lift_releases_a_hold(self, p2):
        """Phase 2 never knows about a hold, so the platform refuses its lift below one."""
        assert "releaseHolds" not in json.dumps(p2)
        assert "hold" not in json.dumps(
            {n: s for n, s in p2["States"].items() if n in _account_ops(p2)})

    def test_a_tightened_day_zero_is_restored_as_tightened(self, p3):
        choices = p3["States"]["RestoreDayZeroPosture"]["Choices"]
        assert {"Next": "RestoreTightened",
                "Condition": "{% $day0Posture = 'TIGHTENED' %}"} in choices

    def test_the_machine_does_not_rewrite_the_reviewers_state(self, phases):
        """The review handler moves the case with the reviewer as the actor. A second write here
        would record a transition nobody made."""
        for phase, definition in phases.items():
            after = reachable_from(definition, "RouteReviewOutcome")
            writers = set(_state_writers(definition)) & after
            assert not writers, f"{phase}: {sorted(writers)} rewrite the reviewer's state"

    def test_the_reviewers_identity_reaches_the_record(self, phases):
        for phase, definition in phases.items():
            after = reachable_from(definition, "RouteReviewOutcome")
            audits = {n: s for n, s in _audit_states(definition).items() if n in after}
            assert audits, phase
            for name, state in audits.items():
                assert state["Arguments"]["Payload"]["actor"] == \
                    "{% $reviewOutcome.reviewerId %}", f"{phase}.{name}"


class TestReviewDecisionsAreCarriedOut:
    """A reviewer's decision used to be written onto the case and then ignored: every outcome went
    to the same persistence state, and an APPROVE never opened an account."""

    def _route(self, definition):
        return {c["Condition"].split("'")[1]: c["Next"]
                for c in definition["States"]["RouteReviewOutcome"]["Choices"]}

    def test_phase1_approve_opens_the_account_and_starts_phase2(self, p1):
        route = self._route(p1)
        reached = reachable_from(p1, route["APPROVE"])
        assert {"OpenProvisionalAfterReview", "OpenAccount", "TriggerPhase2"} <= reached
        assert route["APPROVE"] in _audit_states(p1), "the record comes first"

    def test_phase1_reject_and_step_up_are_recorded_and_open_nothing(self, p1):
        route = self._route(p1)
        for action in ("REJECT", "REQUEST_STEP_UP"):
            reached = reachable_from(p1, route[action])
            assert "OpenAccount" not in reached, action
            assert reached & set(_audit_states(p1)), action

    def test_phase2_approve_lifts_to_enhanced_never_standard(self, p2):
        route = self._route(p2)
        reached = reachable_from(p2, route["APPROVE"])
        assert "ReviewLiftTransfersKeepCeiling" in reached
        assert "LiftAllRestrictions" not in reached
        posture = p2["States"]["ReviewLiftTransfersKeepCeiling"]["Arguments"]["Payload"][
            "arguments"]["posture"]
        assert posture == "ENHANCED"

    def test_phase2_reject_freezes_and_step_up_tightens(self, p2):
        route = self._route(p2)
        assert _account_ops(p2)[route["REJECT"]] == "freeze"
        assert _account_ops(p2)[route["REQUEST_STEP_UP"]] == "restrict"

    def test_phase3_review_is_clear_or_exit(self, p3):
        route = self._route(p3)
        assert set(route) == {"APPROVE", "REJECT"}
        assert _account_ops(p3)[route["REJECT"]] == "freeze"
        reached = reachable_from(p3, route["APPROVE"])
        assert {"RestoreStandard", "RestoreEnhanced", "RestoreProvisional"} <= reached

    def test_an_unknown_action_stops_rather_than_guessing(self, phases):
        for definition in phases.values():
            assert definition["States"]["RouteReviewOutcome"]["Default"] == "ReviewNotActionable"
            assert definition["States"]["ReviewNotActionable"]["Type"] == "Fail"


# ============================================================================ audit trail


class TestAuditTrail:
    def test_the_record_is_written_before_the_account_opens(self, p1):
        assert p1["States"]["WriteDecisionRecord"]["Next"] == "OpenProvisional"
        assert p1["States"]["OpenProvisional"]["Next"] == "OpenAccount"

    def test_every_rules_outcome_is_recorded(self, p1):
        """REJECT and STEP_UP used to end at PersistOutcome with no record at all."""
        assert p1["States"]["PersistOutcome"]["Next"] == "NeedsOutcomeRecord"
        choice = p1["States"]["NeedsOutcomeRecord"]
        assert choice["Choices"][0]["Next"] == "RecordOutcome"
        assert p1["States"]["RecordOutcome"]["Catch"][0]["Next"] == "OutcomeRecordFailed"
        assert p1["States"]["OutcomeRecordFailed"]["Type"] == "Fail"

    def test_evidence_is_persisted_and_facts_reread_before_the_tier(self, p1):
        """R2.3, and the facts were written by the tools while the branches ran."""
        assert p1["States"]["Phase1Parallel"]["Next"] == "PersistEvidence"
        assert p1["States"]["PersistEvidence"]["Next"] == "ReloadFacts"
        assert p1["States"]["ReloadFacts"]["Next"] == "LoadListVersion"
        assert p1["States"]["LoadListVersion"]["Next"] == "PreliminaryTier"
        reload_state = p1["States"]["ReloadFacts"]
        assert reload_state["Arguments"]["ConsistentRead"] is True
        assert reload_state["Assign"]["case"] == "{% $states.result.Item %}"

    def test_every_phase_persists_its_agent_runs_for_the_record(self, p1, p2, p3):
        """The audit writer's AGENT_RUN evidence is `<phase>Evidence`; Phase 2 never wrote one."""
        for definition, name, attribute in ((p1, "PersistEvidence", "phase1Evidence"),
                                            (p2, "PersistPhase2Evidence", "phase2Evidence"),
                                            (p3, "PersistPhase3Evidence", "phase3Evidence")):
            assert definition["States"][name]["Arguments"]["UpdateExpression"].startswith(
                f"SET {attribute} = :e")

    def test_every_record_carries_all_four_pins(self, phases):
        for phase, definition in phases.items():
            for name, state in _audit_states(definition).items():
                payload = state["Arguments"]["Payload"]
                for field in PIN_FIELDS:
                    assert payload[field] == PINS[field], f"{phase}.{name}.{field}"

    def test_an_incomplete_record_is_not_retried(self, phases):
        """The writer raises on an incomplete record, deterministically. Retrying delays the
        catcher that makes it visible."""
        for definition in phases.values():
            for name, state in _audit_states(definition).items():
                for retrier in state["Retry"]:
                    assert "States.ALL" not in retrier["ErrorEquals"], name


class TestEveryStateChangeIsAudited:
    """R2.5 as a class invariant: a write to `state` appends to `transitions` in the same
    expression. Two separate updates could interleave and leave record and history disagreeing."""

    def test_at_least_one_state_writer_exists_in_each_phase(self, phases):
        for phase, definition in phases.items():
            assert _state_writers(definition), f"no state writer found in {phase}"

    def test_writing_state_always_appends_a_transition(self, phases):
        offenders = [f"{phase}.{name}"
                     for phase, definition in phases.items()
                     for name, state in _state_writers(definition).items()
                     if "transitions = list_append(" not in state["Arguments"]["UpdateExpression"]]
        assert not offenders, offenders


class TestRunsCanBeFollowedLive:
    """The console's live view reads each phase's execution history while it runs. Pipes and
    EventBridge start Phase 1 and Phase 3 under generated names, so the runs name themselves on
    the case; Phase 2 is started under the provisional decision id, already on the case."""

    NAME = {"S": "{% $states.context.Execution.Name %}"}

    def test_the_phase1_claim_names_the_execution(self, p1):
        claim = p1["States"]["ClaimRun"]["Arguments"]
        assert "phase1Execution = :x" in claim["UpdateExpression"]
        assert claim["ExpressionAttributeValues"][":x"] == self.NAME

    def test_phase3_names_its_execution_before_any_work(self, p3):
        assert p3["States"]["CheckCaseExists"]["Default"] == "RecordExecution"
        record = p3["States"]["RecordExecution"]
        assert record["Arguments"]["UpdateExpression"] == "SET phase3Execution = :x"
        assert record["Arguments"]["ExpressionAttributeValues"][":x"] == self.NAME
        assert record["Next"] == "ReadDayZero"

    def test_naming_phase3_never_stands_in_the_way(self, p3):
        """Bookkeeping: it must not create a case (UpdateItem upserts) nor stop a re-assessment."""
        record = p3["States"]["RecordExecution"]
        assert record["Arguments"]["ConditionExpression"] == "attribute_exists(applicantId)"
        assert record["Catch"] == [{"ErrorEquals": ["States.ALL"], "Next": "ReadDayZero"}]

    def test_phase2_is_started_under_the_id_the_case_holds(self, p1):
        start = p1["States"]["TriggerPhase2"]["Arguments"]
        assert start["Name"] == "{% $provisionalDecisionId %}"
        assert ("provisionalDecisionId = :id"
                in p1["States"]["OpenProvisional"]["Arguments"]["UpdateExpression"])

    def test_each_agent_run_records_the_model_its_task_asked_for(self, phases):
        """The model on the record is the definition's literal, never what an agent reported."""
        checked = 0

        def walk(states: dict) -> None:
            nonlocal checked
            for name, state in states.items():
                for branch in state.get("Branches", []):
                    walk(branch.get("States", {}))
                # An evidence run is followed by its emptiness check; Notify and Briefing are not.
                check = states.get(state.get("Next", ""))
                if state.get("Resource") != INVOKE_HARNESS or not check or check["Type"] != "Choice":
                    continue
                model = state["Arguments"]["Model"]["BedrockModelConfig"]["ModelId"]
                ok = states[check["Default"]]
                record = ok.get("Output") or next(iter(ok.get("Assign", {}).values()), {})
                assert record.get("model") == model, name
                checked += 1

        for definition in phases.values():
            walk(definition["States"])
        # The four evidence agents, Phase 2's re-screen and compliance, Phase 3's re-screen.
        assert checked == 7


# ============================================================== rules Step Functions enforces


class TestCatcherRulesStepFunctionsEnforces:
    """Rules that only surface at deploy time, pinned here instead. `States.ALL must appear alone
    and at end of list` cost a rolled-back stack; so did a duplicate state name."""

    def test_states_all_is_alone_and_last(self, phases):
        for phase, definition in phases.items():
            for name, state in _all_states(definition).items():
                for field in ("Catch", "Retry"):
                    entries = state.get(field, [])
                    for index, entry in enumerate(entries):
                        if "States.ALL" in entry["ErrorEquals"]:
                            assert entry["ErrorEquals"] == ["States.ALL"], (phase, name, field)
                            if field == "Catch":
                                assert index == len(entries) - 1, (phase, name)

    def test_state_names_are_unique_across_the_whole_machine(self, phases):
        for phase, definition in phases.items():
            names: list[str] = []

            def collect(states: dict) -> None:
                for name, state in states.items():
                    names.append(name)
                    for branch in state.get("Branches", []):
                        collect(branch.get("States", {}))

            collect(definition["States"])
            duplicates = {n for n in names if names.count(n) > 1}
            assert not duplicates, f"{phase}: duplicate state names {sorted(duplicates)}"

    def test_each_degraded_branch_terminal_names_its_agent(self, p1):
        terminals = {name for branch in p1["States"]["Phase1Parallel"]["Branches"]
                     for name in branch["States"] if name.endswith("Degraded")}
        assert terminals == {"DocumentsDegraded", "IdentityDegraded", "FraudDegraded",
                             "PreScreenDegraded"}

    def test_no_wire_format_attribute_value_members(self, phases):
        """Step Functions accepts only PascalCase members (`Bool`, `Null`) in writes and returns
        them on reads, inside JSONata strings as much as in the JSON."""
        for phase, definition in phases.items():
            blob = json.dumps(definition)
            for member in ("BOOL", "NULL"):
                for spelling in (f'"{member}"', f"'{member}'", f".{member}"):
                    assert spelling not in blob, (phase, spelling)

    def test_conditional_failures_are_caught_in_both_spellings(self, phases):
        for phase, definition in phases.items():
            for name, state in definition["States"].items():
                for catcher in state.get("Catch", []):
                    errors = catcher["ErrorEquals"]
                    if any("ConditionalCheckFailed" in e for e in errors):
                        assert "DynamoDb.ConditionalCheckFailedException" in errors, (phase, name)
                        assert "DynamoDB.ConditionalCheckFailedException" in errors, (phase, name)


class TestTheAccountPlatformContract:
    def test_every_operation_the_graphs_call_exists(self, phases):
        for phase, definition in phases.items():
            for name, op in _account_ops(definition).items():
                assert op in account_stub.OPERATIONS, f"{phase}.{name} calls unknown {op}"

    def test_every_posture_the_graphs_name_exists(self, phases):
        postures = {p.value for p in account_stub.Posture}
        for phase, definition in phases.items():
            for name, state in definition["States"].items():
                args = (state.get("Arguments") or {}).get("Payload", {})
                posture = args.get("arguments", {}).get("posture") if isinstance(args, dict) \
                    else None
                if posture:
                    assert posture in postures, f"{phase}.{name}: {posture}"
