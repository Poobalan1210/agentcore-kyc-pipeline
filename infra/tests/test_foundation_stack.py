"""Assertions against the synthesised template.

These exist because the two costly mistakes in this stack are both invisible at deploy time
and unfixable afterwards: a bucket without Object Lock looks identical to one with it until
someone tries to delete evidence, and a retention floor of five years looks fine until an
Australian or Japanese regulator asks (B-12, B-13).

Run before every deploy. `make test` does.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from kyc.config import (
    AGENTS,
    JURISDICTIONS,
    OBJECT_LOCK_YEARS,
    InferenceOption,
    max_concurrent_applicants,
    session_quota,
)
from kyc.foundation_stack import FoundationStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = FoundationStack(app, "TestFoundation",
                            env=cdk.Environment(account="123456789012", region="us-east-1"))
    return Template.from_stack(stack)


# ------------------------------------------------------------------ 1.3 Object Lock
def test_every_evidence_bucket_has_object_lock_enabled(template: Template) -> None:
    """R17.2. Cannot be retrofitted — if this fails, the bucket must be recreated."""
    buckets = template.find_resources("AWS::S3::Bucket")
    evidence = {k: v for k, v in buckets.items() if "EvidenceBucket" in k}
    assert len(evidence) == len(JURISDICTIONS), f"expected 4 evidence buckets, got {len(evidence)}"
    for logical_id, body in evidence.items():
        props = body["Properties"]
        assert props.get("ObjectLockEnabled") is True, f"{logical_id} has no Object Lock"


def test_object_lock_is_compliance_mode_and_seven_years(template: Template) -> None:
    """Governance mode allows privileged users to delete. Compliance mode does not, which is
    the entire point. Seven years, not five (B-12, B-13, D-09)."""
    buckets = template.find_resources("AWS::S3::Bucket")
    evidence = {k: v for k, v in buckets.items() if "EvidenceBucket" in k}
    for logical_id, body in evidence.items():
        cfg = body["Properties"]["ObjectLockConfiguration"]
        rule = cfg["Rule"]["DefaultRetention"]
        assert rule["Mode"] == "COMPLIANCE", f"{logical_id} is not COMPLIANCE mode"
        assert rule["Days"] == 365 * OBJECT_LOCK_YEARS, (
            f"{logical_id} retention is {rule['Days']} days, expected "
            f"{365 * OBJECT_LOCK_YEARS}"
        )


def test_every_evidence_bucket_allows_cross_origin_put_only(template: Template) -> None:
    """R32.6 — the browser uploads documents with a presigned PUT straight to S3, so the bucket
    must allow that cross-origin or the upload never leaves the browser. PUT and nothing else:
    evidence is read back through the API's audit route, never cross-origin by the browser."""
    buckets = template.find_resources("AWS::S3::Bucket")
    evidence = {k: v for k, v in buckets.items() if "EvidenceBucket" in k}
    assert len(evidence) == len(JURISDICTIONS)
    for logical_id, body in evidence.items():
        cors = body["Properties"].get("CorsConfiguration")
        assert cors, f"{logical_id} has no CORS rule; browser presigned PUT will be blocked"
        rules = cors["CorsRules"]
        assert len(rules) == 1, f"{logical_id} should have exactly one CORS rule"
        assert rules[0]["AllowedMethods"] == ["PUT"], (
            f"{logical_id} allows {rules[0]['AllowedMethods']}; only PUT is required"
        )


def test_biometric_bucket_has_no_object_lock(template: Template) -> None:
    """R36.3. The one bucket where deletion must remain possible. Object Lock here would turn
    a 24-hour deletion duty into a seven-year violation."""
    buckets = template.find_resources("AWS::S3::Bucket")
    bio = {k: v for k, v in buckets.items() if "DemoBiometric" in k}
    assert len(bio) == 1
    props = next(iter(bio.values()))["Properties"]
    assert not props.get("ObjectLockEnabled"), "biometric bucket must not have Object Lock"
    assert props.get("LifecycleConfiguration"), "biometric bucket needs a 24h lifecycle rule"


def test_all_buckets_block_public_access_and_require_tls(template: Template) -> None:
    for logical_id, body in template.find_resources("AWS::S3::Bucket").items():
        pab = body["Properties"].get("PublicAccessBlockConfiguration")
        assert pab, f"{logical_id} has no public access block"
        assert all(pab[k] is True for k in
                   ("BlockPublicAcls", "BlockPublicPolicy",
                    "IgnorePublicAcls", "RestrictPublicBuckets")), logical_id


# ------------------------------------------------------------------ 1.2 KMS
def test_one_key_per_jurisdiction_plus_trace_and_case_keys(template: Template) -> None:
    """R17.1, R17.3. Four jurisdiction keys, one trace key, one case-store key."""
    template.resource_count_is("AWS::KMS::Key", len(JURISDICTIONS) + 2)


def test_trace_key_is_distinct_from_every_evidence_key(template: Template) -> None:
    """R18.7, R18.8. Reasoning traces are minimised and deleted; evidence is retained under
    Object Lock. Sharing a key couples two opposite retention obligations."""
    aliases = template.find_resources("AWS::KMS::Alias")
    names = {v["Properties"]["AliasName"] for v in aliases.values()}
    assert "alias/kyc90-reasoning-traces" in names
    for j in JURISDICTIONS:
        assert f"alias/kyc90-evidence-{j.code}" in names
    assert len(names) == len(JURISDICTIONS) + 2


def test_key_rotation_enabled(template: Template) -> None:
    for logical_id, body in template.find_resources("AWS::KMS::Key").items():
        assert body["Properties"].get("EnableKeyRotation") is True, logical_id


# ------------------------------------------------------------------ 1.4 DynamoDB
def test_case_table_keys_and_indexes(template: Template) -> None:
    """Design 9.1. PK applicantId, SK sk, plus the two GSIs the review queue and the
    awaiting-Phase-2 dashboard depend on (R6.6)."""
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "KeySchema": [
            {"AttributeName": "applicantId", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    })
    tables = template.find_resources("AWS::DynamoDB::Table")
    props = next(iter(tables.values()))["Properties"]
    index_names = {g["IndexName"] for g in props["GlobalSecondaryIndexes"]}
    assert index_names == {"state-index", "phase2Pending-index"}


def test_case_table_pitr_and_cmk(template: Template) -> None:
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        "SSESpecification": {"SSEEnabled": True, "SSEType": "KMS"},
        "DeletionProtectionEnabled": True,
    })


# ------------------------------------------------------------------ 1.5 IAM
def test_exactly_ten_roles_we_define(template: Template) -> None:
    """Seven harness plus Gateway plus Step Functions plus Pipe. The spec said nine; it was
    miscounted, and the count is asserted here so it cannot drift again.

    Counted by our own naming prefix rather than by raw resource count, because CDK adds one
    unnamed helper role of its own for the biometric bucket's auto-delete custom resource.
    Asserting the raw total would make this test fail for a reason that has nothing to do with
    the ten roles the spec is about — and would hide a real drift behind an off-by-one.
    """
    roles = template.find_resources("AWS::IAM::Role")
    ours = {k: v for k, v in roles.items()
            if str(v["Properties"].get("RoleName", "")).startswith("kyc90-")}
    assert len(ours) == 10, sorted(
        str(v["Properties"].get("RoleName")) for v in roles.values())

    names = {v["Properties"]["RoleName"] for v in ours.values()}
    expected = {f"kyc90-harness-{a.key}" for a in AGENTS} | {
        "kyc90-gateway", "kyc90-statemachine", "kyc90-pipe"}
    assert names == expected, names ^ expected


def test_the_only_extra_role_is_the_auto_delete_helper(template: Template) -> None:
    """Make the one CDK-generated role explicit so it cannot become a hiding place. If a
    second unnamed role ever appears, this fails and someone has to explain it."""
    roles = template.find_resources("AWS::IAM::Role")
    unnamed = [k for k, v in roles.items()
               if not str(v["Properties"].get("RoleName", "")).startswith("kyc90-")]
    assert len(unnamed) == 1, unnamed
    assert "AutoDelete" in unnamed[0] or "Custom" in unnamed[0], unnamed[0]


def test_every_harness_role_denies_the_tool_bypass(template: Template) -> None:
    """R11.7, F-09. InvokeAgentRuntimeCommand ignores allowedTools, so it is denied
    explicitly rather than merely left ungranted."""
    policies = template.find_resources("AWS::IAM::Policy")
    denies = 0
    for body in policies.values():
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Effect") != "Deny":
                continue
            actions = stmt.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            if "bedrock-agentcore:InvokeAgentRuntimeCommand" in actions:
                denies += 1
                assert "sts:AssumeRole" in actions, "role chaining must also be denied (R23.6)"
    assert denies == len(AGENTS), (
        f"{denies} of {len(AGENTS)} harness roles deny the bypass action")


def test_state_machine_role_grants_invoke_agent_runtime_not_only_invoke_harness(
        template: Template) -> None:
    """F-70, measured in Phase 0. The Step Functions integration authorises against
    InvokeAgentRuntime; granting only InvokeHarness produces AccessDeniedException."""
    policies = template.find_resources("AWS::IAM::Policy")
    found = False
    for body in policies.values():
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            if "bedrock-agentcore:InvokeAgentRuntime" in actions:
                found = True
                assert stmt["Effect"] == "Allow"
                assert stmt.get("Resource") != "*", "scope the harness ARN, never *"
    assert found, "no role grants bedrock-agentcore:InvokeAgentRuntime"


def test_no_role_grants_wildcard_bedrock_invoke_on_all_models(template: Template) -> None:
    """R11.5. A harness may invoke only its assigned models."""
    for body in template.find_resources("AWS::IAM::Policy").values():
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Effect") != "Allow":
                continue
            actions = stmt.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            if any(a in ("bedrock:InvokeModel", "bedrock:Converse") for a in actions):
                assert stmt.get("Resource") != "*", (
                    "model invocation must be scoped to specific inference profiles")


# ------------------------------------------------------------------ config invariants
def test_australia_never_uses_nova_2_lite() -> None:
    """R12.10, F-69. apac.amazon.nova-2-lite-v1:0 does not exist and global. gives no
    residency guarantee, so AU must not be assigned a reasoning model."""
    au = next(j for j in JURISDICTIONS if j.code == "au")
    assert au.reasoning_model is None
    for model in (au.workhorse_model, au.fast_model):
        assert "nova-2-lite" not in model, model
        assert not model.startswith("global."), model


def test_no_jurisdiction_uses_global_inference() -> None:
    """R12.11 / F-49. Global Cross-Region carries no residency guarantee, so no jurisdiction
    may be configured onto it."""
    for j in JURISDICTIONS:
        assert j.workhorse_inference is not InferenceOption.GLOBAL, j.code
        if j.reasoning_inference:
            assert j.reasoning_inference is not InferenceOption.GLOBAL, j.code
        for model in filter(None, (j.workhorse_model, j.fast_model, j.reasoning_model)):
            assert not model.startswith("global."), (j.code, model)


def test_no_reasoning_agent_can_pin_sampling() -> None:
    """F-62 + F-86. At `high` the service rejects a pinned call outright — all 8 pinned runs
    failed. At `medium` it *accepts* the pin and then fails silently: measured live, Fraud burned
    8,525 output tokens against a 2048 ceiling and returned `Content: []`, because reasoning draws
    on the same output budget as the answer (F-63).

    The `high` behaviour is the safer of the two. A rejection stops the build; an empty successful
    response is read downstream as "no findings", which is read as "no risk".
    """
    compliance = next(a for a in AGENTS if a.key == "compliance")
    assert compliance.reasoning_effort == "high"
    assert compliance.can_pin_sampling is False

    # F-88: Fraud no longer reasons at all, so it CAN pin sampling again. The rule still holds for
    # any agent that does reason — asserted over the whole set below.
    fraud = next(a for a in AGENTS if a.key == "fraud")
    assert fraud.reasoning_effort is None
    assert fraud.can_pin_sampling is True

    for agent in AGENTS:
        if agent.reasoning_effort is not None:
            assert agent.can_pin_sampling is False, agent.key

    for agent in AGENTS:
        if agent.reasoning_effort is None:
            assert agent.can_pin_sampling is True, agent.key


def test_only_one_agent_runs_at_high_effort() -> None:
    """D-08. High effort measured at 4.70x TTFT with a 48.5s worst case, so exactly one
    agent may use it and it must live in Phase 2."""
    high = [a for a in AGENTS if a.reasoning_effort == "high"]
    assert len(high) == 1, [a.key for a in high]
    assert high[0].phase.value == 2, "a high-effort agent cannot sit on the 90-second path"


def test_session_quota_is_regional_not_global() -> None:
    """F-11. 5,000 in us-east-1 and us-west-2, 2,500 elsewhere. Quoting 5,000 for London or
    Sydney overstates headroom by 2x."""
    assert session_quota("us-east-1") == 5000
    assert session_quota("us-west-2") == 5000
    assert session_quota("eu-west-2") == 2500
    assert session_quota("ap-southeast-2") == 2500
    assert max_concurrent_applicants("us-east-1") == 1000
    assert max_concurrent_applicants("ap-southeast-2") == 500


def test_seven_agents_five_in_phase_one() -> None:
    assert len(AGENTS) == 7
    assert len([a for a in AGENTS if a.phase.value == 1]) == 5
    assert len([a for a in AGENTS if a.phase.value == 2]) == 2
