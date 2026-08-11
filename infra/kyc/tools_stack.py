"""Tools stack — task 2.8.

One Lambda hosting seven tools with name-based dispatch, plus the account platform stub and the
velocity store.

Provisioned concurrency needs a version and an alias, not the `$LATEST` function. Task 2.2 asks
for it on the four Phase 1 tools; since all seven share one function, the alias carries it and
the Phase 2 tools get it as a side effect. That is a cost decision worth naming rather than
hiding: warm capacity is billed continuously, and A-15 measures whether it is worth it.
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
)
from constructs import Construct

from .config import PROJECT

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
                 # The submit_* output contracts write each agent's structured finding here
                 # (R11.13, F-89), which is what lets the state machine read data instead of
                 # parsing the model's prose.
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
            code=lambda_.Code.from_asset(_TOOLS_ASSET_PATH),
            timeout=Duration.seconds(30),
            memory_size=1024,
            log_group=self.tools_log_group,
            environment={
                "VELOCITY_TABLE": self.velocity_table.table_name,
                "CASE_TABLE": case_table_name,
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
            description="Seven KYC tools, name-based dispatch. Ported pattern from P-10.",
        )

        self.velocity_table.grant_read_write_data(self.tools_fn)

        # UpdateItem only, and no GetItem. The contract tools write one nested key each and never
        # read the case, so withholding read access means a compromised tool cannot enumerate
        # applicants — the findings path needs to deposit, not to browse.
        self.tools_fn.add_to_role_policy(iam.PolicyStatement(
            sid="RecordAgentFindings",
            actions=["dynamodb:UpdateItem"],
            resources=[case_table_arn],
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
            code=lambda_.Code.from_asset(_ACCOUNT_STUB_ASSET_PATH),
            timeout=Duration.seconds(10),
            memory_size=512,
            log_group=self.account_log_group,
            environment={
                # D-10 is open; these are placeholders pending the product's risk appetite.
                "PROVISIONAL_DEPOSIT_CEILING": "2000",
                "PROVISIONAL_CASH_LIMIT": "250",
            },
            description=("Account platform stub. Enforces the five R31.1 restrictions as a "
                         "transaction authoriser, not an advisory flag (R31.2)."),
        )

        cdk.CfnOutput(self, "ToolsFunctionArn", value=self.tools_fn.function_arn)
        cdk.CfnOutput(self, "ToolsAliasArn", value=self.tools_alias.function_arn)
        cdk.CfnOutput(self, "AccountStubArn", value=self.account_fn.function_arn)
        cdk.CfnOutput(self, "VelocityTableName", value=self.velocity_table.table_name)
