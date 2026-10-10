"""Public API — task 14.3, R33.1, R34.1, R34.3, R18.1.

HTTP API + Lambda, Cognito JWT authorizer. Three Lambdas, not one: `liveness.py` needs
Rekognition permissions (`CreateFaceLivenessSession`/`GetFaceLivenessSessionResults`) that carry
real cost and blast radius if broadened by a future change, `applicants.py` needs write access
to the case table and the ingress queue, and `review_api.py` needs read access to the audit
bucket and the ability to invoke `SendTaskSuccess`/`SendTaskFailure` — three different
capability sets that a single function would otherwise hold the union of.

Routes and their gate, enforced in the handler (`api/auth.py`), since the HTTP API's JWT
authorizer proves only who the caller is:

  POST /liveness/session                    liveness_fn    the applicant (their own case)
  GET  /liveness/{sessionId}/result         liveness_fn    the applicant (their own case)
  POST /documents/presign                   applicants_fn  the applicant
  POST /applicants                          applicants_fn  the applicant (their own documents)
  GET  /cases/{applicantId}                 applicants_fn  owner, reviewers, presenters
  GET  /accounts/{applicantId}              accounts_fn    owner, reviewers, presenters
  GET  /accounts/{applicantId}/transactions accounts_fn    owner, reviewers, presenters
  POST /accounts/{applicantId}/transactions accounts_fn    owner, presenters
  GET  /review/queue                        review_fn      reviewers
  POST /review/{caseId}/decision            review_fn      reviewers
  GET  /review/{caseId}/trace               review_fn      reviewers
  GET  /audit/{applicantId}                 review_fn      reviewers
  GET  /demo/cases                          demo_fn        presenters   (demo deployments only)
  GET  /demo/controls                       demo_fn        presenters   (demo deployments only)
  PUT  /demo/faults                         demo_fn        presenters   (demo deployments only)
  POST /demo/list-update                    demo_fn        presenters   (demo deployments only)
  POST /demo/reset                          demo_fn        presenters   (demo deployments only)

Five Lambdas, because the capability sets are disjoint: liveness needs Rekognition, intake needs
the ingress queue and the velocity store, the console needs the audit bucket and the task-token
release, the account routes need only the account platform, and the demo routes need write
access to two parameters, the lifecycle bus and the velocity store — a single function would hold
the union of all five.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sqs as sqs,
)
from constructs import Construct

from .config import (
    DEMO_FAULTS_PARAMETER,
    JURISDICTIONS,
    LIST_OVERLAY_PARAMETER,
    LIVENESS_CHALLENGES,
    LIVENESS_LIGHT_CHALLENGE,
    POLICY_ENFORCEMENT_MODES,
    PROJECT,
)

#: Absolute, not "../api" — the relative form only resolves when the process cwd happens to be
#: `infra/` (`cdk synth` run from there). The repo's own pytest.ini requires running pytest from
#: the repo root for `tools`/`decision-engine`/etc. to be importable at all (its docstring is
#: explicit about this), and under that cwd "../api" resolves to a directory one level above the
#: repo that does not exist. Deriving from `__file__` makes this stack's asset path correct
#: under both invocations.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_ASSET_PATH = str(_REPO_ROOT / "api")


def _review_lambda_asset_path() -> str:
    """`review_api.py`'s runtime `sys.path.insert(..., "../review")` (module docstring)
    resolves fine locally, where `api/` and `review/` are adjacent directories in the repo, but
    not inside a Lambda deployment package: **confirmed live** —
    `Runtime.ImportModuleError: No module named 'review_handler'`, since `Code.from_asset("../api")`
    only zips `api/`. `review/review_handler.py` (and the modules it imports:
    `review/decisions.py`, `review/metrics.py`) must ship in the same package as `review_api.py`.

    Builds a combined staging directory once per synth — `api/*.py` plus `review/*.py` (source
    only, no `tests/`) — and returns its path so only the review Lambda's asset differs from the
    other two, which need none of this.
    """
    import shutil

    staging = _REPO_ROOT / "infra" / ".build" / "review-lambda"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    for py_file in (_REPO_ROOT / "api").glob("*.py"):
        shutil.copy2(py_file, staging / py_file.name)
    for py_file in (_REPO_ROOT / "review").glob("*.py"):
        shutil.copy2(py_file, staging / py_file.name)

    return str(staging)


#: The test tree never ships: conftests mutate `sys.path`, which is the sort of thing that
#: resolves a module to the wrong file in a packaged runtime.
_ASSET_EXCLUDES = ["tests", "tests/*", "__pycache__", "*.pyc", ".pytest_cache"]


class ApiStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 user_pool_id: str,
                 user_pool_client_id: str,
                 case_table_name: str,
                 case_table_arn: str,
                 case_key_arn: str,
                 evidence_bucket_names: dict[str, str],
                 evidence_key_arns: dict[str, str],
                 biometric_bucket_name: str,
                 biometric_key_arn: str,
                 applicants_queue_url: str,
                 applicants_queue_arn: str,
                 review_queue_url: str,
                 review_queue_arn: str,
                 audit_bucket_name: str,
                 audit_key_arn: str,
                 # Task 14.9 — the reviewer trace view. Each phase's execution history is the
                 # timeline for the decisions it wrote; the audit record carries the execution
                 # name, since the ingress pipe starts Phase 1 with an auto-generated one.
                 phase1_state_machine_arn: str,
                 # The account platform, for the account routes and the console's account view.
                 account_function_arn: str,
                 # D-41: the velocity store the Fraud agent's tool counts (`KycTools`). Intake
                 # adds one row per person per device and per network; the demo Reset clears it.
                 velocity_table_name: str,
                 velocity_table_arn: str,
                 phase2_state_machine_arn: str | None = None,
                 phase3_state_machine_arn: str | None = None,
                 # The lifecycle bus a list update is published to (Act 3).
                 lifecycle_bus_arn: str | None = None,
                 lifecycle_bus_name: str | None = None,
                 # Presenter controls. A demo capability: off means the routes and their function
                 # do not exist at all, rather than existing behind a flag.
                 demo_controls: bool = True,
                 # D-36: which Face Liveness challenge every session asks for. The service
                 # default unless the app chooses otherwise (it does, for a demo).
                 liveness_challenge: str = LIVENESS_LIGHT_CHALLENGE,
                 # D-40: the mode the Cedar policy engines are attached in, so the console can say
                 # how tool calls are governed while a run is in progress, before any decision
                 # record pins it. Omitted, the trace reports the mode as unknown.
                 policy_enforcement: str | None = None,
                 **kwargs) -> None:
        if liveness_challenge not in LIVENESS_CHALLENGES:
            raise ValueError(
                f"liveness_challenge must be one of {LIVENESS_CHALLENGES}, "
                f"got {liveness_challenge!r}")
        if policy_enforcement is not None and policy_enforcement not in POLICY_ENFORCEMENT_MODES:
            raise ValueError(
                f"policy_enforcement must be one of {POLICY_ENFORCEMENT_MODES}, "
                f"got {policy_enforcement!r}")
        super().__init__(scope, construct_id, **kwargs)
        machine_arns = {"PHASE1_STATE_MACHINE_ARN": phase1_state_machine_arn,
                        "PHASE2_STATE_MACHINE_ARN": phase2_state_machine_arn,
                        "PHASE3_STATE_MACHINE_ARN": phase3_state_machine_arn}
        machine_arns = {k: v for k, v in machine_arns.items() if v}

        common_env = {
            "CASE_TABLE_NAME": case_table_name,
            "LOG_LEVEL": "INFO",
            "POWERTOOLS_SERVICE_NAME": f"{PROJECT}-api",
        }

        # ------------------------------------------------------------ liveness Lambda (14.2)
        self.liveness_fn = lambda_.Function(
            self, "LivenessFunction",
            function_name=f"{PROJECT}-api-liveness",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="liveness.lambda_handler",
            code=lambda_.Code.from_asset(_API_ASSET_PATH, exclude=_ASSET_EXCLUDES),
            timeout=Duration.seconds(15),
            memory_size=512,
            log_group=logs.LogGroup(
                self, "LivenessLogGroup",
                log_group_name=f"/aws/lambda/{PROJECT}-api-liveness",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                **common_env,
                "BIOMETRIC_BUCKET_NAME": biometric_bucket_name,
                # The biometric bucket lives in this stack's own region (`foundation_stack.py`
                # provisions one bucket, not one per jurisdiction). `create_session` only sets
                # `Settings.OutputConfig` — the only way `GetFaceLivenessSessionResults` ever
                # returns an `S3Object` reference rather than nothing — when the Face Liveness
                # call's own region matches this, since AWS requires the OutputConfig bucket and
                # the endpoint to be in the same region.
                "BIOMETRIC_BUCKET_REGION": self.region,
                "LIVENESS_CHALLENGE": liveness_challenge,
            },
            description=(
                "POST /liveness/session, GET /liveness/{id}/result. Applies the threshold "
                "server-side; the browser never receives the raw confidence score (F-44)."
            ),
        )
        self.liveness_fn.add_to_role_policy(iam.PolicyStatement(
            sid="FaceLivenessSessionLifecycle",
            # Both actions take no resource ARN — SessionId, not an IAM resource, scopes a call
            # to a specific session (confirmed by the docs' own IAM examples, which use "*").
            actions=["rekognition:CreateFaceLivenessSession",
                    "rekognition:GetFaceLivenessSessionResults"],
            resources=["*"],
        ))
        self.liveness_fn.add_to_role_policy(iam.PolicyStatement(
            sid="RecordLivenessResult",
            # GetItem to check the session is bound to this case before recording its result, and
            # to answer a retry from what is already recorded. By key only: no Query, no Scan.
            actions=["dynamodb:UpdateItem", "dynamodb:GetItem"],
            resources=[case_table_arn],
        ))
        kms.Key.from_key_arn(self, "CaseKeyForLiveness", case_key_arn).grant_encrypt_decrypt(
            self.liveness_fn)
        self.liveness_fn.add_to_role_policy(iam.PolicyStatement(
            sid="CopyLivenessImages",
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[f"arn:aws:s3:::{biometric_bucket_name}/*"],
        ))
        kms.Key.from_key_arn(
            self, "BiometricKeyForLiveness", biometric_key_arn
        ).grant_encrypt_decrypt(self.liveness_fn)

        # ------------------------------------------------------------ intake hash key (D-41)
        #
        # The device id and the network an application came from are stored only as HMACs under
        # this key, which never leaves KMS: an unkeyed hash of an IPv4 address is reversed by
        # trying all 2^32 of them. Intake may compute a MAC with it and do nothing else.
        self.intake_hash_key = kms.Key(
            self, "IntakeHashKey",
            key_spec=kms.KeySpec.HMAC_256,
            key_usage=kms.KeyUsage.GENERATE_VERIFY_MAC,
            description=("Keyed hashes of the device and network an application came from, for "
                         "velocity (D-41). The rows they key expire after 24 hours."),
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )

        # ------------------------------------------------------------ applicants Lambda (14.3)
        self.applicants_fn = lambda_.Function(
            self, "ApplicantsFunction",
            function_name=f"{PROJECT}-api-applicants",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="applicants.lambda_handler",
            code=lambda_.Code.from_asset(_API_ASSET_PATH, exclude=_ASSET_EXCLUDES),
            timeout=Duration.seconds(15),
            memory_size=512,
            log_group=logs.LogGroup(
                self, "ApplicantsLogGroup",
                log_group_name=f"/aws/lambda/{PROJECT}-api-applicants",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                **common_env,
                "APPLICANTS_QUEUE_URL": applicants_queue_url,
                "VELOCITY_TABLE_NAME": velocity_table_name,
                "INTAKE_HASH_KEY_ID": self.intake_hash_key.key_arn,
                **{f"EVIDENCE_BUCKET_{code.upper()}": name
                   for code, name in evidence_bucket_names.items()},
            },
            description=(
                "POST /documents/presign, POST /applicants, GET /cases/{id}. Enqueues onto "
                "the real ingress queue (task 7.1/7.2), the same path spike/live_run_via_queue.py "
                "exercises."
            ),
        )
        for code, name in evidence_bucket_names.items():
            self.applicants_fn.add_to_role_policy(iam.PolicyStatement(
                sid=f"PresignUpload{code.upper()}",
                actions=["s3:PutObject"],
                resources=[f"arn:aws:s3:::{name}/*"],
            ))
        self.applicants_fn.add_to_role_policy(iam.PolicyStatement(
            sid="EncryptEvidenceOnUpload",
            actions=["kms:GenerateDataKey", "kms:Encrypt"],
            resources=sorted(evidence_key_arns.values()),
        ))
        self.applicants_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ReadWriteOwnCase",
            # `UpdateItem`, not `PutItem`: intake writes the case with a conditional *update* so
            # that liveness evidence persisted before intake (F-40's 3-minute window) survives.
            # `PutItem` is deliberately no longer granted — with it, a future change back to a put
            # would silently overwrite that evidence rather than failing, and losing liveness
            # evidence is invisible until a case is stepped up for the wrong reason.
            actions=["dynamodb:UpdateItem", "dynamodb:GetItem"],
            resources=[case_table_arn],
        ))
        kms.Key.from_key_arn(
            self, "CaseKeyForApplicants", case_key_arn
        ).grant_encrypt_decrypt(self.applicants_fn)
        applicants_queue = sqs.Queue.from_queue_arn(
            self, "ApplicantsQueueRef", applicants_queue_arn)
        applicants_queue.grant_send_messages(self.applicants_fn)
        self.applicants_fn.add_to_role_policy(iam.PolicyStatement(
            sid="CountApplicationVelocity",
            # D-41: one row per person per device and per network. PutItem on the velocity store
            # only; the case table still gets none (`ReadWriteOwnCase` above says why).
            actions=["dynamodb:PutItem"],
            resources=[velocity_table_arn],
        ))
        self.applicants_fn.add_to_role_policy(iam.PolicyStatement(
            sid="HashIntakeIdentifiers",
            actions=["kms:GenerateMac"],
            resources=[self.intake_hash_key.key_arn],
        ))

        # ------------------------------------------------------------ review Lambda (14.3)
        self.review_fn = lambda_.Function(
            self, "ReviewApiFunction",
            function_name=f"{PROJECT}-api-review",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="review_api.lambda_handler",
            code=lambda_.Code.from_asset(_review_lambda_asset_path()),
            timeout=Duration.seconds(15),
            memory_size=512,
            log_group=logs.LogGroup(
                self, "ReviewApiLogGroup",
                log_group_name=f"/aws/lambda/{PROJECT}-api-review",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                **common_env,
                "AUDIT_BUCKET_NAME": audit_bucket_name,
                "ACCOUNT_FUNCTION_NAME": account_function_arn,
                **machine_arns,
                **({"POLICY_ENFORCEMENT": policy_enforcement} if policy_enforcement else {}),
            },
            description=(
                "GET /review/queue, POST /review/{id}/decision, GET /review/{id}/trace, "
                "GET /audit/{id}. Delegates decision handling to review/review_handler.py — no "
                "second implementation of first-writer-wins."
            ),
        )
        self.review_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ReadReviewQueue",
            actions=["dynamodb:Query", "dynamodb:GetItem"],
            resources=[case_table_arn, f"{case_table_arn}/index/state-index"],
        ))
        # No SQS grant at all. The console used to find task tokens by reading the review queue,
        # and every read spent a live token's redrive budget; the token is now on the case record
        # (`review/token_materializer.py`), so this function has no reason to touch the queue.
        self.review_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ReadAccountForConsole",
            actions=["lambda:InvokeFunction"],
            resources=[account_function_arn],
        ))
        self.review_fn.add_to_role_policy(iam.PolicyStatement(
            sid="WriteReviewDecision",
            actions=["dynamodb:UpdateItem"],
            resources=[case_table_arn],
        ))
        kms.Key.from_key_arn(
            self, "CaseKeyForReview", case_key_arn
        ).grant_encrypt_decrypt(self.review_fn)
        self.review_fn.add_to_role_policy(iam.PolicyStatement(
            sid="CloseTheReviewLoop",
            # design §7.5 — the receiver ends the SQS .waitForTaskToken wait in asl.py's
            # ManualReview state. No resource scoping possible: task tokens are opaque and
            # carry no state machine ARN (harnesses_stack.py notes the same for the SFN role).
            actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
            resources=["*"],
        ))
        self.review_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ReadAuditRecords",
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[f"arn:aws:s3:::{audit_bucket_name}",
                      f"arn:aws:s3:::{audit_bucket_name}/*"],
        ))
        kms.Key.from_key_arn(
            self, "AuditKeyForReview", audit_key_arn
        ).grant_decrypt(self.review_fn)
        self.review_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ReadExecutionHistoryForTrace",
            # `DescribeExecution`/`GetExecutionHistory` only — read-only. Nothing here can start,
            # stop, or send a task token through this permission; that capability is granted
            # separately, above, and only for `SendTaskSuccess`/`SendTaskFailure`.
            #
            # An execution ARN is a different resource type from a state machine ARN
            # (`...:execution:<name>:<executionName>`, not `...:stateMachine:<name>`), so the
            # resource pattern is rebuilt from Phase 1's name rather than appending a wildcard to
            # the state machine ARN, which would not match anything.
            actions=["states:DescribeExecution", "states:GetExecutionHistory"],
            resources=[
                cdk.Fn.sub(
                    "arn:aws:states:${AWS::Region}:${AWS::AccountId}:execution:${Name}:*",
                    {"Name": cdk.Fn.select(6, cdk.Fn.split(":", arn))},
                )
                for arn in machine_arns.values()
            ],
        ))

        # ------------------------------------------------------------ accounts Lambda
        self.accounts_fn = lambda_.Function(
            self, "AccountsFunction",
            function_name=f"{PROJECT}-api-accounts",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="accounts.lambda_handler",
            code=lambda_.Code.from_asset(_API_ASSET_PATH, exclude=_ASSET_EXCLUDES),
            timeout=Duration.seconds(10),
            memory_size=256,
            log_group=logs.LogGroup(
                self, "AccountsLogGroup",
                log_group_name=f"/aws/lambda/{PROJECT}-api-accounts",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={**common_env, "ACCOUNT_FUNCTION_NAME": account_function_arn},
            description=("GET /accounts/{id}, GET|POST /accounts/{id}/transactions. The account "
                         "platform refuses what the restrictions forbid and says which one."),
        )
        self.accounts_fn.add_to_role_policy(iam.PolicyStatement(
            sid="UseAccountPlatform",
            actions=["lambda:InvokeFunction"],
            resources=[account_function_arn],
        ))

        # ------------------------------------------------------------ demo Lambda
        self.demo_fn: lambda_.Function | None = None
        if demo_controls:
            if not (lifecycle_bus_arn and lifecycle_bus_name):
                raise ValueError("demo controls publish list updates to the lifecycle bus; "
                                 "pass lifecycle_bus_arn and lifecycle_bus_name")
            parameter_arns = [
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{name}"
                for name in (DEMO_FAULTS_PARAMETER, LIST_OVERLAY_PARAMETER)]
            self.demo_fn = lambda_.Function(
                self, "DemoFunction",
                function_name=f"{PROJECT}-api-demo",
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler="demo.lambda_handler",
                code=lambda_.Code.from_asset(_API_ASSET_PATH, exclude=_ASSET_EXCLUDES),
                # A list update waits out the screening tool's cache before publishing.
                timeout=Duration.seconds(20),
                memory_size=256,
                log_group=logs.LogGroup(
                    self, "DemoLogGroup",
                    log_group_name=f"/aws/lambda/{PROJECT}-api-demo",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
                environment={
                    **common_env,
                    "DEMO_FAULTS_PARAMETER": DEMO_FAULTS_PARAMETER,
                    "LIST_OVERLAY_PARAMETER": LIST_OVERLAY_PARAMETER,
                    "LIFECYCLE_BUS_NAME": lifecycle_bus_name,
                    "LIST_UPDATE_SETTLE_SECONDS": "4",
                    "VELOCITY_TABLE_NAME": velocity_table_name,
                },
                description=("Presenter controls: demo faults and a synthetic sanctions list "
                             "update (Act 3). Presenters group only; demo deployments only."),
            )
            self.demo_fn.add_to_role_policy(iam.PolicyStatement(
                sid="WriteDemoControls",
                actions=["ssm:GetParameter", "ssm:PutParameter"],
                resources=parameter_arns,
            ))
            self.demo_fn.add_to_role_policy(iam.PolicyStatement(
                sid="PublishListUpdate",
                actions=["events:PutEvents"],
                resources=[lifecycle_bus_arn],
            ))
            self.demo_fn.add_to_role_policy(iam.PolicyStatement(
                sid="FindOpenAccounts",
                actions=["dynamodb:Query"],
                resources=[f"{case_table_arn}/index/state-index"],
            ))
            self.demo_fn.add_to_role_policy(iam.PolicyStatement(
                sid="ClearVelocityCounts",
                # Reset clears the device and network counts (D-41). Scan and delete on the
                # velocity store only: the case table stays read-only to this function.
                actions=["dynamodb:Scan", "dynamodb:BatchWriteItem"],
                resources=[velocity_table_arn],
            ))
            kms.Key.from_key_arn(self, "CaseKeyForDemo", case_key_arn).grant_decrypt(
                self.demo_fn)

        # ------------------------------------------------------------ HTTP API + JWT auth
        authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool_id}",
            jwt_audience=[user_pool_client_id],
        )

        self.http_api = apigwv2.HttpApi(
            self, "HttpApi",
            api_name=f"{PROJECT}-api",
            description=(
                "Public API: liveness session lifecycle, presigned upload, case status, "
                "review, audit (R33.1, R34.1)."
            ),
            default_authorizer=authorizer,
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=([apigwv2.CorsHttpMethod.GET, apigwv2.CorsHttpMethod.POST]
                               + ([apigwv2.CorsHttpMethod.PUT] if demo_controls else [])),
                allow_headers=["Authorization", "Content-Type"],
            ),
        )

        liveness_integration = integrations.HttpLambdaIntegration(
            "LivenessIntegration", self.liveness_fn)
        applicants_integration = integrations.HttpLambdaIntegration(
            "ApplicantsIntegration", self.applicants_fn)
        review_integration = integrations.HttpLambdaIntegration(
            "ReviewIntegration", self.review_fn)

        self.http_api.add_routes(
            path="/liveness/session", methods=[apigwv2.HttpMethod.POST],
            integration=liveness_integration)
        self.http_api.add_routes(
            path="/liveness/{sessionId}/result", methods=[apigwv2.HttpMethod.GET],
            integration=liveness_integration)
        self.http_api.add_routes(
            path="/documents/presign", methods=[apigwv2.HttpMethod.POST],
            integration=applicants_integration)
        self.http_api.add_routes(
            path="/applicants", methods=[apigwv2.HttpMethod.POST],
            integration=applicants_integration)
        self.http_api.add_routes(
            path="/cases/{applicantId}", methods=[apigwv2.HttpMethod.GET],
            integration=applicants_integration)
        self.http_api.add_routes(
            path="/review/queue", methods=[apigwv2.HttpMethod.GET],
            integration=review_integration)
        self.http_api.add_routes(
            path="/review/{caseId}/decision", methods=[apigwv2.HttpMethod.POST],
            integration=review_integration)
        self.http_api.add_routes(
            path="/review/{caseId}/trace", methods=[apigwv2.HttpMethod.GET],
            integration=review_integration)
        self.http_api.add_routes(
            path="/audit/{applicantId}", methods=[apigwv2.HttpMethod.GET],
            integration=review_integration)

        accounts_integration = integrations.HttpLambdaIntegration(
            "AccountsIntegration", self.accounts_fn)
        self.http_api.add_routes(
            path="/accounts/{applicantId}", methods=[apigwv2.HttpMethod.GET],
            integration=accounts_integration)
        self.http_api.add_routes(
            path="/accounts/{applicantId}/transactions",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=accounts_integration)

        if self.demo_fn is not None:
            demo_integration = integrations.HttpLambdaIntegration(
                "DemoIntegration", self.demo_fn)
            for path, method in (("/demo/cases", apigwv2.HttpMethod.GET),
                                 ("/demo/controls", apigwv2.HttpMethod.GET),
                                 ("/demo/faults", apigwv2.HttpMethod.PUT),
                                 ("/demo/list-update", apigwv2.HttpMethod.POST),
                                 ("/demo/reset", apigwv2.HttpMethod.POST)):
                self.http_api.add_routes(path=path, methods=[method],
                                         integration=demo_integration)

        cdk.CfnOutput(self, "ApiUrl", value=self.http_api.url or "")
        cdk.CfnOutput(self, "LivenessFunctionArn", value=self.liveness_fn.function_arn)
        cdk.CfnOutput(self, "ApplicantsFunctionArn", value=self.applicants_fn.function_arn)
        cdk.CfnOutput(self, "ReviewApiFunctionArn", value=self.review_fn.function_arn)
        cdk.CfnOutput(self, "AccountsFunctionArn", value=self.accounts_fn.function_arn)
