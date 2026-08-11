"""Turning raw agent output into a usable finding, or admitting it is not one — task 6.1.

This module exists because of one observed anomaly, and it is the most safety-relevant code in
the package.

A harness invocation completed with status `SUCCEEDED` and returned `Content: []`. Nothing
failed. No exception, no error field, no throttle. Read naively, an empty screening result means
"no sanctions hits were found" — and so the applicant who should have been stopped is the one
who gets approved fastest. The failure is silent, it favours the attacker, and it looks exactly
like the happy path in every log and metric.

So the rule is: **absence of findings is never evidence of absence of risk.** An empty or
unparseable agent result is `UNAVAILABLE`, which routes to a human. It is never a clean pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from models import ResultStatus

#: Error markers a tool may return in-band. `IMAGE_UNREADABLE` is deliberately absent — see
#: `INVALID_MARKERS`.
UNAVAILABLE_MARKERS = frozenset({
    "UNAVAILABLE",
    "SOURCE_UNAVAILABLE",
    "TIMEOUT",
    "THROTTLED",
    "MISSING_TOOL_NAME",  # F-80: the Gateway client-context bug. A tool never ran.
    # The agent replied but its signal could not be read out of the reply (see `signals.py`).
    # UNAVAILABLE, not INVALID: the applicant cannot fix our inability to parse a model's prose,
    # so this must reach a reviewer rather than become a step-up demand they cannot satisfy.
    "SIGNAL_NOT_DETERMINED",
})

#: Permanent, applicant-fixable defects. These are NOT retryable, which is the whole distinction:
#: `UNAVAILABLE` means "ask again later", `INVALID` means "this input can never work".
#: `IMAGE_UNREADABLE` belongs here — a missing or illegible image will not become legible on
#: retry, and the applicant is the only party who can resolve it.
INVALID_MARKERS = frozenset({
    "IMAGE_UNREADABLE",
    "DOCUMENT_UNSUPPORTED",
    "SCHEMA_VIOLATION",
})


def agent_result_status(raw: Any) -> ResultStatus:
    """Classify a raw agent result before any rule reads its content.

    Returns `UNAVAILABLE` for anything empty, absent, or structurally unusable. That is the
    fail-closed default and it is intentional: the cost of a false `UNAVAILABLE` is a human
    glancing at a good application, and the cost of a false `OK` is onboarding a sanctioned
    person.
    """
    if raw is None:
        return ResultStatus.UNAVAILABLE

    # The observed anomaly: SUCCEEDED with Content: []. An empty container is not a finding.
    if isinstance(raw, (str, bytes)) and not raw.strip():
        return ResultStatus.UNAVAILABLE
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) == 0:
        return ResultStatus.UNAVAILABLE
    if isinstance(raw, Mapping) and not raw:
        return ResultStatus.UNAVAILABLE

    if isinstance(raw, Mapping):
        content = raw.get("content", raw.get("Content"))
        if content is not None and _is_empty(content):
            return ResultStatus.UNAVAILABLE

        marker = _marker_of(raw)
        if marker in INVALID_MARKERS:
            return ResultStatus.INVALID
        if marker in UNAVAILABLE_MARKERS:
            return ResultStatus.UNAVAILABLE
        # An unrecognised error marker is still an error. Defaulting to OK here would let any
        # future error code the tools introduce read as a clean pass.
        if marker is not None:
            return ResultStatus.UNAVAILABLE

        status = raw.get("status", raw.get("Status"))
        if isinstance(status, str):
            upper = status.upper()
            if upper in INVALID_MARKERS:
                return ResultStatus.INVALID
            if upper in UNAVAILABLE_MARKERS or upper in {"FAILED", "ERROR"}:
                return ResultStatus.UNAVAILABLE

    return ResultStatus.OK


def all_results_usable(results: Mapping[str, Any]) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Classify a whole set of agent results.

    Returns `(usable, unavailable_agents, invalid_agents)`. The two failure lists are kept apart
    because they route differently: `INVALID` is the applicant's to fix and becomes a `STEP_UP`,
    while `UNAVAILABLE` is ours and must never be turned into a demand the applicant cannot
    satisfy — it becomes `MANUAL_REVIEW`.
    """
    unavailable: list[str] = []
    invalid: list[str] = []
    for agent, raw in results.items():
        status = agent_result_status(raw)
        if status is ResultStatus.UNAVAILABLE:
            unavailable.append(agent)
        elif status is ResultStatus.INVALID:
            invalid.append(agent)
    return (not unavailable and not invalid, tuple(sorted(unavailable)), tuple(sorted(invalid)))


def _is_empty(content: Any) -> bool:
    if content is None:
        return True
    if isinstance(content, (str, bytes)):
        return not content.strip()
    if isinstance(content, (Sequence, Mapping)):
        return len(content) == 0
    return False


def _marker_of(raw: Mapping[str, Any]) -> str | None:
    for key in ("error", "Error", "errorCode", "error_code", "reason"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
        if isinstance(v, Mapping):
            inner = v.get("code") or v.get("Code")
            if isinstance(inner, str) and inner.strip():
                return inner.strip().upper()
    return None
