#!/usr/bin/env python3
"""CDK entry point — KYC in 90 Seconds.

Two-stage deploy (F-17). Cedar policies must reference concrete Gateway ARNs and Cedar
forbids wildcard resources, so the Gateways must exist before their policies can be written.
`make deploy` runs both stages and prints why there are two, rather than pretending a single
`cdk deploy` works.

Stage 1: Foundation -> Tools -> Gateways (+ policy engines) -> Harnesses -> Orchestration
         -> Ingress -> Lifecycle, and Auth -> Api alongside
Stage 2: Policies   (needs Gateway ARNs captured from stage 1)

Context flags: `-c policyEnforcement=LOG_ONLY|ENFORCE` (default LOG_ONLY),
`-c demoControls=true|false` (default true), `-c evidenceLock=DEMO|COMPLIANCE` and
`-c livenessChallenge=FaceMovementChallenge|FaceMovementAndLightChallenge` (both default from
demoControls), and `-c provisionedConcurrency=<n>` (default 0).
"""

import os
from pathlib import Path

import aws_cdk as cdk

from kyc.api_stack import ApiStack
from kyc.auth_stack import AuthStack
from kyc.config import (
    EVIDENCE_LOCKS,
    JURISDICTIONS,
    LIVENESS_CHALLENGES,
    LIVENESS_LIGHT_CHALLENGE,
    LIVENESS_MOVEMENT_CHALLENGE,
    PRIMARY_REGION,
    PROJECT,
)
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

# Cedar enforcement, one switch for both halves: the Gateway attachment mode (LOG_ONLY | ENFORCE)
# and the per-policy EnforcementMode (LOG_ONLY | ACTIVE) must move together, or the Gateway
# enforces policies that are still only logging. LOG_ONLY by default: switch with
# `cdk deploy -c policyEnforcement=ENFORCE` only after scripts/check_policy_decisions.py shows the
# logged decisions permit every call the clean case makes.
policy_enforcement = str(app.node.try_get_context("policyEnforcement") or "LOG_ONLY").upper()

# Presenter demo controls (`api/demo.py`): fault switches and the synthetic list update behind
# Act 3. On by default because this repository is the demo; `-c demoControls=false` removes the
# routes and their function from the API entirely.
demo_controls = str(app.node.try_get_context("demoControls") or "true").lower() != "false"

# Evidence retention (D-35), one value for the buckets' default and the audit writer. A demo
# deployment (demo controls on) locks in GOVERNANCE for one day, because its documents carry the
# presenter's real face and it decides nobody's real application. Anything else gets COMPLIANCE
# for seven years. `-c evidenceLock=COMPLIANCE|DEMO` overrides either way.
_lock_name = str(app.node.try_get_context("evidenceLock")
                 or ("DEMO" if demo_controls else "COMPLIANCE")).upper()
if _lock_name not in EVIDENCE_LOCKS:
    raise ValueError(f"evidenceLock must be one of {tuple(EVIDENCE_LOCKS)}, got {_lock_name!r}")
evidence_lock = EVIDENCE_LOCKS[_lock_name]

# Face Liveness challenge (D-36). A demo uses the movement challenge: no flashing colours on a
# projector, and several seconds quicker. Anything else keeps the light challenge, the service
# default AWS recommends for the highest accuracy. `-c livenessChallenge=<type>` overrides.
liveness_challenge = str(
    app.node.try_get_context("livenessChallenge")
    or (LIVENESS_MOVEMENT_CHALLENGE if demo_controls else LIVENESS_LIGHT_CHALLENGE))
if liveness_challenge not in LIVENESS_CHALLENGES:
    raise ValueError(
        f"livenessChallenge must be one of {LIVENESS_CHALLENGES}, got {liveness_challenge!r}")

foundation = FoundationStack(
    app, "KycFoundation",
    env=env,
    evidence_lock=evidence_lock,
    description=(
        "KYC in 90 Seconds foundation: per-jurisdiction KMS, S3 evidence buckets with "
        f"Object Lock at creation ({evidence_lock.mode}, {evidence_lock.days} day(s)), "
        "DynamoDB case store, ten IAM roles."
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
    description=("One Lambda / six evidence tools that record facts on the case, the account "
                 "platform stub and its table, and the demo control parameters."),
)
tools.add_dependency(foundation)

gateways = GatewaysStack(
    app, "KycGateways",
    env=env,
    gateway_role_arn=foundation.gateway_role.role_arn,
    # The alias, not $LATEST, so a Gateway target cannot silently start calling unreleased code.
    tools_lambda_arn=tools.tools_alias.function_arn,
    policy_enforcement=policy_enforcement,
    description=("Four Gateways (one per jurisdiction, forced by F-04), six targets each, "
                 f"four Cedar policy engines attached {policy_enforcement}."),
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

# R18.6 — the four version pins every decision record carries, as they are in force at deploy time:
# the endpoint name, the harness version each `live` endpoint serves, the policy engine with the
# mode it is attached in, and the Gateway that actually serves the tools. The Gateway ARN used to
# be read from the case record, where the API had fabricated it from the jurisdiction code.
audit_pins = {
    "endpointName": "live",
    "harnessVersion": ",".join(
        f"{agent}={version}" for agent, version in sorted(harnesses.harness_versions.items())),
    "policyEngineVersion": (
        f"{gateways.policy_engines['us'].get_att('PolicyEngineArn').to_string()}"
        f"#{policy_enforcement}"),
    "gatewayArn": harnesses.gateway_arn,
}

orchestration = OrchestrationStack(
    app, "KycOrchestration",
    env=env,
    case_table_name=foundation.case_table.table_name,
    case_table_arn=foundation.case_table.table_arn,
    case_key_arn=foundation.case_key.key_arn,
    trace_key_arn=foundation.trace_key.key_arn,
    # Decision records go to the US evidence bucket: Object Lock from creation (F-29, and it
    # cannot be retrofitted), which is what R18.3 requires. Reasoning traces must never land
    # here — Compliance mode forbids deletion, so they would make R18.9 permanently unmeetable
    # rather than merely awkward.
    audit_bucket_name=foundation.evidence_buckets["us"].bucket_name,
    evidence_lock=evidence_lock,
    audit_bucket_arn=foundation.evidence_buckets["us"].bucket_arn,
    audit_key_arn=foundation.jurisdiction_keys["us"].key_arn,
    sfn_role_arn=foundation.sfn_role.role_arn,
    harness_arns=harnesses.harness_arns,
    harness_endpoint_arns=harnesses.endpoint_arns,
    trace_log_group_names=harnesses.trace_log_group_names,
    model_ids=harnesses.model_ids,
    account_function_arn=tools.account_fn.function_arn,
    audit_pins=audit_pins,
    description=(
        "Phase 1 and Phase 2 state machines (JSONata, F-71), the pure decision engine, the "
        "audit writer and the human-review queue."
    ),
)
orchestration.add_dependency(harnesses)
orchestration.add_dependency(tools)

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

# Phase 3 — the lifecycle loop (G-01), triggered by a risk event or a sanctions list update.
# Deliberately a separate stack from orchestration: the append-only decision history needs its own
# IAM boundary (PutItem and nothing else), and bundling it with the mutable case-table grants would
# make that boundary a comment rather than a control. Depends on orchestration for the
# engine/audit/review ARNs, on harnesses for the Pre-Screen and Briefing agents it invokes, and on
# tools for the account platform it freezes or restricts.
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
    # The same Pre-Screen agent, screening tool and Cedar policy as Phase 1, re-run against the
    # updated list. One model and one policy for both answers means the day-12 result is
    # comparable with the day-zero one.
    harness_arns=harnesses.harness_arns,
    model_ids=harnesses.model_ids,
    account_function_arn=tools.account_fn.function_arn,
    audit_pins=audit_pins,
    description=(
        "Phase 3 lifecycle re-assessment on a risk event or sanctions list update: EventBridge "
        "bus + archive + DLQ, Standard state machine, append-only decision history (G-01)."
    ),
)
lifecycle.add_dependency(orchestration)
lifecycle.add_dependency(tools)

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
    phase2_state_machine_arn=orchestration.phase2.attr_arn,
    phase3_state_machine_arn=lifecycle.phase3.attr_arn,
    account_function_arn=tools.account_fn.function_arn,
    lifecycle_bus_arn=lifecycle.bus.event_bus_arn,
    lifecycle_bus_name=lifecycle.bus.event_bus_name,
    demo_controls=demo_controls,
    liveness_challenge=liveness_challenge,
    description=(
        "HTTP API behind a Cognito JWT authorizer: liveness, intake, case status, accounts, "
        "the reviewer console" + (", presenter demo controls." if demo_controls else ".")
    ),
)
api.add_dependency(auth)
api.add_dependency(ingress)
api.add_dependency(orchestration)
api.add_dependency(lifecycle)
api.add_dependency(tools)

all_stacks = [foundation, tools, gateways, harnesses, orchestration, ingress, lifecycle, auth,
              api]

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
        policy_enforcement=policy_enforcement,
        description=(
            "Cedar policies (stage 2, F-17): per-agent-per-tool authorisation, prompt-injection "
            f"guardrail, PII suppression, content filter. Gateway mode {policy_enforcement}."
        ),
    )
    all_stacks.append(policies)

for stack in all_stacks:
    for k, v in TAGS.items():
        cdk.Tags.of(stack).add(k, v)

app.synth()
