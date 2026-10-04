"""Tools stack — task 2.8.

One Lambda hosting the six evidence tools with name-based dispatch, the account platform stub and
its table, the velocity store, and the two runtime control parameters.

Provisioned concurrency needs a version and an alias, not the `$LATEST` function. Task 2.2 asks
for it on the Phase 1 tools; since all six share one function, the alias carries it and the Phase 2
tools get it as a side effect. That is a cost decision worth naming rather than hiding: warm
capacity is billed continuously, and A-15 measures whether it is worth it.

The alias is also why the controls are parameters. The Gateway calls `live`, a published version,
so an environment variable edited on the function never reaches a Gateway call. The tools read the
parameters per call instead (`tools/kyctools/controls.py`).
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from constructs import Construct

from .config import DEMO_FAULTS_PARAMETER, LIST_OVERLAY_PARAMETER, PROJECT

#: Never shipped in a deployment package: conftests mutate `sys.path`, which is harmless locally
#: and the sort of thing that resolves a module to the wrong file in a packaged runtime.
_ASSET_EXCLUDES = ["tests", "tests/*", "test_*.py", "pytest.ini", "__pycache__", "*.pyc",
                   ".pytest_cache"]

# Resolved from this file rather than from the working directory, matching `api_stack.py`. A
# relative `"../tools"` only works when CDK is invoked from `infra/`, so synthesising from anywhere
# else — including a repo-root pytest run — failed with "Cannot find asset at .../tools".
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS_ASSET_PATH = str(_REPO_ROOT / "tools")
_ACCOUNT_STUB_ASSET_PATH = str(_REPO_ROOT / "account-stub")


class ToolsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 evidence_bucket_names: dict[str, str],
                 evidence_key_arns: dict[str, str],
                 trace_key_arn: str,
                 # The liveness reference frame lives here, not in an evidence bucket: a real human
                 # face must stay deletable within 24 hours (R36.1-R36.3), and Object Lock in
                 # COMPLIANCE mode would make that impossible. `rekognition_compare_faces` needs to
                 # read it, which is why the tools Lambda is granted access to a second bucket.
                 biometric_bucket_name: str,
                 # Every evidence tool reads its inputs from the case record and records its
                 # result there, which is what lets the rules decide on recorded facts instead of
                 # on the model's prose.
                 case_table_name: str,
                 case_table_arn: str,
                 case_key_arn: str,
                 provisioned_concurrency: int = 0,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------ velocity store
        self.velocity_table = dynamodb.Table(
            self, "VelocityTable",
            table_name=f"{PROJECT}-velocity",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Velocity signals are transient by nature; TTL keeps the store from becoming a
            # long-lived record of who applied from which device (R17 data minimisation).
            time_to_live_attribute="expiresAt",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------ runtime controls
        #
        # Created with neutral values. The demo API's presenter routes change them on stage, and a
        # later deploy leaves those changes alone unless these defaults change. Both are plain
        # strings holding JSON; neither holds anything secret.
        self.demo_faults_parameter = ssm.StringParameter(
            self, "DemoFaults",
            parameter_name=DEMO_FAULTS_PARAMETER,
            string_value="{}",
            description=("Demo fault flags: {unavailableTools: [...], agentTimeouts: {agent: s}}. "
                         "Fail open, and can only send a case to a person, never approve one."),
        )
        self.list_overlay_parameter = ssm.StringParameter(
            self, "ListOverlay",
            parameter_name=LIST_OVERLAY_PARAMETER,
            string_value='{"entries": []}',
            description=("Sanctions list update published on top of the synthetic list: "
                         "{version, entries[]}. Fails closed: unreadable means unavailable."),
        )

        # ------------------------------------------------------------ the tool Lambda
        # An explicit LogGroup rather than `log_retention`, which is deprecated and creates a
        # custom-resource Lambda plus its own IAM role purely to set a retention value.
        self.tools_log_group = logs.LogGroup(
            self, "ToolsLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT}-tools",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.tools_fn = lambda_.Function(
            self, "ToolsFunction",
            function_name=f"{PROJECT}-tools",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(_TOOLS_ASSET_PATH, exclude=_ASSET_EXCLUDES),
            timeout=Duration.seconds(30),
            memory_size=1024,
            log_group=self.tools_log_group,
            environment={
                "VELOCITY_TABLE": self.velocity_table.table_name,
                "CASE_TABLE": case_table_name,
                "DEMO_FAULTS_PARAMETER": DEMO_FAULTS_PARAMETER,
                "LIST_OVERLAY_PARAMETER": LIST_OVERLAY_PARAMETER,
                # So the face-compare tool can default the liveness frame's bucket rather than
                # trusting an agent to name it. A model that guessed the wrong bucket would produce
                # an unreadable-image finding that looks like a bad document.
                "BIOMETRIC_BUCKET": biometric_bucket_name,
                # Task 2.6: off by default. The demo profile turns it on so the throttle and
                # rate-limit demos are reproducible rather than dependent on luck.
                "DEMO_PROFILE": "off",
                "DEMO_LATENCY_MS": "0",
                "LOG_LEVEL": "INFO",
                "POWERTOOLS_SERVICE_NAME": f"{PROJECT}-tools",
            },
            description=("Six KYC evidence tools, name-based dispatch. Each reads its inputs from "
                         "the case record and records its result there."),
        )

        self.velocity_table.grant_read_write_data(self.tools_fn)

        # GetItem and UpdateItem on the case record, by key only: no Query, no Scan. The tools
        # read the identity, documents and liveness frame the applicant submitted — so an agent
        # cannot choose what is checked — and record what they found. Without Query or Scan a
        # compromised tool can read a case it is named for, but cannot enumerate applicants.
        self.tools_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ReadCaseRecordFacts",
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[case_table_arn],
        ))
        self.tools_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ReadRuntimeControls",
            actions=["ssm:GetParameter"],
            resources=[self.demo_faults_parameter.parameter_arn,
                       self.list_overlay_parameter.parameter_arn],
        ))
        kms.Key.from_key_arn(self, "CaseKeyForFindings", case_key_arn).grant_encrypt_decrypt(
            self.tools_fn)

        for code, name in evidence_bucket_names.items():
            bucket = s3.Bucket.from_bucket_name(self, f"Evidence{code.upper()}", name)
            # Read only. A tool has no business writing evidence, and the write path goes
            # through the Public API with Object Lock semantics attached.
            bucket.grant_read(self.tools_fn)

        self.tools_fn.add_to_role_policy(iam.PolicyStatement(
            sid="DecryptEvidence",
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=sorted(evidence_key_arns.values()),
        ))
        # The liveness reference frame, for `rekognition_compare_faces`. Read only, and only this
        # one bucket: the tool compares two images in a request and stores nothing.
        #
        # Encrypted with the *trace* key rather than a jurisdiction evidence key, which is the whole
        # point of keeping it in a separate bucket — trace data is minimised and deleted, evidence is
        # retained under Object Lock, and a face belongs in the first category (R36).
        biometric = s3.Bucket.from_bucket_name(
            self, "BiometricBucketRef", biometric_bucket_name)
        biometric.grant_read(self.tools_fn)
        self.tools_fn.add_to_role_policy(iam.PolicyStatement(
            sid="DecryptBiometric",
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[trace_key_arn],
        ))
        self.tools_fn.add_to_role_policy(iam.PolicyStatement(
            sid="CompareFaces",
            actions=["rekognition:CompareFaces", "rekognition:DetectFaces"],
            resources=["*"],
        ))
        self.tools_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ExtractDocumentFields",
            # `DetectDocumentText` only — the synchronous OCR call `s3_get_document` makes to read
            # the machine-readable zone. Not `AnalyzeID`, `AnalyzeDocument` or any of the async
            # `Start*`/`Get*` operations: none is used, and the async pair in particular would let
            # this role start work that outlives the request and publish to an SNS topic.
            #
            # `resources=["*"]` because Textract's text-detection actions are not resource-scoped —
            # there is no ARN to name. The blast radius is bounded by the action list instead.
            actions=["textract:DetectDocumentText"],
            resources=["*"],
        ))

        # ------------------------------------------------------------ warm alias
        version = self.tools_fn.current_version
        self.tools_alias = lambda_.Alias(
            self, "ToolsAlias",
            alias_name="live",
            version=version,
            provisioned_concurrent_executions=(provisioned_concurrency or None),
        )

        # ------------------------------------------------------------ account stub
        #
        # A stand-in for the core banking platform, and the part of the demo that makes a
        # restriction real: the state machines move the account's posture, and a transfer the
        # posture forbids is refused here with the restriction that refused it (R31.2).
        #
        # One item per account plus one per transaction, under the same partition. DESTROY, not
        # RETAIN: this is a stub platform with demo balances, not a record of any decision — the
        # decisions live in the case table, the history table and the Object Lock bucket.
        self.account_table = dynamodb.Table(
            self, "AccountTable",
            table_name=f"{PROJECT}-accounts",
            partition_key=dynamodb.Attribute(
                name="applicantId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.account_log_group = logs.LogGroup(
            self, "AccountStubLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT}-account-stub",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.account_fn = lambda_.Function(
            self, "AccountStubFunction",
            function_name=f"{PROJECT}-account-stub",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="account_stub.handler",
            code=lambda_.Code.from_asset(_ACCOUNT_STUB_ASSET_PATH, exclude=_ASSET_EXCLUDES),
            timeout=Duration.seconds(10),
            memory_size=512,
            log_group=self.account_log_group,
            environment={
                "ACCOUNT_TABLE": self.account_table.table_name,
                # D-10 is open; a placeholder pending the product's risk appetite.
                "PROVISIONAL_DEPOSIT_CEILING": "2000",
            },
            description=("Account platform stub. Enforces restrictions as a transaction "
                         "authoriser, not an advisory flag (R31.2): a provisional account takes "
                         "deposits up to a ceiling and lets no money leave."),
        )
        self.account_table.grant_read_write_data(self.account_fn)

        cdk.CfnOutput(self, "ToolsFunctionArn", value=self.tools_fn.function_arn)
        cdk.CfnOutput(self, "ToolsAliasArn", value=self.tools_alias.function_arn)
        cdk.CfnOutput(self, "AccountStubArn", value=self.account_fn.function_arn)
        cdk.CfnOutput(self, "AccountTableName", value=self.account_table.table_name)
        cdk.CfnOutput(self, "VelocityTableName", value=self.velocity_table.table_name)
        cdk.CfnOutput(self, "DemoFaultsParameter", value=DEMO_FAULTS_PARAMETER)
        cdk.CfnOutput(self, "ListOverlayParameter", value=LIST_OVERLAY_PARAMETER)
