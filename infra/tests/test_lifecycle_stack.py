"""Phase 3 lifecycle stack — synth assertions. G-01, R21, R31.

The properties that matter in this stack are mostly *negative*: the workflow must NOT be able to
edit a decision, must NOT restore an account without a person, must NOT drop an event it could not
deliver. A positive smoke test ("it synthesises") would pass while any of those regressed, and each
one is a single careless line away.
"""

from __future__ import annotations

import json
import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from kyc.asl import phase1_definition, phase2_definition, phase3_definition
from kyc.lifecycle_stack import (
    LIFECYCLE_DETAIL_TYPES,
    LIST_UPDATE_DETAIL_TYPE,
    RISK_EVENT_DETAIL_TYPE,
    LifecycleStack,
)
from tests.test_asl import COMMON, PHASE1_EXTRA, PHASE3_EXTRA, WITHOUT_A_PERSON, reachable_from

ACCOUNT, REGION = "111122223333", "us-east-1"
CASE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/kyc90-cases"
KEY_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/abcd1234-1111-2222-3333-444455556666"
ENGINE_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:kyc90-decision-engine"
AUDIT_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:kyc90-audit-writer"
ACCOUNT_FN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:kyc90-account-stub"
REVIEW_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:kyc90-review"
HARNESSES = {k: f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/kyc90_{k}-abc123"
             for k in ("briefing", "documents", "identity", "fraud", "prescreen", "compliance",
                       "notify")}
PINS = {"endpointName": "live", "harnessVersion": "prescreen=2,briefing=1",
        "policyEngineVersion": "pe#LOG_ONLY", "gatewayArn": "gw"}


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = LifecycleStack(
        app, "Lifecycle",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
        case_table_name="kyc90-cases",
        case_table_arn=CASE_ARN,
        case_key_arn=KEY_ARN,
        decision_engine_arn=ENGINE_ARN,
        audit_writer_arn=AUDIT_ARN,
        review_queue_url=f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/kyc90-review",
        review_queue_arn=REVIEW_ARN,
        harness_arns=HARNESSES,
        model_ids={k: "us.amazon.nova-lite-v1:0" for k in HARNESSES},
        account_function_arn=ACCOUNT_FN,
        audit_pins=PINS,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def definition() -> dict:
    return phase3_definition(**COMMON, **PHASE3_EXTRA)


def _statements(template: Template, sid: str) -> list[dict]:
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Sid") == sid:
                found.append(stmt)
    return found


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


class TestTheHistoryIsAppendOnly:
    """The single most important property in the stack."""

    def test_the_workflow_can_only_put_never_update_or_delete_history(self, template):
        """A grant of UpdateItem or DeleteItem on the history table would make the day-zero
        decision editable, which is the one thing this whole flow exists to prevent."""
        actions: set[str] = set()
        for stmt in _statements(template, "AppendOnlyDecisionHistory"):
            actions.update(_as_list(stmt["Action"]))
        assert actions == {"dynamodb:PutItem"}, actions

    def test_every_history_write_is_a_guarded_insert(self, definition):
        """`attribute_not_exists(sk)` is what makes a replayed event a no-op instead of a
        second row — for the decision and for the reviewer's decision on it."""
        puts = {n: s for n, s in definition["States"].items()
                if s.get("Resource", "").endswith("dynamodb:putItem")}
        assert set(puts) == {"AppendDecision", "AppendReviewClearance", "AppendReviewExit"}
        for name, state in puts.items():
            assert state["Arguments"]["ConditionExpression"] == "attribute_not_exists(sk)", name

    def test_a_duplicate_event_succeeds_rather_than_failing(self, definition):
        """A replay is not an error: the decision it would write already exists. Failing here
        would page someone every time an archive replay ran."""
        put = definition["States"]["AppendDecision"]
        dupe = [c for c in put["Catch"]
                if "DynamoDb.ConditionalCheckFailedException" in c["ErrorEquals"]]
        assert dupe and dupe[0]["Next"] == "DuplicateEventIgnored"
        assert definition["States"]["DuplicateEventIgnored"]["Type"] == "Succeed"

    def test_the_sort_key_is_derived_from_the_event(self, definition):
        """The engine composes `DECISION#<occurredAt>#<eventId>`, so two deliveries of one event
        compute one key. The old key used the execution start time, so a replay wrote twice."""
        item = definition["States"]["AppendDecision"]["Arguments"]["Item"]
        assert item["sk"] == {"S": "{% $decision.historySortKey %}"}
        review_key = definition["States"]["AppendReviewExit"]["Arguments"]["Item"]["sk"]["S"]
        assert "$riskEvent.occurredAt" in review_key and "$riskEvent.eventId" in review_key
        assert "Execution" not in review_key

    def test_the_history_table_survives_a_stack_destroy(self, template):
        template.has_resource("AWS::DynamoDB::Table", {
            "Properties": Match.object_like({"TableName": "kyc90-decision-history"}),
            "DeletionPolicy": "Retain",
        })

    def test_no_state_writes_a_day_zero_decision(self, definition):
        """The case-table updates move the current risk view and the Phase 3 review; none may
        touch `phase1` or `phase2`, or the day-zero record would become mutable by the side door."""
        for name, state in definition["States"].items():
            if state.get("Resource", "").endswith("dynamodb:updateItem"):
                expr = state["Arguments"]["UpdateExpression"]
                for forbidden in ("phase1 =", "phase2 =", "phase1Review", "phase2Review",
                                  "decision =", "fullTier"):
                    assert forbidden not in expr, f"{name} writes {forbidden}"


class TestReplaySafety:
    def test_the_bus_is_dedicated_not_the_default_bus(self, template):
        template.has_resource_properties("AWS::Events::EventBus", {"Name": "kyc90-lifecycle"})

    def test_an_archive_exists_so_events_can_be_replayed(self, template):
        template.resource_count_is("AWS::Events::Archive", 1)

    def test_the_archive_and_the_rule_are_scoped_to_lifecycle_events(self, template):
        """An unscoped archive would capture every event on the bus, so a replay would re-drive
        traffic the workflow was never meant to see."""
        archive = list(template.find_resources("AWS::Events::Archive").values())[0]
        assert archive["Properties"]["EventPattern"]["detail-type"] == \
            list(LIFECYCLE_DETAIL_TYPES)
        rule = list(template.find_resources("AWS::Events::Rule").values())[0]
        assert rule["Properties"]["EventPattern"]["detail-type"] == list(LIFECYCLE_DETAIL_TYPES)
        assert set(LIFECYCLE_DETAIL_TYPES) == {RISK_EVENT_DETAIL_TYPE, LIST_UPDATE_DETAIL_TYPE}

    def test_the_state_machine_is_standard_not_express(self, template):
        template.has_resource_properties("AWS::StepFunctions::StateMachine", {
            "StateMachineType": "STANDARD",
        })

    def test_an_undeliverable_event_lands_in_a_dead_letter_queue(self, template):
        """Without a DLQ an event that cannot start an execution is dropped after the retries:
        for an AML control, no decision, no alert and no record."""
        rule = list(template.find_resources("AWS::Events::Rule").values())[0]
        target, = rule["Properties"]["Targets"]
        assert "DeadLetterConfig" in target, target
        assert target["RetryPolicy"]["MaximumRetryAttempts"] == 3
        template.has_resource_properties("AWS::SQS::Queue", {
            "QueueName": "kyc90-lifecycle-event-dlq",
            "SqsManagedSseEnabled": True,
        })


class TestSanctionsListUpdate:
    """Act 3: a list update re-screens an open account on the updated list."""

    def test_the_rescreen_is_the_prescreen_agent_on_the_screening_tool(self, definition):
        rescreen = definition["States"]["ReScreen"]
        assert rescreen["Arguments"]["HarnessArn"] == COMMON["harness_arns"]["prescreen"]

    def test_the_facts_are_reread_and_passed_as_recorded(self, definition):
        assert definition["States"]["PersistPhase3Evidence"]["Next"] == "ReloadFacts"
        assert definition["States"]["ReloadFacts"]["Next"] == "LoadListVersion"
        assert definition["States"]["LoadListVersion"]["Next"] == "Reassess"
        payload = definition["States"]["Reassess"]["Arguments"]["Payload"]
        assert "$case.factScreening.S" in payload["facts"]["screening"]
        assert payload["phaseStartedAt"] == "{% $phaseStartedAt %}"

    def test_a_failed_rescreen_is_judged_by_the_fact_not_by_a_flag(self, definition):
        """No `evidenceComplete` flag any more: a re-screen that did not run leaves the screening
        fact older than this execution, and the engine returns SYSTEM_ERROR for it."""
        assert definition["States"]["ReScreen"]["Catch"][0]["Next"] == "ReScreenDegraded"
        assert definition["States"]["ReScreenDegraded"]["Next"] == "PersistPhase3Evidence"
        assert "evidenceComplete" not in json.dumps(definition)

    def test_a_confirmed_match_freezes_first_and_then_reaches_a_person(self, definition):
        """Strict liability: the account freezes now, and the review confirms afterwards."""
        route = {c["Next"] for c in definition["States"]["RouteOutcome"]["Choices"]
                 if "'FREEZE'" in c["Condition"]}
        assert route == {"FreezeAccount"}
        freeze = definition["States"]["FreezeAccount"]
        assert freeze["Arguments"]["Payload"]["operation"] == "freeze"
        assert "ManualReview" in reachable_from(definition, "FreezeAccount")

    def test_a_freeze_survives_a_failed_history_write(self, definition):
        choice = definition["States"]["HistoryNotRecorded"]
        assert choice["Choices"][0]["Next"] == "FreezeAccount"
        assert choice["Default"] == "HistoryReferral"

    def test_a_failed_freeze_still_reaches_a_person(self, definition):
        assert definition["States"]["FreezeAccount"]["Catch"][0]["Next"] == "FreezeNotApplied"
        assert definition["States"]["FreezeNotApplied"]["Next"] == "ReviewGate"

    def test_increased_risk_restricts_and_never_lifts(self, definition):
        """`restrict` only adds restrictions, so a re-assessment can never lift one."""
        for name in ("RestrictForReview", "ReapplyRestrictions"):
            assert definition["States"][name]["Arguments"]["Payload"]["operation"] == "restrict"

    def test_the_high_risk_path_escalates_to_the_review_queue(self, definition):
        """The old escalation sent an SQS message with no task token, which nothing could
        resume and the console never showed. It is now the same review loop as onboarding."""
        reached = reachable_from(definition, "RestrictForReview")
        assert {"ManualReview", "AwaitReviewDecision"} <= reached
        body = definition["States"]["AwaitReviewDecision"]["Arguments"]["MessageBody"]
        assert body["phase"] == "phase3"
        assert body["taskToken"] == "{% $states.context.Task.Token %}"

    def test_one_review_per_event(self, definition):
        """The previous event's review is cleared so first-writer-wins applies to this one."""
        update = definition["States"]["ManualReview"]["Arguments"]["UpdateExpression"]
        assert "phase3Review" in update.split(" REMOVE ")[1]


class TestFailClosed:
    LIFTS = ("RestoreStandard", "RestoreEnhanced", "RestoreProvisional")

    def test_nothing_restores_an_account_without_a_person(self, definition):
        reached = reachable_from(definition, definition["StartAt"], blocked=WITHOUT_A_PERSON)
        for lift in self.LIFTS:
            assert lift not in reached, lift

    def test_restoring_waits_for_the_reviewers_record(self, definition):
        reached = reachable_from(definition, "AwaitReviewDecision",
                                 blocked={"RecordReviewedClearance"})
        for lift in self.LIFTS:
            assert lift not in reached, lift

    def test_a_restore_never_exceeds_the_day_zero_posture(self, definition):
        choice = definition["States"]["RestoreDayZeroPosture"]
        assert all("$day0Posture" in c["Condition"] for c in choice["Choices"])
        assert choice["Default"] == "RestoreProvisional"

    def test_an_incomplete_or_failed_reassessment_reaches_a_person(self, definition):
        for name in ("RescreenIncomplete", "ReassessFailed", "HistoryReferral"):
            assert definition["States"][name]["Next"] == "ReviewGate", name

    def test_a_missing_case_fails_rather_than_inventing_a_day_zero(self, definition):
        assert definition["States"]["CheckCaseExists"]["Choices"][0]["Next"] == "EventUnroutable"
        assert definition["States"]["EventUnroutable"]["Type"] == "Fail"

    def test_every_task_has_a_catch_except_the_review_mark(self, definition):
        """The mark has none by design: a case that cannot be marked must not be queued."""
        for name, state in definition["States"].items():
            if state.get("Type") == "Task" and name != "ManualReview":
                assert state.get("Catch"), f"{name} has no Catch; a failure would kill the run"


class TestLiveRunRegressions:
    """Found by the first real executions, not by synth. Kept so a refactor cannot reintroduce
    them — synth passes either way, which is the whole point."""

    def test_dynamodb_boolean_uses_pascal_case(self, definition):
        item = definition["States"]["AppendDecision"]["Arguments"]["Item"]
        assert "Bool" in item["riskChanged"] and "BOOL" not in item["riskChanged"]

    def test_harness_grant_covers_the_endpoint_child_resource(self, template):
        """InvokeHarness authorises against `.../harness/<id>/harness-endpoint/live`, so a grant
        on the bare harness ARN is a runtime 403."""
        stmt, = _statements(template, "InvokePhase3Harnesses")
        assert any(str(r).endswith("/*") for r in _as_list(stmt["Resource"]))


class TestLeastPrivilege:
    def test_invoke_agent_runtime_command_is_never_granted(self, template):
        """F-09: it bypasses the model loop and ignores `allowedTools`."""
        assert "InvokeAgentRuntimeCommand" not in json.dumps(template.to_json())

    def test_the_harness_grant_is_scoped_to_the_two_phase3_agents(self, template):
        """Never a wildcard across harnesses, which would let Phase 3 invoke any agent."""
        stmt, = _statements(template, "InvokePhase3Harnesses")
        expected = {r for k in ("prescreen", "briefing")
                    for r in (HARNESSES[k], f"{HARNESSES[k]}/*")}
        assert set(_as_list(stmt["Resource"])) == expected

    def test_the_account_platform_grant_is_the_stub_alone(self, template):
        stmt, = _statements(template, "MoveAccountRestrictions")
        assert _as_list(stmt["Resource"]) == [ACCOUNT_FN]

    def test_the_list_version_read_is_the_one_parameter(self, template):
        stmt, = _statements(template, "ReadScreeningListVersion")
        assert stmt["Action"] == "ssm:GetParameter"
        rendered = json.dumps(stmt["Resource"])
        assert "parameter/kyc90/screening/list-overlay" in rendered
        assert "*" not in rendered


class TestReKycCadence:
    def test_a_schedule_group_exists_for_periodic_review(self, template):
        """Periodic re-KYC is industry convention layered on the event path, kept in its own
        group so the cadence can be disabled without touching the regulatory path."""
        template.resource_count_is("AWS::Scheduler::ScheduleGroup", 1)


def _all_definitions() -> tuple[dict, ...]:
    return (phase1_definition(**COMMON, **PHASE1_EXTRA),
            phase2_definition(**COMMON),
            phase3_definition(**COMMON, **PHASE3_EXTRA))


class TestCaseViewBinding:
    """A `caseView` projection must never read a variable assigned in its own `Assign` block.

    Pins the live defect where `LoadCase` assigned `case` and, in the same block, built `caseView`
    from `$case`. Same-block assignments see the *incoming* scope, so `$case` was unset, every
    `$exists` guard evaluated false, and every agent was handed a case with `documents: []` and
    `liveness: null` for a submission that had both. It degraded to review in silence.
    """

    def _load_case_states(self):
        out = []
        for definition in _all_definitions():
            for name, body in definition["States"].items():
                if "caseView" in body.get("Assign", {}):
                    out.append((name, body["Assign"]))
        return out

    def test_case_view_blocks_exist_in_every_phase(self):
        assert len(self._load_case_states()) == 3

    def test_case_view_never_reads_same_block_variable(self):
        for name, assign in self._load_case_states():
            rendered = json.dumps(assign["caseView"])
            for var in set(assign) - {"caseView"}:
                assert f"${var}." not in rendered and f"${var} " not in rendered, (
                    f"{name}: caseView reads ${var}, which is assigned in the same block")

    def test_case_view_is_rooted_at_the_result(self):
        for name, assign in self._load_case_states():
            assert "$states.result.Item" in json.dumps(assign["caseView"]), name

    def test_case_view_carries_no_date_of_birth_or_document_location(self):
        """Prompts get what an agent needs to call its tool, not the applicant's identity."""
        for name, assign in self._load_case_states():
            rendered = json.dumps(assign["caseView"])
            for field in ("dateOfBirth", "address", "bucket", "'key'", "referenceImageKey"):
                assert field not in rendered, (name, field)


class TestNoUnguardedCaseReads:
    """Every optional case attribute in a Lambda payload must be `$exists`-guarded.

    JSONata fails the whole state when an expression returns undefined, so an unguarded read of an
    attribute nothing writes is not a null — it is a dead state. Pins `jurisdictionRisk` (read and
    never written) killing every Phase 1 decision, then `customerAge` killing every Phase 3 one.
    """

    #: Written by the API at case creation, before any phase can run.
    ALWAYS_WRITTEN = {"applicantId", "caseId", "jurisdiction", "productApplied"}

    def _payload_reads(self):
        found = []
        for definition in _all_definitions():
            for name, body in definition["States"].items():
                payload = (body.get("Arguments") or {}).get("Payload")
                if not payload:
                    continue

                # Recursive: payloads nest, and a flat scan once missed `day0.decidedAt`.
                def scan(node, path, name=name):
                    if isinstance(node, str):
                        for attr in re.findall(r"\$case\.([A-Za-z0-9_]+)", node):
                            if attr in self.ALWAYS_WRITTEN:
                                continue
                            if f"$exists($case.{attr})" not in node:
                                found.append((name, path, attr))
                    elif isinstance(node, dict):
                        for key, value in node.items():
                            scan(value, f"{path}.{key}" if path else str(key))
                    elif isinstance(node, (list, tuple)):
                        for index, value in enumerate(node):
                            scan(value, f"{path}[{index}]")

                scan(payload, "")
        return found

    def test_no_unguarded_optional_case_reads(self):
        unguarded = self._payload_reads()
        assert not unguarded, f"unguarded $case reads would kill the state: {unguarded}"

    def test_the_day_zero_projection_is_guarded_too(self, definition):
        day0 = definition["States"]["ReadDayZero"]["Assign"]["day0"]
        for attr in ("phase2", "phase1", "fullDecidedAt", "provisionalDecidedAt",
                     "decisionVersion"):
            assert f"$exists($case.{attr})" in day0, attr
        # An unknown day-zero tier reads as LOW, so a new HIGH alert cannot hide as "unchanged".
        assert ": 'LOW'" in day0
