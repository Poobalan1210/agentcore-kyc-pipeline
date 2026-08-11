"""Cognito auth stack — task 14.1, R32.2, R33.7, R34.6, F-42."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from kyc.auth_stack import AuthStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = AuthStack(
        app, "TestAuth",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return Template.from_stack(stack)


def test_user_pool_exists_with_email_sign_in(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"UsernameAttributes": ["email"]},
    )


def test_two_groups_exist_applicants_and_reviewers(template: Template) -> None:
    """R32.2/R34.6 — the two roles the API authorizer scopes on."""
    groups = template.find_resources("AWS::Cognito::UserPoolGroup")
    names = {body["Properties"]["GroupName"] for body in groups.values()}
    assert names == {"applicants", "reviewers"}


def test_user_pool_client_has_no_secret(template: Template) -> None:
    """A public SPA client — a client secret shipped to a browser is not a secret."""
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {"GenerateSecret": False},
    )


def test_identity_pool_disallows_unauthenticated_access(template: Template) -> None:
    """No guest path is needed — an applicant already authenticates via the user pool before
    any liveness session (R32.2). Enabling unauthenticated identities would let an anonymous
    browser mint AWS credentials against this account for no requirement this build has."""
    template.has_resource_properties(
        "AWS::Cognito::IdentityPool",
        {"AllowUnauthenticatedIdentities": False},
    )


def test_authenticated_role_grants_only_start_liveness_session(template: Template) -> None:
    """R33.7 — no AWS credential broader than a scoped identity-pool credential reaches the
    browser. The one permission FaceLivenessDetector needs, and nothing else."""
    policies = template.find_resources("AWS::IAM::Policy")
    liveness_policies = [
        body for body in policies.values()
        if any(
            s.get("Sid") == "StartLivenessSessionOnly"
            for s in body["Properties"]["PolicyDocument"]["Statement"]
        )
    ]
    assert len(liveness_policies) == 1
    statement = liveness_policies[0]["Properties"]["PolicyDocument"]["Statement"][0]
    assert statement["Action"] == "rekognition:StartFaceLivenessSession"
    assert statement["Effect"] == "Allow"


def test_authenticated_role_has_exactly_one_statement(template: Template) -> None:
    """A second statement here would be a second AWS credential class the browser holds,
    exactly what R33.7 forbids."""
    policies = template.find_resources("AWS::IAM::Policy")
    for body in policies.values():
        statements = body["Properties"]["PolicyDocument"]["Statement"]
        if any(s.get("Sid") == "StartLivenessSessionOnly" for s in statements):
            assert len(statements) == 1


def test_identity_pool_role_attachment_targets_the_authenticated_role(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::IdentityPoolRoleAttachment",
        {"Roles": Match.object_like({"authenticated": Match.any_value()})},
    )


def test_outputs_expose_ids_the_frontend_and_api_stacks_need(template: Template) -> None:
    outputs = template.find_outputs("*")
    assert {"UserPoolId", "UserPoolClientId", "IdentityPoolId", "UserPoolArn"} <= set(outputs)
