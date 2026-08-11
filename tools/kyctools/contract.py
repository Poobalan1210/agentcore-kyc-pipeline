"""Shared tool contract.

Every tool returns the same envelope. The important field is `status`, and the important
value is `UNAVAILABLE`.

R25.7 and AC-12: a screening tool that cannot reach its source must say so. It must never
return "no match", because "no match" and "could not check" produce opposite compliance
outcomes and an agent cannot tell them apart from a bare empty list. Fail closed means the
absence of a match is only meaningful when the check actually ran.

Phase 0 also surfaced a case that motivates `EMPTY`: a model invocation SUCCEEDED but returned
an empty content array. A success with no answer is not a finding of "nothing found", and the
decision engine must not read it as one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Status(str, Enum):
    OK = "OK"
    #: The source could not be reached, timed out, or returned something unusable. The caller
    #: MUST NOT infer "no match" from this.
    UNAVAILABLE = "UNAVAILABLE"
    #: The call succeeded but produced no usable payload. Distinct from OK with zero hits.
    EMPTY = "EMPTY"
    #: The request was malformed or violated a precondition. Not retryable.
    INVALID = "INVALID"


class HitClassification(str, Enum):
    """R27.5. PROBABLE and POSSIBLE both route to MANUAL_REVIEW; only CONFIRMED is decisive."""

    CONFIRMED = "CONFIRMED"
    PROBABLE = "PROBABLE"
    POSSIBLE = "POSSIBLE"
    NO_MATCH = "NO_MATCH"


@dataclass
class ToolResult:
    status: Status
    tool: str
    #: Payload. Only meaningful when status is OK.
    data: dict[str, Any] = field(default_factory=dict)
    #: Populated for UNAVAILABLE and INVALID. Free text for humans, never for branching.
    reason: str | None = None
    #: Machine-readable reason code, safe to branch on.
    reason_code: str | None = None
    #: R18.1 provenance: which list, which version, retrieved when.
    source: str | None = None
    list_version: str | None = None
    retrieved_at: str | None = None
    latency_ms: float | None = None

    @property
    def usable(self) -> bool:
        """True only when the result can be reasoned about. Callers should branch on this
        rather than on `status == OK`, so a new non-usable status cannot silently pass."""
        return self.status is Status.OK

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return {k: v for k, v in d.items() if v is not None}


def ok(tool: str, data: dict[str, Any], **prov: Any) -> ToolResult:
    return ToolResult(status=Status.OK, tool=tool, data=data,
                      retrieved_at=_now(), **prov)


def unavailable(tool: str, reason: str, reason_code: str = "SOURCE_UNAVAILABLE",
                **prov: Any) -> ToolResult:
    """The only correct response when a screening source cannot be consulted."""
    return ToolResult(status=Status.UNAVAILABLE, tool=tool, reason=reason,
                      reason_code=reason_code, retrieved_at=_now(), **prov)


def invalid(tool: str, reason: str, reason_code: str = "INVALID_REQUEST") -> ToolResult:
    return ToolResult(status=Status.INVALID, tool=tool, reason=reason,
                      reason_code=reason_code, retrieved_at=_now())


def empty(tool: str, reason: str = "source returned no usable payload") -> ToolResult:
    return ToolResult(status=Status.EMPTY, tool=tool, reason=reason,
                      reason_code="EMPTY_PAYLOAD", retrieved_at=_now())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ToolError(Exception):
    """Raised inside a tool to force an UNAVAILABLE envelope rather than a stack trace.

    Tools must not raise bare exceptions. An unhandled exception becomes a Lambda error, and a
    Lambda error is ambiguous at the agent boundary — it could be a transport failure or a
    genuine negative. UNAVAILABLE is unambiguous.
    """

    def __init__(self, reason: str, reason_code: str = "SOURCE_UNAVAILABLE") -> None:
        super().__init__(reason)
        self.reason = reason
        self.reason_code = reason_code
