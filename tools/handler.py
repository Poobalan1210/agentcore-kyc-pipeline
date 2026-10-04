"""Single Lambda, six evidence tools, name-based dispatch — task 2.1.

Ported from P-10, which runs 17 tools this way behind a Gateway with SigV4. One function to
operate and one place for logging, retries and redaction; the Gateway is what makes them look
like separate tools to the agent, and Cedar is what authorises them individually.

Why one function rather than six: six functions means six cold-start profiles, six
provisioned-concurrency budgets and six places for the redaction logic to drift. The
counter-argument is blast radius, and that is answered at the Gateway and Cedar layer where
authorisation happens per tool anyway (R14.2).

The handler never raises. Any exception becomes an UNAVAILABLE envelope, because a Lambda error
is ambiguous at the agent boundary — it cannot be distinguished from a negative finding — while
UNAVAILABLE is explicit (R25.7).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from kyctools.contract import Status, ToolError, unavailable, invalid
from kyctools.redact import get_logger, log
from kyctools.tools_impl import SCREENING_TOOLS, TOOLS

LOG = get_logger("kyctools.handler")


def _tool_from_context(context: Any) -> str | None:
    """AgentCore Gateway passes the tool name in the Lambda **client context**, not the event.

    The event body is the tool's arguments and nothing else, so a dispatcher that only reads the
    event sees no tool name and rejects every Gateway call. The symptom is misleading: the tool
    returns `MISSING_TOOL_NAME`, the agent reads it as an unavailable source, and the pipeline
    correctly fails closed — so nothing looks broken except that no tool ever runs (F-80).
    """
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    for key in ("bedrockAgentCoreToolName", "bedrockagentcoreToolName",
                "bedrock-agentcore-tool-name", "toolName"):
        value = custom.get(key)
        if value:
            # Gateway target names are namespaced as "Target___tool"; keep the tool half.
            return value.split("___")[-1]
    return None


def _extract(event: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Find the tool name and arguments.

    Direct invocation, MCP `tools/call` and an API Gateway proxy each shape the event
    differently, so all three are accepted rather than coupling the handler to one caller. The
    Gateway path supplies the name out of band — see `_tool_from_context`.
    """
    # AgentCore Gateway MCP-style
    if "toolName" in event:
        return event["toolName"], event.get("arguments") or event.get("input") or {}
    if "tool" in event:
        return event["tool"], event.get("arguments") or event.get("input") or {}
    # MCP tools/call
    params = event.get("params") or {}
    if "name" in params:
        return params["name"], params.get("arguments") or {}
    # Lambda function URL / API Gateway proxy
    if "body" in event:
        try:
            body = json.loads(event["body"]) if isinstance(event["body"], str) \
                else event["body"]
        except (TypeError, ValueError):
            return None, {}
        return _extract(body or {})
    return None, {}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    started = time.perf_counter()
    tool_name, args = _extract(event or {})

    # When the Gateway is the caller the event holds only the arguments, so fall back to the
    # client context for the name. Order matters: an explicit name in the event wins, which
    # keeps direct invocation and the tests working unchanged.
    if not tool_name:
        tool_name = _tool_from_context(context)
        if tool_name and not args:
            args = {k: v for k, v in (event or {}).items()
                    if k not in ("toolName", "tool", "params", "body")}

    ctx = {
        "requestId": getattr(context, "aws_request_id", None),
        "remainingMs": (context.get_remaining_time_in_millis()
                        if context and hasattr(context, "get_remaining_time_in_millis")
                        else None),
    }

    if not tool_name:
        result = invalid("dispatch", "no tool name in event", "MISSING_TOOL_NAME")
        log(LOG, logging.WARNING, "dispatch failed", status=result.status.value)
        return result.to_dict()

    fn = TOOLS.get(tool_name)
    if fn is None:
        # An unknown tool name is a configuration error worth shouting about: it usually means
        # a Gateway target and this dispatch table have drifted apart.
        result = invalid(tool_name, "unknown tool", "UNKNOWN_TOOL")
        log(LOG, logging.ERROR, "unknown tool requested",
            tool=tool_name, status=result.status.value)
        return result.to_dict()

    log(LOG, logging.INFO, "tool invoked", tool=tool_name, **{
        k: v for k, v in ctx.items() if v is not None})

    try:
        result = fn(args, ctx)
    except ToolError as e:
        result = unavailable(tool_name, e.reason, e.reason_code)
    except Exception as e:  # noqa: BLE001 — deliberate catch-all, see module docstring
        result = unavailable(tool_name, "unhandled error: %s" % type(e).__name__,
                             "UNHANDLED_ERROR")
        log(LOG, logging.ERROR, "tool raised", tool=tool_name, exc_info=True)

    result.latency_ms = round((time.perf_counter() - started) * 1000, 1)

    # The invariant that matters: a screening tool must never report a clean result it did not
    # establish. If this ever fires, something has converted UNAVAILABLE into "no match".
    if tool_name in SCREENING_TOOLS and result.status is Status.OK:
        assert "matched" in result.data or "pepStatus" in result.data, (
            "screening tool %s returned OK without a verdict field" % tool_name)

    log(LOG, logging.INFO, "tool completed",
        tool=tool_name, status=result.status.value, latency_ms=result.latency_ms,
        reason_code=result.reason_code)
    return result.to_dict()


# Local smoke run: python handler.py
#
# Every evidence tool reads the case record, so the smoke run exercises the pure matcher directly.
if __name__ == "__main__":
    import pprint

    from kyctools.data.sanctions import ALL_ENTRIES
    from kyctools.matching import Candidate, screen

    for name, dob, country in (
        ("Dmitri Volkov", "1968-04-12", "RU"),
        ("Dmitri Volkov", "1994-10-05", "UA"),
        ("Mohammed Al-Rashid", "1979-03-11", "SY"),
        ("Avery Quinn Hale", "1990-11-06", "US"),
    ):
        alerts, exclusions = screen(Candidate(name=name, dob=dob, country=country), ALL_ENTRIES)
        print("\n=== %s (%s)" % (name, dob))
        pprint.pp({"alerts": [h.to_dict() for h in alerts],
                   "excluded": [h.to_dict() for h in exclusions]})
