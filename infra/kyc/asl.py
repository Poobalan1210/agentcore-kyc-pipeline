"""Amazon States Language definitions for the three phases.

Built as plain dicts rather than CDK L2 constructs because the properties that matter here are
properties of the graph: a degraded branch cannot open an account without a human, no failure path
lifts a restriction, every account change has a decision record. Those are reachability claims, and
a dict can be walked by a unit test (`infra/tests/test_asl.py`) while a synthesised construct tree
cannot be, at least not legibly.

What the graphs decide on
-------------------------
Nothing an agent says. Each evidence tool records its result on the case record, and the decision
engine reads those recorded facts (`decision-engine/facts.py`). The state machines pass the facts
through exactly as the tools wrote them, so the only component that interprets them is tested
Python. Agent results reach the engine as an availability signal only: whether each agent ran and
returned anything.

Account changes and their paperwork happen in a fixed order. Restrictions tighten first and are
recorded after, so a failed audit write can never leave an account less restricted than the rules
decided. Accounts open, and restrictions lift, only after the decision record is written.

Every state that changes the case's `state` appends to `transitions` in the same write, and a human
decision is carried out rather than merely recorded: the reviewer's APPROVE opens the account (or
lifts restrictions), REJECT closes it off, and the graph walks there from the same wait state.

Things established the hard way, all of which still hold
--------------------------------------------------------
* `QueryLanguage: JSONata` is mandatory (F-71). `Arguments` and `{% %}` do not exist under
  JSONPath, and the validation error names neither.
* The resource URI is `bedrockagentcore`, no hyphen; the ARNs are `bedrock-agentcore`.
* `reasoningConfig` nests under `AdditionalParams.additionalModelRequestFields` (F-61).
* At `maxReasoningEffort: high`, `Temperature` and `MaxTokens` must be unset (F-62).
* `RuntimeSessionId` needs 33+ characters (F-36); `$uuid()` yields 36.
* The plain harness ARN plus `Qualifier: live` (F-85). Every endpoint-suffixed form is rejected.
* DynamoDB AttributeValue members are PascalCase in Step Functions, in writes and in reads: `Bool`
  and `Null`, never `BOOL` and `NULL`.
* Assignments in one `Assign` block all see the incoming scope, so a projection must be rooted at
  `$states.result`, never at a variable assigned beside it. Found live: every agent was sent a case
  with no documents and no liveness, and the case degraded to review in silence.
* `[$map(...)]`, not `$map(...)`: JSONata collapses a one-element sequence to its element.
* `!= null`, not `$exists`, for a variable that is assigned null.
* An expression that evaluates to undefined fails the whole state, so every optional read is
  guarded. An unguarded read of an attribute nothing writes is a dead state, not a null.
* Execution names are capped at 80 characters.
* `States.ALL` must be alone, and last, in a catcher list.
* State names are unique across the whole machine, including inside Parallel branches.
"""

from __future__ import annotations

from typing import Any

from .config import (
    AGENT_BY_KEY,
    CASE_FACT_ATTRIBUTES,
    DECIDED_JURISDICTIONS,
    LIST_OVERLAY_PARAMETER,
    LIVENESS_FAILURE_LIMIT,
    PHASE1_EVIDENCE_AGENTS,
    SCREENING_LIST_BASE_VERSION,
    Agent,
)

# --------------------------------------------------------------------------------- resources

#: No hyphen. The ARNs in `Arguments` use `bedrock-agentcore`; this URI does not.
INVOKE_HARNESS = "arn:aws:states:::bedrockagentcore:invokeHarness"
LAMBDA_INVOKE = "arn:aws:states:::lambda:invoke"
DDB_GET_ITEM = "arn:aws:states:::aws-sdk:dynamodb:getItem"
DDB_UPDATE_ITEM = "arn:aws:states:::aws-sdk:dynamodb:updateItem"
DDB_PUT_ITEM = "arn:aws:states:::aws-sdk:dynamodb:putItem"
SSM_GET_PARAMETER = "arn:aws:states:::aws-sdk:ssm:getParameter"
SQS_WAIT_FOR_TOKEN = "arn:aws:states:::sqs:sendMessage.waitForTaskToken"
START_EXECUTION = "arn:aws:states:::states:startExecution"
PUT_METRIC_DATA = "arn:aws:states:::aws-sdk:cloudwatch:putMetricData"

# ---------------------------------------------------------------------------------- timeouts

#: Hard ceiling is 15 minutes on a Task (F-14), and the harness is separately bounded at 900s/120s
#: (F-83) because a Task giving up does not stop a harness from running and billing. The four
#: Phase 1 agents run in parallel inside this budget; none of them reasons (F-88).
AGENT_TIMEOUT_SECONDS = 90

#: For a reasoning-enabled agent that is given no explicit budget. No Phase 1 agent reasons.
REASONING_AGENT_TIMEOUT_SECONDS = 180

#: A6 runs at high effort with no clock pressure (D-08). Measured p50 21.7s and max 48.5s, so 90s
#: would be too tight; 600s stays under the 900s Task ceiling.
COMPLIANCE_TIMEOUT_SECONDS = 600

#: The briefing runs before a case is queued, so it delays the reviewer and never the applicant.
BRIEFING_TIMEOUT_SECONDS = 60

#: The Phase 3 re-screen is off the 90-second path, but it runs once per open account per event.
RESCREEN_TIMEOUT_SECONDS = 120

#: Seven days is the SQS maximum. The point is that a review does not expire quietly.
REVIEW_WAIT_SECONDS = 604800

#: When a case already has a review open — a Phase 3 event arriving while a Phase 2 review waits,
#: say — the second review waits for the first instead of replacing its task token. Ten minutes
#: between checks for up to seven days, the same ceiling as a review itself: about 9,000 history
#: events, well inside the 25,000 a Standard execution allows.
REVIEW_GATE_WAIT_SECONDS = 600
REVIEW_GATE_MAX_WAITS = 1008

# ----------------------------------------------------------------------------------- literals

#: F-36: the minimum is 33 characters and `$uuid()` yields 36.
RUNTIME_SESSION_ID = "{% $uuid() %}"

#: F-85. The endpoint is selected by `Qualifier`, not by suffixing the ARN. R18.6 records the
#: endpoint name on the decision, alongside the version it serves.
HARNESS_QUALIFIER = "live"

#: The error the review receiver sends when a case has moved on and can no longer be resumed.
REVIEW_UNRESUMABLE = "ReviewUnresumable"

#: Both spellings. The aws-sdk integration names the error after the SDK client (`DynamoDb`); the
#: optimised integration spells it `DynamoDB`. Catching one while receiving the other would turn an
#: expected replay into a failed execution.
CONDITIONAL_CHECK_FAILED = ("DynamoDb.ConditionalCheckFailedException",
                            "DynamoDB.ConditionalCheckFailedException")

#: The four version pins R18.6 requires on every decision record. The audit writer refuses a
#: record without them (gate G9 caught this as MISSING_VERSION_PINS), and they travel in the payload
#: because the state machine is what knows which endpoint, harness version and Gateway ran.
PIN_FIELDS = ("endpointName", "harnessVersion", "policyEngineVersion", "gatewayArn")

# ------------------------------------------------------------------------------------ retries

#: R13.2: throttling is the one agent error worth retrying; everything else is a degraded result.
THROTTLE_RETRY: list[dict[str, Any]] = [{
    "ErrorEquals": ["BedrockAgentCore.ThrottlingException"],
    "IntervalSeconds": 2,
    "MaxAttempts": 3,
    "BackoffRate": 2.0,
}]

#: Transient Lambda faults only. An error the function itself raised — an incomplete audit record,
#: an account that does not exist — is deterministic, and retrying it only delays the catcher.
LAMBDA_RETRY: list[dict[str, Any]] = [{
    "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException",
                    "Lambda.SdkClientException", "Lambda.TooManyRequestsException"],
    "IntervalSeconds": 1,
    "MaxAttempts": 3,
    "BackoffRate": 2.0,
}]

_TRANSIENT_DYNAMODB = ["DynamoDb.ProvisionedThroughputExceededException",
                       "DynamoDb.ThrottlingException",
                       "DynamoDb.RequestLimitExceededException",
                       "DynamoDb.InternalServerErrorException",
                       "DynamoDb.DynamoDbException"]

#: Reads and conditional writes retry transient faults only. Retrying a failed condition would add
#: seven seconds to every replay and then land in the same catcher; retrying a read of a case that
#: does not exist would do the same.
TRANSIENT_RETRY: list[dict[str, Any]] = [{
    "ErrorEquals": _TRANSIENT_DYNAMODB,
    "IntervalSeconds": 1,
    "MaxAttempts": 3,
    "BackoffRate": 2.0,
}]

#: Unconditional writes are safe to repeat, so they retry anything.
WRITE_RETRY: list[dict[str, Any]] = [{
    "ErrorEquals": ["States.ALL"],
    "IntervalSeconds": 1,
    "MaxAttempts": 3,
    "BackoffRate": 2.0,
}]

# ---------------------------------------------------------------------------- small builders

NOW = {"S": "{% $now() %}"}
EMPTY_LIST = {"L": []}

#: Appended in the same expression that sets `state`. Two separate updates could interleave with a
#: concurrent writer and leave the record and its history disagreeing (R2.5).
APPEND_TRANSITION = "transitions = list_append(if_not_exists(transitions, :empty), :tr)"


def _catch(next_state: str, errors: list[str] | tuple[str, ...] | None = None
           ) -> list[dict[str, Any]]:
    return [{"ErrorEquals": list(errors or ["States.ALL"]), "Next": next_state}]


def _catch_conditional(on_conditional: str, on_other: str | None = None
                       ) -> list[dict[str, Any]]:
    """A named catcher for a failed condition, then `States.ALL` alone and last."""
    catchers = [{"ErrorEquals": list(CONDITIONAL_CHECK_FAILED), "Next": on_conditional}]
    if on_other:
        catchers.append({"ErrorEquals": ["States.ALL"], "Next": on_other})
    return catchers


def _case_key(applicant: str = "{% $applicantId %}") -> dict[str, Any]:
    return {"applicantId": {"S": applicant}, "sk": {"S": "CASE"}}


def _transition(to: str, reason: str, actor: str = "system") -> dict[str, Any]:
    """One `transitions` entry. `from` is the tracked `$caseState`, so it is what this execution
    last wrote rather than what the case said when it was first loaded."""
    return {"L": [{"M": {
        "from": {"S": "{% $caseState %}"},
        "to": {"S": to},
        "at": {"S": "{% $now() %}"},
        "actor": {"S": actor},
        "reason": {"S": reason},
    }}]}


def _pass(next_state: str, *, assign: dict[str, Any] | None = None,
          comment: str | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"Type": "Pass", "QueryLanguage": "JSONata"}
    if comment:
        state["Comment"] = comment
    if assign:
        state["Assign"] = assign
    state["Next"] = next_state
    return state


def _succeed(comment: str) -> dict[str, Any]:
    return {"Type": "Succeed", "QueryLanguage": "JSONata", "Comment": comment}


def _fail(error: str, cause: str) -> dict[str, Any]:
    return {"Type": "Fail", "Error": error, "Cause": cause}


def _get_case(case_table: str, *, next_state: str, catch_to: str,
              assign: dict[str, Any] | None = None,
              applicant: str = "{% $applicantId %}") -> dict[str, Any]:
    """A strongly consistent read of the case record.

    Strongly consistent because the reads that matter come seconds after concurrent tool writes.
    An eventually consistent read would drop facts at random, and a missing fact looks exactly like
    a tool that never ran.
    """
    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": DDB_GET_ITEM,
        "Arguments": {"TableName": case_table, "Key": _case_key(applicant),
                      "ConsistentRead": True},
        "Assign": assign or {"case": "{% $states.result.Item %}"},
        "Retry": TRANSIENT_RETRY,
        "Catch": _catch(catch_to),
        "Next": next_state,
    }


def _update(case_table: str, *, update: str, values: dict[str, Any], next_state: str,
            names: dict[str, str] | None = None, condition: str | None = None,
            catch: list[dict[str, Any]] | None = None, assign: dict[str, Any] | None = None,
            comment: str | None = None, applicant: str = "{% $applicantId %}"
            ) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "TableName": case_table,
        "Key": _case_key(applicant),
        "UpdateExpression": update,
        "ExpressionAttributeValues": values,
    }
    if names:
        arguments["ExpressionAttributeNames"] = names
    if condition:
        arguments["ConditionExpression"] = condition
    state: dict[str, Any] = {"Type": "Task", "QueryLanguage": "JSONata"}
    if comment:
        state["Comment"] = comment
    state.update({
        "Resource": DDB_UPDATE_ITEM,
        "Arguments": arguments,
        "Retry": TRANSIENT_RETRY if condition else WRITE_RETRY,
    })
    if catch:
        state["Catch"] = catch
    if assign:
        state["Assign"] = assign
    state["Next"] = next_state
    return state


#: The list version in force, composed exactly as the screening tool composes it
#: (`tools_impl.current_list`): the base list alone, or base + '+' + the update's version once a
#: published update carries entries.
CURRENT_LIST_VERSION = (
    "{% ($o := $parse($states.result.Parameter.Value); "
    "$count($o.entries) > 0 ? '" + SCREENING_LIST_BASE_VERSION + "+' & $o.version : '"
    + SCREENING_LIST_BASE_VERSION + "') %}")

#: What the rules are told when the list cannot be read. It matches no screening, so nothing is
#: decided on a list nobody could confirm is current.
LIST_UNREADABLE = "UNREADABLE"


def _load_list_version(s: dict[str, Any], *, next_state: str) -> None:
    """LoadListVersion -> next_state, with ListVersionUnreadable on any failure (fail closed)."""
    s["LoadListVersion"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": SSM_GET_PARAMETER,
        "Arguments": {"Name": LIST_OVERLAY_PARAMETER},
        "Assign": {"currentListVersion": CURRENT_LIST_VERSION},
        "Retry": [{"ErrorEquals": ["Ssm.ThrottlingException", "Ssm.InternalServerErrorException"],
                   "IntervalSeconds": 1, "MaxAttempts": 2, "BackoffRate": 2.0}],
        "Catch": _catch("ListVersionUnreadable"),
        "Next": next_state,
    }
    s["ListVersionUnreadable"] = _pass(
        next_state, assign={"currentListVersion": LIST_UNREADABLE},
        comment="Fail closed: a screening cannot be shown to be of the current list.")


def _require_pins(pins: dict[str, str] | None) -> dict[str, str]:
    missing = [k for k in PIN_FIELDS if not str((pins or {}).get(k) or "").strip()]
    if missing:
        raise ValueError(f"audit_pins is missing {missing}; R18.6 needs all four on every record")
    return dict(pins or {})


# ----------------------------------------------------------------------- case projections

#: What an agent sees of the case. Unwrapped once here: the raw getItem result wraps every value in
#: a type tag, and a model reaching through `documents.L[0].M.key.S` got it right on some runs and
#: wrong on others — a documented class of tool-use failure with deeply nested, type-tagged JSON.
#:
#: Deliberately small. The tools read their inputs from the case record, so an agent needs the
#: applicant id to call its tool and the name to report what it screened; nothing here is an input
#: to a decision. Dates of birth, addresses and document locations stay out of prompts.
#:
#: `ITEM` is replaced with the root the projection is built from, always `$states.result.Item` or
#: a variable assigned in an earlier state — never one assigned in the same block.
_CASE_VIEW_FIELDS: dict[str, str] = {
    "caseId": "{% $exists(ITEM.caseId) ? ITEM.caseId.S : null %}",
    "jurisdiction": "{% $exists(ITEM.jurisdiction) ? ITEM.jurisdiction.S : null %}",
    "productApplied": "{% $exists(ITEM.productApplied) ? ITEM.productApplied.S : null %}",
    "declaredIdentity": (
        "{% $exists(ITEM.declaredIdentity) ? {"
        "'givenNames': ITEM.declaredIdentity.M.givenNames.S, "
        "'surname': ITEM.declaredIdentity.M.surname.S"
        "} : null %}"),
    # `[$map(...)]` so one document is still a list, and `$map` with a lambda rather than
    # `array.{...}`, which groups instead of mapping and would merge two documents into one.
    "documents": (
        "{% $exists(ITEM.documents) ? [$map(ITEM.documents.L, function($d) {"
        "{'type': $d.M.type.S, 'issuingCountry': $d.M.issuingCountry.S}"
        "})] : [] %}"),
    "liveness": "{% $exists(ITEM.liveness) ? {'result': ITEM.liveness.M.result.S} : null %}",
}


def _case_view(item: str, applicant: str) -> dict[str, str]:
    view = {"applicantId": applicant}
    view.update({k: v.replace("ITEM", item) for k, v in _CASE_VIEW_FIELDS.items()})
    return view


def _facts() -> dict[str, str]:
    """The recorded facts, exactly as the tools wrote them, or null where a fact is absent.

    Null, not `{}`: the engine reads null as "the check did not run", which routes to a person, and
    an empty object would read as "it ran and found nothing".
    """
    return {name: "{% $exists($case.ATTR) ? $case.ATTR.S : null %}".replace("ATTR", attr)
            for name, attr in CASE_FACT_ATTRIBUTES.items()}


#: The declared half of the identity cross-check, named field by field: the engine expects plain
#: strings, and forwarding the map wholesale would also carry whatever else a caller put in the
#: request body.
_DECLARED_IDENTITY = (
    "{% $exists($case.declaredIdentity) ? {"
    "'surname': $case.declaredIdentity.M.surname.S, "
    "'givenNames': $case.declaredIdentity.M.givenNames.S, "
    "'dateOfBirth': $case.declaredIdentity.M.dateOfBirth.S, "
    "'nationality': $case.declaredIdentity.M.nationality.S"
    "} : null %}")

#: Screening alerts a reviewer has cleared (written by the review handler on APPROVE). The engine
#: removes cleared PROBABLE/POSSIBLE alerts and never a CONFIRMED one.
_DISPOSITIONS = ("{% $exists($case.screeningDispositions) ? "
                 "$parse($case.screeningDispositions.S) : [] %}")

#: The engine's decision fields a reviewer briefing may see. Tier inputs and thresholds stay out:
#: the briefing explains evidence, it does not re-derive the tier.
_BRIEFING_DECISION_FIELDS = ("outcome", "rule", "reasonCode", "explanation", "requiredItem",
                             "tier", "tierRule", "edgeCasesMatched", "identityMatch",
                             "factsRead", "day0Tier", "newTier")


def _briefing_prompt(phase: str, *, include_event: bool = False) -> str:
    """The Briefing agent's input: why the case is here and what the tools recorded.

    Document text lines are removed. They are untrusted text printed on a document, the one place
    an applicant can put words straight into a model's context, and the briefing needs the
    verified fields rather than the raw lines.
    """
    fields = ", ".join(f"'{f}'" for f in _BRIEFING_DECISION_FIELDS)
    facts = ", ".join(
        "'NAME': $exists($case.ATTR) ? $parse($case.ATTR.S) : null"
        .replace("NAME", name).replace("ATTR", attr)
        for name, attr in CASE_FACT_ATTRIBUTES.items() if name != "documentExtraction")
    event = ("'riskEvent': $sift($riskEvent, function($v, $k) { "
             "$k in ['eventType', 'severity', 'occurredAt', 'detail'] }), "
             if include_event else "")
    return (
        "{% $string({"
        "'phase': '" + phase + "', "
        "'reviewReason': $reviewReason, "
        + event +
        "'decision': $decision != null ? "
        "$sift($decision, function($v, $k) { $k in [" + fields + "] }) : null, "
        "'facts': {"
        "'documentChecks': $exists($case.documentExtraction) ? "
        "$sift($parse($case.documentExtraction.S), function($v, $k) { $k != 'textLines' }) "
        ": null, " + facts +
        "}}) %}")


def _notify_prompt(notice: str) -> str:
    """The Notify agent's input: the account's status and limits, and nothing else.

    It never sees documents, screening results or the reason for a decision, so it cannot reveal
    them. The API applies a second, deterministic check to the text before an applicant sees it.
    """
    return ("{% $string({"
            "'notice': '" + notice + "', "
            "'accountStatus': $account != null ? $account.state : null, "
            "'restrictions': $account != null ? $account.restrictionDetails : [], "
            "'limits': $account != null ? $account.limits : {}"
            "}) %}")


def _pending_review(extra: str = "") -> str:
    """The record a case carries while it waits for a person.

    Written onto the phase attribute before the wait, because the review handler's first-writer-wins
    guard requires the phase to exist before it can be reviewed. It keeps the engine's decision —
    including the tier, which is what stops a reviewer approving a PROHIBITED case — and says the
    outcome is pending.
    """
    return ("{% $string($merge([($decision != null ? $decision : {}), {"
            "'outcome': 'MANUAL_REVIEW', "
            "'engineOutcome': $decision != null ? $decision.outcome : null, "
            "'reviewReason': $reviewReason" + extra + "}])) %}")


def _reviewed(outcome: str) -> str:
    """A reviewer's decision as a record: the engine decision it acted on, and who decided.

    Returns the bare JSONata expression so callers can wrap or extend it.
    """
    return ("$merge([($decision != null ? $decision : {}), {"
            "'outcome': '" + outcome + "', "
            "'rule': 'REVIEW.' & $reviewOutcome.action, "
            "'decidedBy': 'REVIEWER', "
            "'reviewerId': $reviewOutcome.reviewerId, "
            "'reviewedAt': $reviewOutcome.decidedAt, "
            # The screening alerts the reviewer resolved, so the immutable record says what a
            # person cleared and not only that they approved.
            "'clearedAlerts': $exists($reviewOutcome.clearedAlerts) ? "
            "$reviewOutcome.clearedAlerts : [], "
            "'engineOutcome': $decision != null ? $decision.outcome : null, "
            "'reviewReason': $reviewReason}])")


# ------------------------------------------------------------------------------ agent tasks


def timeout_argument(agent_key: str, default: int) -> str:
    """The harness timeout, shortened by a demo fault when one is set for this agent.

    `$min` with the default is the safety property: a fault can make an agent run out of time
    sooner, never give it longer. A value that is not a number fails the task, which the branch
    catches as a degraded result — the same place a real timeout lands.
    """
    lookup = "$lookup($agentTimeouts, '" + agent_key + "')"
    return ("{% $exists(" + lookup + ") ? $max([1, $floor($min([$number(" + lookup + "), "
            + str(default) + "]))]) : " + str(default) + " %}")


def agent_task(
    agent: Agent,
    *,
    harness_arn: str,
    model_id: str,
    next_state: str,
    catch_to: str,
    prompt_expression: str,
    timeout_seconds: int | None = None,
    overridable_timeout: bool = False,
    keep_reply: bool = False,
    end: bool = False,
) -> dict[str, Any]:
    """One `invokeHarness` Task, shaped per design §7.3.

    `catch_to` is required with no default on purpose. An agent Task without a Catch fails the
    whole execution, and a partial result becomes an outage rather than a degraded decision — the
    distinction the degraded-path demo exists to show.

    `keep_reply` assigns the reply to `${agent}Result` for a later state to store. Only the agents
    whose words are the product keep it (the briefing, the applicant message); an evidence agent's
    reply goes into its branch record and nowhere near a decision.
    """
    default = timeout_seconds or (REASONING_AGENT_TIMEOUT_SECONDS if agent.reasoning_effort
                                  else AGENT_TIMEOUT_SECONDS)

    model_config: dict[str, Any] = {"ModelId": model_id}
    if agent.reasoning_effort:
        model_config["AdditionalParams"] = {
            "additionalModelRequestFields": {
                "reasoningConfig": {
                    "type": "enabled",
                    "maxReasoningEffort": agent.reasoning_effort,
                }
            }
        }
    if agent.can_pin_sampling:
        # F-62: only legal without reasoning. Pinning sampling is what makes a run reproducible
        # (R26.3); the agent that cannot pin it is the one whose output never decides anything.
        model_config["Temperature"] = 0.0

    task: dict[str, Any] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": INVOKE_HARNESS,
        "Arguments": {
            "HarnessArn": harness_arn,
            "Qualifier": HARNESS_QUALIFIER,
            "RuntimeSessionId": RUNTIME_SESSION_ID,
            "Messages": [{"Role": "user", "Content": [{"Text": prompt_expression}]}],
            "Model": {"BedrockModelConfig": model_config},
            "MaxIterations": agent.max_iterations,
            "TimeoutSeconds": (timeout_argument(agent.key, default) if overridable_timeout
                               else default),
        },
        "Retry": THROTTLE_RETRY,
        "Catch": _catch(catch_to),
    }
    if keep_reply:
        task["Assign"] = {f"{agent.key}Result": "{% $states.result.Output %}"}
    if end:
        task["End"] = True
    else:
        task["Next"] = next_state
    return task


#: An agent can finish without failing and still return nothing. Measured live: Fraud finished
#: with `StopReason: timeout_exceeded` and `Content: []` after 55.7s and 8,525 output tokens. No
#: error, no Catch — and read naively, "no findings" is "no risk".
EMPTY_AGENT_RESULT = (
    "{% $not($exists($states.input.Output.Message.Content)) "
    "or $count($states.input.Output.Message.Content) = 0 "
    "or $states.input.StopReason = 'timeout_exceeded' %}")


def _emptiness_check(*, ok: str, degraded: str) -> dict[str, Any]:
    return {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{"Condition": EMPTY_AGENT_RESULT, "Next": degraded}],
        "Default": ok,
    }


def _ok_record(agent_key: str, model_id: str | None = None) -> dict[str, Any]:
    """An agent run, keyed by agent: the harness envelope carries no agent name.

    `model` is the model the task asked for (`Model.BedrockModelConfig.ModelId`), a literal from
    this definition rather than anything the agent reported, so the console's live view can say
    which model ran each lane.
    """
    record: dict[str, Any] = {
        "agent": agent_key,
        "status": "OK",
        "content": "{% $states.input.Output.Message.Content %}",
        "stopReason": ("{% $exists($states.input.StopReason) ? "
                       "$states.input.StopReason : null %}"),
        "latencyMs": ("{% $exists($states.input.Metrics.LatencyMs) ? "
                      "$states.input.Metrics.LatencyMs : 0 %}"),
        "totalTokens": ("{% $exists($states.input.Usage.TotalTokens) ? "
                        "$states.input.Usage.TotalTokens : 0 %}"),
    }
    if model_id:
        record["model"] = model_id
    return record


def _degraded_record(agent_key: str) -> dict[str, Any]:
    # UNAVAILABLE to the engine, never INVALID: the applicant cannot fix an agent that returned
    # nothing, so this reaches a reviewer rather than becoming a step-up they can never satisfy.
    return {"agent": agent_key, "status": "DEGRADED", "reason": "EMPTY_OR_FAILED_AGENT_RESULT"}


def _phase1_branch(key: str, *, harness_arns: dict[str, str],
                   model_ids: dict[str, str]) -> dict[str, Any]:
    """One Parallel branch: invoke, check for an empty result, emit a keyed record.

    State names carry the agent's suffix because names must be unique across the whole machine,
    not per branch, and so the execution history says which branch failed.
    """
    agent = AGENT_BY_KEY[key]
    name = agent.role_suffix
    return {
        "StartAt": name,
        "States": {
            name: agent_task(
                agent,
                harness_arn=harness_arns[key],
                model_id=model_ids[key],
                next_state=f"{name}Check",
                catch_to=f"{name}Degraded",
                prompt_expression="{% $string($caseView) %}",
                overridable_timeout=True,
            ),
            f"{name}Check": _emptiness_check(ok=f"{name}Ok", degraded=f"{name}Degraded"),
            f"{name}Ok": {"Type": "Pass", "QueryLanguage": "JSONata",
                          "Output": _ok_record(key, model_ids[key]), "End": True},
            f"{name}Degraded": {"Type": "Pass", "QueryLanguage": "JSONata",
                                "Output": _degraded_record(key), "End": True},
        },
    }


# ------------------------------------------------------------------- Lambda-backed tasks


def _engine_task(function_arn: str, *, payload: dict[str, Any], next_state: str,
                 catch_to: str) -> dict[str, Any]:
    """The pure-rules Lambda. No model (R4.6), so no reasoning config and no throttle retry."""
    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": LAMBDA_INVOKE,
        "Arguments": {"FunctionName": function_arn, "Payload": payload},
        "Assign": {"decision": "{% $states.result.Payload %}"},
        "Retry": [{"ErrorEquals": ["Lambda.ServiceException", "Lambda.TooManyRequestsException"],
                   "IntervalSeconds": 1, "MaxAttempts": 3, "BackoffRate": 2.0}],
        "Catch": _catch(catch_to),
        "Next": next_state,
    }


def _audit_task(audit_writer_arn: str, *, phase: str, decision: str, pins: dict[str, str],
                next_state: str, catch_to: str, actor: str | None = None,
                assign: dict[str, Any] | None = None,
                include_ingress: bool = False) -> dict[str, Any]:
    """Write the immutable decision record (R18.3). The writer mints the decision id.

    The writer raises on an incomplete record, and that is not retried: it is deterministic, and
    the catcher is where a decided case without a record becomes visible.
    """
    payload: dict[str, Any] = {
        "applicantId": "{% $applicantId %}",
        "phase": phase,
        "decision": decision,
        "traceId": "{% $states.context.Execution.Name %}",
    }
    payload.update({k: pins[k] for k in PIN_FIELDS})
    if actor:
        payload["actor"] = actor
    if include_ingress:
        # T2's queue-side leg, measured not estimated (A-05). Null on the direct-start path; the
        # writer must not coerce that to 0, since unmeasured and instant are different facts.
        payload["ingressLatencyMs"] = "{% $ingressLatencyMs %}"
    state: dict[str, Any] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": LAMBDA_INVOKE,
        "Arguments": {"FunctionName": audit_writer_arn, "Payload": payload},
        "Retry": LAMBDA_RETRY,
    }
    if assign:
        state["Assign"] = assign
    state["Catch"] = _catch(catch_to)
    state["Next"] = next_state
    return state


def _account_task(function_arn: str, *, operation: str, next_state: str, catch_to: str,
                  arguments: dict[str, Any] | None = None,
                  keep_account: bool = False) -> dict[str, Any]:
    """One call to the account platform. Every operation returns the account as it now stands.

    The platform enforces the restrictions (R31.2): the case record says what was decided, the
    account says what the customer can actually do. `keep_account` assigns that view to
    `$account`, which is all the applicant message is ever written from.
    """
    args: dict[str, Any] = {"applicantId": "{% $applicantId %}"}
    args.update(arguments or {})
    state: dict[str, Any] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": LAMBDA_INVOKE,
        "Arguments": {"FunctionName": function_arn,
                      "Payload": {"operation": operation, "arguments": args}},
        "Retry": LAMBDA_RETRY,
    }
    if keep_account:
        state["Assign"] = {"account": "{% $states.result.Payload.account %}"}
    state["Catch"] = _catch(catch_to)
    state["Next"] = next_state
    return state


# ------------------------------------------------------------------------- human review


def _persist_briefing(case_table: str, *, phase: str, next_state: str) -> dict[str, Any]:
    """Store the briefing for the reviewer console. A missing briefing never blocks a review."""
    return _update(
        case_table,
        update="SET reviewBriefing = :b, updatedAt = :t",
        values={
            ":b": {"S": ("{% $string({'phase': '" + phase + "', "
                         "'text': $join([$briefingResult.Message.Content.Text], '\\n'), "
                         "'generatedAt': $now()}) %}")},
            ":t": NOW,
        },
        catch=_catch(next_state),
        next_state=next_state,
    )


def _mark_awaiting_review(case_table: str, *, phase: str, pending: str, next_state: str,
                          remove: tuple[str, ...] = ()) -> dict[str, Any]:
    """Move the case into AWAITING_REVIEW, and write what is pending, before the wait begins.

    Without this the review loop is inert in both directions: the console finds cases through the
    `state` GSI, and the review handler refuses a case that is not AWAITING_REVIEW or whose phase
    attribute does not exist yet. It is a separate state because the SQS wait performs exactly one
    action, and writing after the send would leave a window where a token exists for a case the
    console cannot see.

    A stale token from an earlier review is removed here; the materialiser writes the new one. No
    Catch: a case that cannot be marked must not be queued, because its token would wait seven days
    for a reviewer who can never see it.
    """
    removed = ["reviewTaskToken", "reviewTaskTokenAt", *remove]
    return _update(
        case_table,
        update=("SET #s = :awaiting, reviewPhase = :phase, reviewReason = :reason, "
                f"{phase} = :pending, reviewQueuedAt = :t, updatedAt = :t, "
                + APPEND_TRANSITION + " REMOVE " + ", ".join(removed)),
        names={"#s": "state"},
        values={
            ":awaiting": {"S": "AWAITING_REVIEW"},
            ":phase": {"S": phase},
            ":reason": {"S": "{% $reviewReason != null ? $reviewReason : 'UNSPECIFIED' %}"},
            ":pending": {"S": pending},
            ":t": NOW,
            ":empty": EMPTY_LIST,
            # R2.5 — `system` as the actor. The reviewer's decision appends its own entry.
            ":tr": _transition("AWAITING_REVIEW",
                               "{% 'queued for human review: ' & "
                               "($reviewReason != null ? $reviewReason : 'UNSPECIFIED') %}"),
        },
        assign={"caseState": "AWAITING_REVIEW"},
        next_state=next_state,
    )


def _review_task(queue_url: str, *, phase: str, next_state: str, catch_to: str
                 ) -> dict[str, Any]:
    """The human wait — a separate state, never an agent call (F-12, design §7.5).

    `phase` travels in the message because nothing downstream can infer it: a Phase 2 review sits
    on a case that already has a `phase1` decision, so the record alone is ambiguous.
    """
    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": SQS_WAIT_FOR_TOKEN,
        "Arguments": {
            "QueueUrl": queue_url,
            "MessageBody": {
                "applicantId": "{% $applicantId %}",
                "caseId": "{% $case.caseId.S %}",
                "taskToken": "{% $states.context.Task.Token %}",
                "phase": phase,
                "reason": "{% $reviewReason != null ? $reviewReason : 'UNSPECIFIED' %}",
            },
        },
        "TimeoutSeconds": REVIEW_WAIT_SECONDS,
        "Assign": {"reviewOutcome": "{% $states.result.reviewOutcome %}"},
        # Two catchers: `States.ALL` must be alone and last, and the named errors come first so a
        # leading wildcard cannot swallow them.
        "Catch": [
            {"ErrorEquals": [REVIEW_UNRESUMABLE, "States.Timeout"], "Next": catch_to},
            {"ErrorEquals": ["States.ALL"], "Next": catch_to},
        ],
        "Next": next_state,
    }


def _route_review(*, approve: str, reject: str, step_up: str | None) -> dict[str, Any]:
    """Carry out the reviewer's action. Anything unrecognised stops rather than guessing."""
    choices = [
        {"Condition": "{% $reviewOutcome.action = 'APPROVE' %}", "Next": approve},
        {"Condition": "{% $reviewOutcome.action = 'REJECT' %}", "Next": reject},
    ]
    if step_up:
        choices.append({"Condition": "{% $reviewOutcome.action = 'REQUEST_STEP_UP' %}",
                        "Next": step_up})
    return {"Type": "Choice", "QueryLanguage": "JSONata", "Choices": choices,
            "Default": "ReviewNotActionable"}


def _review_path(s: dict[str, Any], *, case_table: str, harness_arns: dict[str, str],
                 model_ids: dict[str, str], review_queue_url: str, phase: str, pending: str,
                 route: dict[str, Any], abandoned: str, not_needed: str,
                 remove: tuple[str, ...] = (), include_event: bool = False) -> None:
    """ReviewGate -> Briefing -> PersistBriefing -> ManualReview -> AwaitReviewDecision -> Route.

    Every entry into review assigns `$reviewReason` and goes to `ReviewGate`.

    The gate re-reads the case. If another review is already open — a list update arriving while a
    Phase 2 review waits — this one waits for it rather than replacing its task token, which would
    leave the first execution waiting seven days on a token nobody can see. If that review froze
    or closed the case, this one is no longer needed. The account action that sent this case here
    has already happened, and holds stop the open review from undoing it.

    The briefing runs before the case is marked, so a reviewer never opens a case whose briefing
    is still being written; if the agent fails, the case is queued without one.
    """
    s["ReviewGate"] = _get_case(
        case_table, next_state="IsReviewOpen", catch_to="Briefing",
        assign={"case": "{% $states.result.Item %}",
                "caseState": ("{% $exists($states.result.Item.state) ? "
                              "$states.result.Item.state.S : 'UNKNOWN' %}")})
    s["IsReviewOpen"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": ("{% $caseState = 'AWAITING_REVIEW' and $reviewWaits < "
                           f"{REVIEW_GATE_MAX_WAITS} %}}"),
             "Next": "CountReviewWait"},
            {"Condition": "{% $caseState = 'AWAITING_REVIEW' %}", "Next": "ReviewStillOpen"},
            {"Condition": "{% $caseState in ['FROZEN', 'DECIDED'] %}", "Next": not_needed},
        ],
        "Default": "Briefing",
    }
    s["CountReviewWait"] = _pass("WaitForOpenReview",
                                 assign={"reviewWaits": "{% $reviewWaits + 1 %}"})
    s["WaitForOpenReview"] = {"Type": "Wait", "QueryLanguage": "JSONata",
                              "Seconds": REVIEW_GATE_WAIT_SECONDS, "Next": "ReviewGate"}
    s["ReviewStillOpen"] = _fail(
        "ReviewStillOpen",
        "Another review on this case stayed open for seven days, so this one was never queued. "
        "The account action that led here stands; the case needs a person.")

    briefing = AGENT_BY_KEY["briefing"]
    s["Briefing"] = agent_task(
        briefing,
        harness_arn=harness_arns["briefing"],
        model_id=model_ids["briefing"],
        next_state="PersistBriefing",
        catch_to="ManualReview",
        prompt_expression=_briefing_prompt(phase, include_event=include_event),
        timeout_seconds=BRIEFING_TIMEOUT_SECONDS,
        keep_reply=True,
    )
    s["PersistBriefing"] = _persist_briefing(case_table, phase=phase, next_state="ManualReview")
    s["ManualReview"] = _mark_awaiting_review(
        case_table, phase=phase, pending=pending, next_state="AwaitReviewDecision",
        remove=remove)
    s["AwaitReviewDecision"] = _review_task(
        review_queue_url, phase=phase, next_state="RouteReviewOutcome", catch_to=abandoned)
    s["RouteReviewOutcome"] = route
    s["ReviewNotActionable"] = _fail(
        "ReviewNotActionable",
        "The review returned an action this phase does not carry out. The case is unchanged and "
        "still needs a person.")


# ====================================================================================== Phase 1


def phase1_definition(
    *,
    case_table: str,
    harness_arns: dict[str, str],
    model_ids: dict[str, str],
    decision_engine_arn: str,
    audit_writer_arn: str,
    review_queue_url: str,
    phase2_state_machine_arn: str,
    account_function_arn: str,
    controls_parameter: str,
    audit_pins: dict[str, str],
) -> dict[str, Any]:
    """Phase 1: a provisional decision inside 90 seconds.

    The approval path, and the only path to an account without a person::

        NormalizeInput -> LoadCase -> CheckIdempotency -> ClaimRun -> CheckPreconditions
          -> LoadControls -> Phase1Parallel (Documents | Identity | Fraud | PreScreen)
          -> PersistEvidence -> ReloadFacts -> PreliminaryTier -> Phase1Decision
          -> WriteDecisionRecord -> OpenProvisional -> OpenAccount -> TriggerPhase2

    `Phase1Decision` approves on one explicit outcome, PROVISIONAL_APPROVE_RESTRICTED. REJECT and
    STEP_UP are persisted and recorded. Everything else — MANUAL_REVIEW, an outcome nobody has
    written a branch for, a degraded agent, a failed write on the approval path — goes to review,
    and the reviewer's action is carried out: APPROVE records the decision, opens the account and
    starts Phase 2.
    """
    pins = _require_pins(audit_pins)
    s: dict[str, Any] = {}

    # ------------------------------------------------------------------------------ intake
    #
    # The EventBridge Pipe carries no InputTemplate (design §10.2), so an SQS-sourced execution
    # receives the raw batch of records while a direct StartExecution sends the bare object. Both
    # are live paths, so both are normalised. `attributes.SentTimestamp` is the enqueue time T2
    # needs (A-05): stamped by SQS at SendMessage, so it is measured rather than estimated.
    s["NormalizeInput"] = _pass(
        "LoadCase",
        assign={
            "applicantId": (
                "{% $type($states.input) = 'array' "
                "? $parse($states.input[0].body).applicantId "
                ": $states.input.applicantId %}"),
            "eventId": (
                "{% $type($states.input) = 'array' "
                "? $parse($states.input[0].body).eventId "
                ": $states.input.eventId %}"),
            # Null, not a fabricated value, when started directly: a missing T2 datum must read
            # as "not measured on this path", never as zero latency.
            "enqueuedAtMs": (
                "{% $type($states.input) = 'array' "
                "? $number($states.input[0].attributes.SentTimestamp) : null %}"),
            "firstStateEnteredAtMs": "{% $toMillis($now()) %}",
            # Facts recorded before this (less the engine's tolerance) are stale, so a step-up
            # continuation cannot be decided on the previous attempt's results.
            "phaseStartedAt": "{% $states.context.Execution.StartTime %}",
            # Every variable a later state reads has a value on every path.
            "agentTimeouts": "{% {} %}",
            "branchResults": "{% [] %}",
            "decision": "{% null %}",
            "outcomeRecord": "{% null %}",
            "reviewReason": "{% null %}",
            "reviewOutcome": "{% null %}",
            "reviewWaits": "{% 0 %}",
            "provisionalDecisionId": "{% null %}",
            "briefingResult": "{% null %}",
            "currentListVersion": "{% null %}",
        },
    )

    s["LoadCase"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": DDB_GET_ITEM,
        "Arguments": {"TableName": case_table, "Key": _case_key(), "ConsistentRead": True},
        "Assign": {
            "case": "{% $states.result.Item %}",
            "caseState": ("{% $exists($states.result.Item.state) ? "
                          "$states.result.Item.state.S : 'UNKNOWN' %}"),
            # `!= null`, not `$exists`: `enqueuedAtMs` is assigned null on the direct-start path,
            # so `$exists` is true there and the subtraction fails.
            "ingressLatencyMs": ("{% $enqueuedAtMs != null "
                                 "? $firstStateEnteredAtMs - $enqueuedAtMs : null %}"),
            "livenessFailures": ("{% $exists($states.result.Item.livenessFailures) ? "
                                 "$number($states.result.Item.livenessFailures.N) : 0 %}"),
            # Rooted at `$states.result.Item`, not `$case`: assignments in one block all see the
            # incoming scope, where `$case` is still unset.
            "caseView": _case_view("$states.result.Item", "{% $applicantId %}"),
        },
        "Retry": TRANSIENT_RETRY,
        # No item means no case. The Assign fails on the undefined result and lands here.
        "Catch": _catch("CaseNotFound"),
        "Next": "CheckIdempotency",
    }
    s["CaseNotFound"] = _fail(
        "CaseNotFound",
        "No case record for this applicant. A submission must create its case before it is queued.")

    # R21.3 — a replayed event returns without invoking anything. Checked on the stored key, not on
    # the execution name, because SQS redelivery and an API retry arrive as different executions.
    s["CheckIdempotency"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{
            "Condition": ("{% $exists($case.idempotencyKey) and "
                          "$case.idempotencyKey.S = $applicantId & '#' & $eventId %}"),
            "Next": "ReturnCached",
        }],
        "Default": "ClaimRun",
    }
    s["ReturnCached"] = _succeed(
        "R21.3. This event is already claimed or decided. Zero agent invocations, so a replay "
        "costs nothing and cannot produce a different answer.")

    # The claim, and the idempotency key with it, is written before any work. The key used to be
    # written only when an outcome was persisted — and read from inside `phase1`, a JSON string, so
    # the check could never match. A redelivery during a seven-day review wait would run every agent
    # again and queue a second review. Conditional on the case still being RECEIVED, so an event for
    # a case that has moved on (decided, open, in review) is a no-op too.
    #
    # The claim also names the execution doing the work (`phase1Execution`). EventBridge Pipes
    # starts Phase 1 under a generated name, so without it nothing could find the run while it is
    # running: the console's live view reads its history from that name, state by state.
    s["ClaimRun"] = _update(
        case_table,
        update=("SET #s = :state, idempotencyKey = :k, phase1StartedAt = :t, "
                "phase1Execution = :x, ingressLatencyMs = :il, updatedAt = :t, "
                + APPEND_TRANSITION),
        names={"#s": "state"},
        values={
            ":state": {"S": "IN_PROGRESS"},
            ":received": {"S": "RECEIVED"},
            ":k": {"S": "{% $applicantId & '#' & $eventId %}"},
            ":x": {"S": "{% $states.context.Execution.Name %}"},
            ":t": NOW,
            # Recorded whenever measured. `Null`, the PascalCase member Step Functions accepts, on
            # the direct-start path: unmeasured and instant are different facts.
            ":il": ("{% $ingressLatencyMs != null ? {'N': $string($ingressLatencyMs)} "
                    ": {'Null': true} %}"),
            ":empty": EMPTY_LIST,
            ":tr": _transition("IN_PROGRESS", "phase 1 started"),
        },
        condition=("#s = :received AND "
                   "(attribute_not_exists(idempotencyKey) OR idempotencyKey <> :k)"),
        catch=_catch_conditional("ReturnCached"),
        assign={"caseState": "IN_PROGRESS"},
        next_state="CheckPreconditions",
    )

    # R38 — the guard that makes the expensive part of the graph unreachable without evidence. A
    # missing precondition is a process gap, not an applicant failure, so it steps up, never
    # rejects. After LIVENESS_FAILURE_LIMIT failures the checks run anyway and the rules route the
    # case to a reviewer with an alternative verification path, with no lockout (R30.2, D-05).
    decided = "[" + ", ".join(f"'{code}'" for code in DECIDED_JURISDICTIONS) + "]"
    s["CheckPreconditions"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": "{% $not($exists($case.liveness)) %}",
             "Next": "StepUpLivenessRequired"},
            {"Condition": ("{% $case.liveness.M.result.S = 'FAILED' and $livenessFailures < "
                           f"{LIVENESS_FAILURE_LIMIT} %}}"),
             "Next": "StepUpLivenessFailed"},
            {"Condition": "{% $not($case.liveness.M.result.S in ['PASS', 'FAILED']) %}",
             "Next": "StepUpLivenessRequired"},
            {"Condition": ("{% $not($exists($case.documents)) or "
                           "$count($case.documents.L) = 0 %}"),
             "Next": "StepUpDocumentsRequired"},
            # R12.4. Which law applies is the bank entity the customer applied to, recorded at
            # intake — not the passport country. A case filed with an entity this deployment does
            # not decide goes to a person rather than being decided under the wrong rules.
            {"Condition": ("{% $not($exists($case.jurisdiction)) or "
                           f"$not($case.jurisdiction.S in {decided}) %}}"),
             "Next": "JurisdictionNotDecided"},
        ],
        "Default": "LoadControls",
    }

    for name, reason, item in (
        ("StepUpLivenessRequired", "LIVENESS_REQUIRED", "A completed liveness check"),
        ("StepUpLivenessFailed", "LIVENESS_FAILED", "A repeated liveness check"),
        ("StepUpDocumentsRequired", "DOCUMENTS_REQUIRED", "At least one identity document"),
    ):
        s[name] = _pass("PersistOutcome", assign={"outcomeRecord": {
            "outcome": "STEP_UP",
            "reasonCode": reason,
            "requiredItem": item,
            "rule": "PRECONDITION",
            "decidedBy": "RULES",
            "agentsInvoked": 0,
        }})

    s["JurisdictionNotDecided"] = _pass(
        "ReviewGate", assign={"reviewReason": "JURISDICTION_NOT_DECIDED"})

    # Demo faults, read per execution. Fail open: an unreadable parameter means no faults, and a
    # fault can only shorten an agent's time, so the worst a bad value does is send a case to review.
    s["LoadControls"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": SSM_GET_PARAMETER,
        "Arguments": {"Name": controls_parameter},
        "Assign": {
            "agentTimeouts": ("{% ($c := $parse($states.result.Parameter.Value); "
                              "$type($c.agentTimeouts) = 'object' ? $c.agentTimeouts : {}) %}"),
        },
        "Catch": _catch("Phase1Parallel"),
        "Next": "Phase1Parallel",
    }

    # ------------------------------------------------------------------------ the evidence
    s["Phase1Parallel"] = {
        "Type": "Parallel",
        "QueryLanguage": "JSONata",
        "Branches": [_phase1_branch(key, harness_arns=harness_arns, model_ids=model_ids)
                     for key in PHASE1_EVIDENCE_AGENTS],
        "Assign": {"branchResults": "{% $states.result %}"},
        # Each branch catches its own failures, so this fires only for something the branches
        # could not absorb. It degrades to review; it never approves.
        "Catch": _catch("DegradedPath"),
        "Next": "PersistEvidence",
    }

    # R2.3 — what the agents said is kept before any outcome, as the reviewer's narrative and the
    # audit record's agent runs. It is not an input to the decision.
    s["PersistEvidence"] = _update(
        case_table,
        update="SET phase1Evidence = :e, updatedAt = :t",
        values={":e": {"S": "{% $string($branchResults) %}"}, ":t": NOW},
        catch=_catch("DegradedPath"),
        next_state="ReloadFacts",
    )

    # The tools recorded their facts while the branches ran, so the copy from LoadCase cannot hold
    # them. Strongly consistent: four concurrent writers finished seconds ago.
    s["ReloadFacts"] = _get_case(case_table, next_state="LoadListVersion",
                                 catch_to="DegradedPath")
    # Read at decision time, after the screening: an update published while the agents ran makes
    # the screening an outdated one, and the rules send it to a person.
    _load_list_version(s, next_state="PreliminaryTier")

    s["PreliminaryTier"] = _engine_task(
        decision_engine_arn,
        payload={
            "phase": 1,
            # Expiry and age are computed against it; the rules are pure and cannot read a clock.
            "asOf": "{% $now() %}",
            "phaseStartedAt": "{% $phaseStartedAt %}",
            "currentListVersion": "{% $currentListVersion %}",
            "productApplied": "{% $case.productApplied.S %}",
            "declaredIdentity": _DECLARED_IDENTITY,
            "screeningDispositions": _DISPOSITIONS,
            # Only the current attempt's failures count: an applicant who passed on the fourth try
            # is not sent to review for the three before it.
            "livenessFailures": ("{% $exists($case.liveness) and "
                                 "$case.liveness.M.result.S = 'FAILED' ? "
                                 "$livenessFailures : 0 %}"),
            "facts": _facts(),
            # Availability only: which agents ran and returned something. Keyed by agent name,
            # which the harness envelope does not carry.
            "agentResults": ("{% $merge($branchResults.{ $lookup($, 'agent') ? "
                             "$lookup($, 'agent') : 'unknown': $ }) %}"),
        },
        next_state="Phase1Decision",
        catch_to="DegradedPath",
    )

    s["Phase1Decision"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            # R13.5 — checked first, so no later condition can approve on missing evidence.
            {"Condition": "{% $count($branchResults[status = 'DEGRADED']) > 0 %}",
             "Next": "DegradedPath"},
            # The one approval edge, on the one approving outcome, named explicitly. It used to be
            # the Default, so any outcome without a branch of its own opened an account.
            {"Condition": "{% $decision.outcome = 'PROVISIONAL_APPROVE_RESTRICTED' %}",
             "Next": "WriteDecisionRecord"},
            {"Condition": "{% $decision.outcome = 'REJECT' %}", "Next": "Reject"},
            {"Condition": "{% $decision.outcome = 'STEP_UP' %}", "Next": "StepUp"},
        ],
        "Default": "EngineReferral",
    }

    s["DegradedPath"] = _pass(
        "ReviewGate",
        comment=("R13.5. A partial pipeline reaches an account only through a person's decision; "
                 "there is no edge from here to the approval path."),
        assign={"reviewReason": "DEGRADED_PIPELINE"})
    s["EngineReferral"] = _pass(
        "ReviewGate",
        assign={"reviewReason": ("{% $decision.outcome = 'MANUAL_REVIEW' ? "
                                 "'ENGINE_MANUAL_REVIEW' : 'UNRECOGNISED_OUTCOME' %}")})

    # ------------------------------------------------------------------ the approval path
    #
    # The record first. The account opens only once its decision record exists, so a failed write
    # leaves nothing open and puts the case in front of a person (R18.3).
    s["WriteDecisionRecord"] = _audit_task(
        audit_writer_arn, phase="phase1", decision="{% $decision %}", pins=pins,
        include_ingress=True,
        # Minted by the writer: the engine never returns a decision id. Reading `$decision.
        # decisionId` failed every provisional approval at Phase2NotStarted.
        assign={"provisionalDecisionId": "{% $states.result.Payload.decisionId %}"},
        next_state="OpenProvisional", catch_to="AuditWriteFailed")
    s["AuditWriteFailed"] = _pass(
        "ReviewGate",
        comment="R18.3. Nothing was opened. A decided case without its record goes to a person.",
        assign={"reviewReason": "AUDIT_RECORD_MISSING"})

    s["OpenProvisional"] = _update(
        case_table,
        update=("SET #s = :state, phase1 = :d, phase2Pending = :p, provisionalDecidedAt = :t, "
                "provisionalDecisionId = :id, updatedAt = :t, " + APPEND_TRANSITION),
        names={"#s": "state"},
        values={
            ":state": {"S": "PROVISIONAL"},
            ":inProgress": {"S": "IN_PROGRESS"},
            ":d": {"S": ("{% $string($merge([$decision, {'decisionId': $provisionalDecisionId, "
                         "'decidedBy': 'RULES'}])) %}")},
            # Feeds the phase2Pending GSI, so a Phase 2 that never starts is detectable.
            ":p": {"S": "PENDING"},
            ":t": NOW,
            ":id": {"S": "{% $provisionalDecisionId %}"},
            ":empty": EMPTY_LIST,
            ":tr": _transition("PROVISIONAL", "{% 'provisional approval: ' & $decision.rule %}"),
        },
        # R21.4 — an account is opened at most once per case, whichever execution gets here.
        condition="#s = :inProgress AND attribute_not_exists(provisionalDecidedAt)",
        catch=_catch_conditional("ProvisionalConflict", "ProvisionalConflict"),
        assign={"caseState": "PROVISIONAL"},
        next_state="OpenAccount",
    )
    s["ProvisionalConflict"] = _pass(
        "ReviewGate", assign={"reviewReason": "PROVISIONAL_NOT_RECORDED"})

    # R31.1 / R31.2 — the restrictions exist at the account platform, which refuses a transaction
    # rather than trusting anything to read a flag.
    s["OpenAccount"] = _account_task(
        account_function_arn, operation="openProvisional",
        arguments={"decisionId": "{% $provisionalDecisionId %}"},
        next_state="TriggerPhase2", catch_to="AccountNotOpened")
    s["AccountNotOpened"] = _fail(
        "AccountNotOpened",
        "The provisional decision is recorded but the account platform did not open the account. "
        "Phase 2 was not started; the phase2Pending GSI still holds the case.")

    # R13.11 — StartExecution WITHOUT .sync: waiting for Phase 2 would put a multi-minute screening
    # job inside the 90-second budget and collapse the two-phase design.
    s["TriggerPhase2"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": START_EXECUTION,
        "Arguments": {
            "StateMachineArn": phase2_state_machine_arn,
            "Input": {
                "applicantId": "{% $applicantId %}",
                "provisionalDecisionId": "{% $provisionalDecisionId %}",
            },
            # R21.7 — a duplicate StartExecution with the same name is rejected. The decision id
            # alone: a real Cognito-derived applicant id is 76 characters, and with the id prefixed
            # the name broke the 80-character limit for every real applicant.
            "Name": "{% $provisionalDecisionId %}",
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 3,
                   "BackoffRate": 2.0}],
        "Catch": _catch("Phase2NotStarted"),
        "End": True,
    }
    s["Phase2NotStarted"] = _fail(
        "Phase2NotStarted",
        "The provisional account is open but Phase 2 did not start. Restrictions remain in force, "
        "and the phase2Pending GSI still holds the case.")

    # ------------------------------------------------------------------ rules outcomes
    s["Reject"] = _pass("PersistOutcome", assign={"outcomeRecord": "{% $decision %}"})
    s["StepUp"] = _pass("PersistOutcome", assign={"outcomeRecord": "{% $decision %}"})

    outcome_state = ("{% $outcomeRecord.outcome = 'REJECT' ? 'DECIDED' "
                     ": 'AWAITING_APPLICANT' %}")
    s["PersistOutcome"] = _update(
        case_table,
        update="SET #s = :state, phase1 = :d, updatedAt = :t, " + APPEND_TRANSITION,
        names={"#s": "state"},
        values={
            ":state": {"S": outcome_state},
            ":d": {"S": "{% $string($outcomeRecord) %}"},
            ":t": NOW,
            ":empty": EMPTY_LIST,
            ":tr": _transition(outcome_state,
                               "{% 'phase 1 outcome: ' & $outcomeRecord.outcome %}"),
        },
        assign={"caseState": outcome_state},
        next_state="NeedsOutcomeRecord",
    )
    # A precondition step-up is not a decision about the applicant: no agent ran, so there is no
    # evidence to record beyond the transition already on the case.
    s["NeedsOutcomeRecord"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{"Condition": "{% $decision != null %}", "Next": "RecordOutcome"}],
        "Default": "Phase1Complete",
    }
    s["RecordOutcome"] = _audit_task(
        audit_writer_arn, phase="phase1", decision="{% $decision %}", pins=pins,
        include_ingress=True, next_state="Phase1Complete", catch_to="OutcomeRecordFailed")
    s["OutcomeRecordFailed"] = _fail(
        "OutcomeRecordFailed",
        "The outcome stands and is on the case record, but its immutable decision record was not "
        "written (R18.3). Failing the execution is the alarm.")
    s["Phase1Complete"] = _succeed("Phase 1 outcome persisted and recorded.")

    # ------------------------------------------------------------------ the review path
    _review_path(
        s, case_table=case_table, harness_arns=harness_arns, model_ids=model_ids,
        review_queue_url=review_queue_url, phase="phase1", pending=_pending_review(),
        route=_route_review(approve="ReviewApproved", reject="ReviewRejected",
                            step_up="ReviewStepUp"),
        abandoned="ReviewAbandoned", not_needed="ReviewNotNeeded")
    s["ReviewNotNeeded"] = _succeed(
        "The case was closed or frozen before its review could be queued; nothing to decide.")
    s["ReviewAbandoned"] = _fail(
        "ReviewAbandoned",
        f"The review wait ended without a decision ({REVIEW_UNRESUMABLE} or a timeout). The case "
        "is unchanged and still needs a person; this is not an infrastructure fault.")

    # APPROVE: the reviewer's decision gets its record, then the account opens exactly as it would
    # have on the approval path. The review handler has already moved the case to PROVISIONAL with
    # the reviewer as the actor, so no second transition is written here.
    reviewer = "{% $reviewOutcome.reviewerId %}"
    s["ReviewApproved"] = _audit_task(
        audit_writer_arn, phase="phase1",
        decision="{% " + _reviewed("PROVISIONAL_APPROVE_RESTRICTED") + " %}", pins=pins,
        actor=reviewer,
        assign={"provisionalDecisionId": "{% $states.result.Payload.decisionId %}"},
        next_state="OpenProvisionalAfterReview", catch_to="ReviewedDecisionUnrecorded")
    s["ReviewedDecisionUnrecorded"] = _fail(
        "ReviewedDecisionUnrecorded",
        "A reviewer approved the case but its decision record could not be written, so the account "
        "was not opened (R18.3). The case stays as the reviewer left it.")
    s["OpenProvisionalAfterReview"] = _update(
        case_table,
        update=("SET phase1 = :d, phase2Pending = :p, provisionalDecidedAt = :t, "
                "provisionalDecisionId = :id, updatedAt = :t"),
        names={"#s": "state"},
        values={
            ":d": {"S": ("{% $string($merge([" + _reviewed("PROVISIONAL_APPROVE_RESTRICTED")
                         + ", {'decisionId': $provisionalDecisionId}])) %}")},
            ":p": {"S": "PENDING"},
            ":t": NOW,
            ":id": {"S": "{% $provisionalDecisionId %}"},
            ":provisional": {"S": "PROVISIONAL"},
        },
        condition="#s = :provisional AND attribute_not_exists(provisionalDecidedAt)",
        catch=_catch_conditional("ReviewOutcomeConflict", "ReviewOutcomeConflict"),
        assign={"caseState": "PROVISIONAL"},
        next_state="OpenAccount",
    )
    s["ReviewOutcomeConflict"] = _fail(
        "ReviewOutcomeConflict",
        "The reviewer's approval could not be applied: the case is not in the state the reviewer "
        "left it in, or it already has a provisional decision.")

    # REJECT and REQUEST_STEP_UP: persisted first, so the case is consistent even if the record
    # write fails, then recorded. The review handler has already set DECIDED / AWAITING_APPLICANT.
    for name, outcome, extra in (
        ("ReviewRejected", "REJECT", ""),
        ("ReviewStepUp", "STEP_UP",
         ", {'requiredItem': 'Additional information requested by a reviewer'}"),
    ):
        record = "$merge([" + _reviewed(outcome) + extra + "])"
        s[name] = _update(
            case_table,
            update="SET phase1 = :d, updatedAt = :t",
            values={":d": {"S": "{% $string(" + record + ") %}"}, ":t": NOW},
            next_state=f"{name}Record",
        )
        s[f"{name}Record"] = _audit_task(
            audit_writer_arn, phase="phase1", decision="{% " + record + " %}", pins=pins,
            actor=reviewer, next_state="Phase1Complete", catch_to="OutcomeRecordFailed")

    return {
        "Comment": "KYC Phase 1 — provisional decision inside 90 seconds (design §7.2)",
        "QueryLanguage": "JSONata",
        "StartAt": "NormalizeInput",
        "States": s,
    }


# ====================================================================================== Phase 2

#: The account posture each Phase 2 outcome moves to, and what the case records about it. Lifts
#: remove restrictions; tighten and freeze only ever add them.
_PHASE2_LIFTS = (
    # (audit state, account state, case state, posture, restrictionChange)
    ("RecordStandardApproval", "LiftAllRestrictions", "EnrolStandardMonitoring",
     "STANDARD", "LIFT_ALL"),
    ("RecordEnhancedApproval", "LiftTransfersKeepCeiling", "EnrolEnhancedMonitoring",
     "ENHANCED", "LIFT_TRANSFERS_KEEP_CEILING"),
)
_PHASE2_RESTRICTIONS = (
    # (account state, operation, posture, case state, audit state, target state, restrictionChange)
    ("Tighten", "restrict", "TIGHTENED", "RecordTightened", "WriteTightenedRecord",
     "RESTRICTED", "TIGHTEN"),
    ("FreezeAccount", "freeze", None, "RecordFrozen", "WriteFrozenRecord",
     "FROZEN", "FREEZE"),
)

#: A reviewer-approved case gets the enhanced posture and the most frequent re-KYC the tier allows.
_RE_KYC_INTERVAL = ("{% $decision != null and $decision.reKycInterval != null ? "
                    "$decision.reKycInterval : 'quarterly' %}")


def _record_phase2(case_table: str, *, record: str, restriction_change: str, next_state: str,
                   to_state: str | None = None, require_state: str | None = None
                   ) -> dict[str, Any]:
    """Write the Phase 2 decision onto the case and clear `phase2Pending`.

    `to_state` is set only for decisions the rules made. After a review, the reviewer's own write
    has already moved the case with the reviewer as the actor, and writing the state again would
    record a transition nobody made. `require_state` guards a lift: the case must still be where
    this execution expects it, so a lift cannot land on a case Phase 3 has frozen in the meantime.
    """
    sets = ["phase2 = :d", "restrictionChange = :r", "fullDecidedAt = :t",
            "reKycInterval = :i", "updatedAt = :t"]
    values: dict[str, Any] = {
        ":d": {"S": record},
        ":r": {"S": restriction_change},
        ":t": NOW,
        ":i": {"S": _RE_KYC_INTERVAL},
    }
    names: dict[str, str] | None = None
    if to_state:
        names = {"#s": "state"}
        sets = ["#s = :state", *sets, APPEND_TRANSITION]
        values.update({
            ":state": {"S": to_state},
            ":empty": EMPTY_LIST,
            ":tr": _transition(to_state, "{% 'phase 2 outcome: ' & $decision.outcome %}"),
        })
    condition = None
    if require_state:
        names = {"#s": "state"}
        values[":required"] = {"S": require_state}
        condition = "#s = :required"
    return _update(
        case_table,
        update="SET " + ", ".join(sets) + " REMOVE phase2Pending",
        names=names,
        values=values,
        condition=condition,
        catch=_catch("Phase2Failed"),
        assign={"caseState": to_state} if to_state else None,
        next_state=next_state,
    )


def phase2_definition(
    *,
    case_table: str,
    harness_arns: dict[str, str],
    model_ids: dict[str, str],
    decision_engine_arn: str,
    audit_writer_arn: str,
    review_queue_url: str,
    account_function_arn: str,
    audit_pins: dict[str, str],
) -> dict[str, Any]:
    """Phase 2, per design §7.4: politically exposed persons and adverse media, then the full tier.

    The invariant that matters is negative: **no failure or timeout path reaches a state that lifts
    a restriction** (Property 3, R31.6). Silence is not permission. A lift is reachable only from
    the rules' two approving outcomes and from a reviewer's APPROVE, and only after the decision
    record is written. Tighten and freeze act first and record after. A frozen account never
    reaches MONITORING.
    """
    pins = _require_pins(audit_pins)
    s: dict[str, Any] = {}

    s["LoadCase"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": DDB_GET_ITEM,
        "Arguments": {"TableName": case_table,
                      "Key": _case_key("{% $states.input.applicantId %}"),
                      "ConsistentRead": True},
        "Assign": {
            "applicantId": "{% $states.input.applicantId %}",
            # An absent field is undefined, and undefined kills the state. Phase 1 always sends
            # it; the guard covers a manual re-drive or a sweeper retry.
            "provisionalDecisionId": ("{% $exists($states.input.provisionalDecisionId) ? "
                                      "$states.input.provisionalDecisionId : 'unknown' %}"),
            "phaseStartedAt": "{% $states.context.Execution.StartTime %}",
            "case": "{% $states.result.Item %}",
            "caseState": ("{% $exists($states.result.Item.state) ? "
                          "$states.result.Item.state.S : 'UNKNOWN' %}"),
            "caseView": _case_view("$states.result.Item", "{% $states.input.applicantId %}"),
            "decision": "{% null %}",
            "complianceBranch": "{% null %}",
            "rescreenBranch": "{% null %}",
            "account": "{% null %}",
            "reviewReason": "{% null %}",
            "reviewOutcome": "{% null %}",
            "reviewWaits": "{% 0 %}",
            "phase2DecisionId": "{% null %}",
            "briefingResult": "{% null %}",
            "notifyResult": "{% null %}",
            "currentListVersion": "{% null %}",
        },
        "Retry": TRANSIENT_RETRY,
        "Catch": _catch("CaseNotFound"),
        "Next": "CheckPhase2Idempotency",
    }
    s["CaseNotFound"] = _fail("CaseNotFound", "Phase 2 was started for a case that does not exist.")

    # R21.7 — idempotent on the case. `phase2` is written before a review wait as well as on an
    # outcome, so a re-drive during a review cannot start the work again either.
    s["CheckPhase2Idempotency"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": "{% $exists($case.phase2) %}", "Next": "AlreadyComplete"},
            {"Condition": "{% $not($exists($case.provisionalDecidedAt)) %}",
             "Next": "NotProvisional"},
        ],
        "Default": "ReadAccount",
    }
    s["AlreadyComplete"] = _succeed("Phase 2 already ran, or is waiting on a person (R21.7).")
    s["NotProvisional"] = _fail(
        "NotProvisional",
        "Phase 2 started for a case with no provisional decision; there is no account to review.")

    # --------------------------------------------------------------- the opening message
    #
    # Off Phase 1's clock: the applicant has their account inside 90 seconds, and the message
    # explaining its limits follows here. Every failure skips the message, never the screening.
    s["ReadAccount"] = _account_task(
        account_function_arn, operation="getAccount",
        next_state="NotifyProvisional", catch_to="LoadListVersion", keep_account=True)
    notify = AGENT_BY_KEY["notify"]
    s["NotifyProvisional"] = agent_task(
        notify, harness_arn=harness_arns["notify"], model_id=model_ids["notify"],
        next_state="PersistProvisionalNotice", catch_to="LoadListVersion",
        prompt_expression=_notify_prompt("ACCOUNT_OPENED_WITH_LIMITS"), keep_reply=True)
    s["PersistProvisionalNotice"] = _persist_notice(
        case_table, kind="ACCOUNT_OPENED_WITH_LIMITS", next_state="LoadListVersion")

    # ------------------------------------------------------------- the list, once more
    #
    # Phase 2 reads Phase 1's screening rather than repeating it — unless the list has moved since.
    # A case can wait for a reviewer for days between its screening and its account opening, and a
    # list update published in that window would otherwise never reach it: the account would open,
    # and here its restrictions lift, on a screening of the old list. Read once, here: an update
    # published after this point reaches the account through Phase 3, whose holds and freezes this
    # execution cannot undo.
    _load_list_version(s, next_state="CheckListCurrent")
    s["CheckListCurrent"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{
            "Condition": ("{% $exists($case.factScreening) and "
                          "$parse($case.factScreening.S).listVersion = $currentListVersion %}"),
            "Next": "ComplianceRisk",
        }],
        "Default": "ReScreen",
    }
    prescreen = AGENT_BY_KEY["prescreen"]
    s["ReScreen"] = agent_task(
        prescreen,
        harness_arn=harness_arns["prescreen"],
        model_id=model_ids["prescreen"],
        next_state="ReScreenCheck",
        # A failed re-screen leaves the screening on the old list, which the rules refuse to
        # decide on. The compliance checks still run, so the reviewer has the full picture.
        catch_to="ReScreenDegraded",
        prompt_expression=("{% $string($merge([$caseView, {'phase': 'phase2', "
                           "'reason': 'the screening list was updated'}])) %}"),
        timeout_seconds=RESCREEN_TIMEOUT_SECONDS,
    )
    s["ReScreenCheck"] = _emptiness_check(ok="ReScreenOk", degraded="ReScreenDegraded")
    s["ReScreenOk"] = _pass("ComplianceRisk", assign={
        "rescreenBranch": _ok_record("prescreen", model_ids["prescreen"])})
    s["ReScreenDegraded"] = _pass("ComplianceRisk",
                                  assign={"rescreenBranch": _degraded_record("prescreen")})

    # ----------------------------------------------------------------------- the evidence
    compliance = AGENT_BY_KEY["compliance"]
    s["ComplianceRisk"] = agent_task(
        compliance,
        harness_arn=harness_arns["compliance"],
        model_id=model_ids["compliance"],
        next_state="ComplianceCheck",
        # A failed agent is an unavailable result, which the rules send to a person. The facts its
        # tools recorded before it failed still count.
        catch_to="ComplianceDegraded",
        prompt_expression="{% $string($merge([$caseView, {'phase': 'phase2'}])) %}",
        timeout_seconds=COMPLIANCE_TIMEOUT_SECONDS,
    )
    s["ComplianceCheck"] = _emptiness_check(ok="ComplianceOk", degraded="ComplianceDegraded")
    s["ComplianceOk"] = _pass("PersistPhase2Evidence", assign={
        "complianceBranch": _ok_record("compliance", model_ids["compliance"])})
    s["ComplianceDegraded"] = _pass("PersistPhase2Evidence",
                                    assign={"complianceBranch": _degraded_record("compliance")})

    s["PersistPhase2Evidence"] = _update(
        case_table,
        update="SET phase2Evidence = :e, updatedAt = :t",
        # `$append`, so one agent run is still a list: the audit writer reads a list.
        values={":e": {"S": ("{% $string($append($rescreenBranch != null ? [$rescreenBranch] "
                             ": [], [$complianceBranch])) %}")},
                ":t": NOW},
        catch=_catch("Phase2Failed"),
        next_state="ReloadFacts",
    )
    s["ReloadFacts"] = _get_case(case_table, next_state="FullTier", catch_to="Phase2Failed")

    s["FullTier"] = _engine_task(
        decision_engine_arn,
        payload={
            "phase": 2,
            "asOf": "{% $now() %}",
            "phaseStartedAt": "{% $phaseStartedAt %}",
            "productApplied": "{% $case.productApplied.S %}",
            "declaredIdentity": _DECLARED_IDENTITY,
            "screeningDispositions": _DISPOSITIONS,
            # All six. Phase 2 owns PEP and adverse media and reads Phase 1's document, face,
            # velocity and screening facts as recorded, so both tiers rest on the same evidence
            # (R4.4) and no Phase 1 value is re-derived from prose.
            "facts": _facts(),
            "currentListVersion": "{% $currentListVersion %}",
            "agentResults": ("{% $merge([{'compliance': $complianceBranch}, "
                             "$rescreenBranch != null ? {'prescreen': $rescreenBranch} : {}]) %}"),
        },
        next_state="Phase2Decision",
        catch_to="Phase2Failed",
    )

    s["Phase2Decision"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            # The same structural guard as Phase 1 (R13.5), checked before any lift. The rules
            # already route an unavailable agent to review; this makes it a property of the graph
            # too, so loosening a rule later cannot lift restrictions on missing evidence.
            {"Condition": ("{% $complianceBranch.status = 'DEGRADED' or "
                           "($rescreenBranch != null and $rescreenBranch.status = 'DEGRADED') %}"),
             "Next": "ComplianceReferral"},
            {"Condition": "{% $decision.outcome = 'REJECT' %}", "Next": "FreezeAccount"},
            {"Condition": "{% $decision.outcome = 'RESTRICT_FURTHER' %}", "Next": "Tighten"},
            {"Condition": "{% $decision.outcome = 'FULL_APPROVE_ENHANCED_MONITORING' %}",
             "Next": "RecordEnhancedApproval"},
            {"Condition": "{% $decision.outcome = 'FULL_APPROVE_STANDARD' %}",
             "Next": "RecordStandardApproval"},
        ],
        # No approval by default. An unrecognised outcome keeps restrictions and asks a person, so
        # adding an outcome later cannot start lifting restrictions by accident.
        "Default": "Phase2Referral",
    }
    s["Phase2Referral"] = _pass(
        "ReviewGate",
        assign={"reviewReason": ("{% $decision.outcome = 'MANUAL_REVIEW' ? "
                                 "'ENGINE_MANUAL_REVIEW' : 'UNRECOGNISED_OUTCOME' %}")})
    s["ComplianceReferral"] = _pass("ReviewGate", assign={"reviewReason": "DEGRADED_PIPELINE"})

    def linked(record: str) -> str:
        """A Phase 2 record names the provisional decision it supersedes, so the two immutable
        records of one application can be joined without the mutable case record."""
        return ("$merge([" + record + ", {'supersedesDecisionId': $provisionalDecisionId}])")

    rules_decision = "{% " + linked("$decision") + " %}"

    # ---------------------------------------------------------------- rules: lift (R31.3/4)
    for audit_state, account_state, case_state, posture, change in _PHASE2_LIFTS:
        s[audit_state] = _audit_task(
            audit_writer_arn, phase="phase2", decision=rules_decision, pins=pins,
            assign={"phase2DecisionId": "{% $states.result.Payload.decisionId %}"},
            next_state=account_state, catch_to="Phase2AuditMissing")
        # Refused by the platform if the account is frozen, so a Phase 3 freeze that landed while
        # Phase 2 ran cannot be undone by it.
        s[account_state] = _account_task(
            account_function_arn, operation="lift",
            arguments={"posture": posture, "outcome": "{% $decision.outcome %}",
                       "decisionId": "{% $phase2DecisionId %}"},
            next_state=case_state, catch_to="Phase2Failed", keep_account=True)
        # The only states in this machine that write MONITORING.
        s[case_state] = _record_phase2(
            case_table,
            record=("{% $string($merge([" + linked("$decision") + ", "
                    "{'decisionId': $phase2DecisionId, 'decidedBy': 'RULES'}])) %}"),
            restriction_change=change, to_state="MONITORING", require_state="PROVISIONAL",
            next_state="NotifyOutcome")
    s["Phase2AuditMissing"] = _pass(
        "ReviewGate",
        comment="R18.3. Nothing was lifted. A decision without its record goes to a person.",
        assign={"reviewReason": "AUDIT_RECORD_MISSING"})

    # ---------------------------------------------------------- rules: restrict (R31.5)
    for (account_state, operation, posture, case_state, audit_state, target,
         change) in _PHASE2_RESTRICTIONS:
        arguments: dict[str, Any] = {"outcome": "{% $decision.outcome %}",
                                     "reason": "{% $decision.rule %}"}
        if posture:
            arguments["posture"] = posture
        s[account_state] = _account_task(
            account_function_arn, operation=operation, arguments=arguments,
            next_state=case_state, catch_to="Phase2Failed", keep_account=True)
        s[case_state] = _record_phase2(
            case_table,
            record=("{% $string($merge([" + linked("$decision") + ", "
                    "{'decidedBy': 'RULES'}])) %}"),
            restriction_change=change, to_state=target, next_state=audit_state)
        s[audit_state] = _audit_task(
            audit_writer_arn, phase="phase2", decision=rules_decision, pins=pins,
            next_state="NotifyOutcome", catch_to="Phase2Failed")

    # -------------------------------------------------------------------- the review path
    _review_path(
        s, case_table=case_table, harness_arns=harness_arns, model_ids=model_ids,
        review_queue_url=review_queue_url, phase="phase2", pending=_pending_review(),
        route=_route_review(approve="RecordReviewedApproval", reject="ReviewFreezeAccount",
                            step_up="ReviewTighten"),
        # Restrictions stay exactly as Phase 1 left them.
        abandoned="Phase2Failed",
        # Frozen or closed while this decision waited for another review: nothing in Phase 2 can
        # apply any more, and a person should know Phase 2 never finished.
        not_needed="Phase2Failed")

    reviewer = "{% $reviewOutcome.reviewerId %}"
    approved = linked(_reviewed("FULL_APPROVE_ENHANCED_MONITORING"))
    s["RecordReviewedApproval"] = _audit_task(
        audit_writer_arn, phase="phase2", decision="{% " + approved + " %}", pins=pins,
        actor=reviewer,
        assign={"phase2DecisionId": "{% $states.result.Payload.decisionId %}"},
        next_state="ReviewLiftTransfersKeepCeiling", catch_to="Phase2Failed")
    # A reviewer's approval lifts to the enhanced posture, never the standard one: the case needed
    # a person, which is itself a reason to keep the deposit ceiling and monitor more closely.
    s["ReviewLiftTransfersKeepCeiling"] = _account_task(
        account_function_arn, operation="lift",
        arguments={"posture": "ENHANCED", "outcome": "FULL_APPROVE_ENHANCED_MONITORING",
                   "decisionId": "{% $phase2DecisionId %}"},
        next_state="RecordReviewedApprovalOnCase", catch_to="Phase2Failed", keep_account=True)
    s["RecordReviewedApprovalOnCase"] = _record_phase2(
        case_table,
        record=("{% $string($merge([" + approved + ", "
                "{'decisionId': $phase2DecisionId}])) %}"),
        restriction_change="LIFT_TRANSFERS_KEEP_CEILING", require_state="MONITORING",
        next_state="NotifyOutcome")

    for account_state, operation, posture, case_state, audit_state, outcome, change in (
        ("ReviewFreezeAccount", "freeze", None, "RecordReviewedFreeze",
         "WriteReviewedFreezeRecord", "REJECT", "FREEZE"),
        ("ReviewTighten", "restrict", "TIGHTENED", "RecordReviewedTighten",
         "WriteReviewedTightenRecord", "RESTRICT_FURTHER", "TIGHTEN"),
    ):
        arguments = {"outcome": outcome, "reason": "{% 'REVIEW.' & $reviewOutcome.action %}"}
        if posture:
            arguments["posture"] = posture
        s[account_state] = _account_task(
            account_function_arn, operation=operation, arguments=arguments,
            next_state=case_state, catch_to="Phase2Failed", keep_account=True)
        reviewed = linked(_reviewed(outcome))
        s[case_state] = _record_phase2(
            case_table, record="{% $string(" + reviewed + ") %}",
            restriction_change=change, next_state=audit_state)
        s[audit_state] = _audit_task(
            audit_writer_arn, phase="phase2", decision="{% " + reviewed + " %}",
            pins=pins, actor=reviewer, next_state="NotifyOutcome", catch_to="Phase2Failed")

    # -------------------------------------------------------------------- the message
    s["NotifyOutcome"] = agent_task(
        notify, harness_arn=harness_arns["notify"], model_id=model_ids["notify"],
        next_state="PersistOutcomeNotice",
        # The copy failing must not undo a restriction change that already landed.
        catch_to="Phase2Complete",
        prompt_expression=_notify_prompt("ACCOUNT_UPDATED"), keep_reply=True)
    s["PersistOutcomeNotice"] = _persist_notice(
        case_table, kind="ACCOUNT_UPDATED", next_state="Phase2Complete")
    s["Phase2Complete"] = _succeed("Phase 2 outcome applied at the account and recorded.")

    # R31.6 / Property 3. One outgoing edge, to a Fail. It touches neither the account nor the
    # case, so the account stays exactly as restricted as it was.
    s["Phase2Failed"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Comment": ("Restrictions PERSIST. No edge from here lifts anything (R31.6, Property 3): "
                    "silence must not equal permission. Raises an alarm metric and fails loudly."),
        "Resource": PUT_METRIC_DATA,
        "Arguments": {
            "Namespace": "KYC90/Phase2",
            "MetricData": [{"MetricName": "Phase2Failed", "Value": 1, "Unit": "Count"}],
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 1, "MaxAttempts": 2,
                   "BackoffRate": 2.0}],
        # Even if the alarm write fails, fail the execution: swallowing it would leave a restricted
        # account with no signal that anyone needs to look.
        "Catch": _catch("Phase2FailedTerminal"),
        "Next": "Phase2FailedTerminal",
    }
    s["Phase2FailedTerminal"] = _fail(
        "Phase2Failed",
        "Phase 2 did not complete. Restrictions remain in force. Requires human action; it will "
        "not resolve itself.")

    return {
        "Comment": "KYC Phase 2 — full screening, no clock pressure (design §7.4)",
        "QueryLanguage": "JSONata",
        "StartAt": "LoadCase",
        "States": s,
    }


def _persist_notice(case_table: str, *, kind: str, next_state: str) -> dict[str, Any]:
    """Append the applicant message to the case. The API filters it before anyone sees it."""
    return _update(
        case_table,
        update=("SET notifications = list_append(if_not_exists(notifications, :empty), :n), "
                "updatedAt = :t"),
        values={
            ":empty": EMPTY_LIST,
            ":t": NOW,
            ":n": {"L": [{"M": {
                "kind": {"S": kind},
                "text": {"S": "{% $join([$notifyResult.Message.Content.Text], '\\n') %}"},
                "accountState": {"S": ("{% $account != null and $exists($account.state) ? "
                                       "$account.state : 'UNKNOWN' %}")},
                "at": NOW,
            }}]},
        },
        catch=_catch(next_state),
        next_state=next_state,
    )


# ====================================================================================== Phase 3

#: The day-zero decision, read from where it actually is: `phase2` when Phase 2 has decided, else
#: `phase1`. An unknown tier reads as LOW so that a re-assessment can only find risk the same or
#: higher — defaulting to HIGH would hide a new HIGH alert as "unchanged".
_DAY0 = (
    "{% ($d := $exists($case.phase2) ? $parse($case.phase2.S) : "
    "($exists($case.phase1) ? $parse($case.phase1.S) : {}); {"
    "'caseId': $case.caseId.S, "
    "'tier': $exists($d.tier) and $d.tier != null ? $d.tier : 'LOW', "
    "'outcome': $exists($d.outcome) and $d.outcome != null ? $d.outcome : 'UNKNOWN', "
    "'decidedAt': $exists($case.fullDecidedAt) ? $case.fullDecidedAt.S : "
    "($exists($case.provisionalDecidedAt) ? $case.provisionalDecidedAt.S : ''), "
    "'version': $exists($case.decisionVersion) ? $number($case.decisionVersion.N) : 1"
    "}) %}")

#: The posture a reviewer's clearance restores: what Phase 2 granted, or the provisional
#: restrictions if Phase 2 had not decided yet. Never more than the customer had before the event.
#: `review/decisions.py` derives the case state for the same clearance from the same record, and
#: `infra/tests/test_jsonata_expressions.py` asserts the two agree.
_DAY0_POSTURE = (
    "{% ($o := $exists($case.phase2) ? $parse($case.phase2.S).outcome : null; "
    "$o = 'FULL_APPROVE_STANDARD' ? 'STANDARD' : "
    "($o = 'FULL_APPROVE_ENHANCED_MONITORING' ? 'ENHANCED' : "
    "($o = 'RESTRICT_FURTHER' ? 'TIGHTENED' : 'PROVISIONAL'))) %}")


def _set_risk_state(case_table: str, *, risk_state: str, next_state: str, catch_to: str
                    ) -> dict[str, Any]:
    """Move the CURRENT risk view on the case. Never touches a decision.

    Narrow on purpose: `riskState`, the decision version and timestamps. The decision history is
    append-only and lives in a separate table, so this cannot reach it even by accident.
    """
    return _update(
        case_table,
        update=("SET riskState = :s, decisionVersion = :v, lastReassessedAt = :t, "
                "updatedAt = :t"),
        values={":s": {"S": risk_state},
                ":v": {"N": "{% $string($decision.version) %}"},
                ":t": NOW},
        # A Catch must never land somewhere less restrictive than the state that failed.
        catch=_catch(catch_to),
        next_state=next_state,
    )


def _history_row(history_table: str, *, sort_key: str, item: dict[str, Any], next_state: str,
                 on_duplicate: str, catch_to: str) -> dict[str, Any]:
    """An INSERT into the decision history, never an update (`attribute_not_exists(sk)`)."""
    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": DDB_PUT_ITEM,
        "Arguments": {
            "TableName": history_table,
            "Item": {
                "customerId": {"S": "{% $applicantId %}"},
                "sk": {"S": sort_key},
                "caseId": {"S": "{% $case.caseId.S %}"},
                "eventId": {"S": "{% $riskEvent.eventId %}"},
                "eventType": {"S": "{% $riskEvent.eventType %}"},
                "executionName": {"S": "{% $states.context.Execution.Name %}"},
                **item,
            },
            "ConditionExpression": "attribute_not_exists(sk)",
        },
        "Retry": TRANSIENT_RETRY,
        # The failed condition is the expected outcome of a replay, not an error.
        "Catch": _catch_conditional(on_duplicate, catch_to),
        "Next": next_state,
    }


def phase3_definition(
    *,
    case_table: str,
    history_table: str,
    decision_engine_arn: str,
    audit_writer_arn: str,
    review_queue_url: str,
    harness_arns: dict[str, str],
    model_ids: dict[str, str],
    account_function_arn: str,
    audit_pins: dict[str, str],
) -> dict[str, Any]:
    """Phase 3 — lifecycle re-assessment of an open account. G-01, R21, R31.

    Triggered by a risk event on the lifecycle bus. The one the demo drives is a sanctions list
    update: the Pre-Screen agent re-runs the same screening tool against the updated list, the tool
    records a fresh screening fact, and the rules re-assess the customer on it.

    **STANDARD, not Express.** Exactly-once, and the idempotency that makes an archive replay safe.

    **The history write is an INSERT, never an update.** `PutItem` with `attribute_not_exists(sk)`,
    where the sort key is derived from the event (`lifecycle.history_sort_key`), so a replayed
    event lands on the same key and is refused. The day-zero decision is structurally unreachable:
    nothing in this graph writes `phase1` or `phase2`.

    **The account moves before the paperwork when it tightens, after it when it relaxes.** A
    confirmed match freezes the account now and a person confirms it afterwards — sanctions breaches
    are strict liability, and waiting for the review would leave a listed person able to move money.
    Any other rise in risk restricts the account and queues a review, where APPROVE restores the
    day-zero posture and REJECT freezes. Nothing restores an account without a person.
    """
    pins = _require_pins(audit_pins)
    s: dict[str, Any] = {}

    # The day-zero decision, read but never written. Strongly consistent, because an event seconds
    # after a Phase 2 decision would otherwise compare against a tier that has already moved.
    s["LoadDayZero"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": DDB_GET_ITEM,
        "Arguments": {
            "TableName": case_table,
            "Key": _case_key("{% $states.input.detail.customerId %}"),
            "ConsistentRead": True,
        },
        "Assign": {
            "riskEvent": "{% $states.input.detail %}",
            "applicantId": "{% $states.input.detail.customerId %}",
            # A fact recorded before this (less the engine's tolerance) is stale: the re-screen
            # must produce a new one, or the control did not run.
            "phaseStartedAt": "{% $states.context.Execution.StartTime %}",
            "case": "{% $states.result.Item %}",
            "caseState": ("{% $exists($states.result.Item.state) ? "
                          "$states.result.Item.state.S : 'UNKNOWN' %}"),
            "caseView": _case_view("$states.result.Item",
                                   "{% $states.input.detail.customerId %}"),
            "decision": "{% null %}",
            "rescreenBranch": "{% null %}",
            "reviewReason": "{% null %}",
            "reviewOutcome": "{% null %}",
            "reviewWaits": "{% 0 %}",
            "briefingResult": "{% null %}",
            "currentListVersion": "{% null %}",
        },
        "Retry": TRANSIENT_RETRY,
        # An event for a customer with no case is a routing bug. No day zero is invented for it.
        "Catch": _catch("EventUnroutable"),
        "Next": "CheckCaseExists",
    }

    s["CheckCaseExists"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": "{% $not($exists($case.applicantId)) %}", "Next": "EventUnroutable"},
            # Nothing to re-assess without an open relationship: never opened, rejected, or
            # already frozen.
            {"Condition": ("{% $not($exists($case.provisionalDecidedAt)) or "
                           "$case.state.S in ['FROZEN', 'DECIDED'] %}"),
             "Next": "NoOpenAccount"},
        ],
        "Default": "RecordExecution",
    }
    # The run names itself on the case, so the console's live view can follow it while it runs:
    # EventBridge starts Phase 3 under a generated name. Bookkeeping only, so it never stands in
    # the way: conditional on the case existing (an UpdateItem would otherwise create one), and
    # any failure carries straight on to the re-assessment.
    s["RecordExecution"] = _update(
        case_table,
        update="SET phase3Execution = :x",
        values={":x": {"S": "{% $states.context.Execution.Name %}"}},
        condition="attribute_exists(applicantId)",
        catch=[{"ErrorEquals": ["States.ALL"], "Next": "ReadDayZero"}],
        next_state="ReadDayZero",
    )
    s["ReadDayZero"] = _pass("ReScreen", assign={"day0": _DAY0, "day0Posture": _DAY0_POSTURE})

    # ---------------------------------------------------------------------- the re-screen
    prescreen = AGENT_BY_KEY["prescreen"]
    s["ReScreen"] = agent_task(
        prescreen,
        harness_arn=harness_arns["prescreen"],
        model_id=model_ids["prescreen"],
        next_state="ReScreenCheck",
        catch_to="ReScreenDegraded",
        prompt_expression=("{% $string($merge([$caseView, {'reason': 'lifecycle re-screen', "
                           "'eventType': $riskEvent.eventType}])) %}"),
        timeout_seconds=RESCREEN_TIMEOUT_SECONDS,
    )
    s["ReScreenCheck"] = _emptiness_check(ok="ReScreenOk", degraded="ReScreenDegraded")
    s["ReScreenOk"] = _pass("PersistPhase3Evidence", assign={
        "rescreenBranch": _ok_record("prescreen", model_ids["prescreen"])})
    # Not a separate decision path. Whether the control ran is read from the fact itself: a
    # re-screen that failed leaves the screening fact older than this execution, and the engine
    # returns SYSTEM_ERROR for it — never "risk unchanged" (R25.7).
    s["ReScreenDegraded"] = _pass("PersistPhase3Evidence",
                                  assign={"rescreenBranch": _degraded_record("prescreen")})
    s["PersistPhase3Evidence"] = _update(
        case_table,
        update="SET phase3Evidence = :e, updatedAt = :t",
        values={":e": {"S": "{% $string([$rescreenBranch]) %}"}, ":t": NOW},
        catch=_catch("ReloadFacts"),
        next_state="ReloadFacts",
    )
    s["ReloadFacts"] = _get_case(case_table, next_state="LoadListVersion",
                                 catch_to="ReassessFailed")
    # A re-screen of anything but the current list is not the control this event asked for —
    # the tool's short cache can serve the list from just before the update — so the rules
    # return SYSTEM_ERROR for it rather than "unchanged".
    _load_list_version(s, next_state="Reassess")

    s["Reassess"] = _engine_task(
        decision_engine_arn,
        payload={
            "phase": 3,
            "asOf": "{% $now() %}",
            # The rules own the record but cannot read a clock (design §8).
            "decidedAt": "{% $states.context.Execution.StartTime %}",
            "phaseStartedAt": "{% $phaseStartedAt %}",
            "currentListVersion": "{% $currentListVersion %}",
            "productApplied": "{% $case.productApplied.S %}",
            "declaredIdentity": _DECLARED_IDENTITY,
            "screeningDispositions": _DISPOSITIONS,
            "facts": _facts(),
            "riskEvent": "{% $riskEvent %}",
            "day0": "{% $day0 %}",
        },
        next_state="AppendDecision",
        catch_to="ReassessFailed",
    )
    s["ReassessFailed"] = _pass(
        "ReviewGate",
        comment=("A re-assessment that cannot complete reaches a person. The day-zero decision "
                 "and the account are unchanged."),
        assign={"reviewReason": "REASSESSMENT_FAILED"})

    # The append. `attribute_not_exists(sk)` makes it idempotent AND makes the history immutable.
    s["AppendDecision"] = _history_row(
        history_table,
        sort_key="{% $decision.historySortKey %}",
        item={
            "outcome": {"S": "{% $decision.outcome %}"},
            "day0Tier": {"S": "{% $decision.day0Tier %}"},
            "newTier": {"S": "{% $decision.newTier %}"},
            # `Bool`, not `BOOL`: Step Functions rejects the wire-format spelling.
            "riskChanged": {"Bool": "{% $decision.riskChanged %}"},
            "version": {"N": "{% $string($decision.version) %}"},
            "decidedAt": {"S": "{% $decision.decidedAt %}"},
            "explanation": {"S": "{% $decision.explanation %}"},
            # The list the re-screen actually ran against, which is what an examiner asks about.
            "listVersion": {"S": (
                "{% $exists($decision.factsRead.listCurrency.screenedAgainst) and "
                "$decision.factsRead.listCurrency.screenedAgainst != null ? "
                "$decision.factsRead.listCurrency.screenedAgainst : 'UNKNOWN' %}")},
            "currentListVersion": {"S": "{% $currentListVersion %}"},
        },
        next_state="RecordAudit",
        on_duplicate="DuplicateEventIgnored",
        catch_to="HistoryNotRecorded",
    )
    # The history could not be written. A freeze still happens — strict liability does not wait on
    # a database — and anything else goes to a person rather than being applied unrecorded.
    s["HistoryNotRecorded"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{"Condition": "{% $decision.outcome = 'FREEZE' %}", "Next": "FreezeAccount"}],
        "Default": "HistoryReferral",
    }
    s["HistoryReferral"] = _pass("ReviewGate", assign={"reviewReason": "HISTORY_NOT_RECORDED"})

    s["RecordAudit"] = _audit_task(
        audit_writer_arn, phase="phase3", decision="{% $decision %}", pins=pins,
        # The history row above is the durable record of this decision; the Object Lock copy
        # failing is logged by the writer and does not hold up the account action.
        next_state="RouteOutcome", catch_to="RouteOutcome")

    # Only the current view moves. The day-zero decision is untouched by every branch below.
    s["RouteOutcome"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": "{% $decision.outcome = 'FREEZE' %}", "Next": "FreezeAccount"},
            {"Condition": "{% $decision.outcome = 'RISK_INCREASED_REVIEW' %}",
             "Next": "RestrictForReview"},
            {"Condition": "{% $decision.outcome = 'RISK_INCREASED_RESTRICT' %}",
             "Next": "ReapplyRestrictions"},
            {"Condition": "{% $decision.outcome = 'SYSTEM_ERROR' %}",
             "Next": "RescreenIncomplete"},
        ],
        "Default": "RecordUnchanged",
    }

    # FREEZE: the account first, then the risk view, then a person confirms the match.
    s["FreezeAccount"] = _account_task(
        account_function_arn, operation="freeze",
        arguments={"outcome": "{% $decision.outcome %}",
                   "reason": "{% $riskEvent.eventType %}"},
        next_state="FrozenRiskState", catch_to="FreezeNotApplied")
    # A failed freeze still reaches a person, flagged as such. Silently continuing would leave a
    # customer unfrozen with no alert.
    s["FreezeNotApplied"] = _pass("ReviewGate", assign={"reviewReason": "ACCOUNT_FREEZE_FAILED"})
    s["FrozenRiskState"] = _set_risk_state(
        case_table, risk_state="FROZEN", next_state="FrozenReferral", catch_to="FrozenReferral")
    s["FrozenReferral"] = _pass("ReviewGate", assign={"reviewReason": "ACCOUNT_FROZEN"})

    # RISK_INCREASED_REVIEW: back to the provisional restrictions while a person decides, under a
    # hold named for this event. Nothing lifts below it — a Phase 2 approval decided on older
    # evidence included — until the reviewer's clearance of this event releases it.
    s["RestrictForReview"] = _account_task(
        account_function_arn, operation="restrict",
        arguments={"posture": "PROVISIONAL", "outcome": "{% $decision.outcome %}",
                   "reason": "{% $riskEvent.eventType %}", "hold": "{% $riskEvent.eventId %}"},
        next_state="RestrictedRiskState", catch_to="RestrictionNotApplied")
    s["RestrictionNotApplied"] = _pass(
        "ReviewGate", assign={"reviewReason": "RESTRICTION_NOT_APPLIED"})
    s["RestrictedRiskState"] = _set_risk_state(
        case_table, risk_state="RESTRICTED", next_state="RiskReferral", catch_to="RiskReferral")
    s["RiskReferral"] = _pass("ReviewGate", assign={"reviewReason": "RISK_INCREASED"})

    # RISK_INCREASED_RESTRICT: the enhanced posture — deposit ceiling back on — with no review.
    # `restrict` only adds restrictions, so this can never lift one the account still has.
    s["ReapplyRestrictions"] = _account_task(
        account_function_arn, operation="restrict",
        # Held too: MEDIUM risk keeps at least the enhanced posture until a person says otherwise,
        # so a Phase 2 that decided LOW on older evidence cannot lift it to standard.
        arguments={"posture": "ENHANCED", "outcome": "{% $decision.outcome %}",
                   "reason": "{% $riskEvent.eventType %}", "hold": "{% $riskEvent.eventId %}"},
        next_state="ReappliedRiskState", catch_to="RestrictionNotApplied")
    s["ReappliedRiskState"] = _set_risk_state(
        case_table, risk_state="RESTRICTED", next_state="Done",
        catch_to="RestrictionNotApplied")

    s["RescreenIncomplete"] = _pass("ReviewGate", assign={"reviewReason": "RESCREEN_INCOMPLETE"})

    s["RecordUnchanged"] = _update(
        case_table,
        update="SET decisionVersion = :v, lastReassessedAt = :t, updatedAt = :t",
        values={":v": {"N": "{% $string($decision.version) %}"}, ":t": NOW},
        catch=_catch("Done"),
        next_state="Done",
    )

    # -------------------------------------------------------------------- the review path
    _review_path(
        s, case_table=case_table, harness_arns=harness_arns, model_ids=model_ids,
        review_queue_url=review_queue_url, phase="phase3",
        # The new tier travels as `tier`, which is what stops a reviewer clearing a PROHIBITED
        # re-assessment: a confirmed match is not a reviewer's to overturn.
        pending=_pending_review(
            ", 'tier': $decision != null ? $decision.newTier : null, "
            "'eventId': $riskEvent.eventId, 'eventType': $riskEvent.eventType"),
        # Lifecycle reviews are clear or exit; there is nothing to ask the customer for here.
        route=_route_review(approve="AppendReviewClearance", reject="ReviewFreezeAccount",
                            step_up=None),
        abandoned="ReviewAbandoned",
        # Frozen or closed by the review this one waited behind: the account is already off.
        not_needed="Done",
        # One review per event: the previous event's review decision is cleared so the handler's
        # first-writer-wins guard applies to this one. It survives in the history and the audit
        # record.
        remove=("phase3Review",),
        include_event=True,
    )
    s["ReviewAbandoned"] = _fail(
        "ReviewAbandoned",
        "The review wait ended without a decision. The account stays as restricted as this "
        "re-assessment left it, and the case still needs a person.")

    reviewer = "{% $reviewOutcome.reviewerId %}"
    review_sort_key = "{% 'REVIEW#' & $riskEvent.occurredAt & '#' & $riskEvent.eventId %}"
    review_item = {
        "action": {"S": "{% $reviewOutcome.action %}"},
        "reviewerId": {"S": reviewer},
        "decidedAt": {"S": "{% $reviewOutcome.decidedAt %}"},
    }

    # APPROVE (the reviewer cleared the alert): record first, then restore the day-zero posture.
    cleared = _reviewed("RISK_CLEARED_BY_REVIEW")
    s["AppendReviewClearance"] = _history_row(
        history_table, sort_key=review_sort_key,
        item={**review_item, "outcome": {"S": "RISK_CLEARED_BY_REVIEW"}},
        next_state="RecordReviewedClearance", on_duplicate="RecordReviewedClearance",
        catch_to="ReviewAuditFailed")
    s["RecordReviewedClearance"] = _audit_task(
        audit_writer_arn, phase="phase3", decision="{% " + cleared + " %}", pins=pins,
        actor=reviewer, next_state="RestoreDayZeroPosture", catch_to="ReviewAuditFailed")
    s["RestoreDayZeroPosture"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": "{% $day0Posture = 'STANDARD' %}", "Next": "RestoreStandard"},
            {"Condition": "{% $day0Posture = 'ENHANCED' %}", "Next": "RestoreEnhanced"},
            {"Condition": "{% $day0Posture = 'TIGHTENED' %}", "Next": "RestoreTightened"},
        ],
        "Default": "RestoreProvisional",
    }
    for name, posture in (("RestoreStandard", "STANDARD"), ("RestoreEnhanced", "ENHANCED"),
                          ("RestoreTightened", "TIGHTENED"),
                          ("RestoreProvisional", "PROVISIONAL")):
        s[name] = _account_task(
            account_function_arn, operation="lift",
            # Releases this event's hold and no other. Another event's hold that is stricter than
            # the day-zero posture refuses the lift, and the account stays as it is.
            arguments={"posture": posture, "outcome": "RISK_CLEARED_BY_REVIEW",
                       "reason": "{% 'REVIEW.' & $reviewOutcome.action %}",
                       "releaseHolds": "{% [$riskEvent.eventId] %}"},
            next_state="RecordClearanceOnCase", catch_to="Phase3ActionFailed")
    s["RecordClearanceOnCase"] = _update(
        case_table,
        update="SET riskState = :r, phase3 = :d, updatedAt = :t",
        values={
            ":r": {"S": ("{% $day0Posture in ['STANDARD', 'ENHANCED'] ? 'MONITORING' : "
                         "($day0Posture = 'TIGHTENED' ? 'RESTRICTED' : 'PROVISIONAL') %}")},
            ":d": {"S": "{% $string(" + cleared + ") %}"},
            ":t": NOW,
        },
        catch=_catch("Done"),
        next_state="Done",
    )

    # REJECT (the reviewer confirmed it): freeze first, then the paperwork.
    exited = _reviewed("RELATIONSHIP_EXITED")
    s["ReviewFreezeAccount"] = _account_task(
        account_function_arn, operation="freeze",
        arguments={"outcome": "RELATIONSHIP_EXITED",
                   "reason": "{% 'REVIEW.' & $reviewOutcome.action %}"},
        next_state="RecordExitOnCase", catch_to="Phase3ActionFailed")
    s["RecordExitOnCase"] = _update(
        case_table,
        update="SET riskState = :r, phase3 = :d, updatedAt = :t",
        values={":r": {"S": "FROZEN"}, ":d": {"S": "{% $string(" + exited + ") %}"}, ":t": NOW},
        catch=_catch("AppendReviewExit"),
        next_state="AppendReviewExit",
    )
    s["AppendReviewExit"] = _history_row(
        history_table, sort_key=review_sort_key,
        item={**review_item, "outcome": {"S": "RELATIONSHIP_EXITED"}},
        next_state="RecordReviewedExit", on_duplicate="RecordReviewedExit",
        catch_to="ReviewAuditFailed")
    s["RecordReviewedExit"] = _audit_task(
        audit_writer_arn, phase="phase3", decision="{% " + exited + " %}", pins=pins,
        actor=reviewer, next_state="Done", catch_to="ReviewAuditFailed")

    s["ReviewAuditFailed"] = _fail(
        "ReviewAuditFailed",
        "The reviewer's decision could not be recorded. Nothing was restored; a freeze, if "
        "ordered, has been applied.")
    s["Phase3ActionFailed"] = _fail(
        "Phase3ActionFailed",
        "The account platform refused the reviewer's action. The account is unchanged.")

    # Terminal states. A duplicate is a Succeed, not a Fail: the decision it would have written
    # already exists, so paging someone would be noise.
    s["DuplicateEventIgnored"] = _succeed("This event already has a decision in the history.")
    s["NoOpenAccount"] = _succeed("No open account to re-assess for this customer.")
    s["Done"] = _succeed("Re-assessment applied.")
    s["EventUnroutable"] = _fail(
        "EventUnroutable", "No case exists for this customer; the risk event cannot be assessed.")

    return {
        "Comment": "Phase 3 — lifecycle re-assessment. Appends a decision; never rewrites day zero.",
        "QueryLanguage": "JSONata",
        "StartAt": "LoadDayZero",
        "States": s,
    }
