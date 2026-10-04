"""Lifecycle stack — Phase 3, the day-12 flow. G-01, R21, R31.

What this stack exists to fix
-----------------------------
Before it, the build *enrolled* a customer into ongoing monitoring — `EnrolMonitoring` wrote
`state=MONITORING` and a `reKycInterval` — and then nothing ever read them. Enrolment without
re-assessment records that a control exists; it does not implement one. Ongoing monitoring is not
an enhancement to KYC: FATF R.10(d) requires ongoing due diligence and scrutiny of transactions,
and in the US it is the fifth element of the AML program added by the FinCEN CDD Rule
(31 CFR 1020.210(b)(5)). The FFIEC manual is explicit that the trigger is **event-driven** — "the
requirement to update customer information is event-driven and occurs as a result of normal
monitoring" — and that no categorical periodic obligation exists. So the EventBridge path below is
the requirement-satisfying one and the Scheduler is the industry convention layered on top; if
scope has to be cut, the Scheduler goes first.

Four deliberate choices
-----------------------
**A custom bus, not the default one.** The default bus carries every AWS service event in the
account. A rule there would match on `detail-type` against a stream it does not own, and an archive
of it would capture unrelated traffic. A dedicated bus makes the archive scoped and the replay
meaningful.

**Archive with replay, sized in days not forever.** The archive is what makes a backfill or a
bug-fix re-drive possible: replay re-sends archived events to the same bus, and because the same
`eventId` flows through, both idempotency layers hold and no duplicate decision is written. That
property is the reason this is safe to operate, and it is asserted in the test suite rather than
assumed.

**A separate append-only history table, not more rows on the case table.** The case table is
mutable by design — Phase 1 and Phase 2 update it. Putting immutable decision rows in the same
table would mean the same IAM grant that lets the workflow move `riskState` also lets it edit a
decision. Splitting them makes the immutability structural rather than a matter of writing careful
`UpdateExpression`s: the workflow's only grant on this table is `PutItem`.

**Standard, not Express.** A lifecycle execution may wait months for a re-KYC date; Express caps
at five minutes and is at-least-once. Standard also gives the idempotent `StartExecution` on
same-name-plus-same-input that collapses a replayed event onto the original run.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_scheduler as scheduler,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
)
from constructs import Construct

from .asl import phase3_definition
from .config import LIST_OVERLAY_PARAMETER, PROJECT

#: How long archived lifecycle events stay replayable. Long enough to re-drive a quarter after a
#: logic fix, short enough that the archive is not a second copy of the customer base forever.
ARCHIVE_RETENTION_DAYS = 120

#: `detail-type`s the rule matches. Narrow on purpose: a bus-wide rule would forward anything
#: published to this bus into a workflow that assumes a KYC customer id.
RISK_EVENT_DETAIL_TYPE = "TransactionRiskEvent"

#: A sanctions list update, fanned out as one event per open account (the demo's Act 3). The same
#: event shape as a transaction risk event, so both reach the same re-assessment.
LIST_UPDATE_DETAIL_TYPE = "SanctionsListUpdate"

LIFECYCLE_DETAIL_TYPES = (RISK_EVENT_DETAIL_TYPE, LIST_UPDATE_DETAIL_TYPE)

#: The agents Phase 3 invokes: Pre-Screen re-runs the screening tool, Briefing explains a case to
#: the reviewer it is escalated to. Nothing else, so the grant is scoped to exactly these two.
PHASE3_AGENTS = ("prescreen", "briefing")


class LifecycleStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        case_table_name: str,
        case_table_arn: str,
        case_key_arn: str,
        decision_engine_arn: str,
        audit_writer_arn: str,
        review_queue_url: str,
        review_queue_arn: str,
        harness_arns: dict[str, str],
        model_ids: dict[str, str],
        account_function_arn: str,
        audit_pins: dict[str, str],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        missing = [a for a in PHASE3_AGENTS if a not in harness_arns or a not in model_ids]
        if missing:
            raise ValueError(f"Phase 3 needs harness ARNs and models for {missing}")
        phase3_harnesses = {a: harness_arns[a] for a in PHASE3_AGENTS}

        # ------------------------------------------------------- decision history (append-only)
        #
        # `customerId` + `sk`, where sk is `DECISION#<event time>#<event id>` for a re-assessment
        # and `REVIEW#<event time>#<event id>` for the reviewer's decision on it. Both are derived
        # from the event, so a replay collides with the original row and is refused; the decision
        # key is composed by the engine and unit-tested (`lifecycle.history_sort_key`).
        self.history_table = dynamodb.Table(
            self, "DecisionHistory",
            table_name=f"{PROJECT}-decision-history",
            partition_key=dynamodb.Attribute(
                name="customerId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=cdk.aws_kms.Key.from_key_arn(self, "CaseKey", case_key_arn),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            # RETAIN, not DESTROY. These rows are the record of why a customer's risk changed;
            # `cdk destroy` taking them with it would be the same class of mistake as making the
            # evidence bucket deletable.
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------- event bus + archive
        self.bus = events.EventBus(
            self, "LifecycleBus", event_bus_name=f"{PROJECT}-lifecycle")

        self.archive = self.bus.archive(
            "LifecycleArchive",
            archive_name=f"{PROJECT}-lifecycle-archive",
            description=("Replayable history of lifecycle risk events. Replay is safe because the "
                         "same eventId re-derives the same history sort key, so no duplicate "
                         "decision can be written."),
            event_pattern=events.EventPattern(detail_type=list(LIFECYCLE_DETAIL_TYPES)),
            retention=Duration.days(ARCHIVE_RETENTION_DAYS),
        )

        # ---------------------------------------------------------------- Phase 3 state machine
        sfn_role = iam.Role(
            self, "Phase3Role",
            role_name=f"{PROJECT}-phase3-sfn",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Phase 3 lifecycle re-assessment. PutItem only on the history table.",
        )

        # The narrow grant that makes immutability structural: `PutItem` and nothing else. No
        # UpdateItem, no DeleteItem — so no code path in this workflow can edit or remove a
        # decision row even if a future state were written carelessly.
        sfn_role.add_to_policy(iam.PolicyStatement(
            sid="AppendOnlyDecisionHistory",
            actions=["dynamodb:PutItem"],
            resources=[self.history_table.table_arn],
        ))
        # The case table is read, plus one narrow update for the CURRENT risk state.
        sfn_role.add_to_policy(iam.PolicyStatement(
            sid="ReadCaseAndMoveRiskState",
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[case_table_arn],
        ))
        sfn_role.add_to_policy(iam.PolicyStatement(
            sid="CaseKeyUse",
            actions=["kms:Decrypt", "kms:GenerateDataKey"],
            resources=[case_key_arn],
        ))
        sfn_role.add_to_policy(iam.PolicyStatement(
            sid="InvokeDecisionEngineAndAudit",
            actions=["lambda:InvokeFunction"],
            resources=[decision_engine_arn, audit_writer_arn],
        ))
        # The account platform: freeze, restrict, and — only after a reviewer's clearance — lift.
        sfn_role.add_to_policy(iam.PolicyStatement(
            sid="MoveAccountRestrictions",
            actions=["lambda:InvokeFunction"],
            resources=[account_function_arn],
        ))
        # The screening list version in force, read before the rules re-assess, so a re-screen of
        # the list from before the update is not mistaken for the control the update asked for.
        sfn_role.add_to_policy(iam.PolicyStatement(
            sid="ReadScreeningListVersion",
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                       f"{LIST_OVERLAY_PARAMETER}"],
        ))
        sfn_role.add_to_policy(iam.PolicyStatement(
            sid="EscalateToHumanReview",
            actions=["sqs:SendMessage"],
            resources=[review_queue_arn],
        ))
        # F-09: `InvokeHarness` needs both actions, and `InvokeAgentRuntimeCommand` is never
        # granted to anything because it bypasses the model loop and ignores `allowedTools`.
        # Both the harness AND its endpoints. `InvokeHarness` is authorised against the
        # ENDPOINT ARN (`.../harness/<id>/harness-endpoint/live`), which is a child of the harness,
        # so a grant on the harness alone is a 403 at runtime. The first live execution failed
        # exactly here — synth cannot catch it because the template is valid either way.
        sfn_role.add_to_policy(iam.PolicyStatement(
            sid="InvokePhase3Harnesses",
            actions=["bedrock-agentcore:InvokeHarness",
                     "bedrock-agentcore:InvokeAgentRuntime"],
            resources=[r for arn in phase3_harnesses.values() for r in (arn, f"{arn}/*")],
        ))

        self.phase3 = sfn.CfnStateMachine(
            self, "Phase3",
            state_machine_name=f"{PROJECT}-phase3-lifecycle",
            role_arn=sfn_role.role_arn,
            definition=phase3_definition(
                case_table=case_table_name,
                history_table=self.history_table.table_name,
                decision_engine_arn=decision_engine_arn,
                audit_writer_arn=audit_writer_arn,
                review_queue_url=review_queue_url,
                harness_arns=phase3_harnesses,
                model_ids={a: model_ids[a] for a in PHASE3_AGENTS},
                account_function_arn=account_function_arn,
                audit_pins=audit_pins,
            ),
            # STANDARD is load-bearing, not a default: exactly-once, up to a year, and the
            # idempotent StartExecution that makes replay safe.
            state_machine_type="STANDARD",
            tracing_configuration=sfn.CfnStateMachine.TracingConfigurationProperty(enabled=True),
        )

        # -------------------------------------------------------------------- rule -> workflow
        #
        # The execution name is NOT set here. EventBridge cannot compute
        # `rekyc-<customer>-<event>`, and a name derived by a template expression would be the
        # F-89 mistake again — a correctness-critical string built somewhere untestable. The
        # workflow reads the engine-computed name and the append itself is guarded by
        # `attribute_not_exists(sk)`, so idempotency holds at the write even when a duplicate
        # execution starts.
        # A failed delivery must not vanish. Without a DLQ an event that cannot start an execution
        # is dropped after the retries, which for an AML control is the worst failure mode there
        # is: no decision, no alert, no record. The code comment used to say this beside
        # `dead_letter_queue=None`. SQS-managed encryption rather than the case key, because
        # EventBridge would need a key-policy grant to write to a customer-managed-key queue and a
        # DLQ that EventBridge cannot write to is the same as none.
        self.event_dlq = sqs.Queue(
            self, "LifecycleEventDlq",
            queue_name=f"{PROJECT}-lifecycle-event-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )

        events.Rule(
            self, "RiskEventRule",
            rule_name=f"{PROJECT}-risk-event",
            event_bus=self.bus,
            description=("Route a risk event or a sanctions list update to the Phase 3 lifecycle "
                         "workflow."),
            event_pattern=events.EventPattern(detail_type=list(LIFECYCLE_DETAIL_TYPES)),
            targets=[targets.SfnStateMachine(
                sfn.StateMachine.from_state_machine_arn(
                    self, "Phase3Target", self.phase3.attr_arn),
                dead_letter_queue=self.event_dlq,
                retry_attempts=3,
                max_event_age=Duration.hours(24),
            )],
        )

        # ------------------------------------------------------------- periodic re-KYC (optional)
        #
        # Industry convention, not the regulatory requirement (see the module docstring). Kept as a
        # group so a whole cadence can be disabled without touching the event path that actually
        # satisfies the rule.
        self.rekyc_group = scheduler.CfnScheduleGroup(
            self, "ReKycGroup", name=f"{PROJECT}-rekyc")

        cdk.CfnOutput(self, "LifecycleBusName", value=self.bus.event_bus_name)
        cdk.CfnOutput(self, "LifecycleEventDlqUrl", value=self.event_dlq.queue_url)
        cdk.CfnOutput(self, "DecisionHistoryTable", value=self.history_table.table_name)
        cdk.CfnOutput(self, "Phase3StateMachineArn", value=self.phase3.attr_arn)
