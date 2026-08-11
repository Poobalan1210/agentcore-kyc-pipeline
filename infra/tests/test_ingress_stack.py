"""Ingress — tasks 7.1, 7.2, design §10, R21.1, DIV-3.

The property that matters is deduplication, not throughput or replay — DIV-3 chooses SQS FIFO
over MSK specifically because MSK does not provide it. These tests pin the two settings that
actually deliver that property (`ContentBasedDeduplication`, per-message-group ordering) and the
two escape hatches that keep a bad message from looping forever (the DLQ, `maxReceiveCount`).

The Pipe's job is equally narrow and equally easy to get subtly wrong: it must invoke
`StartExecution` without waiting for it (`FIRE_AND_FORGET`), and it must not enrich or filter —
design §10.2 is explicit that a message shape it does not like should fail loudly inside the
state machine, not be silently rewritten or dropped one hop earlier.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from kyc.ingress_stack import IngressStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = IngressStack(
        app, "TestIngress",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
        pipe_role_arn="arn:aws:iam::123456789012:role/test-pipe",
        phase1_state_machine_arn=(
            "arn:aws:states:us-east-1:123456789012:stateMachine:test-phase1"),
    )
    return Template.from_stack(stack)


# ------------------------------------------------------------------ 7.1 SQS FIFO


def test_the_queue_is_fifo(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": "kyc90-applicants.fifo", "FifoQueue": True},
    )


def test_content_based_deduplication_is_the_load_bearing_setting(template: Template) -> None:
    """DIV-3: this is the entire reason SQS FIFO was chosen over MSK. Without it a redelivered
    or retried submission runs sanctions screening twice and can produce two different, both
    "authoritative-looking" decisions for one applicant — failure demo 1 (R22.1)."""
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": "kyc90-applicants.fifo", "ContentBasedDeduplication": True},
    )


def test_message_group_id_gives_per_applicant_ordering(template: Template) -> None:
    """R21.1. Per-message-group throughput + message-group dedup scope, so two submissions from
    one applicant are ordered against each other while different applicants still run in
    parallel — the queue setting side of `MessageGroupId = applicantId`. (`MessageGroupId` itself
    is supplied by the producer per message, not a queue property, so it is not assertable here;
    what the queue configuration must not do is silently pool all applicants into one ordering
    domain, which is what these two settings prevent.)"""
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "kyc90-applicants.fifo",
            "FifoThroughputLimit": "perMessageGroupId",
            "DeduplicationScope": "messageGroup",
        },
    )


def test_a_dead_letter_queue_exists_after_three_receives(template: Template) -> None:
    """A message that fails processing loops forever without this, or silently vanishes with a
    naive at-most-once assumption. Three, not one — a single transient throttle must not
    manufacture a demo failure."""
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "kyc90-applicants.fifo",
            "RedrivePolicy": Match.object_like({"maxReceiveCount": 3}),
        },
    )


def test_the_dlq_is_also_fifo_and_deduplicated(template: Template) -> None:
    """A retry storm redelivering the same poison message to the DLQ must not appear as several
    independent failures for one applicant."""
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "kyc90-applicants-dlq.fifo",
            "FifoQueue": True,
            "ContentBasedDeduplication": True,
        },
    )


def test_both_queues_deny_non_tls_traffic(template: Template) -> None:
    policies = template.find_resources("AWS::SQS::QueuePolicy")
    assert len(policies) == 2, "expected a TLS-enforcement policy on both queues"
    for body in policies.values():
        statements = body["Properties"]["PolicyDocument"]["Statement"]
        deny_insecure = [
            s for s in statements
            if s.get("Effect") == "Deny"
            and s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
        ]
        assert deny_insecure, "no deny-insecure-transport statement found"


# ------------------------------------------------------------------ 7.2 EventBridge Pipe


def test_the_pipe_sources_from_the_applicants_queue(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Pipes::Pipe",
        {
            "Source": {"Fn::GetAtt": Match.array_with(["ApplicantsQueueDE67A7FB"])},
        },
    )


def test_the_pipe_targets_phase1_by_the_arn_passed_in(template: Template) -> None:
    """The pipe must target exactly the state machine ARN the orchestration stack hands it —
    hardcoding or recomputing this ARN inside IngressStack would let the two stacks drift apart
    silently if Phase 1 is ever recreated."""
    template.has_resource_properties(
        "AWS::Pipes::Pipe",
        {"Target": "arn:aws:states:us-east-1:123456789012:stateMachine:test-phase1"},
    )


def test_the_pipe_does_not_wait_for_the_execution(template: Template) -> None:
    """FIRE_AND_FORGET, not the `.sync`-equivalent REQUEST_RESPONSE. Phase 1 already manages its
    own completion and triggers Phase 2 asynchronously (task 6.2); blocking the pipe on the
    execution would reintroduce the same latency mistake one layer up, on every submission."""
    template.has_resource_properties(
        "AWS::Pipes::Pipe",
        {
            "TargetParameters": {
                "StepFunctionStateMachineParameters": {"InvocationType": "FIRE_AND_FORGET"},
            }
        },
    )


def test_the_pipe_has_no_enrichment(template: Template) -> None:
    """Design §10.2: no enrichment, no filter. A message that does not match the shape
    `LoadCase` expects must fail loudly inside the state machine's own execution history, not be
    silently rewritten by a pipe enrichment step first."""
    pipes = template.find_resources("AWS::Pipes::Pipe")
    assert pipes, "no pipe found"
    for body in pipes.values():
        assert "Enrichment" not in body["Properties"], "pipe has an enrichment step"


def test_the_pipe_has_no_filter_criteria(template: Template) -> None:
    """Same principle, the other stated exclusion in §10.2: every message that reaches the
    queue is treated as a valid applicant submission attempt."""
    pipes = template.find_resources("AWS::Pipes::Pipe")
    assert pipes, "no pipe found"
    for body in pipes.values():
        source_params = body["Properties"].get("SourceParameters", {})
        assert "FilterCriteria" not in source_params, "pipe source has filter criteria"


def test_the_pipe_role_can_start_phase1_and_nothing_broader(template: Template) -> None:
    """The pipe role must be scoped to `states:StartExecution` on the one Phase 1 ARN it was
    given, not a wildcard that could start arbitrary state machines in the account."""
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Effect": "Allow",
                        "Action": "states:StartExecution",
                        "Resource": (
                            "arn:aws:states:us-east-1:123456789012:"
                            "stateMachine:test-phase1"),
                    })
                ])
            }
        },
    )


def test_the_pipe_depends_on_the_queue(template: Template) -> None:
    """Ordering guard: the pipe must not be created (and therefore start polling) before the
    queue it reads from exists."""
    pipe_resources = template.find_resources("AWS::Pipes::Pipe")
    assert pipe_resources, "no pipe found"
    for body in pipe_resources.values():
        depends_on = body.get("DependsOn", [])
        assert any("ApplicantsQueue" in d for d in depends_on), (
            "pipe has no explicit dependency on the applicants queue"
        )
