"""Span builders shared by the audit tests.

Kept out of the test modules so both suites assert against one definition of what a well-formed
span looks like. A drift between two hand-rolled span shapes would let a classification bug pass
in one file and fail in the other.
"""

from __future__ import annotations

from typing import Any


def agent_span(
    name: str = "DocumentIntelligence",
    *,
    phase: str = "phase1",
    start: int = 100,
    **attrs: Any,
) -> dict[str, Any]:
    return {
        "name": "harness.invoke",
        "spanId": f"span-agent-{name}",
        "traceId": "trace-1",
        "startTimeUnixNano": start,
        "status": {"code": "OK"},
        "attributes": {
            "gen_ai.agent.name": name,
            "kyc.phase": phase,
            "gen_ai.request.model": "amazon.nova-2-lite-v1:0",
            "kyc.harness.version": "3",
            "kyc.endpoint.name": "prod",
            **attrs,
        },
    }


def tool_span(
    name: str = "sanctions_screen",
    *,
    start: int = 200,
    args: Any = '{"name":"A"}',
    result: Any = '{"hits":[]}',
) -> dict[str, Any]:
    return {
        "name": "gateway.tool",
        "spanId": f"span-tool-{name}",
        "parentSpanId": "span-agent-DocumentIntelligence",
        "startTimeUnixNano": start,
        "status": {"code": "OK"},
        "attributes": {
            "gen_ai.tool.name": name,
            "gen_ai.tool.arguments": args,
            "gen_ai.tool.result": result,
            "kyc.gateway.arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/g-1",
        },
    }


def decision_span(
    *,
    outcome: str = "PROVISIONAL_APPROVE_RESTRICTED",
    rule: str = "O1.provisional_approve",
) -> dict[str, Any]:
    return {
        "name": "decision.emit",
        "spanId": "span-decision",
        "startTimeUnixNano": 900,
        "attributes": {
            "kyc.decision.outcome": outcome,
            "kyc.decision.rule": rule,
            "kyc.decision.tier": "LOW",
        },
    }


def complete_spans() -> list[dict[str, Any]]:
    """One of each required kind — the minimum set that satisfies R18.1."""
    return [agent_span(), tool_span(), decision_span()]


#: The exact shape Step Functions returns (F-13, confirmed in docs/MEASUREMENTS.md): one final
#: assistant message, tool-use and reasoning blocks absent.
SFN_OUTPUT: dict[str, Any] = {
    "Output": {"Message": {"Role": "assistant", "Content": [
        {"Text": "Document verified, no sanctions hits found."}]}},
    "StopReason": "end_turn",
}
