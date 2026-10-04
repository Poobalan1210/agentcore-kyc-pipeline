"""Cognito auth — task 14.1, R32.2, R33.7, R34.6, F-42.

Two pools, for two different jobs that must not be conflated:

* **User pool** — who the applicant/reviewer *is*. Two groups, `applicants` and `reviewers`,
  because the Public API's `/review/*` routes are gated to reviewers only (R34.6) and a JWT
  authorizer needs a claim to gate on.
* **Identity pool** — what the *browser* is allowed to call directly. `FaceLivenessDetector`
  streams video straight to the Rekognition streaming service (design §18, F-39) — that call
  never goes through the Public API, so it needs its own scoped credentials. Amplify will not
  even render the component without a configured Auth backend (F-42), so this stack is a hard
  prerequisite for task 14.5, not an optional hardening step.

The identity pool's authenticated role is granted exactly one permission —
`rekognition:StartFaceLivenessSession` — and nothing else, because R33.7 requires that no AWS
credential broader than a scoped identity-pool credential ever reaches the browser. `Resource:
"*"` on that action is not a wildcard exception: `StartFaceLivenessSession` accepts no resource
ARN to scope to (it authorizes by SessionId, not by ARN), so `*` is the only form the action
takes, per the Amplify UI docs' own setup snippet.

Unauthenticated access is deliberately **not** enabled. The applicant already holds a Cognito
user-pool identity (R32.2 requires authenticating before any liveness session), so there is no
guest path that needs it, and enabling it would let an anonymous browser mint identity-pool
credentials against this account for no requirement this build has.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from constructs import Construct

from .config import PROJECT


class AuthStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------ user pool
        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name=f"{PROJECT}-users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            # R32.2 — an applicant authenticates before any liveness session; a password alone
            # is the minimum bar for this POC (R25.7's fail-closed screening is the real control,
            # not login strength) but MFA is not mandated here for the demo's synthetic traffic.
            password_policy=cognito.PasswordPolicy(
                min_length=8, require_lowercase=True, require_uppercase=True,
                require_digits=True, require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # R34.6 — reviewer identity comes from Cognito and is recorded on every decision;
        # `review_handler.py` already reads `claims.sub`/`cognito:username` from the JWT
        # authorizer context, so group membership is what the API authorizer scopes on.
        self.applicants_group = cognito.CfnUserPoolGroup(
            self, "ApplicantsGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="applicants",
            description="Applicants — may create/resume their own case, may not review.",
        )
        self.reviewers_group = cognito.CfnUserPoolGroup(
            self, "ReviewersGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="reviewers",
            description=(
                "Compliance reviewers — may act on the review queue (R6.3, R34.6). Membership "
                "is provisioned out-of-band; self-sign-up never joins this group."),
        )
        # The people running the live demo: the Demo Panel's fault switches and the sanctions list
        # update that drives Act 3. A presenter can freeze any open account by publishing a
        # matching listing, so this group exists only in a demo deployment and is granted by hand.
        self.presenters_group = cognito.CfnUserPoolGroup(
            self, "PresentersGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="presenters",
            description=(
                "Demo presenters — may flip demo faults and publish a synthetic list update. "
                "Granted by hand for a demo only; never by self-sign-up."),
        )

        self.user_pool_client = cognito.UserPoolClient(
            self, "UserPoolClient",
            user_pool=self.user_pool,
            # No client secret: this is a browser SPA (public client), and a "secret" shipped to
            # a browser is not a secret (R33.7's spirit applied to the auth layer, not just the
            # liveness credentials).
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL,
                       cognito.OAuthScope.PROFILE],
            ),
        )

        # ------------------------------------------------------------ identity pool (F-42)
        # Federates the user pool so a signed-in applicant/reviewer's JWT exchanges for scoped
        # AWS credentials — the only path the browser ever holds AWS credentials on, and the
        # only path FaceLivenessDetector uses to reach the Rekognition streaming service.
        self.identity_pool = cognito.CfnIdentityPool(
            self, "IdentityPool",
            identity_pool_name=f"{PROJECT}_identity",
            allow_unauthenticated_identities=False,
            cognito_identity_providers=[
                cognito.CfnIdentityPool.CognitoIdentityProviderProperty(
                    client_id=self.user_pool_client.user_pool_client_id,
                    provider_name=self.user_pool.user_pool_provider_name,
                )
            ],
        )

        authenticated_role = iam.Role(
            self, "IdentityPoolAuthenticatedRole",
            role_name=f"{PROJECT}-identity-authenticated",
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {
                        "cognito-identity.amazonaws.com:aud": self.identity_pool.ref},
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "authenticated"},
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            description=(
                "Scoped credentials for the browser (R33.7). Exactly one permission: "
                "rekognition:StartFaceLivenessSession, the one call FaceLivenessDetector makes "
                "directly against Rekognition, bypassing the Public API by design (F-39)."
            ),
        )
        authenticated_role.add_to_policy(iam.PolicyStatement(
            sid="StartLivenessSessionOnly",
            actions=["rekognition:StartFaceLivenessSession"],
            # No resource ARN to scope to — StartFaceLivenessSession authorizes by the SessionId
            # already embedded in the request, not by an IAM resource, so "*" is not a loosened
            # grant here; it is the only form the action's resource element takes (per the
            # Amplify UI FaceLivenessDetector setup docs).
            resources=["*"],
        ))

        cognito.CfnIdentityPoolRoleAttachment(
            self, "IdentityPoolRoleAttachment",
            identity_pool_id=self.identity_pool.ref,
            roles={"authenticated": authenticated_role.role_arn},
        )

        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId",
                      value=self.user_pool_client.user_pool_client_id)
        cdk.CfnOutput(self, "IdentityPoolId", value=self.identity_pool.ref)
        cdk.CfnOutput(self, "UserPoolArn", value=self.user_pool.user_pool_arn)
