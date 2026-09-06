"""Phase 3 lifecycle stack — synth assertions. G-01, R21, R31.

These tests exist because the properties that matter in this stack are all *negative*: the
workflow must NOT be able to edit a decision, the archive must NOT be missing, the state machine
must NOT be Express. A positive smoke test ("it synthesises") would pass while any of those
regressed, and each one is a single careless line away.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from kyc.asl import phase3_definition
from kyc.lifecycle_stack import RISK_EVENT_DETAIL_TYPE, LifecycleStack

ACCOUNT, REGION = "111122223333", "us-east-1"
CASE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/kyc90-cases"
KEY_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/abcd1234-1111-2222-3333-444455556666"
ENGINE_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:kyc90-decision-engine"
AUDIT_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:kyc90-audit-writer"
REVIEW_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:kyc90-review"
HARNESS_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/kyc-fraud-abc123"


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
        fraud_harness_arn=HARNESS_ARN,
        fraud_model_id="us.amazon.nova-2-lite-v1:0",
    )
    return Template.from_stack(stack)



def _phase3_for_tests() -> dict:
    """The phase 3 definition, for tests that scan all three phases rather than one."""
    return phase3_definition(
        case_table="kyc90-cases", history_table="kyc90-decision-history",
        decision_engine_arn=ENGINE_ARN, audit_writer_arn=AUDIT_ARN,
        review_queue_url="https://sqs/q", fraud_harness_arn=HARNESS_ARN,
        fraud_model_id="us.amazon.nova-2-lite-v1:0")


@pytest.fixture(scope="module")
def definition() -> dict:
    return phase3_definition(
        case_table="kyc90-cases", history_table="kyc90-decision-history",
        decision_engine_arn=ENGINE_ARN, audit_writer_arn=AUDIT_ARN,
        review_queue_url="https://sqs/q", fraud_harness_arn=HARNESS_ARN,
        fraud_model_id="us.amazon.nova-2-lite-v1:0")


class TestTheHistoryIsAppendOnly:
    """The single most important property in the stack."""

    def test_the_workflow_can_only_put_never_update_or_delete_history(self, template):
        """A grant of UpdateItem or DeleteItem on the history table would make the day-zero
        decision editable, which is the one thing this whole flow exists to prevent."""
        policies = template.find_resources("AWS::IAM::Policy")
        history_actions: set[str] = set()
        for policy in policies.values():
            for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
                if stmt.get("Sid") == "AppendOnlyDecisionHistory":
                    actions = stmt["Action"]
                    history_actions.update(
                        actions if isinstance(actions, list) else [actions])
        assert history_actions == {"dynamodb:PutItem"}, history_actions

    def test_the_append_is_guarded_by_a_conditional_write(self, definition):
        """`attribute_not_exists(sk)` is what makes a replayed event a no-op instead of a
        second row."""
        put = definition["States"]["AppendDecision"]
        assert put["Arguments"]["ConditionExpression"] == "attribute_not_exists(sk)"

    def test_a_duplicate_event_succeeds_rather_than_failing(self, definition):
        """A replay is not an error: the decision it would write already exists. Failing here
        would page someone every time an archive replay ran."""
        put = definition["States"]["AppendDecision"]
        dupe = [c for c in put["Catch"]
                if "DynamoDb.ConditionalCheckFailedException" in c["ErrorEquals"]]
        assert dupe, "conditional-check failure must be caught explicitly"
        assert dupe[0]["Next"] == "DuplicateEventIgnored"
        assert definition["States"]["DuplicateEventIgnored"]["Type"] == "Succeed"

    def test_the_history_table_survives_a_stack_destroy(self, template):
        template.has_resource("AWS::DynamoDB::Table", {
            "Properties": Match.object_like({"TableName": "kyc90-decision-history"}),
            "DeletionPolicy": "Retain",
        })

    def test_no_state_writes_the_case_decision_fields(self, definition):
        """The only case-table update permitted is the current risk state. If a future state
        started writing `decision` or `fullTier` back onto the case, the day-zero record would
        become mutable through the side door."""
        for name, state in definition["States"].items():
            if state.get("Resource", "").endswith("dynamodb:updateItem"):
                expr = state["Arguments"]["UpdateExpression"]
                assert "decision =" not in expr, f"{name} edits the decision"
                assert "fullTier" not in expr, f"{name} edits the tier"
                assert expr.startswith("SET riskState ="), f"{name}: {expr}"


class TestReplaySafety:
    def test_the_bus_is_dedicated_not_the_default_bus(self, template):
        template.has_resource_properties("AWS::Events::EventBus",
                                         {"Name": "kyc90-lifecycle"})

    def test_an_archive_exists_so_events_can_be_replayed(self, template):
        template.resource_count_is("AWS::Events::Archive", 1)

    def test_the_archive_is_scoped_to_risk_events(self, template):
        """An unscoped archive would capture every event on the bus, so a replay would re-drive
        traffic the workflow was never meant to see."""
        archives = template.find_resources("AWS::Events::Archive")
        pattern = list(archives.values())[0]["Properties"]["EventPattern"]
        assert pattern["detail-type"] == [RISK_EVENT_DETAIL_TYPE]

    def test_the_state_machine_is_standard_not_express(self, template):
        """Express is at-least-once and caps at five minutes. Standard's idempotent
        StartExecution on same-name-plus-input is what collapses a replayed event."""
        template.has_resource_properties("AWS::StepFunctions::StateMachine", {
            "StateMachineType": "STANDARD",
        })


class TestFailClosed:
    def test_an_incomplete_rescreen_routes_to_the_engine_not_to_success(self, definition):
        """The fail-closed path must still produce a decision record. Skipping straight to a
        terminal state would leave no evidence that the control was attempted."""
        assert definition["States"]["ReScreenIncomplete"]["Next"] == "AppendDecision"

    def test_the_incomplete_path_declares_evidence_incomplete(self, definition):
        """`evidenceComplete: false` is what makes the engine return SYSTEM_ERROR instead of
        inferring 'risk unchanged' from an absent finding."""
        payload = definition["States"]["ReScreenIncomplete"]["Arguments"]["Payload"]
        assert payload["evidenceComplete"] is False
        assert definition["States"]["Reassess"]["Arguments"]["Payload"]["evidenceComplete"] is True

    def test_a_missing_case_fails_rather_than_inventing_a_day_zero(self, definition):
        assert definition["States"]["CheckCaseExists"]["Choices"][0]["Next"] == "EventUnroutable"
        assert definition["States"]["EventUnroutable"]["Type"] == "Fail"

    def test_no_failure_path_reaches_a_state_that_lifts_a_restriction(self, definition):
        """Mirrors Phase 2's Property 3: silence is not permission. `UpdateRiskState` writes
        MONITORING (the unrestricted state), so no Catch may land on it."""
        for name, state in definition["States"].items():
            for catch in state.get("Catch", []):
                assert catch["Next"] != "UpdateRiskState", (
                    f"{name} catches into the unrestricted state")

    def test_every_agent_and_engine_task_has_a_catch(self, definition):
        for name, state in definition["States"].items():
            if state.get("Type") == "Task":
                assert state.get("Catch"), f"{name} has no Catch; a failure would kill the run"


class TestLiveRunRegressions:
    """Both of these were found by the FIRST real execution, not by synth. Kept as tests so a
    refactor cannot reintroduce them — synth passes either way, which is the whole point."""

    def test_dynamodb_boolean_uses_pascal_case(self, definition):
        """Step Functions rejects the wire-format `BOOL` for its aws-sdk integration."""
        item = definition["States"]["AppendDecision"]["Arguments"]["Item"]
        assert "Bool" in item["riskChanged"], item["riskChanged"]
        assert "BOOL" not in item["riskChanged"]

    def test_no_wire_format_bool_anywhere_in_the_definition(self, definition):
        """Both directions: Step Functions rejects `BOOL` as a write field AND returns `Bool` on
        reads, so any `.BOOL` in a JSONata path silently yields undefined and kills the state."""
        import json as _json
        body = _json.dumps(definition)
        assert '"BOOL"' not in body, "wire-format BOOL used as a write field"
        assert ".BOOL" not in body, "wire-format .BOOL used in a read path"

    def test_harness_grant_covers_the_endpoint_child_resource(self, template):
        """InvokeHarness authorises against `.../harness/<id>/harness-endpoint/live`, so a grant
        on the bare harness ARN is a runtime 403."""
        policies = template.find_resources("AWS::IAM::Policy")
        resources = []
        for policy in policies.values():
            for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
                if stmt.get("Sid") == "InvokeFraudHarness":
                    r = stmt["Resource"]
                    resources = r if isinstance(r, list) else [r]
        assert any(str(r).endswith("/*") for r in resources), resources


class TestLeastPrivilege:
    def test_invoke_agent_runtime_command_is_never_granted(self, template):
        """F-09: it bypasses the model loop and ignores `allowedTools`."""
        body = json.dumps(template.to_json())
        assert "InvokeAgentRuntimeCommand" not in body

    def test_the_harness_grant_is_scoped_to_the_fraud_harness(self, template):
        policies = template.find_resources("AWS::IAM::Policy")
        found = False
        for policy in policies.values():
            for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
                if stmt.get("Sid") == "InvokeFraudHarness":
                    r = stmt["Resource"]
                    r = r if isinstance(r, list) else [r]
                    # Scoped to THIS harness and its endpoints — never a wildcard across
                    # harnesses, which would let Phase 3 invoke any agent in the account.
                    assert set(r) == {HARNESS_ARN, f"{HARNESS_ARN}/*"}, r
                    found = True
        assert found, "no InvokeFraudHarness statement"


class TestReKycCadence:
    def test_a_schedule_group_exists_for_periodic_review(self, template):
        """Periodic re-KYC is industry convention layered on top of the event path, kept in its
        own group so the cadence can be disabled without touching the regulatory path."""
        template.resource_count_is("AWS::Scheduler::ScheduleGroup", 1)


class TestCaseViewBinding:
    """A `caseView` projection must never read a variable assigned in its own `Assign` block.

    Pins the live defect where `LoadCase` assigned `case` and, in the same block, built `caseView`
    from `$case`. Same-block assignments see the *incoming* scope, so `$case` was unset, every
    `$exists` guard evaluated false, and all seven agents were handed a case with `documents: []`
    and `liveness: null` for a submission that had both. It degraded to manual review silently.
    """

    def _load_case_states(self):
        from kyc.asl import phase1_definition, phase2_definition
        from tests.test_asl import COMMON
        out = []
        # Phase 3 included: it was omitted, and that is precisely where the next unguarded read
        # (`customerAge`) survived to kill every lifecycle re-assessment in production.
        definitions = (
            phase1_definition(**COMMON, phase2_state_machine_arn="arn:aws:states:::sm/phase2"),
            phase2_definition(**COMMON),
            _phase3_for_tests(),
        )
        for definition in definitions:
            states = definition["States"]
            for name, body in states.items():
                if "caseView" in str(body.get("Assign", {})):
                    out.append((name, body["Assign"]))
        return out

    def test_case_view_blocks_exist(self):
        assert self._load_case_states(), "no caseView projection found — test would vacuously pass"

    def test_case_view_never_reads_same_block_variable(self):
        for name, assign in self._load_case_states():
            same_block = set(assign) - {"caseView"}
            rendered = str(assign["caseView"])
            for var in same_block:
                assert f"${var}." not in rendered and f"${var} " not in rendered, (
                    f"{name}: caseView reads ${var}, which is assigned in the same Assign block "
                    f"and is therefore unset while caseView is evaluated. Root it at "
                    f"$states.result.Item instead."
                )

    def test_case_view_is_rooted_at_the_result(self):
        for name, assign in self._load_case_states():
            rendered = str(assign["caseView"])
            assert "$states.result.Item" in rendered, (
                f"{name}: caseView must project from $states.result.Item"
            )


class TestNoUnguardedCaseReads:
    """Every optional case field in a decision-engine payload must be `$exists`-guarded.

    JSONata fails the whole state with States.QueryEvaluationError when an expression returns
    undefined, so an unguarded read of a field nothing writes is not a null — it is a dead state.
    Pins the live defect where `jurisdictionRisk` (read here and by the decision engine, written
    nowhere) killed `PreliminaryTier` on every case and sent all of them to manual review.
    """

    # Fields whose write site was traced, so an unguarded read cannot be reached with the field
    # absent. Each entry is a claim about a specific writer — if a writer is ever removed, the
    # corresponding read becomes a QueryEvaluationError and this set is the record of why we
    # believed otherwise. `jurisdictionRisk` is deliberately NOT here: nothing writes it.
    ALWAYS_WRITTEN = {
        "applicantId", "caseId", "jurisdiction", "productApplied",
        # written by the API at case creation (see api/ tests asserting the created attributes)
        "gatewayArn",
        # written by Phase 1 PersistEvidence (asl.py: "SET documentConfidence = :dc,
        # velocityCritical = :vc"), which always precedes Phase 2's FullTier
        "documentConfidence", "velocityCritical",
    }

    def _payload_reads(self):
        import re
        from kyc.asl import phase1_definition, phase2_definition
        from tests.test_asl import COMMON
        found = []
        # Phase 3 included: it was omitted, and that is precisely where the next unguarded read
        # (`customerAge`) survived to kill every lifecycle re-assessment in production.
        definitions = (
            phase1_definition(**COMMON, phase2_state_machine_arn="arn:aws:states:::sm/phase2"),
            phase2_definition(**COMMON),
            _phase3_for_tests(),
        )
        for definition in definitions:
            for name, body in definition["States"].items():
                payload = (body.get("Arguments") or {}).get("Payload")
                if not payload:
                    continue
                # Recursive: the payload nests (`day0: {...}`), and a flat scan missed
                # `day0.decidedAt` — which then killed every Phase 3 re-assessment in production.
                def scan(node, path):
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
        assert not unguarded, (
            "unguarded $case reads would fail the state with QueryEvaluationError when the "
            f"attribute is absent: {unguarded}"
        )


class TestNoWireFormatAttributeValues:
    """Step Functions rejects wire-format DynamoDB AttributeValue member names.

    It normalises them to PascalCase, so `BOOL` / `NULL` / `SS` etc. must be written `Bool`,
    `Null`, `Ss`. Pins two live failures: `{"BOOL": false}` on a Phase 3 write, and the same
    spelling in Phase 1's OpenProvisional, which only executed once a clean applicant finally
    reached a provisional approval.
    """

    WIRE_ONLY = ("BOOL", "NULL")

    def test_no_wire_format_members_in_any_definition(self):
        import json
        from kyc.asl import phase1_definition, phase2_definition, phase3_definition
        from tests.test_asl import COMMON
        definitions = {
            "phase1": phase1_definition(
                **COMMON, phase2_state_machine_arn="arn:aws:states:::sm/phase2"),
            "phase2": phase2_definition(**COMMON),
        }
        for name, definition in definitions.items():
            blob = json.dumps(definition)
            for member in self.WIRE_ONLY:
                assert f'"{member}"' not in blob, (
                    f"{name}: '{member}' is the wire-format spelling and is rejected by Step "
                    f"Functions — use its PascalCase form ({member.capitalize()})"
                )


class TestReviewQueuePeekGrants:
    """The review function must be able to release a peeked message, not just read it.

    `_peek_task_tokens` deliberately swallows a failed `ChangeMessageVisibility`, so a missing
    grant produces no error anywhere — it silently restores the bug where every queue load hid the
    tokens from the next poll, and the console rendered actionable cases as "Task token not yet
    visible". This asserts the grant exists so that silence cannot come back.
    """

    def _api_stack_source(self):
        import pathlib
        import kyc.api_stack as api_stack
        return pathlib.Path(api_stack.__file__).read_text()

    def test_change_message_visibility_is_granted(self):
        src = self._api_stack_source()
        assert "sqs:ChangeMessageVisibility" in src, (
            "review_fn must be granted sqs:ChangeMessageVisibility so the token peek can release "
            "messages; without it the console starves its own next poll"
        )

    def test_delete_message_is_never_granted_on_the_review_queue(self):
        src = self._api_stack_source()
        grant_call = src.split("review_queue.grant")[1][:300]
        assert "DeleteMessage" not in grant_call, (
            "never grant DeleteMessage on the review queue — it would discard a live task token"
        )


class TestPhase2TriggerUsesTheMintedDecisionId:
    """Phase 2 must be started with the decision id the audit writer minted.

    Pins the live defect where `TriggerPhase2` read `$decision.decisionId`. The decision engine
    never returns a decisionId — `audit_lambda` mints it — so both expressions failed with
    States.QueryEvaluationError and every provisionally-approved case ended at `Phase2NotStarted`:
    account open, record written, screening never run. Reachable only once a clean applicant could
    actually earn a provisional approval.
    """

    def _phase1(self):
        from kyc.asl import phase1_definition
        from tests.test_asl import COMMON
        return phase1_definition(**COMMON, phase2_state_machine_arn="arn:aws:states:::sm/phase2")

    def test_trigger_does_not_read_a_field_the_engine_never_returns(self):
        trigger = str(self._phase1()["States"]["TriggerPhase2"])
        assert "$decision.decisionId" not in trigger, (
            "the decision engine returns no decisionId; the audit writer mints it"
        )

    def test_the_audit_writer_result_is_captured(self):
        assign = self._phase1()["States"]["WriteDecisionRecord"].get("Assign") or {}
        assert "provisionalDecisionId" in assign
        assert "$states.result.Payload.decisionId" in str(assign["provisionalDecisionId"])

    def test_the_trigger_uses_that_variable_for_input_and_name(self):
        trigger = self._phase1()["States"]["TriggerPhase2"]["Arguments"]
        assert "$provisionalDecisionId" in str(trigger["Input"]["provisionalDecisionId"])
        # R21.7 idempotency: the execution name must be derived from the same id.
        assert "$provisionalDecisionId" in str(trigger["Name"])


class TestPhase2ExecutionNameFitsTheServiceLimit:
    """A Step Functions execution name may not exceed 80 characters.

    Pins the live failure that only a REAL applicant could produce. The name was
    `applicantId & '-' & decisionId`; a Cognito-derived applicant id is 76 characters and the
    decision id is a 36-character uuid4, so the name was 113 and StartExecution was rejected:

        StepFunctions.ValidationException: 1 validation error detected: Value 'APP-b438b4a8-...'
        at 'name' failed to satisfy constraint: Member must have length less than or equal to 80

    Retries exhausted and the execution ended at `Phase2NotStarted` — provisional account open,
    audit record written, screening never run. Every synthetic test passed because the ids used in
    tests were ~19 characters, which is exactly why this needed a real applicant to surface.
    """

    NAME_LIMIT = 80
    #: A real Cognito-derived applicant id, measured from the failing execution.
    REAL_APPLICANT_ID = ("APP-b438b4a8-f011-7029-3b06-95a7ec9c4ced"
                         "-36cddcb4-cc94-4865-902a-7ce7101fbb5e")
    DECISION_ID = "f191f384-0c7f-4a54-8f01-e3893ce14dac"

    def _name_expression(self):
        from kyc.asl import phase1_definition
        from tests.test_asl import COMMON
        d = phase1_definition(**COMMON, phase2_state_machine_arn="arn:aws:states:::sm/phase2")
        return d["States"]["TriggerPhase2"]["Arguments"]["Name"]

    def test_the_real_applicant_id_would_have_broken_the_limit(self):
        """Guards the premise: if this stops being true the test below is meaningless."""
        combined = f"{self.REAL_APPLICANT_ID}-{self.DECISION_ID}"
        assert len(combined) > self.NAME_LIMIT, len(combined)

    def test_the_name_does_not_concatenate_the_applicant_id(self):
        expr = self._name_expression()
        assert "$applicantId" not in expr, (
            "the applicant id is unbounded in practice (76 chars from Cognito) and pushes the "
            f"execution name past the {self.NAME_LIMIT}-character service limit"
        )

    def test_the_name_is_the_decision_id_and_fits(self):
        expr = self._name_expression()
        assert "$provisionalDecisionId" in expr
        # The decision id is a uuid4, so the rendered name is 36 characters.
        assert len(self.DECISION_ID) <= self.NAME_LIMIT
