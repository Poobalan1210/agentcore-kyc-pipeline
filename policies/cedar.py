"""Cedar policy text generation — task 8.1-8.4.

Pure functions from (jurisdiction, agent, gateway ARN, harness role ARN) to Cedar policy
*statements*. Deliberately has no AWS SDK calls and no CDK imports: the whole reason this module
exists separately from `generate.py` is that a policy's *text* should be testable without a live
Gateway ARN, and building it from real captured ARNs is a second, thin step.

Three things here were confirmed live against the real `CreatePolicy` API, not assumed from the
docs — each cost a rejected request to find:

1. **One `AWS::BedrockAgentCore::Policy` resource holds exactly one Cedar statement.** A resource
   whose `Statement` was the concatenation of several `permit(...)` blocks was rejected with
   `unexpected token 'permit'` (the parser stops after the first `;`), while the identical text
   split one statement per resource succeeded. So every function here returns a
   `list[Statement]`, one Cedar statement each, rather than one multi-statement blob.
2. **A guardrail statement (`when guardrails { ... }`) must be submitted under
   `Definition.Policy.Statement`, not `Definition.Cedar.Statement`.** The two look identical —
   both are plain Cedar/Dogwood text — but the API only accepts a guardrails block through the
   `Policy` field; submitting it as `Cedar` fails the *parser*, not schema validation, with
   `unexpected token 'guardrails'`, which reads like a syntax error in the statement rather than
   a wrong field in the request. Every statement kind is generated the same way in `all_statements`,
   so `generate.py`/`policies_stack.py` route by `Statement.kind` rather than assuming one field.
3. **`context.input`/`context.output` alone are rejected; a guardrail dataPath needs at least one
   field segment** — `context.input.<field>`, never the bare object — with the error
   `"Dogwood data path missing field segment"`. The field name itself is not validated against
   the action's actual schema at policy-creation time (an unscoped `action` with a field no tool
   declares still creates successfully), so the value chosen here documents *intent* — which
   argument the guardrail is meant to scan — rather than a value the service enforces.

Four kinds of policy, per jurisdiction (R14.2, R15.1-R15.3):

1. **Per-agent authorisation** — one `permit` statement per (agent, tool) pair the agent is
   allowed to call, scoped to `principal == AgentCore::IamEntity::"<harness role ARN>"`
   (R14.2, R14.3). Everything unlisted is denied by default (F-18) — there is no need for an
   explicit `forbid`.
2. **Prompt-injection** — one `forbid ... when guardrails` statement (R15.1). Cannot share a
   *policy resource* with the authorisation `permit`s: standard `when` blocks cannot mix with
   `guardrails` blocks in one Cedar statement (F-16), and each statement is its own resource
   regardless.
3. **PII suppression** — one `suppressOutput ... when guardrails` statement per suppressed
   category (R15.2).
4. **Content filter** — one `forbid ... when guardrails` statement per filtered category
   (R15.3).

All guardrail thresholds are drawn from the discrete set {0, 0.2, 0.4, 0.6, 0.8, 1.0} (F-16,
R15.4) — `_threshold` below is a closed enum, not a free float, specifically so a typo like
`0.45` fails at generation time rather than silently rounding to a value the service accepts but
nobody chose.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from tool_schemas import agent_tool_names, cedar_action  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from kyc.config import AGENT_BY_KEY, JURISDICTIONS  # noqa: E402

_VALID_THRESHOLDS = frozenset({Decimal("0"), Decimal("0.2"), Decimal("0.4"), Decimal("0.6"),
                               Decimal("0.8"), Decimal("1.0")})


def _threshold(value: str) -> str:
    try:
        parsed = Decimal(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{value!r} is not a decimal") from exc
    if parsed not in _VALID_THRESHOLDS:
        raise ValueError(
            f"{value!r} is not a valid guardrail threshold (F-16) — must be one of "
            f"{sorted(str(t) for t in _VALID_THRESHOLDS)}")
    # One canonical spelling regardless of input form ("1", "1.0", "1.00" all match), matching
    # what the docs show: `decimal("0.4")`.
    return format(parsed, "f") if parsed != parsed.to_integral_value() else f"{int(parsed)}.0"


@dataclass(frozen=True, kw_only=True)
class Statement:
    """One Cedar statement, one `AWS::BedrockAgentCore::Policy` resource.

    `name` is a stable, deterministic identifier derived from the statement's content (agent +
    tool, or category), never an incrementing index — CDK/CloudFormation needs a stable logical
    ID across re-generations, and an index would silently reassign statement N's identity to a
    different statement the moment an agent's tool list changes order.
    """

    name: str
    cedar: str
    kind: str  # "authorization" | "prompt_injection" | "pii_suppression" | "content_filter"


def _harness_entity(role_arn: str) -> str:
    """`AgentCore::IamEntity` principal ID for a harness role — the *assumed-role* ARN form
    (`policy-conditions.html`), not the role ARN itself. AgentCore's own IAM entity resolves to
    `arn:aws:sts::<account>:assumed-role/<role-name>` regardless of which specific role ARN form
    was granted, so policies are written against that shape rather than the `iam::role/` ARN a
    CDK role object reports.
    """
    # role_arn: arn:aws:iam::<account>:role/<role-name>
    _, _, _, _, account, role_part = role_arn.split(":", 5)
    role_name = role_part.split("/", 1)[1]
    return f"arn:aws:sts::{account}:assumed-role/{role_name}"


def authorization_statements(*, gateway_arn: str,
                             harness_roles: dict[str, str]) -> list[Statement]:
    """One `permit` statement per (agent, tool) the agent is allowed to call (R14.2, R14.3,
    F-35).

    `harness_roles` maps agent key -> role ARN (the harness execution role, D-01), covering
    every Phase 1 and Phase 2 agent that has tools. Router and Notify have none (design §5.1's
    tool list), so they get no statement here and therefore cannot call anything through this
    Gateway — which is correct: they never need to.
    """
    statements: list[Statement] = []
    for agent_key, role_arn in sorted(harness_roles.items()):
        agent = AGENT_BY_KEY[agent_key]
        if not agent.tools:
            continue
        entity = _harness_entity(role_arn)
        for real_tool in agent_tool_names(agent.tools):
            action = cedar_action(real_tool)
            cedar = (
                f'permit(\n'
                f'  principal == AgentCore::IamEntity::"{entity}",\n'
                f'  action == AgentCore::Action::"{action}",\n'
                f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
                f');'
            )
            statements.append(Statement(
                name=(f"Authorize{agent_key.capitalize()}"
                     f"{real_tool.title().replace('_', '')}"),
                cedar=cedar, kind="authorization"))
    if not statements:
        raise ValueError("no permit statements generated — check harness_roles/agent.tools")
    return statements


def prompt_injection_statements(*, gateway_arn: str,
                                threshold: str = "0.4") -> list[Statement]:
    """R15.1, F-16. One statement — `forbid ... when guardrails`, never mixed with a plain
    `when` authorisation statement in the same policy resource. Scans `context.input.name` —
    the field every screening tool (sanctions/PEP/adverse-media) carries the applicant-supplied
    free-text name through, and the field an injection attempt is most plausibly smuggled in on
    across this tool set, none of which take a generic `prompt`/`message` field. A bare
    `context.input` (no field) is rejected live with "Dogwood data path missing field
    segment"."""
    t = _threshold(threshold)
    cedar = (
        f'forbid(\n'
        f'  principal,\n'
        f'  action,\n'
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        f')\n'
        f'when guardrails {{\n'
        f'  BedrockGuardrails::PromptAttack(["PROMPT_INJECTION"], [context.input.name])\n'
        f'    ["PROMPT_INJECTION"]\n'
        f'    .confidenceScore\n'
        f'    .greaterThan(decimal("{t}"))\n'
        f'}};'
    )
    return [Statement(name=f"ForbidPromptInjection", cedar=cedar,
                     kind="prompt_injection")]


def pii_suppression_statements(*, gateway_arn: str,
                               categories: tuple[str, ...] = (
                                   "US_SOCIAL_SECURITY_NUMBER", "CREDIT_DEBIT_CARD_NUMBER"),
                               threshold: str = "0.2") -> list[Statement]:
    """R15.2. One `suppressOutput` statement per category.

    `SensitiveInformation` does **not** support the per-category `["CATEGORY"].confidenceScore`
    indexing the docs' `ContentFilter`/`PromptAttack` examples show — confirmed live: it fails
    with "SensitiveInformation guardrail requires an aggregation method call
    (count/categories/maxConfidenceScore/minConfidenceScore); per-category indexing is not
    supported for SensitiveInformation." `.maxConfidenceScore()` chained straight off the
    guardrail call (no line break, no index) is the form the service accepts.
    """
    t = _threshold(threshold)
    statements: list[Statement] = []
    for category in categories:
        # `context.output.result` documents intent (scan the tool's return value) rather than
        # a value the service enforces — none of the seven tools declare an output schema, and
        # an unscoped action's guardrail field is not checked against any schema at creation
        # time (confirmed live: an unscoped action with an arbitrary field name still creates).
        cedar = (
            f'suppressOutput(\n'
            f'  principal,\n'
            f'  action,\n'
            f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
            f')\n'
            f'when guardrails {{\n'
            f'  BedrockGuardrails::SensitiveInformation(["{category}"], [context.output.result])'
            f'.maxConfidenceScore()\n'
            f'    .greaterThan(decimal("{t}"))\n'
            f'}};'
        )
        statements.append(Statement(
            name=f"Suppress{category.title().replace('_', '')}",
            cedar=cedar, kind="pii_suppression"))
    return statements


def content_filter_statements(*, gateway_arn: str,
                              categories: tuple[str, ...] = ("VIOLENCE", "HATE"),
                              threshold: str = "0.2") -> list[Statement]:
    """R15.3. One `forbid` statement per filtered category — blocks the call before a tool
    runs, rather than suppressing a result after the fact, since violent/hateful *input* has no
    legitimate completion to suppress down to. `context.input.prompt` for the same reason as
    `prompt_injection_statements` — a bare `context.input` is rejected."""
    t = _threshold(threshold)
    statements: list[Statement] = []
    for category in categories:
        cedar = (
            f'forbid(\n'
            f'  principal,\n'
            f'  action,\n'
            f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
            f')\n'
            f'when guardrails {{\n'
            f'  BedrockGuardrails::ContentFilter(["{category}"], [context.input.name])\n'
            f'    ["{category}"]\n'
            f'    .confidenceScore\n'
            f'    .greaterThan(decimal("{t}"))\n'
            f'}};'
        )
        statements.append(Statement(
            name=f"ForbidContent{category.title()}", cedar=cedar,
            kind="content_filter"))
    return statements


def all_statements(*, gateway_arn: str, harness_roles: dict[str, str]) -> list[Statement]:
    """Every statement for one jurisdiction's Gateway — the full set task 8.2-8.4 require."""
    return (
        authorization_statements(gateway_arn=gateway_arn, harness_roles=harness_roles)
        + prompt_injection_statements(gateway_arn=gateway_arn)
        + pii_suppression_statements(gateway_arn=gateway_arn)
        + content_filter_statements(gateway_arn=gateway_arn)
    )


#: Every jurisdiction code the policies must be generated for (design §5.1 — four Gateways).
JURISDICTION_CODES = tuple(j.code for j in JURISDICTIONS)
