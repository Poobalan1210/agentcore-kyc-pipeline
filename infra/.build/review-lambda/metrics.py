"""Review queue depth and age as CloudWatch metrics — task 6.5, R6.6, R6.7.

R6.7 is the reason this module exists in a shape worth testing: the review queue is load-bearing,
sized against a 90–95% false-positive rate (B-08, A-08), not an exception path. A queue treated as
capacity needs depth *and* age, and the two answer different questions — depth says how much work
is outstanding, age says how long the oldest applicant has been waiting, which is the number an SLA
is written against. A short queue holding one three-hour-old case is the failure that depth alone
cannot see.

Metrics are derived from the **case records** in `AWAITING_REVIEW` (the `state` GSI, design §9.1),
not from SQS. SQS already emits `ApproximateNumberOfMessagesVisible` and
`ApproximateAgeOfOldestMessage` for free, so re-publishing those would add nothing; what SQS cannot
see is the business queue — a case whose `waitForTaskToken` message was consumed by a reviewer who
then abandoned the form is invisible to SQS and still waiting. `sqs_depth()` is provided for the
cross-check, because a large divergence between the two means messages and cases have drifted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from decisions import QueuedCase, queue_ages

#: Custom namespace. AWS-reserved `AWS/*` namespaces cannot be written to, and keeping every custom
#: metric from this system under one namespace is what lets a single dashboard query them.
NAMESPACE = "KYC90/Review"

_SECONDS = "Seconds"
_COUNT = "Count"


@dataclass(frozen=True, kw_only=True)
class QueueStatistics:
    """Everything R6.6 asks for, computed without a client so it can be asserted directly."""

    depth: int
    oldest_age_seconds: float
    mean_age_seconds: float
    #: Cases past the SLA threshold. Published separately because an alarm on a mean is useless —
    #: a mean stays healthy while a small tail starves, and the tail is what breaches.
    breaching_count: int
    sla_seconds: float


def queue_statistics(
    cases: Sequence[QueuedCase], *, now: datetime, sla_seconds: float = 3600.0
) -> QueueStatistics:
    """Depth and age from the cases currently awaiting review.

    An empty queue reports zeros rather than no data. Absent data cannot be alarmed on without
    `treatMissingData`, and "the metric stopped arriving" and "the queue is empty" are opposite
    situations that must not look identical on the dashboard.
    """
    ages = queue_ages(cases, now=now)
    return QueueStatistics(
        depth=len(cases),
        oldest_age_seconds=ages[0] if ages else 0.0,
        mean_age_seconds=(sum(ages) / len(ages)) if ages else 0.0,
        breaching_count=sum(1 for age in ages if age > sla_seconds),
        sla_seconds=sla_seconds,
    )


def metric_data(
    stats: QueueStatistics, *, timestamp: datetime, jurisdiction: str | None = None
) -> list[dict[str, Any]]:
    """CloudWatch `MetricDatum` list. Pure, so the exact payload is testable.

    Each metric is emitted twice when a jurisdiction is given: once undimensioned and once with the
    dimension. CloudWatch does not aggregate across dimensions, so a dimensioned-only metric makes
    "total cases waiting" unqueryable — and R12 keeps jurisdictions on separate rulesets, so the
    per-jurisdiction breakdown is the one an operator actually acts on.
    """
    if timestamp.tzinfo is None:
        raise ValueError("metric timestamps must be timezone-aware")

    base: list[tuple[str, float, str]] = [
        ("ReviewQueueDepth", float(stats.depth), _COUNT),
        ("ReviewQueueOldestAgeSeconds", stats.oldest_age_seconds, _SECONDS),
        ("ReviewQueueMeanAgeSeconds", stats.mean_age_seconds, _SECONDS),
        ("ReviewQueueBreachingCount", float(stats.breaching_count), _COUNT),
    ]
    dimension_sets: list[list[dict[str, str]]] = [[]]
    if jurisdiction:
        dimension_sets.append([{"Name": "Jurisdiction", "Value": jurisdiction}])

    return [
        {
            "MetricName": name,
            "Value": value,
            "Unit": unit,
            "Timestamp": timestamp,
            "Dimensions": dimensions,
        }
        for dimensions in dimension_sets
        for name, value, unit in base
    ]


def emit_queue_metrics(
    stats: QueueStatistics,
    *,
    timestamp: datetime,
    jurisdiction: str | None = None,
    namespace: str = NAMESPACE,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Publish, returning what was published so the caller can log it.

    `client` is injected for the same reason as everywhere else in this repo: an offline test must
    exercise the real payload, not a mock of the function that builds it.
    """
    import boto3  # local import — the pure paths above must stay importable without boto3

    cloudwatch = client or boto3.client("cloudwatch")
    data = metric_data(stats, timestamp=timestamp, jurisdiction=jurisdiction)
    # PutMetricData caps at 1,000 datums per call; chunking now costs nothing and removes a failure
    # mode that only appears once jurisdictions multiply.
    for start in range(0, len(data), 1000):
        cloudwatch.put_metric_data(Namespace=namespace, MetricData=data[start:start + 1000])
    return data


def sqs_depth(*, queue_url: str, client: Any = None) -> dict[str, int]:
    """SQS's own view, for the drift cross-check described in the module docstring."""
    import boto3

    sqs = client or boto3.client("sqs")
    response = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )
    attributes: Mapping[str, Any] = response.get("Attributes", {})
    return {
        "visible": int(attributes.get("ApproximateNumberOfMessages", 0)),
        "inFlight": int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0)),
    }
