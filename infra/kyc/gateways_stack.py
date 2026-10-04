"""Gateways, targets, rate limits and policy engines — Phase 3 and Phase 4.

Four Gateways, one per jurisdiction. **This is structural, not stylistic.** SigV4 carries no
applicant identity into a tool call (F-04), so jurisdiction cannot be expressed as a Cedar
`principal` and has to be expressed as a separate Gateway. D-01 records the alternative that was
rejected — one Gateway with context-conditioned Cedar — and why it cannot work.

Built with L1 `CfnResource` because `aws-cdk-lib` ships no L2 constructs for these types yet.
The CloudFormation types exist (F-74), which is what makes it possible to keep harnesses and
Gateways in the same Python CDK app rather than splitting the build across a CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import RemovalPolicy, Stack, aws_kms as kms
from constructs import Construct

from .config import JURISDICTIONS, PROJECT

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from tool_schemas import SCHEMAS, TARGET_NAMES, cedar_action  # noqa: E402


def _pascal_tool(schema: dict) -> dict:
    """Convert an MCP tool definition to the PascalCase shape CloudFormation requires.

    `AWS::BedrockAgentCore::GatewayTarget` requires `Name`, `Description` and `InputSchema`;
    the MCP wire format and the Lambda both use camelCase. Converting is better than keeping
    two copies of seven tool definitions in step by hand.
    """
    return {
        "Name": schema["name"],
        "Description": schema["description"],
        "InputSchema": _pascal_schema(schema["inputSchema"]),
    }


def _limit(target_name: str, rate_per_minute: int) -> dict:
    """One rate-limit entry.

    `Dimensions` is a map of dimension name to value, not a list, and `Requests` is a
    single-element array of `{Rate, Period}` rather than an object. Both differ from the API
    shape, and both fail with schema errors that name the key but not the fix.
    """
    return {
        "Dimensions": {"targetName": target_name},
        "Requests": [{"Rate": rate_per_minute, "Period": "minute"}],
    }


def _pascal_schema(node: dict) -> dict:
    """JSON Schema keywords also become PascalCase in this resource type."""
    out: dict = {}
    for key, value in node.items():
        pascal = {"type": "Type", "properties": "Properties", "required": "Required",
                  "items": "Items", "description": "Description"}.get(key, key)
        if key == "properties":
            out[pascal] = {k: _pascal_schema(v) for k, v in value.items()}
        elif key == "items":
            out[pascal] = _pascal_schema(value)
        else:
            out[pascal] = value
    return out


#: The two modes a Gateway can attach its policy engine in. Deliberately a closed set.
POLICY_ENFORCEMENT_MODES = ("LOG_ONLY", "ENFORCE")


class GatewaysStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 gateway_role_arn: str,
                 tools_lambda_arn: str,
                 policy_enforcement: str = "LOG_ONLY",
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # R14.6. LOG_ONLY until the decisions it logs have been checked against real traffic
        # (`scripts/check_policy_decisions.py`), then ENFORCE via `-c policyEnforcement=ENFORCE`.
        # Switching blind is the one way this control fails *open* in practice: if a permit's
        # principal is spelled wrong, ENFORCE denies every tool call, every case degrades to
        # review, and the talk's clean case never opens an account.
        if policy_enforcement not in POLICY_ENFORCEMENT_MODES:
            raise ValueError(f"policy_enforcement must be one of {POLICY_ENFORCEMENT_MODES}, "
                             f"got {policy_enforcement!r}")
        self.policy_enforcement = policy_enforcement

        self.gateways: dict[str, cdk.CfnResource] = {}
        self.policy_engines: dict[str, cdk.CfnResource] = {}
        self.action_names: dict[str, str] = {
            tool: cedar_action(tool) for tool in TARGET_NAMES}

        for j in JURISDICTIONS:
            code = j.code.upper()

            # ---------------------------------------------------------- policy engine (4.1)
            # Its own CMK per jurisdiction so a policy engine cannot read another market's
            # material. LOG_ONLY at attach time — the engine defaults to deny (F-18), so going
            # straight to ENFORCE means debugging denials blind.
            engine = cdk.CfnResource(
                self, f"PolicyEngine{code}",
                type="AWS::BedrockAgentCore::PolicyEngine",
                properties={
                    # F-76: PolicyEngine.Name is ^[A-Za-z][A-Za-z0-9_]*$ — underscores only.
                    # Gateway.Name is the opposite: hyphens only, underscores rejected. The two
                    # conventions cannot be unified, so each is spelled to its own schema and
                    # the difference is deliberate rather than an inconsistency.
                    "Name": f"kyc90_policy_{j.code}",
                    "Description": (
                        f"Cedar policy engine for {j.name}. Attached LOG_ONLY until its logged "
                        f"decisions are checked against real traffic, then ENFORCE (R14.6)."),
                },
            )
            engine.apply_removal_policy(RemovalPolicy.DESTROY)
            self.policy_engines[j.code] = engine

            # ---------------------------------------------------------- gateway (3.1)
            gateway = cdk.CfnResource(
                self, f"Gateway{code}",
                type="AWS::BedrockAgentCore::Gateway",
                properties={
                    "Name": f"KycGateway-{code}",
                    "Description": (
                        f"Tool gateway for {j.name}. Separate Gateway per jurisdiction "
                        f"because SigV4 carries no applicant identity (F-04, D-01)."),
                    "RoleArn": gateway_role_arn,
                    "ProtocolType": "MCP",
                    "ProtocolConfiguration": {
                        "Mcp": {
                            # SEMANTIC tool selection, per task 3.1.
                            "SearchType": "SEMANTIC",
                            "Instructions": (
                                f"KYC tools for {j.name}. Screening tools fail closed: an "
                                f"UNAVAILABLE status means the source could not be checked "
                                f"and must never be treated as 'no match'."),
                        }
                    },
                    # AWS_IAM, not NONE. The docs use NONE for tutorials; it would leave the
                    # Gateway open to anything that can reach the endpoint.
                    "AuthorizerType": "AWS_IAM",
                    "PolicyEngineConfiguration": {
                        "Arn": engine.get_att("PolicyEngineArn").to_string(),
                        "Mode": policy_enforcement,
                    },
                },
            )
            gateway.add_dependency(engine)
            gateway.apply_removal_policy(RemovalPolicy.DESTROY)
            self.gateways[j.code] = gateway

            # ---------------------------------------------------------- targets (3.2)
            # All six tools on every Gateway, all pointing at the one Lambda. Cedar is what
            # decides which agent may call which tool (R14.2); the Gateway just makes them
            # addressable.
            for tool, target_name in TARGET_NAMES.items():
                target = cdk.CfnResource(
                    self, f"Target{code}{target_name}",
                    type="AWS::BedrockAgentCore::GatewayTarget",
                    properties={
                        "GatewayIdentifier": gateway.get_att(
                            "GatewayIdentifier").to_string(),
                        "Name": target_name,
                        "Description": SCHEMAS[tool]["description"][:200],
                        "TargetConfiguration": {
                            "Mcp": {
                                "Lambda": {
                                    "LambdaArn": tools_lambda_arn,
                                    "ToolSchema": {
                                        # CloudFormation wants PascalCase here — Name,
                                        # Description, InputSchema — while the MCP wire format
                                        # and the Lambda use camelCase. Same data, two
                                        # spellings, so it is converted rather than duplicated.
                                        "InlinePayload": [_pascal_tool(SCHEMAS[tool])],
                                    },
                                }
                            }
                        },
                        "CredentialProviderConfigurations": [
                            {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
                        ],
                    },
                )
                target.add_dependency(gateway)
                target.apply_removal_policy(RemovalPolicy.DESTROY)

            # ---------------------------------------------------------- rate limits (3.3)
            # Rate limits FAIL OPEN on transient errors (F-30). They are a cost and stability
            # control and must never be described as a security control — Cedar is the security
            # control, and it fails closed.
            rate_limit = cdk.CfnResource(
                self, f"RateLimit{code}",
                type="AWS::BedrockAgentCore::GatewayRateLimit",
                properties={
                    "GatewayIdentifier": gateway.get_att(
                        "GatewayIdentifier").to_string(),
                    "Description": (
                        "Per-tool ceilings plus a wildcard default. Fails open on transient "
                        "errors (F-30) — cost and stability, never security."),
                    # The dimension key is `targetName` — the schema pattern accepts only
                    # targetName, toolName, qualifiedModelId, or a $.context.* expression.
                    # "TARGET_NAME" fails a pattern constraint with no hint as to why.
                    "DimensionKeys": ["targetName"],
                    "Entries": [
                        # Phase 1 tools sit on the blocking path; generous but bounded. Screening
                        # also serves Phase 3's list-update rescreens, which fan out across the
                        # customer base, so it shares the Phase 1 ceiling.
                        _limit("SanctionsScreen", 600),
                        _limit("DocumentStore", 600),
                        _limit("FaceCompare", 300),
                        _limit("VelocityStore", 600),
                        # R8.4: Phase 2 batch work gets a lower ceiling so a re-KYC sweep
                        # cannot starve Phase 1 of tool capacity.
                        _limit("PepList", 120),
                        _limit("AdverseMedia", 60),
                        # Explicit wildcard default. Without it an unlisted target is
                        # unbounded, which is the opposite of what a default should mean.
                        _limit("*", 60),
                    ],
                },
            )
            rate_limit.add_dependency(gateway)
            rate_limit.apply_removal_policy(RemovalPolicy.DESTROY)

            cdk.CfnOutput(self, f"Gateway{code}Arn",
                          value=gateway.get_att("GatewayArn").to_string(),
                          description=f"Cedar policies in stage 2 need this ARN ({code})")
            cdk.CfnOutput(self, f"Gateway{code}Url",
                          value=gateway.get_att("GatewayUrl").to_string())
            cdk.CfnOutput(self, f"PolicyEngine{code}Arn",
                          value=engine.get_att("PolicyEngineArn").to_string())

        # The Cedar action names are an output because Phase 8 depends on them and F-35's three
        # underscores are the single easiest thing in this build to get wrong.
        cdk.CfnOutput(self, "CedarActionNames",
                      value=json.dumps(self.action_names, sort_keys=True),
                      description="TargetName___tool_name — THREE underscores (F-35)")
