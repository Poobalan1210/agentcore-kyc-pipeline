"""Ingress stack — Phase 7 (tasks 7.1-7.3), design §10.

SQS FIFO -> EventBridge Pipe -> Phase 1 state machine (`StartExecution`). No enrichment, no
filter: every message is a valid applicant, and validation belongs in the state machine where a
failure is visible in execution history rather than silently dropped at the pipe.

**Why SQS FIFO over MSK (DIV-3).** P-01 (the AWS Architecture Blog post this build deliberately
diverges from) uses MSK. MSK gives replay and independent consumers, genuinely useful if the
bank already runs Kafka — but it does not give deduplication, and deduplication is the property
that matters here. Agents are non-deterministic (R21.6): submit the same applicant twice without
it and sanctions screening can run twice and return two different answers, producing two
conflicting audit records with no way to say which one is authoritative. That is failure demo 1
(R22.1). `ContentBasedDeduplication` on the FIFO queue is the fix, at zero additional cost.

**Two independent layers, not one** (R21.1-R21.4). The queue's content-based dedup catches a
retransmitted message within its 5-minute window; the state machine's own `CheckIdempotency`
state plus a DynamoDB conditional write on `applicantId + eventId` (already wired in `asl.py`,
task 6.3) catches everything that slips past that window — a retry hours later, a client that
regenerated the message body with a timestamp field, or dedup simply expiring. Belt and braces:
either layer failing alone still leaves the other standing.

**Why `FIRE_AND_FORGET`, not `.sync`.** The pipe's only job is to start the execution; waiting
for Phase 1 to finish would put the pipe itself, and its own retry/DLQ semantics, in the critical
path of a 90-second budget for no benefit — Phase 1 already manages its own completion and
triggers Phase 2 asynchronously (task 6.2, `TriggerPhase2` without `.sync`). Blocking the pipe on
the execution would be the same mistake one layer up.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_pipes as pipes,
    aws_sqs as sqs,
)
from constructs import Construct

from .config import PROJECT


class IngressStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        pipe_role_arn: str,
        phase1_state_machine_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------ 7.1 SQS FIFO
        self.dlq = sqs.Queue(
            self, "ApplicantsDlq",
            queue_name=f"{PROJECT}-applicants-dlq.fifo",
            fifo=True,
            # Content-based dedup on the DLQ too — a poison message redelivered to the DLQ by a
            # retry storm should not appear as N separate failures for one applicant.
            content_based_deduplication=True,
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )

        self.queue = sqs.Queue(
            self, "ApplicantsQueue",
            queue_name=f"{PROJECT}-applicants.fifo",
            fifo=True,
            # DIV-3 / R21.1. `MessageGroupId = applicantId` gives per-applicant ordering — two
            # submissions from the same applicant are processed in the order they arrived, never
            # interleaved — while different applicants still process in parallel across groups.
            fifo_throughput_limit=sqs.FifoThroughputLimit.PER_MESSAGE_GROUP_ID,
            deduplication_scope=sqs.DeduplicationScope.MESSAGE_GROUP,
            # R21.1: free idempotency within SQS's 5-minute dedup window. A hash of the message
            # body is the dedup key, so the *caller* never has to supply one — this is what
            # "free" means here, as opposed to R21.2's applicantId+eventId key, which the state
            # machine derives itself and which is what actually has to hold for a replay
            # arriving hours later, well outside this window.
            content_based_deduplication=True,
            # R22.1 / failure demo 1: a message that fails processing three times moves to the
            # DLQ rather than looping forever or being silently dropped. Three receives, not one,
            # because a single transient throttle should not manufacture a demo failure.
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=self.dlq),
            retention_period=Duration.days(4),
            visibility_timeout=Duration.seconds(120),
            enforce_ssl=True,
        )

        # ------------------------------------------------------------ 7.2 EventBridge Pipe
        pipe_role = iam.Role.from_role_arn(self, "PipeRole", pipe_role_arn, mutable=True)

        self.queue.grant_consume_messages(pipe_role)
        pipe_role.add_to_principal_policy(iam.PolicyStatement(
            sid="StartPhase1Execution",
            actions=["states:StartExecution"],
            resources=[phase1_state_machine_arn],
        ))

        self.pipe = pipes.CfnPipe(
            self, "IngressPipe",
            name=f"{PROJECT}-ingress",
            role_arn=pipe_role.role_arn,
            source=self.queue.queue_arn,
            source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
                sqs_queue_parameters=pipes.CfnPipe.PipeSourceSqsQueueParametersProperty(
                    # SQS FIFO delivers at least one message group at a time per poll; a small
                    # batch keeps that ordering visible in execution history as separate
                    # executions rather than folding several applicants into one Payload.
                    batch_size=1,
                ),
            ),
            target=phase1_state_machine_arn,
            target_parameters=pipes.CfnPipe.PipeTargetParametersProperty(
                # FIRE_AND_FORGET, not the `.sync`-equivalent REQUEST_RESPONSE — see module
                # docstring. The pipe starts the execution and moves on; Phase 1 manages its own
                # completion and idempotency from there.
                step_function_state_machine_parameters=(
                    pipes.CfnPipe.PipeTargetStateMachineParametersProperty(
                        invocation_type="FIRE_AND_FORGET",
                    )
                ),
                # No InputTemplate: the raw SQS message body becomes the state machine input
                # unmodified. §10.2 is explicit that there is no enrichment and no filter here —
                # a message that does not have the shape `LoadCase` expects fails inside the
                # state machine, visibly, in execution history, rather than being rewritten or
                # discarded by the pipe first.
            ),
            # No enrichment, no filter_criteria (design §10.2, explicitly deferred to no
            # implementation): every message is a valid applicant submission attempt, and
            # deciding otherwise is the state machine's job, not the pipe's.
            desired_state="RUNNING",
            description=(
                "SQS FIFO -> Phase 1 state machine, no enrichment, no filter (design §10.2)."
            ),
        )
        self.pipe.node.add_dependency(self.queue)

        cdk.CfnOutput(self, "ApplicantsQueueUrl", value=self.queue.queue_url)
        cdk.CfnOutput(self, "ApplicantsQueueArn", value=self.queue.queue_arn)
        cdk.CfnOutput(self, "ApplicantsDlqUrl", value=self.dlq.queue_url)
        cdk.CfnOutput(self, "IngressPipeName", value=self.pipe.name)
