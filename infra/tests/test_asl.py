"""State machine graph properties — tasks 6.2, 6.4, 6.5.

These tests walk the ASL graph rather than checking that particular strings are present. The two
requirements that matter most here are *negative* — `DegradedPath` must not reach
`OpenProvisional`, and no Phase 2 failure path may reach a state that lifts a restriction — and a
negative property about reachability cannot be verified by reading a single state in isolation.

That is the whole reason the definitions are plain dicts (design §12.3): a synthesised construct
tree cannot be traversed legibly, so the invariant would only be checkable by eye in the console,
which is to say not checkable at all.
"""

from __future__ import annotations

import pytest
from kyc.asl import (
    AGENT_TIMEOUT_SECONDS,
    INVOKE_HARNESS,
    REVIEW_UNRESUMABLE,
    RUNTIME_SESSION_ID,
    phase1_definition,
    phase2_definition,
)

AGENT_KEYS = ("router", "documents", "identity", "fraud", "prescreen", "compliance", "notify")
# F-85: plain harness ARNs. An endpoint-suffixed ARN is rejected by the invokeHarness integration
# with "Invalid harness ARN format", so the pinned endpoint travels in `Qualifier` instead.
HARNESS_ARNS = {
    k: f"arn:aws:bedrock-agentcore:us-east-1:633898796092:harness/kyc90_{k}-x"
    for k in AGENT_KEYS
}
ENDPOINTS = HARNESS_ARNS
MODELS = {k: "us.amazon.nova-2-lite-v1:0" for k in AGENT_KEYS}
COMMON = dict(
    case_table="kyc90-cases",
    harness_arns=HARNESS_ARNS,
    model_ids=MODELS,
    decision_engine_arn="arn:aws:lambda:us-east-1:633898796092:function:kyc90-decision",
    audit_writer_arn="arn:aws:lambda:us-east-1:633898796092:function:kyc90-audit",
    review_queue_url="https://sqs.us-east-1.amazonaws.com/633898796092/kyc90-review",
)


@pytest.fixture(scope="module")
def p1():
    return phase1_definition(**COMMON, phase2_state_machine_arn="arn:aws:states:::sm/phase2")


@pytest.fixture(scope="module")
def p2():
    return phase2_definition(**COMMON)


def successors(state: dict) -> set[str]:
    """Every state this one can transition to at its own graph level.

    Deliberately does not descend into `Branches`. A Parallel branch is a self-contained graph
    whose state names resolve only within that branch, so folding its targets into the parent's
    successor set would both report false missing states and, worse, make a branch-internal
    terminal look like a parent transition when checking reachability.
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


def reachable_from(definition: dict, start: str) -> set[str]:
    """Transitive closure. Branch-internal states are resolved through `successors`."""
    states = definition["States"]
    seen: set[str] = set()
    frontier = [start]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        state = states.get(name)
        if state is None:
            continue
        frontier.extend(successors(state))
    return seen - {start}


class TestPhase1Structure:
    def test_starts_at_normalize_input(self, p1):
        """Task 7.3/A-05: the pipe (no InputTemplate) delivers a raw SQS record batch, not the
        bare applicant object `LoadCase` expects — `NormalizeInput` reconciles that shape and
        captures the ingress timestamp before `LoadCase` runs."""
        assert p1["StartAt"] == "NormalizeInput"
        assert p1["States"]["NormalizeInput"]["Next"] == "LoadCase"

    def test_jsonata_everywhere(self, p1):
        """F-71 — `Arguments` and `{% %}` do not exist under JSONPath, and the validation error
        names neither, so a single missed state is a confusing build failure."""
        assert p1["QueryLanguage"] == "JSONata"
        for name, state in p1["States"].items():
            if state["Type"] in ("Fail",):
                continue
            assert state.get("QueryLanguage") == "JSONata", f"{name} is not JSONata"

    def test_every_transition_target_exists(self, p1):
        names = set(p1["States"])
        for name, state in p1["States"].items():
            for target in successors(state):
                assert target in names, f"{name} -> missing state {target}"

    def test_every_branch_internal_target_exists(self, p1):
        """Branch names resolve only inside their branch, so they need checking separately —
        a dangling target here fails at deploy with an error that names the branch index, not
        the state."""
        for index, branch in enumerate(p1["States"]["Phase1Parallel"]["Branches"]):
            inner = branch["States"]
            assert branch["StartAt"] in inner, f"branch {index} StartAt is dangling"
            for name, state in inner.items():
                for target in successors(state):
                    assert target in inner, f"branch {index}: {name} -> missing {target}"

    def test_every_branch_state_continues_or_ends(self, p1):
        """A branch state that neither continues nor terminates stalls the whole Parallel.

        A `Choice` continues through `Choices`/`Default` rather than `Next`, so it counts as
        continuing — an earlier version of this test rejected valid Choice states.
        """
        for branch in p1["States"]["Phase1Parallel"]["Branches"]:
            for name, state in branch["States"].items():
                continues = bool(
                    state.get("End") or state.get("Next")
                    or state.get("Default") or state.get("Choices"))
                assert continues, f"{name} neither ends nor continues"

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
        """Measured live: the Fraud agent finished with StopReason `timeout_exceeded` and
        `Content: []`. No error, no Catch — and read naively, "no findings" becomes "no risk"."""
        for branch in p1["States"]["Phase1Parallel"]["Branches"]:
            check = next(s for n, s in branch["States"].items() if n.endswith("Check"))
            condition = check["Choices"][0]["Condition"]
            assert "count($states.input.Output.Message.Content) = 0" in condition
            assert "timeout_exceeded" in condition
            assert check["Choices"][0]["Next"].endswith("Degraded")

    def test_branch_output_is_keyed_by_agent(self, p1):
        """The raw harness envelope carries no agent name, so the decision engine could not build
        its `agentResults` map and rejected the payload — failing closed in a way that looked
        identical to a real partial failure."""
        for branch in p1["States"]["Phase1Parallel"]["Branches"]:
            for name, state in branch["States"].items():
                if name.endswith(("Ok", "Degraded")):
                    assert "agent" in state["Output"]
                    assert state["Output"]["status"] in ("OK", "DEGRADED")

    def test_every_phase1_agent_fits_the_phase_budget(self, p1):
        """With reasoning removed from Fraud (F-88) no Phase 1 agent needs the extended budget,
        so all four sit at 90s and the four branches run in parallel inside it."""
        for name, state in _agent_tasks(p1).items():
            assert state["Arguments"]["TimeoutSeconds"] == 90, name

    def test_the_high_effort_agent_still_gets_the_longer_budget(self, p2):
        """Phase 2 has no clock pressure, which is the whole reason D-08 puts A6 there."""
        assert p2["States"]["ComplianceRisk"]["Arguments"]["TimeoutSeconds"] == 600

    def test_all_five_phase1_agents_are_invoked(self, p1):
        invoked = _harness_arns(p1)
        for key in ("router", "documents", "identity", "fraud", "prescreen"):
            assert ENDPOINTS[key] in invoked, f"{key} is never invoked"

    def test_no_phase2_agent_runs_in_phase1(self, p1):
        """D-08 — Compliance Risk runs at high effort and cannot fit the 90s budget."""
        invoked = _harness_arns(p1)
        assert ENDPOINTS["compliance"] not in invoked
        assert ENDPOINTS["notify"] not in invoked


class TestPreconditionGuard:
    """R38 — the guard that makes the expensive part of the graph unreachable."""

    def test_preconditions_sit_before_the_router(self, p1):
        assert p1["States"]["CheckIdempotency"]["Default"] == "CheckPreconditions"
        assert p1["States"]["CheckPreconditions"]["Default"] == "Router"

    def test_no_agent_is_reachable_without_passing_preconditions(self, p1):
        """The point of R38: a direct POST could otherwise run four agents and reach
        OpenProvisional with no liveness evidence, violating R1.3 at full cost."""
        for target in ("StepUpLivenessRequired", "StepUpLivenessFailed",
                       "StepUpDocumentsRequired"):
            reached = reachable_from(p1, target)
            assert "Router" not in reached
            assert "Phase1Parallel" not in reached

    def test_missing_liveness_steps_up_rather_than_rejects(self, p1):
        """A missing precondition is a process gap, not an applicant failure."""
        for name in ("StepUpLivenessRequired", "StepUpLivenessFailed",
                     "StepUpDocumentsRequired"):
            assert p1["States"][name]["Output"]["outcome"] == "STEP_UP"

    def test_the_guard_costs_zero_agent_invocations(self, p1):
        assert p1["States"]["StepUpLivenessRequired"]["Output"]["agentsInvoked"] == 0

    def test_all_three_precondition_reasons_are_distinct(self, p1):
        reasons = {p1["States"][n]["Output"]["reason"] for n in
                   ("StepUpLivenessRequired", "StepUpLivenessFailed", "StepUpDocumentsRequired")}
        assert reasons == {"LIVENESS_REQUIRED", "LIVENESS_FAILED", "DOCUMENTS_REQUIRED"}


class TestDegradedPathCannotApprove:
    """R13.5 and failure demo 2 — the single most important structural property in Phase 1."""

    def test_degraded_path_cannot_reach_open_provisional(self, p1):
        assert "OpenProvisional" not in reachable_from(p1, "DegradedPath")

    def test_degraded_path_cannot_reach_phase2(self, p1):
        """Approving downstream would be the same failure wearing a different name."""
        reached = reachable_from(p1, "DegradedPath")
        assert "TriggerPhase2" not in reached
        assert "WriteDecisionRecord" not in reached

    def test_degraded_path_routes_to_human_review(self, p1):
        assert p1["States"]["DegradedPath"]["Next"] == "ManualReview"

    def test_every_agent_task_catches_to_a_non_approving_state(self, p1):
        """An uncaught agent failure would fail the execution outright, turning a degraded
        decision into an outage."""
        for name, state in _agent_tasks(p1).items():
            catchers = state.get("Catch", [])
            assert catchers, f"{name} has no Catch"
            for catcher in catchers:
                assert "OpenProvisional" not in reachable_from(p1, catcher["Next"]), name

    def test_a_degraded_branch_is_checked_before_any_approval(self, p1):
        """First condition wins in a Choice, so ordering is the control here."""
        first = p1["States"]["Phase1Decision"]["Choices"][0]
        assert "DEGRADED" in first["Condition"]
        assert first["Next"] == "DegradedPath"


class TestIdempotency:
    """R21 — task 6.3."""

    def test_idempotency_is_checked_before_any_agent(self, p1):
        reached = reachable_from(p1, "ReturnCached")
        assert reached == set(), "ReturnCached must be terminal"
        assert p1["States"]["LoadCase"]["Next"] == "CheckIdempotency"

    def test_a_cached_decision_invokes_no_agent(self, p1):
        assert p1["States"]["ReturnCached"]["Type"] == "Succeed"

    def test_the_key_is_applicant_plus_event(self, p1):
        cond = p1["States"]["CheckIdempotency"]["Choices"][0]["Condition"]
        assert "$applicantId" in cond and "$eventId" in cond

    def test_the_key_is_persisted_on_every_outcome(self, p1):
        """Otherwise a replay of a STEP_UP or REJECT would re-run the whole pipeline."""
        persist = p1["States"]["PersistOutcome"]
        assert ":k" in persist["Arguments"]["ExpressionAttributeValues"]

    def test_open_provisional_is_a_conditional_write(self, p1):
        """R21.4 — two concurrent executions must not both open an account."""
        assert "attribute_not_exists(phase1)" in \
            p1["States"]["OpenProvisional"]["Arguments"]["ConditionExpression"]

    def test_a_lost_conditional_write_goes_to_review_not_to_approval(self, p1):
        catchers = p1["States"]["OpenProvisional"]["Catch"]
        assert catchers[0]["Next"] == "ManualReview"

    def test_phase2_start_is_named_for_idempotency(self, p1):
        """R21.7 — a duplicate StartExecution with the same name is rejected by Step Functions,
        so screening cannot run twice for one provisional decision."""
        args = p1["States"]["TriggerPhase2"]["Arguments"]
        assert "$applicantId" in args["Name"] and "decisionId" in args["Name"]


class TestPhase2IsNotAwaited:
    def test_start_execution_is_not_sync(self, p1):
        """R13.11. `.sync` would put a multi-minute screening job inside the 90-second budget
        and collapse the two-phase design into a one-phase one."""
        resource = p1["States"]["TriggerPhase2"]["Resource"]
        assert resource == "arn:aws:states:::states:startExecution"
        assert ".sync" not in resource

    def test_phase1_ends_at_trigger_phase2(self, p1):
        assert p1["States"]["TriggerPhase2"].get("End") is True


class TestAgentTaskShape:
    """design §7.3, and the facts that make it work."""

    def test_resource_uri_has_no_hyphen(self):
        assert INVOKE_HARNESS.endswith("bedrockagentcore:invokeHarness")
        assert "bedrock-agentcore:invokeHarness" not in INVOKE_HARNESS

    def test_arns_do_have_the_hyphen(self, p1):
        for arn in _harness_arns(p1):
            assert arn.startswith("arn:aws:bedrock-agentcore:")

    def test_the_harness_arn_is_not_endpoint_suffixed(self, p1, p2):
        """F-85, found by gate G9. Every suffix form is rejected with "Invalid harness ARN
        format" — including `/endpoint/{version}`, which design §7.3 originally showed. A full
        execution would have hidden this: the Catch fires and the case goes to review, which is
        indistinguishable from a legitimate degraded decision."""
        for definition in (p1, p2):
            for name, state in _agent_tasks(definition).items():
                arn = state["Arguments"]["HarnessArn"]
                assert "/endpoint/" not in arn, name
                assert "/harness-endpoint/" not in arn, name
                assert arn.count(":harness/") == 1 and arn.count("/") == 1, name

    def test_the_endpoint_is_pinned_by_qualifier(self, p1, p2):
        """Endpoint pinning is not lost by dropping it from the ARN — it moves to `Qualifier`,
        which is what keeps task 5.4's rollback story and R18.6's endpoint name intact."""
        for definition in (p1, p2):
            for name, state in _agent_tasks(definition).items():
                assert state["Arguments"]["Qualifier"] == "live", name

    def test_runtime_session_id_meets_the_minimum_length(self, p1):
        """F-36 — 33 characters minimum; `$uuid()` yields 36."""
        assert RUNTIME_SESSION_ID == "{% $uuid() %}"
        for state in _agent_tasks(p1).values():
            assert state["Arguments"]["RuntimeSessionId"] == RUNTIME_SESSION_ID

    def test_every_agent_task_is_below_the_task_ceiling(self, p1, p2):
        """F-14 — the hard cap is 15 minutes."""
        for definition in (p1, p2):
            for name, state in _agent_tasks(definition).items():
                assert state["Arguments"]["TimeoutSeconds"] < 900, name

    def test_throttling_is_retried_three_times_with_backoff(self, p1):
        for name, state in _agent_tasks(p1).items():
            retry, = state["Retry"]
            assert retry["ErrorEquals"] == ["BedrockAgentCore.ThrottlingException"], name
            assert retry["MaxAttempts"] == 3
            assert retry["BackoffRate"] == 2.0

    def test_reasoning_config_nests_under_additional_model_request_fields(self, p2):
        """F-61 — a bare `reasoningConfig` is rejected, because `AdditionalParams` is spliced
        into a Converse request and accepts only Converse's surface."""
        model = p2["States"]["ComplianceRisk"]["Arguments"]["Model"]["BedrockModelConfig"]
        nested = model["AdditionalParams"]["additionalModelRequestFields"]["reasoningConfig"]
        assert nested["maxReasoningEffort"] == "high"

    def test_the_high_effort_agent_does_not_pin_sampling(self, p2):
        """F-62 — at high effort the call fails outright if Temperature or MaxTokens are set."""
        model = p2["States"]["ComplianceRisk"]["Arguments"]["Model"]["BedrockModelConfig"]
        assert "Temperature" not in model
        assert "MaxTokens" not in model

    def test_non_reasoning_agents_do_pin_sampling(self, p1):
        """R26.3 — an unpinned temperature makes a decision unreproducible in an audit record."""
        model = p1["States"]["Router"]["Arguments"]["Model"]["BedrockModelConfig"]
        assert model["Temperature"] == 0.0

    def test_no_phase1_agent_reasons_at_all(self, p1):
        """F-88. Fraud was the last reasoning agent in Phase 1 and it never converged: two live
        runs returned `Content: []`, at 55.7s/8,525 tokens pinned and 132s/16,629 tokens unpinned.
        Velocity signals are tool lookups, so there was no chain of reasoning to build and the
        budget bought nothing while consuming the whole phase allowance."""
        for name, state in _agent_tasks(p1).items():
            model = state["Arguments"]["Model"]["BedrockModelConfig"]
            reasoning = (model.get("AdditionalParams", {})
                         .get("additionalModelRequestFields", {})
                         .get("reasoningConfig"))
            assert reasoning is None, f"{name} reasons inside the 90s budget"

    def test_phase1_agents_pin_sampling_again(self, p1):
        """With reasoning gone, R26.3's reproducibility requirement is satisfiable in Phase 1."""
        for name, state in _agent_tasks(p1).items():
            model = state["Arguments"]["Model"]["BedrockModelConfig"]
            assert model["Temperature"] == 0.0, name

    def test_no_reasoning_agent_anywhere_pins_sampling(self, p1, p2):
        for definition in (p1, p2):
            for name, state in _agent_tasks(definition).items():
                model = state["Arguments"]["Model"]["BedrockModelConfig"]
                reasoning = (model.get("AdditionalParams", {})
                             .get("additionalModelRequestFields", {})
                             .get("reasoningConfig"))
                if reasoning:
                    assert "Temperature" not in model, name
                    assert "MaxTokens" not in model, name


class TestPhase2RestrictionsNeverLapse:
    """R31.6 / Property 3 — the negative invariant. Silence is not permission."""

    LIFT_STATES = ("LiftAllRestrictions", "LiftTransfersKeepCeiling")

    def test_no_failure_path_reaches_a_lift_state(self, p2):
        reached = reachable_from(p2, "Phase2Failed")
        for lift in self.LIFT_STATES:
            assert lift not in reached, f"a Phase 2 failure can reach {lift}"

    def test_the_failure_state_does_not_touch_restrictions(self, p2):
        """It must leave the case exactly as Phase 1 left it: restricted."""
        failed = p2["States"]["Phase2Failed"]
        rendered = str(failed)
        assert "restrictionChange" not in rendered
        assert "REMOVE phase2Pending" not in rendered

    def test_the_failure_state_terminates_in_a_fail(self, p2):
        assert reachable_from(p2, "Phase2Failed") == {"Phase2FailedTerminal"}
        assert p2["States"]["Phase2FailedTerminal"]["Type"] == "Fail"

    def test_the_failure_state_raises_an_alarm_metric(self, p2):
        """R31.6 requires the alarm, not just the persistence."""
        args = p2["States"]["Phase2Failed"]["Arguments"]
        assert args["MetricData"][0]["MetricName"] == "Phase2Failed"

    def test_an_unrecognised_outcome_does_not_lift_anything(self, p2):
        """So adding a new Phase 2 outcome later cannot accidentally start lifting."""
        assert p2["States"]["Phase2Decision"]["Default"] == "ManualReview"

    def test_every_compliance_failure_catches_to_the_persist_path(self, p2):
        for catcher in p2["States"]["ComplianceRisk"]["Catch"]:
            assert catcher["Next"] == "Phase2Failed"

    def test_notify_failure_does_not_undo_a_restriction_change(self, p2):
        """The copy failing is not a reason to revisit a decision that already landed."""
        assert p2["States"]["Notify"]["Catch"][0]["Next"] == "NotifyFailed"
        assert p2["States"]["NotifyFailed"]["Next"] == "EnrolMonitoring"

    def test_all_four_restriction_transitions_exist(self, p2):
        """R31.3 through R31.5."""
        changes = {
            name: p2["States"][name]["Arguments"]["ExpressionAttributeValues"][":r"]["S"]
            for name in ("LiftAllRestrictions", "LiftTransfersKeepCeiling", "Tighten",
                         "FreezeAndNotify")
        }
        assert set(changes.values()) == {
            "LIFT_ALL", "LIFT_TRANSFERS_KEEP_CEILING", "TIGHTEN", "FREEZE"}


class TestHumanReview:
    """F-12 / design §7.5 — a separate state, never an agent call."""

    def test_review_uses_sqs_wait_for_task_token(self, p1):
        resource = p1["States"]["AwaitReviewDecision"]["Resource"]
        assert resource == "arn:aws:states:::sqs:sendMessage.waitForTaskToken"

    def test_no_agent_task_uses_wait_for_task_token(self, p1, p2):
        """`invokeHarness` has no waitForTaskToken variant; using one would fail at build."""
        for definition in (p1, p2):
            for name, state in _agent_tasks(definition).items():
                assert ".waitForTaskToken" not in state["Resource"], name

    def test_the_task_token_is_passed_in_the_message(self, p1):
        body = p1["States"]["AwaitReviewDecision"]["Arguments"]["MessageBody"]
        assert body["taskToken"] == "{% $states.context.Task.Token %}"

    def test_an_unresumable_review_is_caught_explicitly(self, p1):
        """The receiver raises this when a case moved on without a decision. Without the Catch
        it would surface as a generic failure and be diagnosed as an infrastructure fault."""
        errors = p1["States"]["AwaitReviewDecision"]["Catch"][0]["ErrorEquals"]
        assert REVIEW_UNRESUMABLE in errors

    def test_the_case_is_marked_awaiting_review_before_the_wait(self, p1, p2):
        """The load-bearing assertion for the whole human review loop.

        `review_api.py` finds queueable cases by querying the `state` GSI for `AWAITING_REVIEW`,
        and `decisions.py` refuses any submission whose case is not in that state. Without this
        write the console shows an empty queue while tokens accumulate in SQS for seven days, and
        neither symptom is visible from the state machine's own console.
        """
        for definition in (p1, p2):
            mark = definition["States"]["ManualReview"]
            assert mark["Resource"] == "arn:aws:states:::aws-sdk:dynamodb:updateItem"
            values = mark["Arguments"]["ExpressionAttributeValues"]
            assert values[":awaiting"] == {"S": "AWAITING_REVIEW"}
            # `state` is a DynamoDB reserved word; it is only addressable via an alias.
            assert mark["Arguments"]["ExpressionAttributeNames"]["#s"] == "state"
            assert "#s = :awaiting" in mark["Arguments"]["UpdateExpression"]
            assert mark["Next"] == "AwaitReviewDecision"

    def test_marking_awaiting_review_has_no_catch(self, p1, p2):
        """If the case cannot be marked it must not be queued. A token for a case the console
        cannot see is worse than a failed execution, because the wait lasts seven days."""
        for definition in (p1, p2):
            assert "Catch" not in definition["States"]["ManualReview"]

    def test_every_manual_review_entry_goes_through_the_mark_state(self, p1, p2):
        """Nothing may reach the SQS wait directly — a path that skipped the mark would create an
        invisible review, which is the exact failure this split exists to prevent."""
        for definition in (p1, p2):
            for name, state in definition["States"].items():
                if name == "ManualReview":
                    continue
                targets = [state.get("Next")]
                for catcher in state.get("Catch", []) or []:
                    targets.append(catcher.get("Next"))
                for choice in state.get("Choices", []) or []:
                    targets.append(choice.get("Next"))
                targets.append(state.get("Default"))
                assert "AwaitReviewDecision" not in [t for t in targets if t], (
                    f"{name} reaches the review wait without marking the case AWAITING_REVIEW")

    def test_the_transition_is_recorded_with_system_as_actor(self, p1):
        """R2.5 — every transition recorded. The reviewer's own decision appends a second entry
        with their identity (`decisions.py`), so these must not be conflated."""
        values = p1["States"]["ManualReview"]["Arguments"]["ExpressionAttributeValues"]
        transition = values[":tr"]["L"][0]["M"]
        assert transition["to"] == {"S": "AWAITING_REVIEW"}
        assert transition["actor"] == {"S": "system"}


class TestIdentityCrossCheckPayload:
    """The decision engine cannot enforce that these inputs arrive, so the guarantee lives here.

    `identity_match` reports an absent extraction as `performed: False` rather than as a failed
    check — deliberately, so that callers with no document are not punished. The cost of that choice
    is that a state machine which silently stopped passing the inputs would disable the cross-check
    quietly. These assertions are what makes that impossible.
    """

    def test_the_decision_payload_carries_the_declared_identity(self, p1):
        payload = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]
        declared = payload["declaredIdentity"]
        # Named field by field, not passed wholesale: the engine expects plain strings where the
        # case record holds DynamoDB-typed values, and a verbatim forward would also carry whatever
        # else a caller had put in the request body.
        for field in ("surname", "givenNames", "dateOfBirth", "nationality"):
            assert f"'{field}'" in declared, field
        assert ".M." in declared, "the DynamoDB map must be unwrapped"

    def test_the_decision_payload_carries_the_document_extraction(self, p1):
        payload = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]
        assert "documentExtraction" in payload
        assert "$case.documentExtraction" in payload["documentExtraction"]

    def test_absent_inputs_yield_null_rather_than_an_empty_object(self, p1):
        """`null` is what the engine reads as "the control did not run". An empty object would read
        as "it ran and found nothing", which is the conflation that sent every case to review."""
        payload = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]
        for key in ("declaredIdentity", "documentExtraction"):
            assert "$exists(" in payload[key], key
            assert ": null" in payload[key], key


class TestCatcherRulesStepFunctionsEnforces:
    """Two ASL rules that only surface at deploy time, so they are pinned here instead.

    `States.ALL must appear alone and at end of list` cost one rolled-back stack: a catcher listing
    `[ReviewUnresumable, States.Timeout, States.ALL]` synthesises and validates locally, then fails
    schema validation in the service with a message that names the array index rather than the rule.
    """

    def test_states_all_never_shares_a_catcher_with_another_error(self, p1, p2):
        for definition in (p1, p2):
            for name, state in _all_states(definition).items():
                for index, catcher in enumerate(state.get("Catch", [])):
                    errors = catcher["ErrorEquals"]
                    if "States.ALL" in errors:
                        assert errors == ["States.ALL"], \
                            f"{name} Catch[{index}] mixes States.ALL with {errors}"

    def test_states_all_is_the_last_catcher_when_present(self, p1, p2):
        for definition in (p1, p2):
            for name, state in _all_states(definition).items():
                catchers = state.get("Catch", [])
                for index, catcher in enumerate(catchers):
                    if "States.ALL" in catcher["ErrorEquals"]:
                        assert index == len(catchers) - 1, \
                            f"{name} has a catcher after States.ALL, which can never fire"

    def test_state_names_are_unique_across_the_whole_machine(self, p1, p2):
        """Not per branch — across the entire definition. Four Parallel branches each naming
        their terminal `BranchDegraded` is a DUPLICATE_STATE_NAME rejection, and it cost a
        second rolled-back stack. Local synth accepts it; only the service objects."""
        for definition in (p1, p2):
            names: list[str] = []

            def collect(states: dict) -> None:
                for name, state in states.items():
                    names.append(name)
                    for branch in state.get("Branches", []):
                        collect(branch.get("States", {}))

            collect(definition["States"])
            duplicates = {n for n in names if names.count(n) > 1}
            assert not duplicates, f"duplicate state names: {sorted(duplicates)}"

    def test_each_degraded_branch_terminal_names_its_agent(self, p1):
        """So the execution history says which branch failed, not just that one did."""
        terminals = {
            name
            for branch in p1["States"]["Phase1Parallel"]["Branches"]
            for name in branch["States"]
            if name.endswith("Degraded")
        }
        assert terminals == {"DocumentsDegraded", "IdentityDegraded", "FraudDegraded",
                             "PreScreenDegraded"}

    def test_the_same_rule_holds_for_retriers(self, p1, p2):
        for definition in (p1, p2):
            for name, state in _all_states(definition).items():
                for index, retrier in enumerate(state.get("Retry", [])):
                    errors = retrier["ErrorEquals"]
                    if "States.ALL" in errors:
                        assert errors == ["States.ALL"], \
                            f"{name} Retry[{index}] mixes States.ALL with {errors}"

    def test_review_appears_in_both_phases(self, p1, p2):
        assert "ManualReview" in p1["States"]
        assert "ManualReview" in p2["States"]


class TestAuditTrail:
    def test_the_decision_record_is_written_before_phase2_is_triggered(self, p1):
        """A provisional account with no immutable record is a compliance event (R18.3)."""
        assert p1["States"]["OpenProvisional"]["Next"] == "WriteDecisionRecord"
        assert p1["States"]["WriteDecisionRecord"]["Next"] == "TriggerPhase2"

    def test_a_failed_audit_write_is_surfaced_not_swallowed(self, p1):
        assert p1["States"]["WriteDecisionRecord"]["Catch"][0]["Next"] == "AuditWriteFailed"
        assert p1["States"]["AuditWriteFailed"]["Output"]["reason"] == "AUDIT_RECORD_MISSING"

    def test_evidence_is_persisted_before_any_terminal_state(self, p1):
        """R2.3 — a crash after the agents ran must not discard what they found."""
        assert p1["States"]["Phase1Parallel"]["Next"] == "PersistEvidence"
        assert p1["States"]["PersistEvidence"]["Next"] == "ReloadFindings"
        assert p1["States"]["ReloadFindings"]["Next"] == "PreliminaryTier"

    def test_findings_are_reread_after_the_agents_run(self, p1):
        """R11.13 / F-89. The agents write their structured findings through `submit_*` tools while
        the execution is inside Phase1Parallel, so the `$case` copy taken at LoadCase cannot
        contain them. Reading the stale copy would make the contract look like it had failed."""
        reload_state = p1["States"]["ReloadFindings"]
        assert reload_state["Resource"] == "arn:aws:states:::aws-sdk:dynamodb:getItem"
        # Four concurrent branches wrote seconds ago; an eventually consistent read would drop
        # findings at random and be indistinguishable from agents that never reported.
        assert reload_state["Arguments"]["ConsistentRead"] is True
        assert reload_state["Assign"]["case"] == "{% $states.result.Item %}"

    def test_the_tier_reads_structured_findings_not_prose(self, p1):
        payload = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]
        assert "findings" in payload
        for agent in ("documents", "prescreen", "fraud"):
            assert f"'{agent}'" in payload["findings"]

    def test_findings_are_read_from_flat_attributes(self, p1):
        """One attribute per agent. A nested map cannot be written by the tool, because DynamoDB
        rejects an UpdateExpression that creates a container and writes into it in one call."""
        findings = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]["findings"]
        for attribute in ("findingDocuments", "findingPrescreen", "findingFraud"):
            assert f"$case.{attribute}" in findings

    def test_an_absent_finding_yields_null_not_a_failure(self, p1):
        """JSONata fails a whole state on an undefined reference, so each read is guarded. The
        engine then sees no signal for that agent and routes to a reviewer — the right answer when
        an agent recorded nothing."""
        findings = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]["findings"]
        assert findings.count("$exists(") == 3
        assert findings.count("null") == 3


class TestDecisionEngineIsPure:
    def test_the_tier_task_invokes_a_lambda_not_a_model(self, p1, p2):
        """R4.6 — tier computation must not invoke a model."""
        for definition, state_name in ((p1, "PreliminaryTier"), (p2, "FullTier")):
            state = definition["States"][state_name]
            assert state["Resource"] == "arn:aws:states:::lambda:invoke"
            assert "Model" not in state["Arguments"]

    def test_the_phase_is_passed_explicitly(self, p1, p2):
        assert p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]["phase"] == 1
        assert p2["States"]["FullTier"]["Arguments"]["Payload"]["phase"] == 2

    def test_edge_facts_default_to_empty_rather_than_null(self, p1):
        """The engine rejects unknown keys but accepts an absent block; null would raise."""
        payload = p1["States"]["PreliminaryTier"]["Arguments"]["Payload"]
        assert "$exists($case.edgeFacts)" in payload["edgeFacts"]


# --- helpers -------------------------------------------------------------------------------


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
