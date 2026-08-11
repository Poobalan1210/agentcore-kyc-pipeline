"""Deriving tier inputs from what the agents actually returned.

## Why this module exists at all

The Phase 1 state machine used to read `$prescreenResult.matched`, `$documentsResult.confidence`
and `$fraudResult.velocity` directly in JSONata. Those variables are assigned *inside* Parallel
branches and are not visible at the parent scope, so every one of them resolved to nothing and the
decision engine rejected the payload — which failed closed to `DegradedPath`, looking exactly like
a legitimate degraded decision. Fixing the scope alone would not have helped: the branch output is
the model's prose, and picking values out of prose belongs in tested Python, not in a JSONata
expression nobody can unit test.

## This is demo-grade, and the durable fix is different

The agents are supposed to return structured output through their `submit_*` contract tools —
`submit_prescreen`, `submit_document_analysis`, `submit_fraud_assessment`. They are currently
returning plain text instead, so this module reads the text. **That is a weaker mechanism and it
should be replaced by the output contract**, because a screening result recovered by pattern
matching is a screening result that can be silently misread when the model rephrases itself.

Two rules keep that weakness honest:

* **Nothing is ever defaulted to a safe value.** If a signal cannot be established the derivation
  returns `None`, the agent is treated as `UNAVAILABLE`, and the case goes to a human. A missing
  `pre_screen_matched` that quietly became `False` would approve exactly the applicant who should
  be stopped.
* **Ambiguity counts as failure.** If the text supports both readings, that is `None` too. A
  guessed sanctions result is worse than no result, because it carries the same confidence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

#: Phrases that assert a clean screening result. Matched only when no positive-hit phrase is also
#: present, so "no match on name but a possible hit on date of birth" resolves to ambiguous.
_NO_MATCH_PHRASES = (
    "did not find a match", "no match", "no hits", "hitcount: 0", '"hitcount": 0',
    "no sanctions hits", "matched: false", '"matched": false',
)

#: Phrases that assert a hit. Deliberately broad — a false positive here sends a good applicant to
#: a reviewer, while a false negative onboards a sanctioned person.
_MATCH_PHRASES = (
    "match found", "hit found", "matched: true", '"matched": true', "confirmed match",
    "possible match", "probable match", "sanctions hit", "is a match",
)

#: A bare "critical" is NOT here, and its removal is the whole lesson of this module. It matched
#: the Fraud agent's real reply — "there are no elevated or critical signals for device or IP
#: application counts" — as a positive finding, and sent a clean applicant to manual review. The
#: error direction was safe; the reasoning was wrong, and a pattern that fires on the word inside
#: its own negation is not evidence of anything.
_CRITICAL_PHRASES = (
    "velocity breach", "threshold exceeded", "abnormal velocity", "critical signals detected",
    "critical velocity", "velocity is critical", "elevated velocity",
)

#: Negations are listed explicitly and checked as whole phrases, because "no critical signals" and
#: "critical signals" differ by one word and mean opposite things.
_NOT_CRITICAL_PHRASES = (
    "no velocity", "within threshold", "normal velocity", '"critical": false',
    "critical: false", "no elevated or critical", "no critical", "not critical",
    "no critical signals", "no elevated", "no unusual velocity",
)


def _says(text: str, phrases: tuple[str, ...]) -> bool:
    """Whether any phrase appears as whole words.

    Substring matching is wrong here and the failure is silent: `"abnormal velocity detected"`
    contains `"normal velocity"`, so a plain `in` test read a critical finding as a clean one —
    the exact direction of error this module exists to prevent. Word boundaries fix it.
    """
    return any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text) for p in phrases)


def agent_text(result: Any) -> str:
    """All text an agent returned, lowercased, or '' when there is none."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result.lower()
    if isinstance(result, Mapping):
        content = result.get("content") or result.get("Content") or []
        if isinstance(content, str):
            return content.lower()
        parts = []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, Mapping):
                text = block.get("Text") or block.get("text")
                if text:
                    parts.append(str(text))
        return " ".join(parts).lower()
    return ""


def embedded_json(text: str) -> list[dict[str, Any]]:
    """Every JSON object embedded in the text.

    Agents wrap their answer in prose and `<thinking>` blocks, so the JSON has to be found rather
    than parsed from the whole string. Balanced-brace scanning is used instead of a regex because
    the payloads nest.
    """
    found: list[dict[str, Any]] = []
    depth, start = 0, None
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    found.append(parsed)
                start = None
    return found


def _deep_get(objects: list[dict[str, Any]], *keys: str) -> Any:
    """First value found for any of `keys`, searched depth-first through nested dicts."""
    wanted = {k.lower() for k in keys}

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).lower() in wanted and not isinstance(value, (dict, list)):
                    return value
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    for obj in objects:
        found = walk(obj)
        if found is not None:
            return found
    return None


def document_confidence(result: Any) -> float | None:
    """Document confidence in 0..1, or None if it cannot be established."""
    text = agent_text(result)
    if not text:
        return None

    value = _deep_get(embedded_json(text), "confidence", "documentconfidence",
                      "overallconfidence")
    if value is None:
        # Fall back to a labelled number in prose. Anchored on the word so a stray decimal
        # elsewhere in the reply cannot be mistaken for a confidence score.
        match = re.search(r"confidence[\"']?\s*[:=]\s*([0-9]*\.?[0-9]+)", text)
        value = match.group(1) if match else None
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0:
        # Some replies express it as a percentage. 96 and 0.96 are the same claim; 96.0 is not a
        # valid confidence, so normalising is safer than rejecting.
        number = number / 100.0
    return number if 0.0 <= number <= 1.0 else None


def pre_screen_matched(result: Any) -> bool | None:
    """Whether the exact-match pre-screen found a hit. None when ambiguous or absent.

    Ambiguity resolves to None rather than to `True` or `False`. `False` would approve on a guess;
    `True` would reject on one. Neither is defensible, so a human decides.
    """
    text = agent_text(result)
    if not text:
        return None

    value = _deep_get(embedded_json(text), "matched", "prescreenmatched", "hasmatch")
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"

    count = _deep_get(embedded_json(text), "hitcount", "hits")
    if isinstance(count, (int, float)):
        return count > 0

    says_match = _says(text, _MATCH_PHRASES)
    says_clean = _says(text, _NO_MATCH_PHRASES)
    if says_match and not says_clean:
        return True
    if says_clean and not says_match:
        return False
    return None


def velocity_critical(result: Any) -> bool | None:
    """Whether velocity signals are critical. None when it cannot be established."""
    text = agent_text(result)
    if not text:
        return None

    value = _deep_get(embedded_json(text), "critical", "velocitycritical")
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"

    says_clean = _says(text, _NOT_CRITICAL_PHRASES)
    says_critical = _says(text, _CRITICAL_PHRASES)
    if says_clean and not says_critical:
        return False
    if says_critical and not says_clean:
        return True
    return None


def from_findings(findings: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Read the tier inputs from the agents' structured `submit_*` findings (R11.13).

    This is the authoritative path and the prose derivation below is the fallback. A finding here
    was schema-validated by the service before it was persisted, so its shape is not the model's
    choice — which is what makes the outcome reproducible in the sense R3.5 requires.

    `findings` arrives as DynamoDB-shaped values: `{"documents": {"S": "{...json...}"}}`.

    `determined: false` is honoured as a first-class answer. An agent that could not complete its
    checks says so, and that becomes UNAVAILABLE rather than a clean result — which is the whole
    reason the field exists.
    """
    signals: dict[str, Any] = {}
    undetermined: list[str] = []

    parsed: dict[str, dict[str, Any]] = {}
    for agent in ("documents", "prescreen", "fraud"):
        raw = findings.get(agent)
        # Accepts both the DynamoDB-typed shape `{"S": "..."}` and a plain JSON string, because the
        # state machine unwraps `.S` itself while a direct caller or a test passes the string.
        if isinstance(raw, Mapping):
            raw = raw.get("S", raw)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if isinstance(raw, Mapping) and raw.get("determined") is not False:
            parsed[agent] = dict(raw)
        else:
            undetermined.append(agent)

    doc = parsed.get("documents")
    if doc is not None and isinstance(doc.get("confidence"), (int, float)):
        signals["documentConfidence"] = float(doc["confidence"])
    elif "documents" not in undetermined:
        undetermined.append("documents")

    pre = parsed.get("prescreen")
    if pre is not None and isinstance(pre.get("matched"), bool):
        signals["preScreenMatched"] = pre["matched"]
    elif "prescreen" not in undetermined:
        undetermined.append("prescreen")

    fraud = parsed.get("fraud")
    if fraud is not None and isinstance(fraud.get("velocityCritical"), bool):
        signals["velocitySignals"] = {
            "critical": fraud["velocityCritical"],
            # A list, not a tuple: this crosses the handler's own parser, which validates
            # `detail` as a JSON array. A tuple looks equivalent in Python and is rejected there.
            "detail": [str(d) for d in (fraud.get("velocityDetail") or [])],
        }
    elif "fraud" not in undetermined:
        undetermined.append("fraud")

    return signals, tuple(undetermined)


def derive(agent_results: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Derive the tier inputs, and name every agent whose signal could not be established.

    Returns `(signals, undetermined)`. The caller must treat a non-empty `undetermined` as
    `UNAVAILABLE` for those agents so the case routes to review — never as a usable default.
    """
    signals: dict[str, Any] = {}
    undetermined: list[str] = []

    confidence = document_confidence(agent_results.get("documents"))
    if confidence is None:
        undetermined.append("documents")
    else:
        signals["documentConfidence"] = confidence

    matched = pre_screen_matched(agent_results.get("prescreen"))
    if matched is None:
        undetermined.append("prescreen")
    else:
        signals["preScreenMatched"] = matched

    critical = velocity_critical(agent_results.get("fraud"))
    if critical is None:
        undetermined.append("fraud")
    else:
        signals["velocitySignals"] = {"critical": critical}

    return signals, tuple(undetermined)
