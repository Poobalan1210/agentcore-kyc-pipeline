"""Cedar policies stack — task 8.1, R14.6, R14.8, F-17.

Uses a small fixture manifest rather than the real generated one, so this test suite does not
depend on `policies/generate.py` having been run against a live account first.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from kyc.policies_stack import PoliciesStack

MANIFEST = {
    "us": [
        {
            "name": "AuthorizeDocumentsS3GetDocument",
            "cedar": (
                'permit(\n'
                '  principal == AgentCore::IamEntity::"arn:aws:sts::123456789012:'
                'assumed-role/kyc90-harness-documents",\n'
                '  action == AgentCore::Action::"DocumentStore___s3_get_document",\n'
                '  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:'
                '123456789012:gateway/kycgateway-us-abc"\n'
                ');'
            ),
            "kind": "authorization",
            "gatewayArn": (
                "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/kycgateway-us-abc"),
            "policyEngineArn": (
                "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                "policy-engine/kyc90_policy_us-xyz"),
        },
        {
            "name": "ForbidPromptInjection",
            "cedar": (
                'forbid(\n  principal,\n  action,\n  resource == AgentCore::Gateway::'
                '"arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/kycgateway-us-abc"\n)'
                '\nwhen guardrails {\n  BedrockGuardrails::PromptAttack(["PROMPT_INJECTION"], '
                '[context.input])\n    ["PROMPT_INJECTION"]\n    .confidenceScore\n    '
                '.greaterThan(decimal("0.4"))\n};'
            ),
            "kind": "prompt_injection",
            "gatewayArn": (
                "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/kycgateway-us-abc"),
            "policyEngineArn": (
                "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                "policy-engine/kyc90_policy_us-xyz"),
        },
    ],
}


@pytest.fixture(scope="module")
def manifest_path(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("policies_generated")
    path = d / "manifest.json"
    path.write_text(json.dumps(MANIFEST))
    return path


@pytest.fixture(scope="module")
def template(manifest_path: Path) -> Template:
    app = cdk.App()
    stack = PoliciesStack(
        app, "TestPolicies",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
        manifest_path=manifest_path,
    )
    return Template.from_stack(stack)


def test_one_resource_per_manifest_statement(template: Template) -> None:
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    assert len(resources) == 2


def test_every_statement_starts_log_only(template: Template) -> None:
    """R14.6 — every statement starts LOG_ONLY, promoted to ENFORCE only after thresholds are
    tuned, no later than 2026-10-24."""
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    for body in resources.values():
        assert body["Properties"]["EnforcementMode"] == "LOG_ONLY"


def test_the_policy_engine_id_is_the_last_arn_segment(template: Template) -> None:
    """`AWS::BedrockAgentCore::Policy.PolicyEngineId` expects the bare ID, not the ARN — passing
    the full ARN would fail CloudFormation validation on the pattern constraint."""
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    ids = {body["Properties"]["PolicyEngineId"] for body in resources.values()}
    assert ids == {"kyc90_policy_us-xyz"}
    for engine_id in ids:
        assert "arn:aws" not in engine_id
        assert "/" not in engine_id


def test_the_cedar_statement_is_carried_through_unmodified(template: Template) -> None:
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    statements = set()
    for body in resources.values():
        definition = body["Properties"]["Definition"]
        for key in ("Cedar", "Policy"):
            if key in definition:
                statements.add(definition[key]["Statement"])
    assert any(s.startswith("permit(") for s in statements)
    assert any(s.startswith("forbid(") for s in statements)


def test_authorization_statements_use_the_cedar_field(template: Template) -> None:
    """Plain permit/forbid with a standard `when` (or none) goes under `Definition.Cedar`."""
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    auth = resources["PolicyUSAuthorizeDocumentsS3GetDocument"]
    assert "Cedar" in auth["Properties"]["Definition"]
    assert "Policy" not in auth["Properties"]["Definition"]


def test_guardrail_statements_use_the_policy_field(template: Template) -> None:
    """Confirmed live: a `when guardrails { ... }` statement submitted under `Definition.Cedar`
    fails the parser with `unexpected token 'guardrails'` — it must go under `Definition.Policy`
    instead, even though both fields carry the identical Cedar/Dogwood text."""
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    guardrail = resources["PolicyUSForbidPromptInjection"]
    assert "Policy" in guardrail["Properties"]["Definition"]
    assert "Cedar" not in guardrail["Properties"]["Definition"]


def test_policies_within_one_engine_are_created_sequentially(template: Template) -> None:
    """Confirmed live: CloudFormation's default parallel resource creation against the same
    policy engine produced `CreatePolicy ... already exists` on names never created before in
    that run — a service-side concurrency fault, not stale-name reuse. Each policy after the
    first in a jurisdiction must explicitly DependsOn the previous one so CloudFormation issues
    one CreatePolicy call at a time per engine."""
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    us_names = sorted(name for name in resources if name.startswith("PolicyUS"))
    assert len(us_names) == 2
    second = resources[us_names[1]]
    assert us_names[0] in second.get("DependsOn", [])


def test_missing_manifest_raises_rather_than_synthesising_an_empty_stack(tmp_path: Path) -> None:
    """A stage-2 synth attempted before stage 1 has produced the manifest must fail loudly
    (F-17) rather than silently producing a Cedar-free stack that looks successful."""
    app = cdk.App()
    with pytest.raises(FileNotFoundError):
        PoliciesStack(
            app, "TestPoliciesMissing",
            env=cdk.Environment(account="123456789012", region="us-east-1"),
            manifest_path=tmp_path / "does_not_exist.json",
        )


def test_authorization_statements_fail_on_any_findings(template: Template) -> None:
    """A permit statement the Cedar analyser flags — e.g. an always-allow — must fail creation
    rather than deploy quietly with a warning nobody reads."""
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    auth = resources["PolicyUSAuthorizeDocumentsS3GetDocument"]
    assert auth["Properties"]["ValidationMode"] == "FAIL_ON_ANY_FINDINGS"


def test_guardrail_statements_ignore_findings(template: Template) -> None:
    """Confirmed live: `FAIL_ON_ANY_FINDINGS` rejects every guardrail statement outright with
    CREATE_FAILED / "Overly Restrictive" — Cedar's analyser reads the guardrail's deliberately
    unconditioned `forbid(principal, action, resource == Gateway)` scope as a defect, when it is
    exactly the intended cross-cutting shape. `IGNORE_ALL_FINDINGS` is required for these, not a
    laxer default — schema checks (is this valid Cedar/Dogwood referencing real actions) still
    run either way."""
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    guardrail = resources["PolicyUSForbidPromptInjection"]
    assert guardrail["Properties"]["ValidationMode"] == "IGNORE_ALL_FINDINGS"
