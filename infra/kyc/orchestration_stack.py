"""Orchestration — the two state machines and the Lambdas they call. Tasks 6.2–6.5, 6.7.

Deploy order inside the stack matters in one place: Phase 1 holds the Phase 2 state machine ARN in
`TriggerPhase2`, so Phase 2 is constructed first. The dependency is one-way — Phase 2 never calls
Phase 1 — which is what keeps this from being a circular reference that CloudFormation cannot
resolve.

The state machine definitions themselves live in `asl.py` as plain dicts so their graph properties
can be unit-tested. See `infra/tests/test_asl.py`: the two requirements that matter most are
negative ones, and a negative reachability property is not checkable by reading a rendered
template.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from .asl import phase1_definition, phase2_definition
from .config import AGENTS, PROJECT

REPO_ROOT = Path(__file__).resolve().parents[2]


class OrchestrationStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        case_table_name: str,
        case_table_arn: str,
        case_key_arn: str,
        trace_key_arn: str,
        audit_bucket_name: str,
        audit_bucket_arn: str,
        audit_key_arn: str,
        sfn_role_arn: str,
        harness_arns: dict[str, str],
        # Not used by the state machine — F-85 means the Task takes the plain ARN plus a
        # Qualifier. Carried because R18.6 records the endpoint on the decision, and because
        # dropping the reference entirely would make CloudFormation try to delete an export that
        # this stack's currently-deployed template still uses, which deadlocks both stacks.
        harness_endpoint_arns: dict[str, str],
        trace_log_group_names: dict[str, str],
        model_ids: dict[str, str],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        case_key = kms.Key.from_key_arn(self, "CaseKey", case_key_arn)
        audit_key = kms.Key.from_key_arn(self, "AuditKey", audit_key_arn)
        case_table = dynamodb.Table.from_table_arn(self, "CaseTable", case_table_arn)

        # ---------------------------------------------------------------- review queue
        # A dead-letter queue rather than infinite redelivery: a review message that cannot be
        # processed must become visible, because the applicant is waiting and the workflow is
        # holding a task token that nothing else will release.
        review_dlq = sqs.Queue(
            self, "ReviewDlq",
            queue_name=f"{PROJECT}-review-dlq",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=case_key,
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        self.review_queue = sqs.Queue(
            self, "ReviewQueue",
            queue_name=f"{PROJECT}-review",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=case_key,
            # Long enough for a human. The token itself is held by Step Functions for 7 days
            # (see asl._review_task); this is only how long a reviewer has to pick the message up.
            visibility_timeout=Duration.minutes(15),
            retention_period=Duration.days(14),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=review_dlq),
        )

        # ---------------------------------------------------------------- lambdas
        self.decision_engine = self._python_lambda(
            "DecisionEngine",
            name=f"{PROJECT}-decision",
            source=REPO_ROOT / "decision-engine",
            handler="decision_handler.lambda_handler",
            description=(
                "Pure rules. No model, no network, no I/O — which is what makes R4.10's "
                "1,000-identical-results claim a property of the code."
            ),
            # Pure computation on a small payload. Memory here is for CPU share, not for data.
            memory_mb=512,
            timeout=Duration.seconds(10),
        )

        self.audit_writer = self._python_lambda(
            "AuditWriter",
            name=f"{PROJECT}-audit-writer",
            source=REPO_ROOT / "audit",
            handler="audit_lambda.lambda_handler",
            description="Assembles the decision record from OTEL spans + case record (F-13).",
            memory_mb=1024,
            timeout=Duration.seconds(60),
            environment={
                "CASE_TABLE_NAME": case_table_name,
                "AUDIT_BUCKET_NAME": audit_bucket_name,
                "AUDIT_KMS_KEY_ID": audit_key_arn,
                # The real per-agent AgentCore trace groups, comma-separated — NOT a single
                # guessed group. This was f"/aws/bedrock-agentcore/{PROJECT}", a log group that
                # does not exist and never did: AgentCore writes to
                # /aws/bedrock-agentcore/runtimes/<runtimeId>-live, one group per agent. The
                # span query therefore returned nothing on every run, `build_decision_record`
                # reported MISSING_SPANS=[AGENT_RUN, TOOL_CALL, DECISION], and
                # `WriteDecisionRecord` fail-closed every decision to a human. The fail-closed
                # behaviour was correct; there was simply nothing it could ever succeed on.
                # "aws/spans" FIRST: that is the unified traces destination that
                # UNIFIED_TRACES_DESTINATION_ENABLED=true actually writes OTEL spans to, verified
                # live (real 32-hex traceIds, JSON one span per line). The per-agent groups named
                # by design §9.3 follow it, because they carry only `[runtime-logs]` stdout streams
                # in practice — both are queried so the evidence is found wherever it really lands.
                "SPAN_LOG_GROUP": ",".join(
                    ["aws/spans"] + [v for _, v in sorted(trace_log_group_names.items())]),
                "HARNESS_ENDPOINT_NAME": "live",
                # R18.6 — the endpoint that actually served each agent, so a decision stays
                # explainable after an endpoint is repointed at a new harness version.
                "HARNESS_ENDPOINT_ARNS": ",".join(
                    f"{k}={v}" for k, v in sorted(harness_endpoint_arns.items())),
            },
        )
        self.audit_writer.add_to_role_policy(iam.PolicyStatement(
            sid="ReadCaseRecord",
            actions=["dynamodb:GetItem"],
            resources=[case_table_arn],
        ))
        self.audit_writer.add_to_role_policy(iam.PolicyStatement(
            sid="WriteObjectLockedRecord",
            # No DeleteObject, and none is possible anyway under Compliance mode. Withholding it
            # keeps the intent legible rather than relying on the bucket to enforce it.
            actions=["s3:PutObject", "s3:PutObjectRetention"],
            resources=[f"{audit_bucket_arn}/*"],
        ))
        self.audit_writer.add_to_role_policy(iam.PolicyStatement(
            sid="ReadSpans",
            actions=["logs:FilterLogEvents"],
            # Scoped to the AgentCore runtime trace groups. The previous resource pattern
            # ("/aws/bedrock-agentcore/kyc90*") could not match the real group names
            # ("/aws/bedrock-agentcore/runtimes/harness_kyc90_<agent>-<id>-live"), so even once
            # the group name was corrected this grant would still have denied the read.
            resources=[
                # the unified traces destination, where the spans actually are
                f"arn:aws:logs:{self.region}:{self.account}:log-group:aws/spans:*",
                f"arn:aws:logs:{self.region}:{self.account}:log-group:aws/spans",
                # the per-agent runtime groups named by design §9.3. The previous pattern
                # ("/aws/bedrock-agentcore/kyc90*") could not match the real names
                # ("/aws/bedrock-agentcore/runtimes/harness_kyc90_<agent>-<id>-live").
                f"arn:aws:logs:{self.region}:{self.account}:log-group:"
                f"/aws/bedrock-agentcore/runtimes/harness_{PROJECT}_*",
            ],
        ))
        case_key.grant_decrypt(self.audit_writer)
        audit_key.grant_encrypt(self.audit_writer)

        self.review_handler = self._python_lambda(
            "ReviewHandler",
            name=f"{PROJECT}-review",
            source=REPO_ROOT / "review",
            handler="review_handler.lambda_handler",
            description=(
                "Receives a reviewer decision, writes it conditionally (first writer wins, "
                "R6.5), then releases the task token. Never the other way round."
            ),
            memory_mb=512,
            timeout=Duration.seconds(30),
            environment={"CASE_TABLE_NAME": case_table_name},
        )
        self.review_handler.add_to_role_policy(iam.PolicyStatement(
            sid="ConditionallyWriteReviewDecision",
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[case_table_arn],
        ))
        self.review_handler.add_to_role_policy(iam.PolicyStatement(
            sid="ReleaseTaskToken",
            actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
            resources=["*"],  # task tokens are opaque; they carry no ARN to scope to
        ))

        # ------------------------------------------------- review task token materializer
        # The token a reviewer submits with arrives in the review SQS message body. The console
        # used to discover it by calling `ReceiveMessage` on every `GET /review/queue`, which
        # cannot work: a receive spends the message's finite redrive budget (`maxReceiveCount: 5`,
        # so the sixth *view* of a case sent a LIVE token to the DLQ — observed twice against this
        # stack), received messages are invisible to every other reader for the visibility
        # timeout, and short polling reported an empty queue while five tokens sat on it.
        #
        # So the queue is consumed exactly once, here, and the token is written where a lookup
        # belongs. `GET /review/queue` then reads it from the case record: no visibility, no
        # redrive budget, no exclusivity, and safe to poll as often as the console likes.
        #
        # Deleting the message is safe because the workflow waits on the *token*, not the message
        # (`ManualReview` is `sqs:sendMessage.waitForTaskToken`), and the token stays valid for its
        # full 7-day timeout wherever it is stored.
        self.review_token_materializer = self._python_lambda(
            "ReviewTokenMaterializer",
            name=f"{PROJECT}-review-token",
            source=REPO_ROOT / "review",
            handler="token_materializer.lambda_handler",
            description=(
                "Consumes each review handoff once and writes its task token onto the case "
                "record, so listing the queue is a side-effect-free DynamoDB read."
            ),
            memory_mb=256,
            timeout=Duration.seconds(30),
            environment={"CASE_TABLE_NAME": case_table_name},
        )
        self.review_token_materializer.add_to_role_policy(iam.PolicyStatement(
            sid="MaterialiseReviewTaskToken",
            actions=["dynamodb:UpdateItem"],
            resources=[case_table_arn],
        ))
        case_key.grant_decrypt(self.review_token_materializer)
        # `grant_consume_messages`, not a bare action list: it also grants the KMS decrypt this
        # encrypted queue needs. Granting only the SQS actions leaves the consumer unable to read
        # and the failure surfaces as an empty batch rather than an error.
        self.review_queue.grant_consume_messages(self.review_token_materializer)
        self.review_token_materializer.add_event_source(
            lambda_sources.SqsEventSource(
                self.review_queue,
                batch_size=10,
                # Partial batch failures: one message whose write fails must not redeliver the
                # nine that succeeded, because every redelivery spends redrive budget on a live
                # token.
                report_batch_item_failures=True,
            )
        )
        self.review_handler.add_to_role_policy(iam.PolicyStatement(
            sid="PublishQueueMetrics",
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": "KYC90/Review"}},
        ))
        self.review_handler.add_to_role_policy(iam.PolicyStatement(
            sid="ReadQueueDepth",
            actions=["sqs:GetQueueAttributes"],
            resources=[self.review_queue.queue_arn],
        ))
        case_key.grant_encrypt_decrypt(self.review_handler)

        # ---------------------------------------------------------------- state machines
        sfn_role = iam.Role.from_role_arn(
            self, "StateMachineRole", sfn_role_arn, mutable=True)

        # Names are constructed rather than read from `attr_arn`. Referencing the attribute would
        # make the policy depend on the state machine, while the state machine must depend on the
        # policy — Step Functions validates log-destination access at *create* time and fails with
        # "not authorized to access the Log Destination" if the grant lands afterwards. That cost
        # one rolled-back stack.
        phase1_name = f"{PROJECT}-phase1"
        phase2_name = f"{PROJECT}-phase2"
        phase2_arn = (
            f"arn:aws:states:{self.region}:{self.account}:stateMachine:{phase2_name}")
        log_group_arns = [
            f"arn:aws:logs:{self.region}:{self.account}:log-group:"
            f"/aws/vendedlogs/states/{name}:*"
            for name in (phase1_name, phase2_name)
        ]

        common = dict(
            case_table=case_table_name,
            harness_arns=harness_arns,
            model_ids=model_ids,
            decision_engine_arn=self.decision_engine.function_arn,
            audit_writer_arn=self.audit_writer.function_arn,
            review_queue_url=self.review_queue.queue_url,
        )

        # One explicit policy, created before either state machine, so the grant is in place when
        # Step Functions validates it. `add_to_principal_policy` on an imported role would create
        # this implicitly but with no ordering guarantee against the state machines.
        sfn_policy = iam.Policy(
            self, "StateMachinePolicy",
            policy_name=f"{PROJECT}-statemachine-orchestration",
            roles=[sfn_role],
            statements=[
                iam.PolicyStatement(
                    sid="InvokeOrchestrationLambdas",
                    actions=["lambda:InvokeFunction"],
                    resources=[self.decision_engine.function_arn,
                               self.audit_writer.function_arn],
                ),
                iam.PolicyStatement(
                    sid="ReadWriteCaseRecord",
                    actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                    resources=[case_table_arn],
                ),
                iam.PolicyStatement(
                    sid="EnqueueHumanReview",
                    actions=["sqs:SendMessage"],
                    resources=[self.review_queue.queue_arn],
                ),
                iam.PolicyStatement(
                    sid="StartPhase2",
                    actions=["states:StartExecution"],
                    resources=[phase2_arn],
                ),
                iam.PolicyStatement(
                    sid="RaisePhase2Alarm",
                    actions=["cloudwatch:PutMetricData"],
                    resources=["*"],
                    conditions={"StringEquals": {"cloudwatch:namespace": "KYC90/Phase2"}},
                ),
                iam.PolicyStatement(
                    # Vended log delivery needs these at account scope — CreateLogDelivery and
                    # PutResourcePolicy have no resource-level form, so they cannot be narrowed.
                    sid="StateMachineVendedLogging",
                    actions=["logs:CreateLogDelivery", "logs:GetLogDelivery",
                             "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
                             "logs:ListLogDeliveries", "logs:PutResourcePolicy",
                             "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="WriteExecutionLogs",
                    actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                    resources=log_group_arns,
                ),
                iam.PolicyStatement(
                    sid="UseCaseAndAuditKeys",
                    actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                    resources=[case_key_arn, audit_key_arn],
                ),
            ],
        )

        # Phase 2 first: Phase 1 embeds its ARN. One-way, so no circular reference.
        self.phase2 = sfn.CfnStateMachine(
            self, "Phase2",
            state_machine_name=phase2_name,
            role_arn=sfn_role.role_arn,
            definition=phase2_definition(**common),
            state_machine_type="STANDARD",
            tracing_configuration=sfn.CfnStateMachine.TracingConfigurationProperty(enabled=True),
            logging_configuration=self._logging(
                sfn.CfnStateMachine, f"/aws/vendedlogs/states/{PROJECT}-phase2"),
        )

        self.phase2.node.add_dependency(sfn_policy)

        self.phase1 = sfn.CfnStateMachine(
            self, "Phase1",
            state_machine_name=phase1_name,
            role_arn=sfn_role.role_arn,
            definition=phase1_definition(
                **common, phase2_state_machine_arn=self.phase2.attr_arn),
            state_machine_type="STANDARD",
            tracing_configuration=sfn.CfnStateMachine.TracingConfigurationProperty(enabled=True),
            logging_configuration=self._logging(
                sfn.CfnStateMachine, f"/aws/vendedlogs/states/{PROJECT}-phase1"),
        )
        self.phase1.add_dependency(self.phase2)
        self.phase1.node.add_dependency(sfn_policy)

        cdk.CfnOutput(self, "Phase1Arn", value=self.phase1.attr_arn)
        cdk.CfnOutput(self, "Phase2Arn", value=self.phase2.attr_arn)
        cdk.CfnOutput(self, "ReviewQueueUrl", value=self.review_queue.queue_url)
        cdk.CfnOutput(self, "DecisionEngineArn", value=self.decision_engine.function_arn)
        cdk.CfnOutput(self, "AuditWriterArn", value=self.audit_writer.function_arn)
        cdk.CfnOutput(
            self, "AgentCount", value=str(len(AGENTS)),
            description="Seven agents across two phases, five in Phase 1.")

    # --- helpers ---------------------------------------------------------------------------

    def _python_lambda(
        self,
        construct_id: str,
        *,
        name: str,
        source: Path,
        handler: str,
        description: str,
        memory_mb: int,
        timeout: Duration,
        environment: dict[str, str] | None = None,
    ) -> lambda_.Function:
        """A Python Lambda from a source directory.

        `exclude` drops the test tree from the deployment package. Shipping tests would put
        `conftest.py` on the Lambda's import path, and those conftests mutate `sys.path` — which
        is harmless locally and is the sort of thing that resolves a module to the wrong file in
        a packaged runtime.
        """
        return lambda_.Function(
            self, construct_id,
            function_name=name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler=handler,
            code=lambda_.Code.from_asset(
                str(source),
                exclude=["tests", "tests/*", "__pycache__", "*.pyc", ".pytest_cache"],
            ),
            memory_size=memory_mb,
            timeout=timeout,
            description=description,
            environment=environment or {},
            log_retention=logs.RetentionDays.ONE_MONTH,
            tracing=lambda_.Tracing.ACTIVE,
        )

    def _logging(self, cls, log_group_name: str):
        group = logs.LogGroup(
            self, f"Log{log_group_name.rsplit('-', 1)[-1].title()}",
            log_group_name=log_group_name,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return cls.LoggingConfigurationProperty(
            # ALL rather than ERROR: the demo needs to show what the graph did on a *successful*
            # run too, and execution history alone omits the JSONata assignments.
            level="ALL",
            include_execution_data=True,
            destinations=[cls.LogDestinationProperty(
                cloud_watch_logs_log_group=cls.CloudWatchLogsLogGroupProperty(
                    log_group_arn=group.log_group_arn))],
        )
