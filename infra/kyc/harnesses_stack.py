"""Seven harnesses and their named endpoints — Phase 5.

Built as `AWS::BedrockAgentCore::Harness` L1 resources. The spec originally said to use the
`agentcore` CLI because the SDK control plane is unreachable (F-64) — true, but CloudFormation
is a third path and it works (F-74), so the whole build stays in one Python CDK app instead of
straddling a CLI.

Three postures here are inversions of P-10, and each is deliberate:

  * **Memory disabled on six of seven agents** (DIV-2). Memory is created by default with
    30-day retention (F-05, F-66). Shared recall is useful for SRE incidents and is unintended
    PII retention for passports. `Memory: {"Disabled": {}}` is the explicit opt-out.
  * **`shell` and `file_operations` excluded** (F-06). They are on by default and cost ~900
    input tokens per request. `AllowedTools` is scoped to the Gateway namespace.
  * **Skills empty.** Every capability must be a registered Gateway tool so Cedar governs it
    (R11.17). An AWS-curated skill would bypass that.

The session lifetime **is** bounded here, which corrects F-75. That fact recorded that
`AWS::BedrockAgentCore::Harness` exposes no `LifecycleConfig`, concluded from its absence among
the top-level properties. It is in fact reachable three levels down —
`Environment.AgentCoreRuntimeEnvironment.LifecycleConfiguration` — carrying both `MaxLifetime`
and `IdleRuntimeSessionTimeout` (F-83). This matters more than a schema footnote: without the
bound, a Step Functions Task dies at 15 minutes while the harness runs on and keeps billing
against a default `MaxLifetime` of 8 hours (F-14). `TimeoutSeconds` caps a single invocation,
not the session, so it was never a substitute.
"""

from __future__ import annotations

import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import custom_resources as cr
from constructs import Construct

from .config import AGENTS, JURISDICTIONS, PROJECT, REASONING_TRACE_RETENTION_DAYS

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "harnesses"))
from prompts import OUTPUT_SCHEMAS, PROMPTS  # noqa: E402
from tool_schemas import (  # noqa: E402
    OUTPUT_CONTRACTS,
    TARGET_NAMES,
    agent_tool_names,
    cedar_action,
)


def _prompt_with_contract(agent) -> str:
    """The agent's prompt plus its output-contract obligation, for agents that have one.

    R11.13 requires strict JSON through tool-use rather than prompt-only JSON, and F-89 is the
    measurement of what happens without it: across two consecutive live runs with an identical
    prompt and fixture, one agent reported `{"confidence": 0.96}` and the next said the same thing
    in a sentence, so the derived signal differed between runs.

    The wording below is deliberate on two points. It states that prose is **not read**, because an
    agent told merely to "prefer" the tool will sometimes narrate instead. And it gives an explicit
    way to say "I could not determine this" — without one, an agent that failed to screen has only
    `matched=false` available, which reads downstream as "no sanctions hits".
    """
    base = PROMPTS[agent.key]
    tool = OUTPUT_CONTRACTS.get(agent.key)
    if not tool:
        return base

    namespaced = "%s___%s" % (TARGET_NAMES[tool], tool)
    return base + (
        "\n\n## Reporting your finding — mandatory\n\n"
        f"You MUST call `{namespaced}` exactly once, as your final action, to record your "
        "finding.\n\n"
        "Text in your reply is **not read** by the decision engine. A finding that exists only "
        "as prose is a finding that was never recorded, and the case will be routed to a human "
        "as though you had not run.\n\n"
        "If you could not complete your checks, still call the tool and set `determined` to "
        "false. Do not report a clean result to mean 'I could not check' — a false clean result "
        "approves an applicant nobody screened."
    )

#: Caps a single invocation. Necessary but not sufficient — it says nothing about the session.
HARNESS_TIMEOUT_SECONDS = 900

#: R11.14 / F-14: the session bound, and the control that actually fixes failure demo 4. The
#: Step Functions Task gives up at 15 minutes; without this the harness keeps running and keeps
#: billing for the default 8 hours. Schema allows 60..28800 (F-83).
HARNESS_MAX_LIFETIME_SECONDS = 900

#: R11.15: reclaim an idle session rather than holding it open for the full lifetime.
HARNESS_IDLE_SESSION_TIMEOUT_SECONDS = 120

#: The Gateway namespace. AllowedTools pattern is ^(\*|@?[^/]+(/[^/]+)?)$ so "@kyc-gw/*" is
#: valid. Listing it explicitly excludes shell and file_operations, which the default baseline
#: would otherwise include (F-06).
GATEWAY_TOOL_NAMESPACE = "@kyc-gw/*"
CODE_INTERPRETER_TOOL = "@builtin/code_interpreter"


def _pascal_output_schema(schema: dict) -> dict:
    """The submit-tool contract, in the PascalCase shape CloudFormation wants (F-78)."""
    def conv(node: dict) -> dict:
        out: dict = {}
        for k, v in node.items():
            pk = {"type": "Type", "properties": "Properties", "required": "Required",
                  "items": "Items", "description": "Description", "enum": "Enum",
                  "minimum": "Minimum", "maximum": "Maximum",
                  "maxLength": "MaxLength"}.get(k, k)
            if k == "properties":
                out[pk] = {kk: conv(vv) for kk, vv in v.items()}
            elif k == "items":
                out[pk] = conv(v) if isinstance(v, dict) else v
            else:
                out[pk] = v
        return out

    return {
        "Name": schema["name"],
        "Description": schema["description"],
        "InputSchema": conv(schema["inputSchema"]),
    }


class HarnessesStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 harness_role_arns: dict[str, str],
                 gateway_arns: dict[str, str],
                 trace_key_arn: str,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # R18.7-R18.9 / task 6.8: two-tier retention, trace side. Decision records live under
        # Object Lock Compliance in the evidence buckets (7-year floor, `audit/writer.py`) and
        # MUST NOT hold reasoning traces — Compliance mode forbids deletion, so a trace written
        # there would make R18.9's "delete while preserving the decision record" permanently
        # unsatisfiable. Traces instead go to a per-agent CloudWatch log group (F-20), under this
        # separate trace key, with a bounded retention that R18.9 requires be enforced.
        trace_key = kms.Key.from_key_arn(self, "TraceKeyForLogs", trace_key_arn)

        self.harnesses: dict[str, cdk.CfnResource] = {}
        self.endpoints: dict[str, cdk.CfnResource] = {}
        self.trace_log_group_arns: dict[str, str] = {}

        #: What the orchestration stack invokes. **Plain harness ARNs** (F-85): the Step Functions
        #: invokeHarness integration rejects an endpoint-suffixed ARN with "Invalid harness ARN
        #: format", so the pinned endpoint is selected by the Task's `Qualifier` field instead.
        #: Endpoint pinning is preserved; it is just expressed somewhere other than the ARN.
        self.harness_arns: dict[str, str] = {}

        #: Endpoint ARNs, kept for the audit record (R18.6 records the endpoint name) and for
        #: direct data-plane calls, which do accept them.
        self.endpoint_arns: dict[str, str] = {}

        #: The model each agent is pinned to (R26.3). Exposed rather than recomputed downstream:
        #: two copies of this mapping would be two places for the audit record's model pin to
        #: drift from what actually ran.
        self.model_ids: dict[str, str] = {}

        # Every agent is pinned to the US Gateway in this deployment. The four Gateways exist so
        # jurisdiction is structural (F-04, D-01); which one an agent reaches is chosen by the
        # router at run time via the Step Functions Task, not baked in here. Task 5.5's resource
        # policies are what stop an agent reaching the wrong one.
        default_gateway = gateway_arns["us"]

        for agent in AGENTS:
            # F-76: HarnessName is ^[a-zA-Z][a-zA-Z0-9_]{0,39}$ — underscores, max 40 chars.
            # Hyphens are rejected here and required on Gateways. The two cannot be unified.
            harness_name = f"kyc90_{agent.key}"
            assert len(harness_name) <= 40, harness_name

            model_config: dict = {"ModelId": self._model_for(agent)}
            # F-62 / F-86: sampling is unpinned for ANY reasoning-enabled agent, not only `high`.
            # At `high` the service rejects the call outright if Temperature or MaxTokens are set.
            # At `medium` it accepts them and then fails worse — measured live, the Fraud agent
            # burned 8,525 output tokens against a 2048 ceiling and returned `Content: []`,
            # because reasoning draws from the same output budget as the answer (F-63). A rejected
            # call stops the build; an empty successful one is read as "no findings".
            # See `Agent.can_pin_sampling`.
            if agent.can_pin_sampling:
                model_config["Temperature"] = 0.0
                model_config["MaxTokens"] = 2048

            # The Gateway is attached, but AllowedTools is scoped to exactly what this agent
            # declares — never the `@kyc-gw/*` wildcard.
            #
            # The wildcard let every agent see all seven (now, with contracts off, still seven)
            # tools. Documents and Identity each need exactly one; given all seven they wandered
            # between them and hit `max_iterations_exceeded` without ever recording a finding.
            # Router previously had the opposite problem: Gateway attached, AllowedTools empty —
            # the model saw tools it was not permitted to call and emitted a malformed tool-use
            # sequence, which failed the whole invocation. Neither extreme works; the fix is that
            # what is advertised must equal what is permitted, which per-agent scoping guarantees.
            #
            # This is also what R14.2 asked for: an agent that cannot see a tool cannot misuse it.
            tools: list[dict] = [{
                "Type": "agentcore_gateway",
                "Name": "kyc-gw",
                "Config": {"AgentCoreGateway": {"GatewayArn": default_gateway}},
            }]
            allowed = [
                "@kyc-gw/%s" % cedar_action(tool_name)
                for tool_name in agent_tool_names(agent.tools)
            ]
            contract = OUTPUT_CONTRACTS.get(agent.key)
            if contract:
                allowed.append("@kyc-gw/%s" % cedar_action(contract))
            if not allowed:
                # Router and Notify use no tools. No Gateway is attached for them either, so
                # nothing is advertised that they are not permitted to call.
                tools = []

            if agent.key == "fraud":
                # Code Interpreter is an AgentCore built-in, not a Gateway tool, so the scoped
                # baseline would exclude it and the agent would fail to find its own tool.
                # Fraud is still denied shell and file_operations.
                tools.append({"Type": "agentcore_code_interpreter", "Name": "code-interp"})
                allowed.append(CODE_INTERPRETER_TOOL)

            props: dict = {
                "HarnessName": harness_name,
                "ExecutionRoleArn": harness_role_arns[agent.key],
                "Model": {"BedrockModelConfig": model_config},
                "SystemPrompt": [{"Text": _prompt_with_contract(agent)}],
                "Tools": tools,
                "AllowedTools": allowed,
                "MaxIterations": agent.max_iterations,
                "TimeoutSeconds": HARNESS_TIMEOUT_SECONDS,
                # R11.14 / R11.15 / F-14 / F-83. Bounds the *session*, not just one invocation.
                # No FilesystemConfigurations entry, so Managed Session Storage stays off — the
                # only way to enable it is a SessionStorage mount, and PII-handling agents must
                # not have one (R11.16, F-48).
                "Environment": {
                    "AgentCoreRuntimeEnvironment": {
                        "LifecycleConfiguration": {
                            "MaxLifetime": HARNESS_MAX_LIFETIME_SECONDS,
                            "IdleRuntimeSessionTimeout":
                                HARNESS_IDLE_SESSION_TIMEOUT_SECONDS,
                        },
                    },
                },
                # R11.17: no skills. Every capability goes through a Gateway so Cedar sees it.
                "Skills": [],
                "EnvironmentVariables": {
                    # F-20 / R19.3: per-agent log group so PII-bearing traces can be
                    # access-controlled and encrypted per agent.
                    "UNIFIED_TRACES_DESTINATION_ENABLED": "true",
                    "KYC_AGENT": agent.key,
                    "KYC_PHASE": str(agent.phase.value),
                },
                "Tags": [
                    {"Key": "Agent", "Value": agent.key},
                    {"Key": "Phase", "Value": str(agent.phase.value)},
                    {"Key": "Feature", "Value": "kyc-90-seconds"},
                    {"Key": "project", "Value": PROJECT},
                ],
            }

            # DIV-2. Customer Experience is the single exception: it never sees a document or a
            # screening result, only an outcome and a restriction set, and that containment is
            # what makes recall safe there.
            if agent.key != "notify":
                props["Memory"] = {"Disabled": {}}

            harness = cdk.CfnResource(
                self, f"Harness{agent.role_suffix}",
                type="AWS::BedrockAgentCore::Harness",
                properties=props,
            )
            harness.apply_removal_policy(RemovalPolicy.DESTROY)
            self.harnesses[agent.key] = harness

            # R18.7-R18.9 / task 6.8 (trace side of two-tier retention).
            #
            # F-20's span destination name is `<agent_id>-<endpoint_name>`, where `agent_id` is
            # `AgentRuntimeId` — a value the service assigns and that only exists once the
            # harness is created. Worse: `UNIFIED_TRACES_DESTINATION_ENABLED=true` makes AgentCore
            # auto-create that log group itself, on first invocation, **before** any CDK resource
            # can claim the name. Measured directly against the seven groups this build already
            # created: every one came back `retentionInDays: NEVER EXPIRE`, `kmsKeyId: None` —
            # unencrypted and undeleteable, the exact opposite of R18.9. A plain `logs.LogGroup`
            # construct cannot fix this; CloudFormation refuses to "create" a log group that
            # already exists (`already exists` error, not an update).
            #
            # So this is a custom resource that calls `PutRetentionPolicy` and `AssociateKmsKey`
            # directly against the name AgentCore chose, every deploy. Both APIs are idempotent —
            # re-applying the same retention/key is a no-op — so running this repeatedly is safe,
            # and it is what actually enforces the retention floor on a resource this stack does
            # not own the creation of.
            agent_runtime_id = harness.get_att(
                "Environment.AgentCoreRuntimeEnvironment.AgentRuntimeId").to_string()
            trace_log_group_name = cdk.Fn.join(
                "", ["/aws/bedrock-agentcore/runtimes/", agent_runtime_id, "-live"])
            # THREE_MONTHS is CDK's closest fixed enum to REASONING_TRACE_RETENTION_DAYS (90).
            # CloudWatch Logs retention is a fixed set of values, not an arbitrary integer, and
            # the SDK call below uses the same 90 so the two never drift from each other.
            assert REASONING_TRACE_RETENTION_DAYS == 90, (
                "PutRetentionPolicy call below is hardcoded to 90; update it if this changes")
            trace_retention = cr.AwsCustomResource(
                self, f"TraceRetention{agent.role_suffix}",
                on_create=cr.AwsSdkCall(
                    service="CloudWatchLogs",
                    action="PutRetentionPolicy",
                    parameters={
                        "logGroupName": trace_log_group_name,
                        "retentionInDays": REASONING_TRACE_RETENTION_DAYS,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"trace-retention-{agent.key}"),
                ),
                on_update=cr.AwsSdkCall(
                    service="CloudWatchLogs",
                    action="PutRetentionPolicy",
                    parameters={
                        "logGroupName": trace_log_group_name,
                        "retentionInDays": REASONING_TRACE_RETENTION_DAYS,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"trace-retention-{agent.key}"),
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements([
                    iam.PolicyStatement(
                        actions=["logs:PutRetentionPolicy"],
                        resources=["*"],
                    ),
                ]),
                removal_policy=RemovalPolicy.RETAIN,
            )
            trace_retention.node.add_dependency(harness)

            trace_encryption = cr.AwsCustomResource(
                self, f"TraceEncryption{agent.role_suffix}",
                on_create=cr.AwsSdkCall(
                    service="CloudWatchLogs",
                    action="AssociateKmsKey",
                    parameters={
                        "logGroupName": trace_log_group_name,
                        "kmsKeyId": trace_key.key_arn,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"trace-encryption-{agent.key}"),
                ),
                on_update=cr.AwsSdkCall(
                    service="CloudWatchLogs",
                    action="AssociateKmsKey",
                    parameters={
                        "logGroupName": trace_log_group_name,
                        "kmsKeyId": trace_key.key_arn,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"trace-encryption-{agent.key}"),
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements([
                    iam.PolicyStatement(
                        actions=["logs:AssociateKmsKey"],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        # AssociateKmsKey needs the key policy to allow CloudWatch Logs to use
                        # it; the key policy is set separately (see foundation_stack), this is
                        # only the caller-side grant for the custom resource's own role.
                        actions=["kms:DescribeKey"],
                        resources=[trace_key.key_arn],
                    ),
                ]),
                removal_policy=RemovalPolicy.RETAIN,
            )
            trace_encryption.node.add_dependency(harness)
            # `trace_log_group_name` is a CDK token (an Fn::Join), not a plain string — string
            # interpolation would embed its repr rather than its resolved value. Fn.join builds
            # the ARN the same token-safe way the name itself was built.
            self.trace_log_group_arns[agent.key] = cdk.Fn.join(
                "", [f"arn:aws:logs:{self.region}:{self.account}:log-group:",
                     trace_log_group_name])

            # Task 5.4: a named endpoint pinned to a version, so a bad prompt can be rolled
            # back mid-demo without a redeploy. R18.6 requires the endpoint name on the
            # decision record.
            endpoint = cdk.CfnResource(
                self, f"Endpoint{agent.role_suffix}",
                type="AWS::BedrockAgentCore::HarnessEndpoint",
                properties={
                    "HarnessId": harness.get_att("HarnessId").to_string(),
                    # F-76: EndpointName is ^[a-zA-Z][a-zA-Z0-9_]{0,47}$.
                    "EndpointName": "live",
                    "Description": f"Pinned endpoint for {agent.name}.",
                    # Without this, CloudFormation leaves LiveVersion at whatever it was on
                    # creation and never advances it — an update to the harness (new model, new
                    # prompt, new tools) creates a new version that the `live` endpoint never
                    # points at. Fraud ran this way for several deploys: its harness resource
                    # showed the current model while every invocation through the endpoint used
                    # the original Nova 2 Lite version and its IAM grant, producing
                    # AccessDeniedException on a model the harness config no longer named.
                    #
                    # `TargetVersion` is the writable field (the schema also exposes read-only
                    # `LiveVersion`; setting that is rejected with "extraneous key"). Pinning it
                    # to the harness's current `Version` is what task 5.4's rollback story
                    # actually needs: redeploying the harness moves the endpoint forward by
                    # default, and a deliberate rollback is a separate, explicit action rather
                    # than something discovered by reading a cryptic model-access error days
                    # later.
                    "TargetVersion": harness.get_att("Version").to_string(),
                },
            )
            endpoint.add_dependency(harness)
            endpoint.apply_removal_policy(RemovalPolicy.DESTROY)
            self.endpoints[agent.key] = endpoint
            self.harness_arns[agent.key] = harness.get_att("Arn").to_string()
            self.endpoint_arns[agent.key] = endpoint.get_att("Arn").to_string()
            self.model_ids[agent.key] = self._model_for(agent)

            cdk.CfnOutput(self, f"Harness{agent.role_suffix}Arn",
                          value=harness.get_att("Arn").to_string())
            cdk.CfnOutput(self, f"Endpoint{agent.role_suffix}Arn",
                          value=endpoint.get_att("Arn").to_string())

        # The submit-tool contracts are emitted so the state machine and the evaluators use the
        # same schema the harness was built against, rather than a copy that drifts.
        cdk.CfnOutput(
            self, "OutputContractNames",
            value=",".join(sorted(s["name"] for s in OUTPUT_SCHEMAS.values())),
            description="submit_* tool names enforcing each agent's output contract (R11.13)")

    @staticmethod
    def _model_for(agent) -> str:
        """Model per agent, from the jurisdiction table.

        US is the deployment jurisdiction, so the `us.` geo profiles apply here. The AU
        divergence — no Nova 2 Lite at all, because no APAC geo exists for it (F-50, F-69) —
        lives in `config.py` and is asserted by the infra tests.
        """
        us = next(j for j in JURISDICTIONS if j.code == "us")
        if agent.reasoning_effort:
            return us.reasoning_model or us.workhorse_model
        # F-90: the fast model cannot be trusted with a tool call. Router ran on Nova Micro and
        # failed every invocation with "Model produced invalid sequence as part of ToolUse", with a
        # correctly scoped single tool and a live target — while four agents on Nova Lite made tool
        # calls successfully in the same runs. Since R11.13 requires structured output to travel as
        # tool-use, and every agent now has an output contract, no agent can use the fast model.
        #
        # This is a real constraint on the cost story rather than a detail: the cheapest model is
        # unusable for any agent that must report a finding, which is all of them.
        if agent.key in OUTPUT_CONTRACTS:
            return us.workhorse_model
        if agent.key in ("router", "prescreen", "notify"):
            return us.fast_model
        return us.workhorse_model
