#!/usr/bin/env python3
"""CDK entry point — KYC in 90 Seconds.

Two-stage deploy (F-17). Cedar policies must reference concrete Gateway ARNs and Cedar
forbids wildcard resources, so the Gateways must exist before their policies can be written.
`make deploy` runs both stages and prints why there are two, rather than pretending a single
`cdk deploy` works.

Stage 1: Foundation -> Tools -> Gateways -> PolicyEngines -> Harnesses -> Orchestration
         -> Ingress -> Observability -> Auth -> Api -> Frontend
Stage 2: Policies   (needs Gateway ARNs captured from stage 1)
"""

import os
from pathlib import Path

import aws_cdk as cdk

from kyc.api_stack import ApiStack
from kyc.auth_stack import AuthStack
from kyc.config import JURISDICTIONS, PRIMARY_REGION, PROJECT
from kyc.foundation_stack import FoundationStack
from kyc.gateways_stack import GatewaysStack
from kyc.harnesses_stack import HarnessesStack
from kyc.ingress_stack import IngressStack
from kyc.lifecycle_stack import LifecycleStack
from kyc.orchestration_stack import OrchestrationStack
from kyc.policies_stack import PoliciesStack
from kyc.tools_stack import ToolsStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", PRIMARY_REGION),
)

TAGS = {
    # R24: every resource is tagged so Cost Explorer can split spend by phase, which is what
    # gate G16 measures. Untagged resources make the economics slide an estimate.
    "project": PROJECT,
    "spec": "kyc-90-seconds",
    "managed-by": "cdk",
}

foundation = FoundationStack(
    app, "KycFoundation",
    env=env,
    description=(
        "KYC in 90 Seconds foundation: per-jurisdiction KMS, S3 evidence buckets with "
        "Object Lock at creation, DynamoDB case store, ten IAM roles. IRREVERSIBLE."
    ),
)

tools = ToolsStack(
    app, "KycTools",
    env=env,
    evidence_bucket_names={
        j.code: foundation.evidence_buckets[j.code].bucket_name for j in JURISDICTIONS},
    evidence_key_arns={
        j.code: foundation.jurisdiction_keys[j.code].key_arn for j in JURISDICTIONS},
    trace_key_arn=foundation.trace_key.key_arn,
    biometric_bucket_name=foundation.biometric_bucket.bucket_name,
    case_table_name=foundation.case_table.table_name,
    case_table_arn=foundation.case_table.table_arn,
    case_key_arn=foundation.case_key.key_arn,
    # 0 for now. Task 12.1 measures T2 first; buying warm capacity before knowing whether it
    # helps would make the cost model an estimate, which gate G16 forbids.
    provisioned_concurrency=int(app.node.try_get_context("provisionedConcurrency") or 0),
    description="One Lambda / seven tools, account platform stub, velocity store.",
)
tools.add_dependency(foundation)

gateways = GatewaysStack(
    app, "KycGateways",
    env=env,
    gateway_role_arn=foundation.gateway_role.role_arn,
    # The alias, not $LATEST, so a Gateway target cannot silently start calling unreleased code.
    tools_lambda_arn=tools.tools_alias.function_arn,
    description=("Four Gateways (one per jurisdiction, forced by F-04), seven targets each, "
                 "four Cedar policy engines attached LOG_ONLY."),
)
gateways.add_dependency(tools)

harnesses = HarnessesStack(
    app, "KycHarnesses",
    env=env,
    harness_role_arns={a: r.role_arn
                       for a, r in foundation.harness_roles.items()},
    gateway_arns={j.code: gateways.gateways[j.code].get_att("GatewayArn").to_string()
                  for j in JURISDICTIONS},
    trace_key_arn=foundation.trace_key.key_arn,
    description=("Seven harnesses plus pinned endpoints. Memory disabled on six of seven "
                 "(DIV-2), shell and file_operations excluded, skills empty."),
)
harnesses.add_dependency(gateways)

orchestration = OrchestrationStack(
    app, "KycOrchestration",
    env=env,
    case_table_name=foundation.case_table.table_name,
    case_table_arn=foundation.case_table.table_arn,
    case_key_arn=foundation.case_key.key_arn,
    trace_key_arn=foundation.trace_key.key_arn,
    # Decision records go to the US evidence bucket: it is Object Lock COMPLIANCE from creation
    # (F-29, and it cannot be retrofitted), which is what R18.3 requires. Reasoning traces must
    # never land here — Compliance mode forbids deletion, so they would make R18.9 permanently
    # unmeetable rather than merely awkward.
    audit_bucket_name=foundation.evidence_buckets["us"].bucket_name,
    audit_bucket_arn=foundation.evidence_buckets["us"].bucket_arn,
    audit_key_arn=foundation.jurisdiction_keys["us"].key_arn,
    sfn_role_arn=foundation.sfn_role.role_arn,
    harness_arns=harnesses.harness_arns,
    harness_endpoint_arns=harnesses.endpoint_arns,
    trace_log_group_names=harnesses.trace_log_group_names,
    model_ids=harnesses.model_ids,
    description=(
        "Phase 1 and Phase 2 state machines (JSONata, F-71), the pure decision engine, the "
        "audit writer and the human-review queue."
    ),
)
orchestration.add_dependency(harnesses)

ingress = IngressStack(
    app, "KycIngress",
    env=env,
    pipe_role_arn=foundation.pipe_role.role_arn,
    phase1_state_machine_arn=orchestration.phase1.attr_arn,
    description=(
        "SQS FIFO (DIV-3) -> EventBridge Pipe -> Phase 1 state machine, no enrichment, no "
        "filter (design §10)."
    ),
)
ingress.add_dependency(orchestration)

# Phase 3 — the day-12 lifecycle loop (G-01). Deliberately a separate stack from orchestration:
# the append-only decision history needs its own IAM boundary (PutItem and nothing else), and
# bundling it with the mutable case-table grants would make that boundary a comment rather than a
# control. Depends on orchestration for the engine/audit/review ARNs and on harnesses for the
# fraud harness it re-invokes.
lifecycle = LifecycleStack(
    app, "KycLifecycle",
    env=env,
    case_table_name=foundation.case_table.table_name,
    case_table_arn=foundation.case_table.table_arn,
    case_key_arn=foundation.case_key.key_arn,
    decision_engine_arn=orchestration.decision_engine.function_arn,
    audit_writer_arn=orchestration.audit_writer.function_arn,
    review_queue_url=orchestration.review_queue.queue_url,
    review_queue_arn=orchestration.review_queue.queue_arn,
    # The re-screen runs the Fraud agent off the critical path, so it can afford a stronger model
    # and high reasoning effort — the latency that rules it out of Phase 1 is free here.
    fraud_harness_arn=harnesses.harness_arns["fraud"],
    fraud_model_id=harnesses.model_ids["fraud"],
    description=(
        "Phase 3 lifecycle re-assessment: EventBridge bus + archive, Standard state machine, "
        "append-only decision history (G-01)."
    ),
)
lifecycle.add_dependency(orchestration)

# Independent of the Phase 1-7 backend chain — task 14.1 notes this phase builds in parallel,
# depending only on Phase 1 foundation. No stack dependency to the orchestration chain: Cognito
# needs nothing the backend produces.
auth = AuthStack(
    app, "KycAuth",
    env=env,
    description=(
        "Cognito user pool (applicants/reviewers groups) + identity pool scoped to "
        "rekognition:StartFaceLivenessSession only (R32.2, R33.7, F-42)."
    ),
)

api = ApiStack(
    app, "KycApi",
    env=env,
    user_pool_id=auth.user_pool.user_pool_id,
    user_pool_client_id=auth.user_pool_client.user_pool_client_id,
    case_table_name=foundation.case_table.table_name,
    case_table_arn=foundation.case_table.table_arn,
    case_key_arn=foundation.case_key.key_arn,
    evidence_bucket_names={
        j.code: foundation.evidence_buckets[j.code].bucket_name for j in JURISDICTIONS},
    evidence_key_arns={
        j.code: foundation.jurisdiction_keys[j.code].key_arn for j in JURISDICTIONS},
    biometric_bucket_name=foundation.biometric_bucket.bucket_name,
    biometric_key_arn=foundation.trace_key.key_arn,
    applicants_queue_url=ingress.queue.queue_url,
    applicants_queue_arn=ingress.queue.queue_arn,
    review_queue_url=orchestration.review_queue.queue_url,
    review_queue_arn=orchestration.review_queue.queue_arn,
    # Decision records land in the US evidence bucket (orchestration_stack.py's own choice,
    # for the same Object-Lock-from-creation reason) — mirrored here rather than re-derived.
    audit_bucket_name=foundation.evidence_buckets["us"].bucket_name,
    audit_key_arn=foundation.jurisdiction_keys["us"].key_arn,
    phase1_state_machine_arn=orchestration.phase1.attr_arn,
    description=(
        "HTTP API + 3 Lambdas (liveness, applicants, review) behind a Cognito JWT authorizer "
        "(task 14.3)."
    ),
)
api.add_dependency(auth)
api.add_dependency(ingress)
api.add_dependency(orchestration)

all_stacks = [foundation, tools, gateways, harnesses, orchestration, ingress, auth, api]

# Stage 2 (task 8.1, F-17). Only synthesised if the manifest already exists — `cdk synth
# KycFoundation` etc. for stage 1 must not fail just because stage 2's prerequisite (running
# stage 1 for real, then policies/generate.py) has not happened yet in this environment.
_manifest_path = (
    Path(__file__).resolve().parents[1] / "policies" / "generated" / "manifest.json")
if _manifest_path.exists():
    policies = PoliciesStack(
        app, "KycPolicies",
        env=env,
        manifest_path=_manifest_path,
        description=(
            "Cedar policies (stage 2, F-17): per-agent-per-tool authorisation, prompt-injection "
            "guardrail, PII suppression, content filter. LOG_ONLY until R14.6's cutover."
        ),
    )
    all_stacks.append(policies)

for stack in all_stacks:
    for k, v in TAGS.items():
        cdk.Tags.of(stack).add(k, v)

app.synth()
