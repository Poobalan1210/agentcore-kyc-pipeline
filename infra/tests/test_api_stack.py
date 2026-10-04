"""Public API stack — task 14.3, R33.1, R34.1, R34.3."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from kyc.api_stack import ApiStack

COMMON = dict(
    user_pool_id="us-east-1_ABC123",
    user_pool_client_id="clientid123",
    case_table_name="kyc90-cases",
    case_table_arn="arn:aws:dynamodb:us-east-1:123456789012:table/kyc90-cases",
    case_key_arn="arn:aws:kms:us-east-1:123456789012:key/case-key",
    evidence_bucket_names={"us": "kyc90-evidence-us", "uk": "kyc90-evidence-uk",
                           "au": "kyc90-evidence-au", "jp": "kyc90-evidence-jp"},
    evidence_key_arns={"us": "arn:aws:kms:us-east-1:123456789012:key/us-key",
                       "uk": "arn:aws:kms:us-east-1:123456789012:key/uk-key",
                       "au": "arn:aws:kms:us-east-1:123456789012:key/au-key",
                       "jp": "arn:aws:kms:us-east-1:123456789012:key/jp-key"},
    biometric_bucket_name="kyc90-demo-biometric",
    biometric_key_arn="arn:aws:kms:us-east-1:123456789012:key/trace-key",
    applicants_queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/kyc90-applicants.fifo",
    applicants_queue_arn="arn:aws:sqs:us-east-1:123456789012:kyc90-applicants.fifo",
    review_queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/kyc90-review",
    review_queue_arn="arn:aws:sqs:us-east-1:123456789012:kyc90-review",
    audit_bucket_name="kyc90-evidence-us",
    audit_key_arn="arn:aws:kms:us-east-1:123456789012:key/us-key",
    phase1_state_machine_arn="arn:aws:states:us-east-1:123456789012:stateMachine:kyc90-phase1",
    phase2_state_machine_arn="arn:aws:states:us-east-1:123456789012:stateMachine:kyc90-phase2",
    phase3_state_machine_arn=(
        "arn:aws:states:us-east-1:123456789012:stateMachine:kyc90-phase3-lifecycle"),
    account_function_arn="arn:aws:lambda:us-east-1:123456789012:function:kyc90-account-stub",
    lifecycle_bus_arn="arn:aws:events:us-east-1:123456789012:event-bus/kyc90-lifecycle",
    lifecycle_bus_name="kyc90-lifecycle",
)


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = ApiStack(
        app, "TestApi",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
        **COMMON,
    )
    return Template.from_stack(stack)


CORE_ROUTES = {
    "POST /liveness/session", "GET /liveness/{sessionId}/result",
    "POST /documents/presign", "POST /applicants", "GET /cases/{applicantId}",
    "GET /review/queue", "POST /review/{caseId}/decision", "GET /review/{caseId}/trace",
    "GET /audit/{applicantId}",
    "GET /accounts/{applicantId}", "GET /accounts/{applicantId}/transactions",
    "POST /accounts/{applicantId}/transactions",
}
DEMO_ROUTES = {"GET /demo/cases", "GET /demo/controls", "PUT /demo/faults",
               "POST /demo/list-update", "POST /demo/reset"}
ALL_ROUTES = CORE_ROUTES | DEMO_ROUTES


def _route_keys(template: Template) -> set[str]:
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    return {body["Properties"]["RouteKey"] for body in routes.values()}


def test_every_route_exists(template: Template) -> None:
    """R33.1's minimum, R34's review/audit routes, the trace view, the account routes, and the
    presenter controls."""
    assert _route_keys(template) == ALL_ROUTES


def test_demo_controls_can_be_left_out_entirely() -> None:
    """A deployment without presenters has no demo routes and no demo function at all."""
    app = cdk.App()
    stack = ApiStack(app, "NoDemo", env=cdk.Environment(account="123456789012",
                                                        region="us-east-1"),
                     **{**COMMON, "demo_controls": False})
    template = Template.from_stack(stack)
    assert _route_keys(template) == CORE_ROUTES
    handlers = {b["Properties"]["Handler"]
                for b in template.find_resources("AWS::Lambda::Function").values()}
    assert "demo.lambda_handler" not in handlers


def _liveness_env(template: Template) -> dict[str, str]:
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "liveness.lambda_handler"}})
    (function,) = functions.values()
    return function["Properties"]["Environment"]["Variables"]


def test_the_light_challenge_is_the_stacks_default(template: Template) -> None:
    """D-36 — AWS recommends the light challenge for the highest accuracy; a stack built without
    a choice asks for it explicitly."""
    assert _liveness_env(template)["LIVENESS_CHALLENGE"] == "FaceMovementAndLightChallenge"


def test_a_demo_can_ask_for_the_movement_challenge() -> None:
    app = cdk.App()
    stack = ApiStack(app, "MovementApi", env=cdk.Environment(account="123456789012",
                                                             region="us-east-1"),
                     **{**COMMON, "liveness_challenge": "FaceMovementChallenge"})
    assert _liveness_env(Template.from_stack(stack))["LIVENESS_CHALLENGE"] == (
        "FaceMovementChallenge")


def test_an_unknown_challenge_is_refused_at_synth() -> None:
    with pytest.raises(ValueError, match="liveness_challenge"):
        ApiStack(cdk.App(), "BadApi", env=cdk.Environment(account="123456789012",
                                                          region="us-east-1"),
                 **{**COMMON, "liveness_challenge": "FaceMovement"})


def test_the_api_and_the_stack_agree_on_the_challenge_names() -> None:
    """`api/liveness.py` cannot import the CDK app, so the pair is held equal here, read from
    the source rather than imported, so the API module's own imports stay out of this test."""
    import ast
    from pathlib import Path

    from kyc.config import LIVENESS_LIGHT_CHALLENGE, LIVENESS_MOVEMENT_CHALLENGE

    source = (Path(__file__).resolve().parents[2] / "api" / "liveness.py").read_text()
    constants = {
        node.targets[0].id: node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    assert constants["LIGHT_CHALLENGE"] == LIVENESS_LIGHT_CHALLENGE
    assert constants["MOVEMENT_CHALLENGE"] == LIVENESS_MOVEMENT_CHALLENGE


def test_the_authorizer_is_jwt_backed_by_the_cognito_user_pool(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {
            "AuthorizerType": "JWT",
            "JwtConfiguration": Match.object_like({
                "Issuer": Match.string_like_regexp(
                    r"https://cognito-idp\.us-east-1\.amazonaws\.com/us-east-1_ABC123"),
                "Audience": ["clientid123"],
            }),
        },
    )


def test_every_route_requires_the_jwt_authorizer(template: Template) -> None:
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    for body in routes.values():
        assert body["Properties"]["AuthorizationType"] == "JWT"


def test_review_lambda_asset_bundles_review_handler(template: Template) -> None:
    """Confirmed live: `Code.from_asset("../api")` alone produced
    `Runtime.ImportModuleError: No module named 'review_handler'` in the deployed function —
    `review_api.py` imports `review_handler.py`, which lives in a sibling directory that a
    plain api/-only asset never includes. `_review_lambda_asset_path()` must stage a combined
    directory containing both, and the staged output is what this asserts against directly
    rather than trusting the CDK construct's own bookkeeping."""
    from pathlib import Path

    from kyc.api_stack import _review_lambda_asset_path

    staged = Path(_review_lambda_asset_path())
    assert (staged / "review_api.py").exists()
    assert (staged / "review_handler.py").exists()
    assert (staged / "decisions.py").exists()
    assert (staged / "metrics.py").exists()


def test_separate_lambdas_not_one_monolith(template: Template) -> None:
    """Disjoint IAM capability sets — a single function would hold the union, which is a broader
    blast radius than any one route needs."""
    fns = template.find_resources("AWS::Lambda::Function")
    handlers = {body["Properties"]["Handler"] for body in fns.values()}
    assert handlers == {"liveness.lambda_handler", "applicants.lambda_handler",
                        "review_api.lambda_handler", "accounts.lambda_handler",
                        "demo.lambda_handler"}


def test_liveness_function_has_no_dynamodb_query_permission(template: Template) -> None:
    """The liveness Lambda only ever UpdateItems its own persisted result (R33.5) — it has no
    business reading the review queue or any other applicant's case."""
    policies = template.find_resources("AWS::IAM::Policy")
    liveness_policy = next(
        body for name, body in policies.items() if "LivenessFunction" in name)
    actions = set()
    for stmt in liveness_policy["Properties"]["PolicyDocument"]["Statement"]:
        action = stmt.get("Action")
        if isinstance(action, list):
            actions.update(action)
        elif action:
            actions.add(action)
    assert "dynamodb:Query" not in actions
    assert "dynamodb:Scan" not in actions


def test_applicants_function_can_update_but_not_put_the_case(template: Template) -> None:
    """Intake writes the case with a conditional `UpdateItem`, because liveness evidence is
    persisted to the same item *before* intake runs (F-40's 3-minute window). `PutItem` is
    deliberately not granted: with it, a change back to a put would silently overwrite that
    evidence instead of failing, and lost liveness evidence is invisible until a case is stepped
    up for the wrong reason — which is exactly the bug this replaced."""
    policies = template.find_resources("AWS::IAM::Policy")
    policy = next(body for name, body in policies.items() if "ApplicantsFunction" in name)
    actions = set()
    for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
        action = stmt.get("Action")
        if isinstance(action, list):
            actions.update(action)
        elif action:
            actions.add(action)
    assert "dynamodb:UpdateItem" in actions
    assert "dynamodb:GetItem" in actions
    assert "dynamodb:PutItem" not in actions


def test_review_function_can_read_execution_history_but_not_control_executions(
    template: Template
) -> None:
    """Task 14.9 — the trace view. Read-only and scoped to describe/history: nothing here can
    start, stop, or otherwise control a Step Functions execution. `SendTaskSuccess`/
    `SendTaskFailure` are granted separately and only for closing the human-review wait."""
    policies = template.find_resources("AWS::IAM::Policy")
    policy = next(body for name, body in policies.items() if "ReviewApiFunction" in name)
    actions = set()
    for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
        action = stmt.get("Action")
        actions.update(action if isinstance(action, list) else [action])
    assert "states:DescribeExecution" in actions
    assert "states:GetExecutionHistory" in actions
    for forbidden in ("states:StartExecution", "states:StopExecution",
                      "states:DeleteStateMachine"):
        assert forbidden not in actions, forbidden


def test_review_function_can_send_task_success_and_failure(template: Template) -> None:
    """design §7.5 — closing the ManualReview .waitForTaskToken wait."""
    policies = template.find_resources("AWS::IAM::Policy")
    review_policy = next(
        body for name, body in policies.items() if "ReviewApiFunction" in name)
    statements = review_policy["Properties"]["PolicyDocument"]["Statement"]
    actions = set()
    for stmt in statements:
        action = stmt.get("Action")
        if isinstance(action, list):
            actions.update(action)
        elif action:
            actions.add(action)
    assert "states:SendTaskSuccess" in actions
    assert "states:SendTaskFailure" in actions


def test_review_function_cannot_touch_the_review_queue(template: Template) -> None:
    """The console used to find task tokens by reading the review queue, and every read spent a
    live token's redrive budget (the sixth view of a case sent its token to the DLQ). The token is
    on the case record now, so no SQS action of any kind is granted to this function."""
    actions = _actions_of(template, "ReviewApiFunction")
    assert not {a for a in actions if a.startswith("sqs:")}, actions


def test_applicants_function_can_send_to_the_real_ingress_queue(template: Template) -> None:
    policies = template.find_resources("AWS::IAM::Policy")
    applicants_policy = next(
        body for name, body in policies.items() if "ApplicantsFunction" in name)
    statements = applicants_policy["Properties"]["PolicyDocument"]["Statement"]
    sqs_statements = [
        s for s in statements
        if "sqs:SendMessage" in (s.get("Action") if isinstance(s.get("Action"), list)
                                 else [s.get("Action")])
    ]
    assert sqs_statements, "no sqs:SendMessage grant found"


def test_cors_allows_get_post_and_the_demo_put(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {"CorsConfiguration": Match.object_like({
            "AllowMethods": ["GET", "POST", "PUT"],
        })},
    )


def _actions_of(template: Template, function_logical_prefix: str) -> set[str]:
    actions: set[str] = set()
    for name, body in template.find_resources("AWS::IAM::Policy").items():
        if function_logical_prefix not in name:
            continue
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]:
            action = stmt.get("Action")
            actions.update(action if isinstance(action, list) else [action])
    return actions


def _statement(template: Template, sid: str) -> dict:
    for body in template.find_resources("AWS::IAM::Policy").values():
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Sid") == sid:
                return stmt
    raise AssertionError(f"no statement {sid}")


def test_the_account_routes_can_only_invoke_the_account_platform(template: Template) -> None:
    assert _actions_of(template, "AccountsFunction") == {"lambda:InvokeFunction"}
    assert _statement(template, "UseAccountPlatform")["Resource"] == (
        "arn:aws:lambda:us-east-1:123456789012:function:kyc90-account-stub")


def test_the_demo_function_writes_only_the_two_controls(template: Template) -> None:
    resources = _statement(template, "WriteDemoControls")["Resource"]
    assert resources == [
        "arn:aws:ssm:us-east-1:123456789012:parameter/kyc90/demo/faults",
        "arn:aws:ssm:us-east-1:123456789012:parameter/kyc90/screening/list-overlay"]
    assert _statement(template, "PublishListUpdate")["Resource"] == (
        "arn:aws:events:us-east-1:123456789012:event-bus/kyc90-lifecycle")
    actions = _actions_of(template, "DemoFunction")
    for forbidden in ("dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:Scan",
                      "lambda:InvokeFunction", "states:StartExecution"):
        assert forbidden not in actions, forbidden


def test_the_liveness_function_reads_and_writes_by_key_only(template: Template) -> None:
    actions = _actions_of(template, "LivenessFunction")
    assert {"dynamodb:GetItem", "dynamodb:UpdateItem"} <= actions
    assert "dynamodb:Query" not in actions and "dynamodb:Scan" not in actions


def test_the_trace_view_can_read_every_phases_executions(template: Template) -> None:
    resources = _statement(template, "ReadExecutionHistoryForTrace")["Resource"]
    assert len(resources) == 3


def test_api_lambdas_ship_no_tests(template: Template) -> None:
    """A packaged conftest would mutate `sys.path` inside the runtime."""
    from kyc.api_stack import _ASSET_EXCLUDES

    assert "tests" in _ASSET_EXCLUDES
