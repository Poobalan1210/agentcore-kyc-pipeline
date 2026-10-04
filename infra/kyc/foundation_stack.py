"""Foundation stack — tasks 1.2 to 1.5.

**IRREVERSIBLE.** Two choices here cannot be cleanly undone:

  * Object Lock must be enabled at bucket creation (R17.2). It cannot be retrofitted. If it
    is missed, the only fix is to delete and recreate the bucket.
  * The KMS key-per-jurisdiction split defines the residency boundary. Merging keys later
    means re-encrypting evidence that Object Lock forbids you to delete.

Contents:
  1.2  six KMS keys — four per-jurisdiction, one for reasoning traces, one for the case store
  1.3  four S3 evidence buckets, Object Lock: Compliance mode for 7 years, or Governance for a
       day in a demo deployment (D-35)
  1.4  DynamoDB case store per design 9.1
  1.5  ten IAM roles — seven harness, one Gateway, one Step Functions, one Pipe
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)
from constructs import Construct

from .config import (
    AGENTS,
    EVIDENCE_LOCKS,
    JURISDICTIONS,
    PROJECT,
    EvidenceLock,
)


class FoundationStack(Stack):
    """KMS, S3 with Object Lock, DynamoDB, and the ten IAM roles."""

    def __init__(self, scope: Construct, construct_id: str, *,
                 evidence_lock: EvidenceLock = EVIDENCE_LOCKS["COMPLIANCE"],
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # COMPLIANCE unless the app says otherwise: a stack built without a choice gets the
        # regulated posture, never the demo one by accident.
        self.evidence_lock = evidence_lock

        self.jurisdiction_keys: dict[str, kms.Key] = {}
        self.evidence_buckets: dict[str, s3.Bucket] = {}
        self.harness_roles: dict[str, iam.Role] = {}

        self._build_keys()
        self._build_buckets()
        self._build_case_store()
        self._build_roles()
        self._outputs()

    # ------------------------------------------------------------------ 1.2 KMS
    def _build_keys(self) -> None:
        for j in JURISDICTIONS:
            key = kms.Key(
                self, f"EvidenceKey{j.code.upper()}",
                alias=f"alias/{PROJECT}-evidence-{j.code}",
                description=(
                    f"Evidence encryption for {j.name}. Separate key per jurisdiction so "
                    f"the residency boundary is enforced by key policy, not convention."
                ),
                enable_key_rotation=True,
                removal_policy=RemovalPolicy.RETAIN,
            )
            self.jurisdiction_keys[j.code] = key

        # R18.7 / R18.8: reasoning traces live under their own key with their own lifetime.
        # They must never share a key with decision records, because the two have opposite
        # retention obligations — one is kept for seven years, the other is deleted at case
        # closure plus the appeal window.
        self.trace_key = kms.Key(
            self, "ReasoningTraceKey",
            alias=f"alias/{PROJECT}-reasoning-traces",
            description=(
                "Reasoning traces only. Deliberately distinct from evidence keys: traces are "
                "minimised and deleted, evidence is retained under Object Lock."
            ),
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # CloudWatch Logs must be trusted by the key policy itself to encrypt a log group with
        # it — an IAM grant on the caller alone is not enough, and `logs:AssociateKmsKey`
        # failed with "The specified KMS key does not exist or is not allowed to be used" until
        # this was added (task 6.8). Scoped to log groups in this account/region via
        # `kms:EncryptionContext:aws:logs:arn`, which is how CloudWatch Logs presents its own
        # identity when it calls the key on the service's behalf.
        self.trace_key.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowCloudWatchLogsToUseKey",
            principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
            actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*",
                     "kms:GenerateDataKey*", "kms:Describe*"],
            resources=["*"],
            conditions={
                "ArnLike": {
                    "kms:EncryptionContext:aws:logs:arn":
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:*"
                }
            },
        ))

        self.case_key = kms.Key(
            self, "CaseStoreKey",
            alias=f"alias/{PROJECT}-case-store",
            description="Customer-managed key for the DynamoDB case store.",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ------------------------------------------------------------------ 1.3 S3
    def _build_buckets(self) -> None:
        access_logs = s3.Bucket(
            self, "AccessLogsBucket",
            bucket_name=f"{PROJECT}-access-logs-{self.account}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.access_logs_bucket = access_logs

        for j in JURISDICTIONS:
            bucket = s3.Bucket(
                self, f"EvidenceBucket{j.code.upper()}",
                bucket_name=f"{PROJECT}-evidence-{j.code}-{self.account}",
                encryption=s3.BucketEncryption.KMS,
                encryption_key=self.jurisdiction_keys[j.code],
                bucket_key_enabled=True,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                minimum_tls_version=1.2,
                versioned=True,
                server_access_logs_bucket=access_logs,
                server_access_logs_prefix=f"evidence-{j.code}/",
                # THE irreversible line. object_lock_enabled cannot be turned on later.
                object_lock_enabled=True,
                # The default retention can change in place, and only affects what is written
                # after the change. COMPLIANCE is seven years, not five: Australia and Japan
                # both require 7 (B-12, B-13), so one floor satisfies all four markets. A demo
                # deployment uses GOVERNANCE for a day instead (config.EVIDENCE_LOCKS, D-35).
                object_lock_default_retention=(
                    s3.ObjectLockRetention.compliance
                    if self.evidence_lock.mode == "COMPLIANCE"
                    else s3.ObjectLockRetention.governance
                )(duration=cdk.Duration.days(self.evidence_lock.days)),
                removal_policy=RemovalPolicy.RETAIN,
                # R32.6/R33.1 — documents are uploaded by the browser straight to S3 with a
                # presigned PUT (`api/applicants.py`'s `presign_document`), never proxied through
                # the API. A cross-origin PUT from the applicant app's origin is blocked by the
                # browser unless the *bucket* allows it: the presigned URL authorises the request
                # with S3, CORS is what lets the browser issue it at all. Without this the upload
                # fails in the browser with a CORS error while S3 itself never sees a request,
                # which is indistinguishable from a network fault in the app's error handling.
                #
                # PUT only, and no GET: the browser's write path is the only cross-origin access
                # required. Reading evidence back is a reviewer/audit capability that goes through
                # the API (`review_api.py`'s `get_audit`), so granting the browser cross-origin
                # GET on an evidence bucket would widen access for no functional gain.
                cors=[
                    s3.CorsRule(
                        allowed_methods=[s3.HttpMethods.PUT],
                        # Dev origins plus a wildcard fallback. The wildcard is acceptable here
                        # only because it grants nothing on its own — every request still needs a
                        # valid presigned signature, and BLOCK_ALL public access is unchanged.
                        allowed_origins=["*"],
                        allowed_headers=["*"],
                        # The applicant app does not read response headers from the PUT, so
                        # nothing needs exposing.
                        max_age=3000,
                    )
                ],
            )
            self.evidence_buckets[j.code] = bucket

        # R36: the biometric bucket is the one place deletion must stay possible, so it has
        # NO Object Lock and a 24-hour lifecycle rule. Putting a real face in a Compliance-mode
        # bucket would convert a data-minimisation duty into a permanent violation.
        self.biometric_bucket = s3.Bucket(
            self, "DemoBiometricBucket",
            bucket_name=f"{PROJECT}-demo-biometric-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.trace_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
            object_lock_enabled=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="delete-within-24h",
                    enabled=True,
                    expiration=cdk.Duration.days(1),
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(1),
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

    # ------------------------------------------------------------------ 1.4 DynamoDB
    def _build_case_store(self) -> None:
        self.case_table = dynamodb.Table(
            self, "CaseTable",
            table_name=f"{PROJECT}-cases",
            partition_key=dynamodb.Attribute(
                name="applicantId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.case_key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
            deletion_protection=True,
        )

        # R6.6: the review queue is ordered by age, and its depth and age are published as
        # metrics because it is the load-bearing component when 90-95% of screening hits are
        # false positives (B-08).
        self.case_table.add_global_secondary_index(
            index_name="state-index",
            partition_key=dynamodb.Attribute(
                name="state", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="createdAt", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.case_table.add_global_secondary_index(
            index_name="phase2Pending-index",
            partition_key=dynamodb.Attribute(
                name="phase2Pending", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="provisionalDecidedAt", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

    # ------------------------------------------------------------------ 1.5 IAM
    def _build_roles(self) -> None:
        agentcore = iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com",
            conditions={"StringEquals": {"aws:SourceAccount": self.account}},
        )

        for agent in AGENTS:
            role = iam.Role(
                self, f"HarnessRole{agent.role_suffix}",
                role_name=f"{PROJECT}-harness-{agent.key}",
                assumed_by=agentcore,
                description=f"Execution role for {agent.name}. {agent.purpose}",
            )

            # Each harness may invoke only the models it is assigned, across the geo
            # profiles its jurisdictions need. Scoped by resource, not "*".
            model_arns: list[str] = []
            for j in JURISDICTIONS:
                wanted = {j.fast_model, j.workhorse_model}
                if agent.reasoning_effort and j.reasoning_model:
                    wanted.add(j.reasoning_model)
                for model_id in wanted:
                    model_arns.append(
                        # Every Nova model, not just the one this agent is currently pinned to.
                        # Changing an agent's model otherwise needs a matching IAM change, and
                        # missing it produces `AccessDeniedException` on
                        # `bedrock:InvokeModelWithResponseStream` at *invoke* time — which is what
                        # happened when Fraud moved off Nova 2 Lite. The blast radius is the same
                        # either way: these are the three models the system is allowed to use.
                        f"arn:aws:bedrock:*:{self.account}:inference-profile/us.amazon.nova-*")
                    model_arns.append(
                        f"arn:aws:bedrock:*::foundation-model/"
                        f"{model_id.split('.', 1)[-1] if '.' in model_id else model_id}")

            role.add_to_policy(iam.PolicyStatement(
                sid="InvokeAssignedModelsOnly",
                actions=["bedrock:InvokeModel",
                         "bedrock:InvokeModelWithResponseStream",
                         "bedrock:Converse",
                         "bedrock:ConverseStream"],
                resources=sorted(set(model_arns)),
            ))

            # F-20 / R19.3: spans go to a per-agent log group so PII-bearing traces can be
            # access-controlled and encrypted per agent. logs:PutResourcePolicy is required
            # for the unified span destination and is easy to miss.
            role.add_to_policy(iam.PolicyStatement(
                sid="PerAgentTelemetry",
                actions=["logs:CreateLogGroup", "logs:CreateLogStream",
                         "logs:PutLogEvents", "logs:DescribeLogStreams",
                         "logs:PutResourcePolicy",
                         "xray:PutTraceSegments", "xray:PutSpans",
                         "xray:PutTelemetryRecords",
                         "cloudwatch:PutMetricData"],
                resources=["*"],
            ))
            role.add_to_policy(iam.PolicyStatement(
                sid="DecryptOwnTraces",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[self.trace_key.key_arn],
            ))

            # The Gateways use the AWS_IAM authorizer (task 3.1, deliberately not NONE), so the
            # harness role must be allowed to call the Gateway's MCP endpoint. Without this the
            # harness reaches READY and then fails at invocation time with a bare
            # "403 Forbidden ... /mcp" — the tool never loads, and nothing in the harness
            # configuration hints that a permission is missing (F-79).
            role.add_to_policy(iam.PolicyStatement(
                sid="InvokeJurisdictionGateways",
                actions=["bedrock-agentcore:InvokeGateway",
                         "bedrock-agentcore:GetGateway",
                         "bedrock-agentcore:ListGatewayTargets"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"],
            ))

            # DIV-2: Notify is the one harness with Memory on (`harnesses_stack.py`), so the
            # service provisions a managed memory named after the harness (`kyc90_notify-<id>`)
            # and the harness calls the memory data plane with THIS role on every invocation.
            # Without these actions every notification failed at `ListEvents` with
            # AccessDeniedException: both Notify steps in Phase 2 caught the error and moved on,
            # so cases still decided correctly and the customer was never told anything.
            # The action set is the one AWS documents for a harness execution role; the
            # resource is scoped to this harness's own memory, never `memory/*`.
            if agent.key == "notify":
                role.add_to_policy(iam.PolicyStatement(
                    sid="OwnManagedMemory",
                    actions=["bedrock-agentcore:CreateEvent",
                             "bedrock-agentcore:DeleteEvent",
                             "bedrock-agentcore:GetEvent",
                             "bedrock-agentcore:ListEvents",
                             "bedrock-agentcore:RetrieveMemoryRecords"],
                    resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:"
                               f"memory/{PROJECT}_{agent.key}-*"],
                ))

            # R11.7 / F-09: InvokeAgentRuntimeCommand bypasses the model loop and ignores
            # allowedTools, so it is denied explicitly rather than merely left ungranted.
            # Note the neighbouring action InvokeAgentRuntime is REQUIRED by the Step
            # Functions integration (F-70) — the two are one word apart and must not be
            # confused. Also deny role chaining (R23.6).
            role.add_to_policy(iam.PolicyStatement(
                sid="DenyToolBypassAndRoleChaining",
                effect=iam.Effect.DENY,
                actions=["bedrock-agentcore:InvokeAgentRuntimeCommand",
                         "sts:AssumeRole"],
                resources=["*"],
            ))

            self.harness_roles[agent.key] = role

        # Gateway execution role. F-35: the policy engine needs InvokeGuardrailChecks or
        # every guardrail policy silently does nothing.
        self.gateway_role = iam.Role(
            self, "GatewayRole",
            role_name=f"{PROJECT}-gateway",
            assumed_by=agentcore,
            description="AgentCore Gateway execution role, shared by the four Gateways.",
        )
        self.gateway_role.add_to_policy(iam.PolicyStatement(
            sid="GuardrailChecks",
            actions=["bedrock:InvokeGuardrailChecks", "bedrock:ApplyGuardrail"],
            resources=["*"],
        ))
        # Attaching a policy engine to a Gateway fails at create time without this. The error
        # names it precisely — "Confirm this role has bedrock-agentcore:GetPolicyEngine
        # permissions" — but the spec only listed InvokeGuardrailChecks (F-35), so it cost a
        # rolled-back stack to discover. F-77.
        # The complete set a Gateway needs when a policy engine is attached (F-77). It took two
        # rolled-back stacks to establish, one action at a time, because each failure only names
        # the next missing permission:
        #   GetPolicyEngine   — read the engine being attached
        #   AuthorizeAction   — the service assumes this role as "GenesisPolicyEngineCheck" at
        #                       create time and calls AuthorizeAction to prove the engine works
        self.gateway_role.add_to_policy(iam.PolicyStatement(
            sid="ReadAttachedPolicyEngine",
            actions=["bedrock-agentcore:GetPolicyEngine",
                     "bedrock-agentcore:ListPolicyEngines"],
            resources=["*"],
        ))
        # The authorization family. `AuthorizeAction` and `PartiallyAuthorizeActions` are
        # IAM-only actions — they are not operations in the SDK model, so they cannot be
        # enumerated and each had to be discovered from a separate failed deploy. The wildcard
        # is deliberate here rather than lazy: the set is undocumented and evidently growing,
        # and being one action short fails the stack at create time.
        #
        # It is scoped to policy engines in this account, and these actions only *ask* whether
        # something is permitted. Cedar itself still decides, and it defaults to deny (F-18),
        # so a wildcard here cannot widen what any agent is allowed to do.
        # The authorization check is performed against BOTH the policy engine and the gateway,
        # which took two more failed deploys to establish: the first error named the
        # policy-engine ARN, the next named the gateway ARN for the same action. Scope covers
        # both resource types in this account, and nothing else.
        self.gateway_role.add_to_policy(iam.PolicyStatement(
            sid="EvaluatePolicyEngineDecisions",
            actions=["bedrock-agentcore:*Authorize*"],
            resources=[
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:policy-engine/*",
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
            ],
        ))
        # The Gateway invokes the shared tool Lambda on the agent's behalf.
        self.gateway_role.add_to_policy(iam.PolicyStatement(
            sid="InvokeToolLambda",
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:{PROJECT}-tools",
                       f"arn:aws:lambda:{self.region}:{self.account}:function:{PROJECT}-tools:*"],
        ))

        # Step Functions role. F-70: the action is InvokeAgentRuntime, not InvokeHarness.
        # Proved by TestState in Phase 0 — the denial named it explicitly.
        self.sfn_role = iam.Role(
            self, "StateMachineRole",
            role_name=f"{PROJECT}-statemachine",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Both state machines. Invokes harnesses and the decision engine.",
        )
        self.sfn_role.add_to_policy(iam.PolicyStatement(
            sid="InvokeHarnesses",
            actions=["bedrock-agentcore:InvokeAgentRuntime",
                     "bedrock-agentcore:InvokeHarness"],
            resources=[f"arn:aws:bedrock-agentcore:*:{self.account}:harness/*"],
        ))
        self.case_table.grant_read_write_data(self.sfn_role)
        self.case_key.grant_encrypt_decrypt(self.sfn_role)

        self.pipe_role = iam.Role(
            self, "PipeRole",
            role_name=f"{PROJECT}-pipe",
            assumed_by=iam.ServicePrincipal("pipes.amazonaws.com"),
            description="EventBridge Pipe: SQS FIFO source to Phase 1 state machine target.",
        )

        # Ten roles, not nine: seven harness plus Gateway plus Step Functions plus Pipe.
        self.all_roles = (list(self.harness_roles.values())
                          + [self.gateway_role, self.sfn_role, self.pipe_role])
        assert len(self.all_roles) == 10, len(self.all_roles)

    # ------------------------------------------------------------------ outputs
    def _outputs(self) -> None:
        for j in JURISDICTIONS:
            cdk.CfnOutput(self, f"EvidenceBucket{j.code.upper()}Name",
                          value=self.evidence_buckets[j.code].bucket_name)
            cdk.CfnOutput(self, f"EvidenceKey{j.code.upper()}Arn",
                          value=self.jurisdiction_keys[j.code].key_arn)
        cdk.CfnOutput(self, "CaseTableName", value=self.case_table.table_name)
        cdk.CfnOutput(self, "TraceKeyArn", value=self.trace_key.key_arn)
        cdk.CfnOutput(self, "BiometricBucketName",
                      value=self.biometric_bucket.bucket_name)
        cdk.CfnOutput(self, "EvidenceLock", value=(
            f"{self.evidence_lock.name}: {self.evidence_lock.mode} "
            f"{self.evidence_lock.days} day(s)"))
        cdk.CfnOutput(self, "StateMachineRoleArn", value=self.sfn_role.role_arn)
        cdk.CfnOutput(self, "GatewayRoleArn", value=self.gateway_role.role_arn)
        for key, role in self.harness_roles.items():
            cdk.CfnOutput(self, f"HarnessRole{key.capitalize()}Arn", value=role.role_arn)
