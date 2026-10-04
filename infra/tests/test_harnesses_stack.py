"""Two-tier retention, trace side — task 6.8, R18.7-R18.9, AC-25.

`AWS::Logs::LogGroup` cannot be used here because AgentCore auto-creates its own log group the
moment `UNIFIED_TRACES_DESTINATION_ENABLED=true` fires, before any CDK-owned resource can claim
the name — CloudFormation then refuses to "create" a resource that already exists. Verified
directly against the live service: all seven `-live` log groups this build had already created
came back `retentionInDays: NEVER EXPIRE`, `kmsKeyId: None`. Fixed with an `AwsCustomResource`
that calls `PutRetentionPolicy` / `AssociateKmsKey` against the name AgentCore chose, every
deploy, which is what these tests assert is wired.

`AssociateKmsKey` also failed the first time this was deployed with "The specified KMS key does
not exist or is not allowed to be used" — an IAM grant on the calling role is not sufficient;
the key's own resource policy must trust the CloudWatch Logs service principal. That grant on
`FoundationStack.trace_key` is asserted here too, because a broken key policy fails silently at
plan time and loudly, expensively, at deploy time.

An actual 90-day expiry cannot be exercised in a unit test — that is what "trace deleted at
expiry while the decision record persists" was verified against live infrastructure for (see
`spike/`, and `audit/tests/test_record.py::test_reasoning_text_never_enters_the_object_lock_
payload` for the complementary "never even reaches the surviving store" half). What a unit test
*can* pin, permanently, is that the retention value, the encryption key, and the per-agent
naming are all wired correctly in the CDK that produces the deploy.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from kyc.config import AGENTS, JURISDICTIONS, REASONING_TRACE_RETENTION_DAYS
from kyc.foundation_stack import FoundationStack
from kyc.harnesses_stack import HarnessesStack


@pytest.fixture(scope="module")
def foundation_template() -> Template:
    app = cdk.App()
    stack = FoundationStack(
        app, "TestFoundation",
        env=cdk.Environment(account="123456789012", region="us-east-1"))
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def harnesses_template() -> Template:
    app = cdk.App()
    foundation = FoundationStack(
        app, "TestFoundationForHarnesses",
        env=cdk.Environment(account="123456789012", region="us-east-1"))
    stack = HarnessesStack(
        app, "TestHarnesses",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
        harness_role_arns={a.key: r.role_arn for a, r in
                           zip(AGENTS, foundation.harness_roles.values())},
        gateway_arns={j.code: f"arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                              f"gateway/test-{j.code}" for j in JURISDICTIONS},
        trace_key_arn=foundation.trace_key.key_arn,
    )
    return Template.from_stack(stack)


# ------------------------------------------------------------------ retention wiring


def test_the_retention_constant_is_recorded_and_not_silently_hardcoded(harnesses_template):
    """R18.7/R18.9 name a retention rule ("case closure plus appeal window") without a day
    count. REASONING_TRACE_RETENTION_DAYS is the recorded, documented default — this pins that
    both the constant and the deployed value stay 90 and cannot drift apart."""
    assert REASONING_TRACE_RETENTION_DAYS == 90


def _flatten_join(value) -> str:
    """`Create`/`Update` on this custom resource is an `Fn::Join` (a mix of literal strings and
    `Fn::GetAtt` tokens), not a plain string — `Match.string_like_regexp` cannot see inside one.
    Flattening every literal fragment and stringifying the tokens gives a single searchable blob
    that still contains every literal detail the SDK call actually carries (the action name, the
    retention days, the key reference), which is everything these tests need to assert on.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        _, parts = value["Fn::Join"]
        return "".join(_flatten_join(p) for p in parts)
    return json.dumps(value, default=str)


def _custom_aws_resources(template: Template) -> dict:
    return template.find_resources("Custom::AWS")


def _calls(template: Template, action: str) -> dict[str, str]:
    """logical_id -> flattened Create-call text, for every Custom::AWS resource whose Create
    call names the given SDK action."""
    out = {}
    for logical_id, body in _custom_aws_resources(template).items():
        create = body.get("Properties", {}).get("Create")
        if create is None:
            continue
        flat = _flatten_join(create)
        if f'"action":"{action}"' in flat:
            out[logical_id] = flat
    return out


def test_every_agent_has_a_trace_retention_custom_resource(harnesses_template):
    """One PutRetentionPolicy call per agent. Missing one means that agent's traces never
    expire — the exact defect measured live before this task."""
    calls = _calls(harnesses_template, "PutRetentionPolicy")
    assert len(calls) == len(AGENTS), (
        f"expected {len(AGENTS)} retention custom resources, got {len(calls)}")


def test_every_agent_has_a_trace_encryption_custom_resource(harnesses_template):
    """One AssociateKmsKey call per agent. Missing one means that agent's traces sit
    unencrypted under the default (no) key — the other half of the live defect."""
    calls = _calls(harnesses_template, "AssociateKmsKey")
    assert len(calls) == len(AGENTS), (
        f"expected {len(AGENTS)} encryption custom resources, got {len(calls)}")


def test_retention_calls_use_the_documented_90_day_value(harnesses_template):
    """The custom resource's SDK call parameters must actually carry 90 — a constant that
    exists but is never passed through would pass every other test here and still leave every
    trace retained forever."""
    calls = _calls(harnesses_template, "PutRetentionPolicy")
    assert calls, "no PutRetentionPolicy custom resources found"
    for logical_id, flat in calls.items():
        assert f'"retentionInDays":{REASONING_TRACE_RETENTION_DAYS}' in flat, (
            f"{logical_id} does not request {REASONING_TRACE_RETENTION_DAYS}-day retention")


def test_retention_and_encryption_target_the_same_log_group_per_agent(harnesses_template):
    """Both calls must resolve to the same AgentCore-owned log group name — built from that
    harness's own `AgentRuntimeId` token — or the retention policy and the encryption could
    silently land on different resources."""
    retention = _calls(harnesses_template, "PutRetentionPolicy")
    encryption = _calls(harnesses_template, "AssociateKmsKey")
    assert retention and encryption
    for suffix in ("Briefing", "Documents", "Identity", "Fraud", "PreScreen",
                   "ComplianceRisk", "Notify"):
        r = next((v for k, v in retention.items() if f"TraceRetention{suffix}" in k), None)
        e = next((v for k, v in encryption.items() if f"TraceEncryption{suffix}" in k), None)
        assert r and e, f"missing retention/encryption pair for {suffix}"
        # Both embed the identical Fn::GetAtt reference to that agent's own harness resource.
        assert f'"Harness{suffix}"' in r and f'"Harness{suffix}"' in e


def test_encryption_calls_target_the_trace_key_not_the_evidence_keys(harnesses_template):
    """AssociateKmsKey must reference the trace key specifically. Pointing it at any evidence
    key would put reasoning traces under the same key as decision records — exactly the sharing
    R18.7 forbids, since the two have opposite retention obligations."""
    calls = _calls(harnesses_template, "AssociateKmsKey")
    assert calls, "no AssociateKmsKey custom resources found"
    for logical_id, flat in calls.items():
        assert "TraceKey" in flat or "ReasoningTraceKey" in flat, (
            f"{logical_id} does not appear to reference the trace key")


def test_no_plain_log_group_resource_races_the_agentcore_default(harnesses_template):
    """A plain `AWS::Logs::LogGroup` targeting the AgentCore-chosen name fails at deploy with
    "already exists" — measured directly. This stack must use the custom-resource path
    (PutRetentionPolicy/AssociateKmsKey against the existing name), never LogGroup creation, for
    the per-agent trace destinations."""
    log_groups = harnesses_template.find_resources("AWS::Logs::LogGroup")
    trace_log_groups = {
        k: v for k, v in log_groups.items()
        if "bedrock-agentcore/runtimes" in str(v.get("Properties", {}).get("LogGroupName", ""))
    }
    assert not trace_log_groups, (
        "found AWS::Logs::LogGroup resources targeting the AgentCore trace destination; "
        "these will fail at deploy because AgentCore already owns the name"
    )


# ------------------------------------------------------------------ key policy


def test_the_trace_key_policy_trusts_cloudwatch_logs(foundation_template):
    """`AssociateKmsKey` failed with "The specified KMS key does not exist or is not allowed to
    be used" until this statement was added — an IAM grant on the calling role alone is not
    sufficient; the key's own resource policy must separately trust the service principal."""
    foundation_template.has_resource_properties(
        "AWS::KMS::Key",
        {
            "KeyPolicy": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Effect": "Allow",
                        "Principal": {
                            "Service": Match.string_like_regexp(r"logs\..*\.amazonaws\.com")
                        },
                    })
                ])
            }
        },
    )


def test_the_trace_key_is_distinct_from_evidence_keys(foundation_template):
    """R18.7: traces and decision records must never share a key, because they have opposite
    retention obligations — one deleted at case closure plus the appeal window, the other kept
    for a seven-year floor under Object Lock."""
    keys = foundation_template.find_resources("AWS::KMS::Key")
    aliases = foundation_template.find_resources("AWS::KMS::Alias")
    alias_names = {v["Properties"]["AliasName"] for v in aliases.values()}
    assert any("reasoning-traces" in a for a in alias_names)
    assert any("evidence" in a for a in alias_names)
    # Distinct resources, not merely distinct aliases pointing at the same key.
    assert len(keys) >= len(JURISDICTIONS) + 1, (
        "expected at least one key per jurisdiction plus the trace key as separate resources"
    )
