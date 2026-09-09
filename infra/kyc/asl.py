"""Amazon States Language definitions — tasks 6.2, 6.3, 6.4, 6.5.

Built as plain dicts rather than CDK L2 constructs for two reasons. The optimized integration
`bedrockagentcore:invokeHarness` has no L2 equivalent, and more importantly the properties that
matter here are properties *of the graph* — that `DegradedPath` cannot reach `OpenProvisional`,
that no lift state is reachable from a Phase 2 timeout — and a dict can be walked by a unit test
while a synthesised construct tree cannot be, at least not legibly.

Five things here are non-obvious and each was established the hard way:

* **`QueryLanguage: JSONata` is mandatory** (F-71). `Arguments` and `{% %}` expressions do not
  exist under JSONPath, and the failure is a validation error that names neither.
* **The resource URI is `bedrockagentcore`, no hyphen; the ARNs are `bedrock-agentcore`, with
  one.** Mixing them up produces "Invalid Resource" with no hint which half is wrong.
* **`reasoningConfig` nests under `AdditionalParams.additionalModelRequestFields`** (F-61),
  because `AdditionalParams` is spliced into a Bedrock Converse request and accepts only
  Converse's surface.
* **At `maxReasoningEffort: high`, `Temperature` and `MaxTokens` must be unset** (F-62). The
  Compliance Risk agent is the only one at `high`, so it is the only one that cannot pin
  sampling — `Agent.can_pin_sampling` encodes that rather than leaving it to a comment.
* **`RuntimeSessionId` needs 33+ characters** (F-36), which `$uuid()` satisfies at 36.
"""

from __future__ import annotations

from typing import Any

from .config import AGENTS, AGENT_BY_KEY, Agent

#: No hyphen. The ARNs in `Arguments` use `bedrock-agentcore`; this URI does not.
INVOKE_HARNESS = "arn:aws:states:::bedrockagentcore:invokeHarness"

#: Hard ceiling is 15 minutes on a Task (F-14). Every agent Task sits below it, and the harness
#: itself is separately bounded at 900s/120s (F-83) because a Task giving up does not stop a
#: harness from running and billing.
AGENT_TIMEOUT_SECONDS = 90

#: Reasoning-enabled agents get longer. Measured live: the Fraud agent at `medium` effort hit
#: `StopReason: timeout_exceeded` at 55.7s having produced 8,525 output tokens and returned
#: `Content: []` — it was still thinking when the budget ran out. That is the D-08/DIV-5 argument
#: arriving as data rather than as a spike measurement: reasoning effort buys quality and spends
#: latency, and a 90s Phase 1 cannot hold a high-effort agent at all.
#:
#: 180s is a deliberate compromise, not a fix. It keeps Fraud inside Phase 1 where its velocity
#: signals are the only fraud evidence available (design §8.1), while breaking the 90-second
#: *per-agent* assumption — the end-to-end budget is protected by the four branches running in
#: parallel, not by each one being fast.
REASONING_AGENT_TIMEOUT_SECONDS = 180

#: F-36 — the minimum is 33 characters and `$uuid()` yields 36.
RUNTIME_SESSION_ID = "{% $uuid() %}"

#: R13.2 — throttling is the one error worth retrying; everything else goes to DegradedPath.
THROTTLE_RETRY: list[dict[str, Any]] = [{
    "ErrorEquals": ["BedrockAgentCore.ThrottlingException"],
    "IntervalSeconds": 2,
    "MaxAttempts": 3,
    "BackoffRate": 2.0,
}]

#: The state a failed review token routes to. The review receiver sends this error name when a
#: case has moved on and can no longer be resumed; without a Catch for it the execution fails as
#: though the infrastructure broke.
REVIEW_UNRESUMABLE = "ReviewUnresumable"


def _catch(next_state: str, errors: list[str] | None = None) -> list[dict[str, Any]]:
    return [{"ErrorEquals": errors or ["States.ALL"], "Next": next_state}]


#: F-85. The endpoint is selected by `Qualifier`, not by suffixing the ARN. Keeping the pinned
#: endpoint matters for R18.6, which records the endpoint name on the decision.
HARNESS_QUALIFIER = "live"


def agent_task(
    agent: Agent,
    *,
    harness_arn: str,
    model_id: str,
    next_state: str,
    catch_to: str,
    prompt_expression: str,
    timeout_seconds: int | None = None,
    end: bool = False,
) -> dict[str, Any]:
    """One `invokeHarness` Task, shaped per design §7.3.

    `catch_to` is a required argument with no default on purpose. An agent Task without a Catch
    fails the whole execution, and for Phase 1 that means a partial result becomes an outage
    rather than a degraded decision — the distinction failure demo 2 exists to show.
    """
    if timeout_seconds is None:
        timeout_seconds = (REASONING_AGENT_TIMEOUT_SECONDS if agent.reasoning_effort
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
        # F-62: only legal when reasoning effort is not "high". Pinning sampling is what makes a
        # decision reproducible (R26.3), so the agent that cannot pin it is the one whose output
        # must never be the sole basis of an outcome — which is why A6 feeds the pure rules.
        model_config["Temperature"] = 0.0

    task: dict[str, Any] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": INVOKE_HARNESS,
        "Arguments": {
            # F-85: the *plain* harness ARN. An endpoint-suffixed ARN is rejected with "Invalid
            # harness ARN format" in every suffix form tried, including the `/endpoint/{version}`
            # shape design §7.3 originally showed.
            "HarnessArn": harness_arn,
            "Qualifier": HARNESS_QUALIFIER,
            "RuntimeSessionId": RUNTIME_SESSION_ID,
            "Messages": [{"Role": "user", "Content": [{"Text": prompt_expression}]}],
            "Model": {"BedrockModelConfig": model_config},
            "MaxIterations": agent.max_iterations,
            "TimeoutSeconds": timeout_seconds,
        },
        "Retry": THROTTLE_RETRY,
        "Catch": _catch(catch_to),
        "Assign": {
            # Captured per agent so `Phase1Decision` can tell "returned nothing" from "did not
            # run". Both are failures, but only one is retryable.
            f"{agent.key}Result": "{% $states.result.Output %}",
        },
    }
    if end:
        task["End"] = True
    else:
        task["Next"] = next_state
    return task


def phase1_definition(
    *,
    case_table: str,
    harness_arns: dict[str, str],
    model_ids: dict[str, str],
    decision_engine_arn: str,
    audit_writer_arn: str,
    review_queue_url: str,
    phase2_state_machine_arn: str,
) -> dict[str, Any]:
    """Phase 1, exactly the graph in design §7.2.

    Order is load-bearing at one point in particular: `CheckPreconditions` sits between
    `CheckIdempotency` and `Router`, so the four agents are unreachable for a submission with no
    liveness result or no documents (R38). A direct `POST /applicants` could otherwise run every
    agent and arrive at `OpenProvisional` with no liveness evidence, silently violating R1.3 —
    and it would cost a full pipeline run to do it.
    """
    states: dict[str, Any] = {}

    # ------------------------------------------------------------ 7.3 ingress latency (A-05)
    #
    # The EventBridge Pipe (task 7.2) carries **no** `InputTemplate`, by design — §10.2 requires
    # a malformed message to fail loudly inside the state machine rather than be reshaped one hop
    # earlier. The consequence, confirmed live against the real Pipe rather than assumed: with no
    # `InputTemplate` a pipe sourced from SQS hands the target the *raw batch of SQS records*,
    # `[{ "body": "...", "attributes": { "SentTimestamp": "...", ... }, ... }]` — not the bare
    # applicant payload `LoadCase` was written against. A direct `StartExecution` call (the API
    # handler, `spike/live_run.py`, tests) still sends the bare object. Both shapes are live paths
    # — the queue for real submissions, direct start for the synchronous API contract and for
    # replay/ops tooling — so this state normalises both rather than picking one.
    #
    # `attributes.SentTimestamp` is exactly the enqueue timestamp T2 needs (design §2, R20.1):
    # SQS stamps it in the producer's account at `SendMessage` time, so it costs nothing to carry
    # and closes A-05 with a measured number instead of the "1-3s estimate" placeholder — no
    # separate producer-side instrumentation was needed once the real Pipe payload was inspected.
    states["NormalizeInput"] = {
        "Type": "Pass",
        "QueryLanguage": "JSONata",
        "Assign": {
            "applicantId": (
                "{% $type($states.input) = 'array' "
                "? $parse($states.input[0].body).applicantId "
                ": $states.input.applicantId %}"
            ),
            "eventId": (
                "{% $type($states.input) = 'array' "
                "? $parse($states.input[0].body).eventId "
                ": $states.input.eventId %}"
            ),
            # null, not a fabricated value, when the execution was started directly rather than
            # via the queue — a missing T2 datum must read as "not measured on this path", never
            # as zero latency.
            "enqueuedAtMs": (
                "{% $type($states.input) = 'array' "
                "? $number($states.input[0].attributes.SentTimestamp) : null %}"
            ),
            "firstStateEnteredAtMs": "{% $toMillis($now()) %}",
        },
        "Next": "LoadCase",
    }

    states["LoadCase"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:getItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {
                "applicantId": {"S": "{% $applicantId %}"},
                "sk": {"S": "CASE"},
            },
            "ConsistentRead": True,
        },
        "Assign": {
            "case": "{% $states.result.Item %}",
            # design §2/R20.1 — T2 measured, not estimated. `null`, not fabricated, on the
            # direct-start path. Deliberately `!= null` and not `$exists`: `enqueuedAtMs` is
            # *assigned* `null` on that path (see NormalizeInput), so `$exists($enqueuedAtMs)`
            # is true even then — proved live via `TestState`, where the `$exists` form crashed
            # `firstStateEnteredAtMs - null` with a JSONata type error on exactly this path.
            "ingressLatencyMs": (
                "{% $enqueuedAtMs != null "
                "? $firstStateEnteredAtMs - $enqueuedAtMs : null %}"
            ),
            # Rooted at `$states.result.Item`, NOT at `$case` — even though `case` is assigned the
            # very same value two lines above. Assignments inside a single `Assign` block are all
            # evaluated against the state's incoming variable scope, so `$case` here is the value
            # from *before* this state ran, i.e. unset on the first pass. Every `$exists($case.x)`
            # guard then evaluated false and the whole projection collapsed to nulls.
            #
            # Found live, never by a test: a real submission with a document and a passed liveness
            # check sent every Phase 1 agent `{"applicantId": "APP-...", "caseId": null,
            # "declaredIdentity": null, "documents": [], "liveness": null}`. The Documents agent
            # correctly answered that it could not extract anything, the case degraded to
            # DegradedPath -> ManualReview, and the applicant waited on "Reviewing your
            # application" while a human was asked to decide a case with no evidence attached.
            # Nothing failed, nothing threw, and the 90-second auto-approve path was unreachable.
            #
            # The `$exists` guards are what made it silent: they are meant to turn an absent
            # optional field into an explicit null, and instead they turned an unbound variable
            # into a full set of plausible nulls. The Choice states further down read `$case` in a
            # *later* state, where the binding is live, which is why tiering still saw the
            # documents and liveness this projection had already lost.
            #
            # A clean, unwrapped projection for agent prompts only. The raw
            # `dynamodb:getItem` result wraps every value — `{"S": "..."}` / `{"M": {...}}` /
            # `{"L": [...]}` — and `$string(...)` of it used to be sent to every Phase 1 agent
            # verbatim. Found live: the Documents and Identity agents intermittently reported "the
            # document was not found" / "the image is unreadable" against a bucket and key that,
            # replayed directly against the same tools with the exact values from the case record,
            # succeeded immediately. The model was extracting `documents.L[0].M.key.S` from the
            # wrapped structure correctly on some runs and incorrectly on others — a real,
            # documented category of LLM tool-use failure with deeply nested, type-tagged JSON,
            # not a defect in the tools themselves (`tools/kyctools/tools_impl.py`) or in the
            # face-compare/document-extraction fixes made this session, both of which were
            # verified working in isolation against this exact data.
            #
            # Every field an agent reads is unwrapped once here rather than leaving each agent's
            # own prompt to reach through the DynamoDB shape — the same principle `asl.py` already
            # applies for the decision engine's `declaredIdentity` payload, extended to cover the
            # agents themselves. Absent optional fields resolve to `null` via `$exists` guards
            # rather than being omitted, so an agent sees "not provided" explicitly instead of a
            # key that is simply missing, which is easier for a model to misread as "not asked".
            "caseView": {
                "applicantId": "{% $applicantId %}",
                "caseId": "{% $exists($states.result.Item.caseId) ? $states.result.Item.caseId.S : null %}",
                "jurisdiction": "{% $exists($states.result.Item.jurisdiction) ? $states.result.Item.jurisdiction.S : null %}",
                "productApplied": (
                    "{% $exists($states.result.Item.productApplied) ? $states.result.Item.productApplied.S : null %}"),
                "declaredIdentity": (
                    "{% $exists($states.result.Item.declaredIdentity) ? {"
                    "'surname': $states.result.Item.declaredIdentity.M.surname.S, "
                    "'givenNames': $states.result.Item.declaredIdentity.M.givenNames.S, "
                    "'dateOfBirth': $states.result.Item.declaredIdentity.M.dateOfBirth.S, "
                    "'nationality': $states.result.Item.declaredIdentity.M.nationality.S, "
                    "'address': $exists($states.result.Item.declaredIdentity.M.address) ? "
                    "$states.result.Item.declaredIdentity.M.address.S : null"
                    "} : null %}"),
                # `$map` with an explicit lambda, not `array.{...}` — JSONata's object-constructor
                # chained off an array performs a *grouping* reduction, not a per-item map, and
                # would silently collapse multiple documents into one merged object instead of a
                # list of them.
                # `[ $map(...) ]`, not bare `$map(...)`. JSONata's result sequences collapse a
                # single value to itself rather than a one-element array ("singleton sequence
                # equivalence" — confirmed against a real case via `test-state`, where exactly one
                # uploaded document made `documents` serialise as a bare object instead of a
                # one-item list). An agent prompt that sometimes sees `documents: {...}` and
                # sometimes `documents: [{...}]` for the same field is precisely the kind of
                # inconsistency that produces intermittent tool-call failures, so the wrapping
                # brackets make the shape an array unconditionally, regardless of count.
                "documents": (
                    "{% $exists($states.result.Item.documents) ? [$map($states.result.Item.documents.L, function($d) {"
                    "{'type': $d.M.type.S, 'bucket': $d.M.bucket.S, 'key': $d.M.key.S, "
                    "'issuingCountry': $d.M.issuingCountry.S}"
                    "})] : [] %}"),
                "liveness": (
                    "{% $exists($states.result.Item.liveness) ? {"
                    "'result': $states.result.Item.liveness.M.result.S, "
                    "'region': $states.result.Item.liveness.M.region.S, "
                    "'referenceImageKey': $exists($states.result.Item.liveness.M.referenceImageKey) and "
                    "$states.result.Item.liveness.M.referenceImageKey.S != '' ? "
                    "$states.result.Item.liveness.M.referenceImageKey.S : null"
                    "} : null %}"),
                "documentExtraction": (
                    "{% $exists($states.result.Item.documentExtraction) ? "
                    "$parse($states.result.Item.documentExtraction.S) : null %}"),
            },
        },
        "Next": "CheckIdempotency",
    }

    # R21.3 — a cached decision returns without invoking any agent. The check is on the *stored*
    # idempotency key rather than on execution name, because SQS redelivery and an API retry
    # arrive as different executions for the same applicantId+eventId.
    states["CheckIdempotency"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{
            "Condition": (
                "{% $exists($case.phase1) and "
                "$case.phase1.idempotencyKey.S = $applicantId & '#' & $eventId %}"
            ),
            "Next": "ReturnCached",
        }],
        "Default": "CheckPreconditions",
    }

    states["ReturnCached"] = {
        "Type": "Succeed",
        "QueryLanguage": "JSONata",
        "Comment": (
            "R21.3. Returns the stored decision unchanged. Zero agent invocations, so a replay "
            "costs nothing and cannot produce a different answer."
        ),
    }

    # R38. Fails to STEP_UP, never REJECT: a missing precondition is a process gap, not an
    # applicant failure, and rejecting for it would be both wrong and unappealable.
    states["CheckPreconditions"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": "{% $not($exists($case.liveness)) %}", "Next": "StepUpLivenessRequired"},
            {"Condition": "{% $case.liveness.M.result.S = 'FAILED' %}",
             "Next": "StepUpLivenessFailed"},
            {"Condition": "{% $not($exists($case.documents)) or $count($case.documents.L) = 0 %}",
             "Next": "StepUpDocumentsRequired"},
        ],
        "Default": "Router",
    }

    for name, reason, item in (
        ("StepUpLivenessRequired", "LIVENESS_REQUIRED", "A completed liveness check"),
        ("StepUpLivenessFailed", "LIVENESS_FAILED", "A repeated liveness check"),
        ("StepUpDocumentsRequired", "DOCUMENTS_REQUIRED", "At least one identity document"),
    ):
        states[name] = {
            "Type": "Pass",
            "QueryLanguage": "JSONata",
            "Output": {
                "outcome": "STEP_UP",
                "reason": reason,
                "requiredItem": item,
                "agentsInvoked": 0,
            },
            "Next": "PersistOutcome",
        }

    router = AGENT_BY_KEY["router"]
    states["Router"] = agent_task(
        router,
        harness_arn=harness_arns["router"],
        model_id=model_ids["router"],
        next_state="CheckJurisdiction",
        catch_to="DegradedPath",
        prompt_expression="{% $string($caseView) %}",
    )

    # R12.4 — an unresolved jurisdiction goes to a human. Guessing a ruleset is worse than
    # waiting for one, because the wrong ruleset produces a confident decision under the wrong law.
    states["CheckJurisdiction"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{
            "Condition": "{% $routerResult.jurisdiction = 'UNRESOLVED' %}",
            "Next": "ManualReview",
        }],
        "Default": "Phase1Parallel",
    }

    parallel_agents = ("documents", "identity", "fraud", "prescreen")
    states["Phase1Parallel"] = {
        "Type": "Parallel",
        "QueryLanguage": "JSONata",
        "Branches": [
            {
                "StartAt": AGENT_BY_KEY[key].role_suffix,
                "States": {
                    AGENT_BY_KEY[key].role_suffix: agent_task(
                        AGENT_BY_KEY[key],
                        harness_arn=harness_arns[key],
                        model_id=model_ids[key],
                        next_state=f"{AGENT_BY_KEY[key].role_suffix}Check",
                        catch_to=f"{AGENT_BY_KEY[key].role_suffix}Degraded",
                        prompt_expression="{% $string($caseView) %}",
                    ),
                    # An agent can complete without failing and still return nothing. Measured
                    # live: the Fraud agent finished with `StopReason: timeout_exceeded`,
                    # `Content: []`, after 55.7s and 8,525 output tokens. No error, no Catch, and
                    # a naive reader would treat "no findings" as "no risk" — which approves
                    # exactly the applicant who should be stopped.
                    #
                    # So emptiness is checked here, in the graph, not only in the decision
                    # engine. Two layers is deliberate: the engine's `normalise.py` is the
                    # backstop, and this makes the degradation visible in the execution history
                    # where an operator will actually look.
                    f"{AGENT_BY_KEY[key].role_suffix}Check": {
                        "Type": "Choice",
                        "QueryLanguage": "JSONata",
                        "Choices": [{
                            "Condition": (
                                "{% $not($exists($states.input.Output.Message.Content)) "
                                "or $count($states.input.Output.Message.Content) = 0 "
                                "or $states.input.StopReason = 'timeout_exceeded' %}"),
                            "Next": f"{AGENT_BY_KEY[key].role_suffix}Degraded",
                        }],
                        "Default": f"{AGENT_BY_KEY[key].role_suffix}Ok",
                    },
                    # Normalised shape. The raw harness envelope carries no agent name, so the
                    # decision engine could not key its `agentResults` map by agent and rejected
                    # the payload outright — which failed closed to DegradedPath and looked, in
                    # the console, exactly like a genuine partial failure.
                    f"{AGENT_BY_KEY[key].role_suffix}Ok": {
                        "Type": "Pass",
                        "QueryLanguage": "JSONata",
                        "Output": {
                            "agent": key,
                            "status": "OK",
                            "content": "{% $states.input.Output.Message.Content %}",
                            "stopReason": "{% $states.input.StopReason %}",
                            "latencyMs": (
                                "{% $exists($states.input.Metrics.LatencyMs) ? "
                                "$states.input.Metrics.LatencyMs : 0 %}"),
                            "totalTokens": (
                                "{% $exists($states.input.Usage.TotalTokens) ? "
                                "$states.input.Usage.TotalTokens : 0 %}"),
                        },
                        "End": True,
                    },
                    # Per-branch terminal so one failure degrades that branch rather than
                    # aborting the Parallel. The aggregate decision then sees which branches
                    # are incomplete, which is the input R13.5 needs.
                    #
                    # Named per agent because state names must be unique across the ENTIRE
                    # machine, not per branch — four branches sharing `BranchDegraded` is a
                    # DUPLICATE_STATE_NAME rejection at deploy time. The per-agent name is also
                    # what makes the failed branch identifiable in the execution history.
                    f"{AGENT_BY_KEY[key].role_suffix}Degraded": {
                        "Type": "Pass",
                        "QueryLanguage": "JSONata",
                        "Output": {
                            "agent": key,
                            "status": "DEGRADED",
                            # UNAVAILABLE, not INVALID: the applicant cannot fix an agent that
                            # returned nothing, so this must route to a reviewer rather than
                            # become a step-up demand they can never satisfy.
                            "reason": "EMPTY_OR_FAILED_AGENT_RESULT",
                        },
                        "End": True,
                    },
                },
            }
            for key in parallel_agents
        ],
        "Assign": {"branchResults": "{% $states.result %}"},
        "Catch": _catch("DegradedPath"),
        "Next": "PersistEvidence",
    }

    # R2.3 — evidence is persisted before any terminating state, so a crash after the agents ran
    # does not discard what they found.
    states["PersistEvidence"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:updateItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {"applicantId": {"S": "{% $applicantId %}"}, "sk": {"S": "CASE"}},
            "UpdateExpression": "SET phase1Evidence = :e, updatedAt = :t",
            "ExpressionAttributeValues": {
                ":e": {"S": "{% $string($branchResults) %}"},
                ":t": {"S": "{% $now() %}"},
            },
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 1, "MaxAttempts": 3,
                   "BackoffRate": 2.0}],
        "Next": "ReloadFindings",
    }

    # Between the agents and the tier, re-read the case. The agents wrote their findings through
    # the submit_* contract tools while this execution was inside Phase1Parallel, so the copy in
    # `$case` from LoadCase cannot contain them (R11.13, F-89).
    states["ReloadFindings"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:getItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {"applicantId": {"S": "{% $applicantId %}"}, "sk": {"S": "CASE"}},
            # Strongly consistent: the findings were written seconds ago by four concurrent
            # branches, and an eventually consistent read here would drop findings at random and
            # look exactly like agents that failed to report.
            "ConsistentRead": True,
        },
        "Assign": {"case": "{% $states.result.Item %}"},
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 1, "MaxAttempts": 3,
                   "BackoffRate": 2.0}],
        "Catch": _catch("DegradedPath"),
        "Next": "PreliminaryTier",
    }

    states["PreliminaryTier"] = _decision_engine_task(
        decision_engine_arn,
        phase=1,
        next_state="Phase1Decision",
        catch_to="DegradedPath",
    )

    states["Phase1Decision"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            # R13.5 — an incomplete branch can never reach OpenProvisional. Checked first so no
            # later condition can approve a case built on missing evidence.
            {"Condition": (
                "{% $count($branchResults[status = 'DEGRADED']) > 0 %}"),
             "Next": "DegradedPath"},
            {"Condition": "{% $decision.outcome = 'REJECT' %}", "Next": "Reject"},
            {"Condition": "{% $decision.outcome = 'MANUAL_REVIEW' %}", "Next": "ManualReview"},
            {"Condition": "{% $decision.outcome = 'STEP_UP' %}", "Next": "StepUp"},
        ],
        "Default": "OpenProvisional",
    }

    # Failure demo 2. There is deliberately no edge from here to OpenProvisional — a partial
    # parallel failure must never open an account, and that is a property of the graph rather
    # than of a condition someone could later loosen.
    states["DegradedPath"] = {
        "Type": "Pass",
        "QueryLanguage": "JSONata",
        "Comment": (
            "Structurally cannot reach OpenProvisional (R13.5). Routes to human review only."
        ),
        "Output": {"outcome": "MANUAL_REVIEW", "reason": "DEGRADED_PIPELINE"},
        "Next": "ManualReview",
    }

    states["OpenProvisional"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:updateItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {"applicantId": {"S": "{% $applicantId %}"}, "sk": {"S": "CASE"}},
            "UpdateExpression": (
                "SET #s = :state, phase1 = :d, phase2Pending = :p, provisionalDecidedAt = :t, "
                # Promoted to first-class attributes so Phase 2 can read the values Phase 1
                # actually used, rather than re-deriving them from prose a second time and
                # possibly reaching a different answer for the same applicant (R4.4).
                "documentConfidence = :dc, velocityCritical = :vc, "
                # R2.5 — entering PROVISIONAL is a state change and must be recorded. Without
                # this the audit trail's first entry was `PROVISIONAL -> AWAITING_REVIEW`, so the
                # account opening itself -- the single most consequential transition in the whole
                # flow -- had no record of when or why it happened.
                "transitions = list_append(if_not_exists(transitions, :empty), :tr)"),
            "ExpressionAttributeNames": {"#s": "state"},
            "ExpressionAttributeValues": {
                ":state": {"S": "PROVISIONAL"},
                ":empty": {"L": []},
                ":tr": {"L": [{"M": {
                    "from": {"S": "{% $case.state.S %}"},
                    "to": {"S": "PROVISIONAL"},
                    "at": {"S": "{% $now() %}"},
                    "actor": {"S": "system"},
                    "reason": {"S": "{% 'provisional decision: ' & $decision.outcome %}"},
                }}]},
                ":d": {"S": "{% $string($decision) %}"},
                ":dc": {"N": "{% $string($decision.tierInputs.document_confidence) %}"},
                # "Bool", not "BOOL". Step Functions normalises DynamoDB AttributeValue member
                # names to PascalCase and rejects the wire-format spelling outright:
                # 'The field "BOOL" is not supported by Step Functions. Did you mean \'Bs\'?'
                # Only reachable once a case actually earns a provisional approval, so the two
                # bugs upstream of here (the null caseView and the unguarded jurisdictionRisk)
                # kept this line from ever executing — it surfaced on the first run where a
                # clean applicant got as far as OpenProvisional.
                ":vc": {"Bool": "{% $decision.tierInputs.velocity_critical %}"},
                # Feeds the phase2Pending GSI so a Phase 2 that never starts is detectable
                # rather than merely absent.
                ":p": {"S": "PENDING"},
                ":t": {"S": "{% $now() %}"},
            },
            # R21.4 — a conditional write, so a concurrent execution cannot double-open.
            "ConditionExpression": "attribute_not_exists(phase1)",
        },
        "Catch": _catch("ManualReview", ["DynamoDB.ConditionalCheckFailedException"]),
        "Next": "WriteDecisionRecord",
    }

    states["WriteDecisionRecord"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Arguments": {
            "FunctionName": audit_writer_arn,
            "Payload": {
                "applicantId": "{% $applicantId %}",
                "phase": "phase1",
                "decision": "{% $decision %}",
                # R18.6 needs all four pins, and the writer refuses the record without them —
                # gate G9 caught this as MISSING_VERSION_PINS. They travel in the payload rather
                # than being read by the writer, because the *state machine* is what chose the
                # endpoint and model, so it is the only component that knows what actually ran.
                "endpointName": HARNESS_QUALIFIER,
                "harnessVersion": "{% $exists($routerResult.harnessVersion) ? "
                                  "$routerResult.harnessVersion : '1' %}",
                "policyEngineVersion": "{% $exists($case.policyEngineVersion) ? "
                                       "$case.policyEngineVersion.S : '1' %}",
                "gatewayArn": "{% $case.gatewayArn.S %}",
                "traceId": "{% $states.context.Execution.Name %}",
                # A-05/task 7.3: T2's queue-side leg, measured not estimated. Null on the
                # direct-StartExecution path (see NormalizeInput) — the writer must not coerce
                # that into 0, since an unmeasured datum and an instant one are different facts.
                "ingressLatencyMs": "{% $ingressLatencyMs %}",
            },
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 3,
                   "BackoffRate": 2.0}],
        # The decision id is MINTED HERE, by the audit writer, and returned in its result — the
        # decision engine never produces one (`audit_lambda` does
        # `str(decision.get("decisionId") or uuid.uuid4())`). `TriggerPhase2` used to read
        # `$decision.decisionId`, which is always undefined, so its two JSONata expressions failed
        # with States.QueryEvaluationError and every provisionally-approved case ended at
        # `Phase2NotStarted`: the account was open, the record written, and Phase 2 never ran.
        # Reachable only once a case could actually earn a provisional approval, which is why it had
        # never fired.
        "Assign": {"provisionalDecisionId": "{% $states.result.Payload.decisionId %}"},
        # An audit write that fails must not silently drop. It routes to review so a human sees
        # a decided case with no record, which is a compliance event in its own right (R18.3).
        "Catch": _catch("AuditWriteFailed"),
        "Next": "TriggerPhase2",
    }

    states["AuditWriteFailed"] = {
        "Type": "Pass",
        "QueryLanguage": "JSONata",
        "Comment": (
            "R18.3. The provisional account is already open and the decision stands; what is "
            "missing is its immutable record. Surfaced rather than swallowed."
        ),
        "Output": {"outcome": "MANUAL_REVIEW", "reason": "AUDIT_RECORD_MISSING"},
        "Next": "ManualReview",
    }

    # R13.11 — StartExecution WITHOUT .sync. Phase 1 ends the moment the provisional decision is
    # recorded; waiting for Phase 2 would put a multi-minute screening job inside the 90-second
    # budget and defeat the entire two-phase design.
    states["TriggerPhase2"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::states:startExecution",
        "Arguments": {
            "StateMachineArn": phase2_state_machine_arn,
            "Input": {
                "applicantId": "{% $applicantId %}",
                "provisionalDecisionId": "{% $provisionalDecisionId %}",
            },
            # Idempotent on the provisional decision (R21.7): a duplicate StartExecution with the
            # same name is rejected rather than running screening twice.
            #
            # The decision id ALONE, not `applicantId & '-' & decisionId`. Step Functions caps an
            # execution name at 80 characters, and a real Cognito-derived applicant id is 76 of
            # them, so the combined name was 113 and every real applicant failed with
            # `StepFunctions.ValidationException: ... 'name' failed to satisfy constraint: Member
            # must have length less than or equal to 80` -> retries exhausted -> Phase2NotStarted:
            # provisional account open, audit record written, screening never run.
            #
            # It survived every one of my own tests because synthetic ids were short
            # ("APP-act1-wuq51frrm2", 19 chars) and stayed under the cap. Only a real applicant id
            # crosses it — the reason a green synthetic run proves nothing about production data.
            #
            # Dropping the applicant id loses nothing: `decisionId` is a uuid4 minted per decision
            # by the audit writer, so it is already globally unique, and uniqueness is the entire
            # job of this name. The applicant is still recoverable from the execution's input.
            "Name": "{% $provisionalDecisionId %}",
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 3,
                   "BackoffRate": 2.0}],
        "Catch": _catch("Phase2NotStarted"),
        "End": True,
    }

    states["Phase2NotStarted"] = {
        "Type": "Fail",
        "Error": "Phase2NotStarted",
        "Cause": (
            "Provisional account is open but Phase 2 did not start. The phase2Pending GSI still "
            "holds this case, so the sweeper will retry; restrictions remain in force meanwhile."
        ),
    }

    states["StepUp"] = {
        "Type": "Pass",
        "QueryLanguage": "JSONata",
        "Output": {"outcome": "STEP_UP", "requiredItem": "{% $decision.requiredItem %}"},
        "Next": "PersistOutcome",
    }
    states["Reject"] = {
        "Type": "Pass",
        "QueryLanguage": "JSONata",
        "Output": {"outcome": "REJECT", "reasonCode": "{% $decision.reasonCode %}"},
        "Next": "PersistOutcome",
    }
    # Two states, not one. `ManualReview` keeps its name as the *entry* point so every existing
    # transition into it (Phase1Decision, DegradedPath, the idempotency catch) is unchanged, and
    # the SQS wait moves to `AwaitReviewDecision` behind it.
    states["ManualReview"] = _mark_awaiting_review(
        case_table, next_state="AwaitReviewDecision")
    states["AwaitReviewDecision"] = _review_task(
        review_queue_url, next_state="PersistOutcome", catch_to="ReviewAbandoned",
        phase="phase1")
    states["ReviewAbandoned"] = {
        "Type": "Pass",
        "QueryLanguage": "JSONata",
        "Comment": (
            f"Catches {REVIEW_UNRESUMABLE} from the review receiver, raised when the case moved "
            "on without a decision. Distinguished from an agent failure so it is not diagnosed "
            "as an infrastructure fault."
        ),
        "Output": {"outcome": "MANUAL_REVIEW", "reason": "REVIEW_NOT_RESUMABLE"},
        "Next": "PersistOutcome",
    }

    states["PersistOutcome"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:updateItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {"applicantId": {"S": "{% $applicantId %}"}, "sk": {"S": "CASE"}},
            "UpdateExpression": (
                "SET phase1 = :d, idempotencyKey = :k, updatedAt = :t, ingressLatencyMs = :il"
            ),
            "ExpressionAttributeValues": {
                ":d": {"S": "{% $string($states.input) %}"},
                ":k": {"S": "{% $applicantId & '#' & $eventId %}"},
                ":t": {"S": "{% $now() %}"},
                # A-05/task 7.3: recorded on the case whenever it was measured, so it is
                # queryable per-applicant rather than only visible in the audit Lambda's logs.
                # `NULL` (not a fabricated `0` or `"N": "0"`) on the direct-StartExecution path,
                # since an unmeasured datum and an instant one are different facts.
                # `!= null`, not `$exists` — `ingressLatencyMs` is assigned `null` rather than
                # omitted on the direct-start path, so `$exists` alone is always true here.
                ":il": (
                    "{% $ingressLatencyMs != null "
                    "? {'N': $string($ingressLatencyMs)} : {'NULL': true} %}"
                ),
            },
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 1, "MaxAttempts": 3,
                   "BackoffRate": 2.0}],
        "End": True,
    }

    return {
        "Comment": "KYC Phase 1 — provisional decision inside 90 seconds (design §7.2)",
        "QueryLanguage": "JSONata",
        "StartAt": "NormalizeInput",
        "States": states,
    }


def phase2_definition(
    *,
    case_table: str,
    harness_arns: dict[str, str],
    model_ids: dict[str, str],
    decision_engine_arn: str,
    audit_writer_arn: str,
    review_queue_url: str,
) -> dict[str, Any]:
    """Phase 2, per design §7.4.

    The invariant that matters here is negative: **no failure or timeout path reaches a state
    that lifts a restriction.** Silence is not permission (Property 3, R31.6). A test walks the
    graph to prove it, because this is exactly the kind of edge that gets added later by someone
    tidying up a Catch block.
    """
    states: dict[str, Any] = {}

    states["LoadCase"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:getItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {"applicantId": {"S": "{% $states.input.applicantId %}"}, "sk": {"S": "CASE"}},
            "ConsistentRead": True,
        },
        "Assign": {
            "applicantId": "{% $states.input.applicantId %}",
            # JSONata fails the whole state with QueryEvaluationError when an expression returns
            # undefined, so an absent field is not a null — it is a dead execution. Gate G9 hit
            # exactly that. Phase 1 always supplies this; the guard covers a manual re-drive or a
            # sweeper retry, where losing the execution outright would be the worse failure.
            "provisionalDecisionId": (
                "{% $exists($states.input.provisionalDecisionId) ? "
                "$states.input.provisionalDecisionId : 'unknown' %}"),
            "case": "{% $states.result.Item %}",
            # Same unwrapping as Phase 1's `LoadCase` — see that state's comment for why. Phase 2's
            # ComplianceRisk agent needs the declared identity (fuzzy sanctions/PEP/adverse media
            # run against the name, not against a document), so that is what is unwrapped here;
            # documents/liveness are Phase 1 concerns and are omitted rather than unwrapped for
            # nothing.
            "caseView": {
                "applicantId": "{% $states.input.applicantId %}",
                "jurisdiction": "{% $exists($states.result.Item.jurisdiction) ? $states.result.Item.jurisdiction.S : null %}",
                "declaredIdentity": (
                    "{% $exists($states.result.Item.declaredIdentity) ? {"
                    "'surname': $states.result.Item.declaredIdentity.M.surname.S, "
                    "'givenNames': $states.result.Item.declaredIdentity.M.givenNames.S, "
                    "'dateOfBirth': $states.result.Item.declaredIdentity.M.dateOfBirth.S, "
                    "'nationality': $states.result.Item.declaredIdentity.M.nationality.S"
                    "} : null %}"),
            },
        },
        "Next": "CheckPhase2Idempotency",
    }

    # R21.7 — idempotent on applicantId + provisionalDecisionId.
    states["CheckPhase2Idempotency"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{
            "Condition": "{% $exists($case.phase2) %}",
            "Next": "AlreadyComplete",
        }],
        "Default": "ComplianceRisk",
    }
    states["AlreadyComplete"] = {
        "Type": "Succeed",
        "Comment": "Phase 2 already ran for this provisional decision (R21.7).",
    }

    compliance = AGENT_BY_KEY["compliance"]
    states["ComplianceRisk"] = agent_task(
        compliance,
        harness_arn=harness_arns["compliance"],
        model_id=model_ids["compliance"],
        next_state="FullTier",
        # Not DegradedPath: a Phase 2 failure must keep restrictions, which is a different
        # posture from Phase 1's degrade-to-review.
        catch_to="Phase2Failed",
        prompt_expression="{% $string($caseView) %}",
        # A6 runs at high effort with no clock pressure (D-08). Measured p50 21.7s and max 48.5s,
        # so 90s would be too tight; 600s stays under the 900s Task ceiling.
        timeout_seconds=600,
    )

    states["FullTier"] = _decision_engine_task(
        decision_engine_arn, phase=2, next_state="Phase2Decision", catch_to="Phase2Failed")

    states["Phase2Decision"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": "{% $decision.outcome = 'REJECT' %}", "Next": "FreezeAndNotify"},
            {"Condition": "{% $decision.outcome = 'MANUAL_REVIEW' %}", "Next": "ManualReview"},
            {"Condition": "{% $decision.outcome = 'RESTRICT_FURTHER' %}", "Next": "Tighten"},
            {"Condition": "{% $decision.outcome = 'FULL_APPROVE_ENHANCED_MONITORING' %}",
             "Next": "LiftTransfersKeepCeiling"},
            {"Condition": "{% $decision.outcome = 'FULL_APPROVE_STANDARD' %}",
             "Next": "LiftAllRestrictions"},
        ],
        # No approval by default. An unrecognised outcome keeps restrictions and asks a human,
        # so adding a new outcome later cannot accidentally start lifting restrictions.
        "Default": "ManualReview",
    }

    for name, restriction_change, comment in (
        ("LiftAllRestrictions", "LIFT_ALL", "R31.3 — tier LOW, standard monitoring."),
        ("LiftTransfersKeepCeiling", "LIFT_TRANSFERS_KEEP_CEILING",
         "R31.4 — tier MEDIUM, deposit ceiling retained."),
        ("Tighten", "TIGHTEN", "R31.5 — restrictions tightened, not lifted."),
        ("FreezeAndNotify", "FREEZE", "R31.5 — frozen, compliance notified."),
    ):
        states[name] = {
            "Type": "Task",
            "QueryLanguage": "JSONata",
            "Comment": comment,
            "Resource": "arn:aws:states:::aws-sdk:dynamodb:updateItem",
            "Arguments": {
                "TableName": case_table,
                "Key": {"applicantId": {"S": "{% $applicantId %}"}, "sk": {"S": "CASE"}},
                "UpdateExpression": (
                    "SET restrictionChange = :r, phase2 = :d, updatedAt = :t "
                    "REMOVE phase2Pending"),
                "ExpressionAttributeValues": {
                    ":r": {"S": restriction_change},
                    ":d": {"S": "{% $string($decision) %}"},
                    ":t": {"S": "{% $now() %}"},
                },
            },
            "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 3,
                       "BackoffRate": 2.0}],
            "Catch": _catch("Phase2Failed"),
            "Next": "Notify",
        }

    # Same split as Phase 1: mark the case queueable, then wait. A Phase 2 review that never
    # reaches the console is the more dangerous of the two, because the case is already
    # provisionally open with restrictions in force.
    states["ManualReview"] = _mark_awaiting_review(
        case_table, next_state="AwaitReviewDecision")
    states["AwaitReviewDecision"] = _review_task(
        review_queue_url, next_state="Notify", catch_to="Phase2Failed",
        phase="phase2")

    notify = AGENT_BY_KEY["notify"]
    states["Notify"] = agent_task(
        notify,
        harness_arn=harness_arns["notify"],
        model_id=model_ids["notify"],
        next_state="EnrolMonitoring",
        # Notification copy failing must not undo a restriction change that already landed.
        catch_to="NotifyFailed",
        prompt_expression="{% $string($decision) %}",
    )
    states["NotifyFailed"] = {
        "Type": "Pass",
        "QueryLanguage": "JSONata",
        "Comment": "The decision and the restriction change stand; only the copy failed.",
        "Next": "EnrolMonitoring",
    }

    states["EnrolMonitoring"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:updateItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {"applicantId": {"S": "{% $applicantId %}"}, "sk": {"S": "CASE"}},
            # R2.5 — the transition append is not optional. This state used to set `state` alone,
            # so a completed Phase 2 left the case reading MONITORING while the audit trail ended
            # at PROVISIONAL: a state change with no record of it, in a system whose central claim
            # is an append-only decision history. Observed on the first Phase 2 execution ever to
            # reach a terminal state.
            "UpdateExpression": (
                "SET #s = :state, reKycInterval = :i, updatedAt = :t, "
                "transitions = list_append(if_not_exists(transitions, :empty), :tr)"
            ),
            "ExpressionAttributeNames": {"#s": "state"},
            "ExpressionAttributeValues": {
                ":state": {"S": "MONITORING"},
                ":i": {"S": "{% $decision.reKycInterval %}"},
                ":t": {"S": "{% $now() %}"},
                ":empty": {"L": []},
                ":tr": {"L": [{"M": {
                    "from": {"S": "{% $case.state.S %}"},
                    "to": {"S": "MONITORING"},
                    "at": {"S": "{% $now() %}"},
                    "actor": {"S": "system"},
                    "reason": {"S": "phase 2 complete; enrolled in ongoing monitoring"},
                }}]},
            },
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 3,
                   "BackoffRate": 2.0}],
        "End": True,
    }

    # R31.6 / Property 3. This state has exactly one outgoing edge, to a Fail. It does NOT touch
    # restrictionChange and does NOT remove phase2Pending, so the account stays exactly as
    # Phase 1 left it — restricted — and the GSI still shows the case as pending.
    states["Phase2Failed"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Comment": (
            "Restrictions PERSIST. No edge from here lifts anything (R31.6, Property 3): "
            "silence must not equal permission. Raises an alarm metric and fails loudly."
        ),
        "Resource": "arn:aws:states:::aws-sdk:cloudwatch:putMetricData",
        "Arguments": {
            "Namespace": "KYC90/Phase2",
            "MetricData": [{
                "MetricName": "Phase2Failed",
                "Value": 1,
                "Unit": "Count",
            }],
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 1, "MaxAttempts": 2,
                   "BackoffRate": 2.0}],
        # Even if the alarm write fails, still fail the execution. Swallowing it would leave a
        # restricted account with no signal that anyone needs to look.
        "Catch": _catch("Phase2FailedTerminal"),
        "Next": "Phase2FailedTerminal",
    }
    states["Phase2FailedTerminal"] = {
        "Type": "Fail",
        "Error": "Phase2Failed",
        "Cause": (
            "Phase 2 did not complete. Restrictions remain in force and the case is still "
            "flagged pending. Requires human action; it will not resolve itself."
        ),
    }

    return {
        "Comment": "KYC Phase 2 — full screening, no clock pressure (design §7.4)",
        "QueryLanguage": "JSONata",
        "StartAt": "LoadCase",
        "States": states,
    }


# --- shared task builders ------------------------------------------------------------------


def _decision_engine_task(
    function_arn: str, *, phase: int, next_state: str, catch_to: str
) -> dict[str, Any]:
    """The pure-rules Lambda. No model (R4.6), so no reasoning config and no retry on throttle.

    Two shapes here were wrong until gate G9 tested the state in isolation, and both would have
    been invisible in a full run — the Catch would have fired and the case would have gone to
    review, which looks exactly like a legitimate degraded decision.

    * A `Parallel` state returns an **array**, and the engine requires `agentResults` to be an
      object keyed by agent name. It rejected the array outright.
    * Phase 2 needs `pepStatus` and `adverseMediaSeverity`, which the shared payload never sent.
      The engine has no defaults on purpose — a missing `pepStatus` silently becoming `NONE`
      would turn "we never checked" into "we checked and found nothing".
    """
    payload: dict[str, Any] = {
        "phase": phase,
        "edgeFacts": "{% $exists($case.edgeFacts) ? $case.edgeFacts : {} %}",
        "jurisdictionRisk": (
            # Guarded, and defaulted to LOW to match `edge_cases.py`'s own
            # `risk_by_code.get(code, RiskTier.LOW)` precedent for an unrecorded jurisdiction.
            #
            # Unguarded, this single expression made Phase 1 incapable of ever auto-approving.
            # Nothing in the system writes `jurisdictionRisk` onto the case record — it is read
            # here and by the decision engine, and written nowhere — so JSONata returned
            # undefined, `PreliminaryTier` died with States.QueryEvaluationError, the catch sent
            # every case to DegradedPath -> ManualReview, and the 90-second path was
            # unreachable for *all* applicants. Confirmed live: a clean case whose passport MRZ
            # parsed with valid check digits and whose liveness returned PASS still degraded.
            #
            # The neighbouring `edgeFacts` line already uses this `$exists` form; this one did
            # not, which is the whole defect.
            #
            # NOTE (needs compliance sign-off): defaulting an absent jurisdiction risk to LOW
            # is a fail-OPEN. It is safe only because `CheckJurisdiction` has already rejected
            # anything outside the four supported jurisdictions before this state is reached.
            # The durable fix is for case creation to set `jurisdictionRisk` from a maintained
            # jurisdiction risk list; this guard stops the crash, it does not supply real risk.
            "{% $exists($case.jurisdictionRisk) ? $case.jurisdictionRisk.S : 'LOW' %}"),
        "productApplied": "{% $case.productApplied.S %}",
        # R11.13 / F-89. The structured findings each agent recorded through its `submit_*`
        # contract tool, re-read from the case record. This is the deterministic channel: the tool
        # call is schema-validated by the service before it lands, so the shape is not the model's
        # choice, whereas the assistant message is prose and changed shape between two otherwise
        # identical runs.
        #
        # `$exists` guards rather than defaults — an absent findings map yields `{}`, the engine
        # finds no signals, and the case goes to a reviewer. That is the correct answer when no
        # agent recorded anything.
        # Assembled from one flat attribute per agent. Flat rather than a nested map because
        # DynamoDB rejects an UpdateExpression whose document paths overlap, and the tool has to
        # create the container and write into it in a single call.
        "findings": (
            "{% {'documents': $exists($case.findingDocuments) ? "
            "$case.findingDocuments.S : null, "
            "'prescreen': $exists($case.findingPrescreen) ? $case.findingPrescreen.S : null, "
            "'fraud': $exists($case.findingFraud) ? $case.findingFraud.S : null} %}"),
        # The two halves of the identity cross-check (`decision-engine/identity_match.py`): what the
        # applicant declared, and what the document proved.
        #
        # `documentExtraction` is written by the document tool itself, not by an agent, and that is
        # deliberate — its MRZ check digits make it the one part of document extraction that is
        # verified rather than trusted, so it reaches the rules without passing through a model's
        # prose.
        #
        # `$exists` guards rather than defaults, and the distinction matters here: an absent
        # extraction yields `null`, which the engine reports as `performed: False` — the control did
        # not run — instead of as a failed check. Conflating those two sent every case to review.
        # Fields named one by one rather than passing the map wholesale. Two reasons: the engine
        # would otherwise receive DynamoDB-typed values (`{"S": "REEVES"}`) where it expects plain
        # strings, and naming them keeps this payload a fixed shape — the case record is written by
        # the Public API from a request body, and forwarding it verbatim into the rules would carry
        # whatever else a caller had put there.
        "declaredIdentity": (
            "{% $exists($case.declaredIdentity) ? {"
            "'surname': $case.declaredIdentity.M.surname.S, "
            "'givenNames': $case.declaredIdentity.M.givenNames.S, "
            "'dateOfBirth': $case.declaredIdentity.M.dateOfBirth.S, "
            "'nationality': $case.declaredIdentity.M.nationality.S"
            "} : null %}"),
        "documentExtraction": (
            "{% $exists($case.documentExtraction) ? $case.documentExtraction.S : null %}"),
    }
    if phase == 1:
        # Re-read the case so `findings` reflects what the agents just wrote. The copy loaded at
        # LoadCase predates every agent, so it necessarily has no findings in it — reading that
        # stale copy would make the whole contract look like it had failed.
        # `documentConfidence`, `preScreenMatched` and `velocitySignals` are deliberately ABSENT.
        # They used to be read here as `$documentsResult.confidence`, `$prescreenResult.matched`
        # and `$fraudResult.velocity` — variables assigned *inside* the Parallel branches, which
        # are not visible at this scope. Every one resolved to nothing, the engine rejected the
        # payload, and the Catch sent the case to DegradedPath, indistinguishable in the console
        # from a genuine partial failure.
        #
        # Fixing the scope alone would not have been enough: the branch output is the model's
        # prose, and pulling values out of prose belongs in tested Python. The engine's
        # `signals.py` now derives them from `agentResults` and marks any agent whose signal it
        # cannot establish as UNAVAILABLE, which routes to a reviewer rather than to a default.
        payload["agentResults"] = (
            "{% $merge($branchResults.{ $lookup($, 'agent') ? "
            "$lookup($, 'agent') : 'unknown': $ }) %}")
    else:
        payload["agentResults"] = "{% {'compliance': $complianceResult} %}"
        # Carried forward from what Phase 1 actually measured and wrote to the case record, not
        # re-derived and not defaulted. R4.4 requires both tiers be recorded and comparable, which
        # is only meaningful if the shared inputs are the same values both times.
        #
        # `$exists` guards rather than invents: if Phase 1 did not record them the expression
        # returns nothing, JSONata fails the state, and the Catch routes to Phase2Failed with
        # restrictions intact. A quiet default here would let Phase 2 lift restrictions on inputs
        # nobody ever measured.
        payload["documentConfidence"] = "{% $number($case.documentConfidence.N) %}"
        payload["velocitySignals"] = (
            "{% {'critical': $case.velocityCritical.Bool} %}")
        payload["pepStatus"] = (
            "{% $exists($complianceResult.pepStatus) ? $complianceResult.pepStatus : 'NONE' %}")
        payload["adverseMediaSeverity"] = (
            "{% $exists($complianceResult.adverseMediaSeverity) ? "
            "$complianceResult.adverseMediaSeverity : 'NONE' %}")
        payload["screeningHits"] = (
            "{% $exists($complianceResult.screeningHits) ? "
            "$complianceResult.screeningHits : [] %}")

    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Arguments": {"FunctionName": function_arn, "Payload": payload},
        "Assign": {"decision": "{% $states.result.Payload %}"},
        "Retry": [{"ErrorEquals": ["Lambda.ServiceException", "Lambda.TooManyRequestsException"],
                   "IntervalSeconds": 1, "MaxAttempts": 3, "BackoffRate": 2.0}],
        "Catch": _catch(catch_to),
        "Next": next_state,
    }


def _mark_awaiting_review(case_table: str, *, next_state: str) -> dict[str, Any]:
    """Move the case into `AWAITING_REVIEW` before the human wait begins.

    Without this state the human review loop is inert in both directions, and neither failure is
    visible from the state machine's own console:

    * `review_api.py`'s `list_queue` finds cases by querying the `state` GSI for
      `AWAITING_REVIEW`, so the reviewer console shows an empty queue while task tokens pile up
      in SQS.
    * `decisions.py`'s `resolve()` refuses any submission whose case is not in `AWAITING_REVIEW`,
      returning `UNRESUMABLE` (410) — so even a reviewer who somehow obtained a token could not
      act on it.

    It is a separate state rather than an extra field on the SQS send because
    `sqs:sendMessage.waitForTaskToken` performs exactly one action; there is nowhere in it to put
    a DynamoDB write, and doing the write *after* the send would leave a window in which a token
    exists for a case the console cannot see.

    The write is unconditional on purpose. A case can legitimately arrive here more than once
    (Phase 1 review, then a Phase 2 review later), so a guard like `attribute_not_exists` would
    make the second review silently unqueueable — the same class of bug this state fixes.
    """
    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:updateItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {"applicantId": {"S": "{% $applicantId %}"}, "sk": {"S": "CASE"}},
            "UpdateExpression": (
                "SET #s = :awaiting, updatedAt = :t, "
                "transitions = list_append(if_not_exists(transitions, :empty), :tr)"
            ),
            # `state` is a DynamoDB reserved word, so it can only be referenced via an alias.
            "ExpressionAttributeNames": {"#s": "state"},
            "ExpressionAttributeValues": {
                ":awaiting": {"S": "AWAITING_REVIEW"},
                ":t": {"S": "{% $now() %}"},
                ":empty": {"L": []},
                # R2.5 — every transition recorded, with `system` as the actor. The reviewer's own
                # decision appends its own entry with the reviewer's identity (`decisions.py`).
                ":tr": {"L": [{"M": {
                    "from": {"S": "{% $case.state.S %}"},
                    "to": {"S": "AWAITING_REVIEW"},
                    "at": {"S": "{% $now() %}"},
                    "actor": {"S": "system"},
                    "reason": {"S": "queued for human review"},
                }}]},
            },
        },
        "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 1, "MaxAttempts": 3,
                   "BackoffRate": 2.0}],
        # No Catch: if the case cannot be marked, it must not be queued. A token for a case the
        # console cannot see is worse than a failed execution, because it waits seven days.
        "Next": next_state,
    }


def _review_task(
    queue_url: str, *, next_state: str, catch_to: str, phase: str
) -> dict[str, Any]:
    """The human wait — a separate state, never an agent call (F-12, design §7.5).

    `invokeHarness` has no `waitForTaskToken` variant, so the wait cannot live inside an agent.
    Modelling it as SQS `.waitForTaskToken` also means the queue depth and age are observable,
    which R6.6 needs and an in-agent wait could never provide.

    `phase` is carried in the message body because nothing downstream can infer it. Both
    `phase1_definition` and `phase2_definition` enter this same state, and the case record alone is
    ambiguous — a Phase 2 review sits on a case that already has a `phase1` decision written. With
    the phase absent, `review_handler.py`'s `phase=str(body.get("phase") or "phase1")` and the
    console's `queuedCase.phase ?? "phase1"` both silently defaulted, so EVERY console review was
    recorded as `phase1Review` and `existing_review`/`preliminary_tier` inspected the wrong slot.
    """
    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
        "Arguments": {
            "QueueUrl": queue_url,
            "MessageBody": {
                "applicantId": "{% $applicantId %}",
                "caseId": "{% $case.caseId.S %}",
                "taskToken": "{% $states.context.Task.Token %}",
                "phase": phase,
            },
        },
        # 7 days is the SQS maximum and the point is that a review does not expire quietly. A
        # heartbeat-less wait that timed out would move the case on with no human decision.
        "TimeoutSeconds": 604800,
        # Two catchers, not one. Step Functions rejects a definition where `States.ALL` shares a
        # list with anything else — it "must appear alone and at end of list" — and the specific
        # errors have to come first regardless, because catchers are evaluated in order and a
        # leading `States.ALL` would swallow everything behind it.
        "Catch": [
            {"ErrorEquals": [REVIEW_UNRESUMABLE, "States.Timeout"], "Next": catch_to},
            {"ErrorEquals": ["States.ALL"], "Next": catch_to},
        ],
        "Next": next_state,
    }


def phase3_definition(
    *,
    case_table: str,
    history_table: str,
    decision_engine_arn: str,
    audit_writer_arn: str,
    review_queue_url: str,
    fraud_harness_arn: str,
    fraud_model_id: str,
) -> dict[str, Any]:
    """Phase 3 — lifecycle re-assessment (the day-12 flow). G-01, R21, R31.

    Two structural decisions carry the whole flow, and both are the opposite of Phase 1's.

    **STANDARD, not Express.** A lifecycle execution may sit in a `Wait` for months until the next
    re-KYC date, and Express caps at five minutes and is at-least-once. Standard is also what makes
    the idempotency work: `StartExecution` is idempotent for STANDARD on the same name **and** the
    same input, so a replayed `TransactionRiskEvent` collapses onto the original execution instead
    of producing a second, different decision. The execution name is computed by the decision engine
    (`lifecycle.idempotency_name`) rather than composed in JSONata here — the same reasoning as
    F-89, that a correctness-critical string belongs in tested Python, not in a template expression
    nobody can unit test.

    **The write is an INSERT, never an update.** `PutItem` with
    `ConditionExpression: attribute_not_exists(sk)` against the history table. Two consequences,
    and the second is the point of the flow: a duplicate event cannot append a second row for the
    same event, and the day-zero decision is structurally unreachable — there is no code path here
    that can modify it, because the only write this graph performs is a create.

    `.waitForTaskToken` is deliberately absent on the agent invocation: Step Functions'
    `invokeHarness` integration is Request-Response only (F-12), so the callback pattern is not
    available and the task caps at 15 minutes regardless of `TimeoutSeconds` (F-14). A harness that
    outlives its caller keeps running, so every state below is written to tolerate arriving late.
    """
    states: dict[str, Any] = {}

    # The day-zero decision, read but never written. `ConsistentRead` because a re-assessment
    # triggered seconds after a Phase 2 decision would otherwise read the pre-decision copy and
    # compare against a tier that has already moved.
    states["LoadDayZero"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:getItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {
                "applicantId": {"S": "{% $states.input.detail.customerId %}"},
                "sk": {"S": "CASE"},
            },
            "ConsistentRead": True,
        },
        "Assign": {
            "riskEvent": "{% $states.input.detail %}",
            "case": "{% $states.result.Item %}",
            # Absent rather than defaulted. An event for a customer with no case is a routing bug,
            # and inventing a version 1 here would append a decision to a case that never existed.
            "day0Version": (
                "{% $exists($states.result.Item.decisionVersion) ? "
                "$number($states.result.Item.decisionVersion.N) : 1 %}"),
        },
        "Catch": _catch("EventUnroutable"),
        "Next": "CheckCaseExists",
    }

    # A missing case is not a re-assessment. Fail loudly rather than fabricating a day zero.
    states["CheckCaseExists"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [{
            "Condition": "{% $not($exists($case.applicantId)) %}",
            "Next": "EventUnroutable",
        }],
        "Default": "ReScreenFraud",
    }

    # The re-screen. This is where a top-tier Nova belongs: Phase 3 is asynchronous and off the
    # 90-second budget entirely, so the latency cost of a stronger model buys accuracy for free —
    # the same argument that put `high` reasoning effort on Phase 2's Compliance Risk agent (D-08).
    fraud_agent = next(a for a in AGENTS if a.key == "fraud")
    states["ReScreenFraud"] = agent_task(
        fraud_agent,
        harness_arn=fraud_harness_arn,
        model_id=fraud_model_id,
        prompt_expression=(
            "{% 'Re-assess this approved customer against a new risk event. Report signals only; "
            "the decision is not yours to make. Event: ' & $string($riskEvent) & "
            "' Case: ' & $string($case) %}"),
        next_state="ExtractFindings",
        catch_to="ReScreenIncomplete",
        # Off the 90-second critical path, so the ceiling is generous rather than tight: an
        # over-running re-screen should degrade to SYSTEM_ERROR and a human, not to a timeout that
        # loses the event. Same reasoning as Phase 2's 600s (D-08).
        timeout_seconds=600,
    )

    # Re-read the case so the structured `submit_fraud_assessment` finding the agent just wrote is
    # visible. Same reason as Phase 1's ReloadFindings: the copy loaded at LoadDayZero predates the
    # agent, so it necessarily has no finding in it.
    states["ExtractFindings"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:getItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {
                "applicantId": {"S": "{% $riskEvent.customerId %}"},
                "sk": {"S": "CASE"},
            },
            "ConsistentRead": True,
        },
        "Assign": {"case": "{% $states.result.Item %}"},
        "Catch": _catch("ReScreenIncomplete"),
        "Next": "Reassess",
    }

    # `evidenceComplete: true` is the assertion that the re-screen produced a finding. When
    # ReScreenIncomplete routes here instead, it is false, and the engine returns SYSTEM_ERROR
    # rather than "risk unchanged" — an unavailable control is never a clean result (R25.7).
    states["Reassess"] = _phase3_engine_task(
        decision_engine_arn, evidence_complete=True,
        next_state="AppendDecision", catch_to="ReassessFailed")

    states["ReScreenIncomplete"] = _phase3_engine_task(
        decision_engine_arn, evidence_complete=False,
        next_state="AppendDecision", catch_to="ReassessFailed")

    # The append. `attribute_not_exists(sk)` is what makes this idempotent AND what makes the
    # history immutable: the only write in this graph creates a row, so there is no path that can
    # edit the day-zero decision. A replayed event fails the condition and lands in
    # DuplicateEventIgnored, which is a success — the decision already exists.
    states["AppendDecision"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:putItem",
        "Arguments": {
            "TableName": history_table,
            "Item": {
                "customerId": {"S": "{% $riskEvent.customerId %}"},
                # Computed by the engine (`lifecycle.history_sort_key`) so the zero-padding that
                # keeps version 10 sorting after version 2 is covered by a unit test.
                "sk": {"S": "{% $decision.historySortKey %}"},
                "caseId": {"S": "{% $case.caseId.S %}"},
                "eventId": {"S": "{% $riskEvent.eventId %}"},
                "eventType": {"S": "{% $riskEvent.eventType %}"},
                "outcome": {"S": "{% $decision.outcome %}"},
                "day0Tier": {"S": "{% $decision.day0Tier %}"},
                "newTier": {"S": "{% $decision.newTier %}"},
                # `Bool`, not `BOOL`. Step Functions normalises DynamoDB AttributeValue member
                # names to PascalCase for its aws-sdk integration and rejects the wire-format
                # spelling outright ("The field \"BOOL\" is not supported by Step Functions").
                # Found by the first live execution, not by synth — the template is valid
                # CloudFormation either way, so only a real run surfaces it.
                "riskChanged": {"Bool": "{% $decision.riskChanged %}"},
                "version": {"N": "{% $string($decision.version) %}"},
                "decidedAt": {"S": "{% $decision.decidedAt %}"},
                "explanation": {"S": "{% $decision.explanation %}"},
            },
            "ConditionExpression": "attribute_not_exists(sk)",
        },
        "Catch": [{
            # The conditional-check failure is the expected outcome of a replay, not an error.
            "ErrorEquals": ["DynamoDb.ConditionalCheckFailedException"],
            "Next": "DuplicateEventIgnored",
        }] + _catch("ReassessFailed"),
        "Next": "RecordAudit",
    }

    states["RecordAudit"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Arguments": {
            "FunctionName": audit_writer_arn,
            "Payload": {
                "phase": 3,
                "caseId": "{% $case.caseId.S %}",
                "customerId": "{% $riskEvent.customerId %}",
                "eventId": "{% $riskEvent.eventId %}",
                "decision": "{% $decision %}",
            },
        },
        "Catch": _catch("RouteOutcome"),
        "Next": "RouteOutcome",
    }

    # Only the *current* view moves. The day-zero row is untouched by every branch below.
    states["RouteOutcome"] = {
        "Type": "Choice",
        "QueryLanguage": "JSONata",
        "Choices": [
            {"Condition": "{% $decision.outcome = 'FREEZE' %}", "Next": "FreezeAccount"},
            {"Condition": "{% $decision.outcome = 'RISK_INCREASED_REVIEW' %}",
             "Next": "EscalateToReview"},
            {"Condition": "{% $decision.outcome = 'RISK_INCREASED_RESTRICT' %}",
             "Next": "ReapplyRestrictions"},
            {"Condition": "{% $decision.outcome = 'SYSTEM_ERROR' %}",
             "Next": "EscalateToReview"},
        ],
        "Default": "UpdateRiskState",
    }

    # Each terminal risk state is written by exactly one state and then STOPS. An earlier version
    # chained ReapplyRestrictions -> UpdateRiskState, which wrote RESTRICTED and then immediately
    # overwrote it with MONITORING — the restriction was applied and lifted in the same execution.
    # The synth test `test_no_failure_path_reaches_a_state_that_lifts_a_restriction` is what caught
    # it, which is the argument for asserting negative properties rather than smoke-testing.
    states["FreezeAccount"] = _set_risk_state(
        case_table, state="FROZEN", next_state="EscalateToReview",
        # A failed freeze still reaches a human. Escalation changes no risk state, so it cannot
        # relax anything; silently continuing would leave a customer unfrozen with no alert.
        catch_to="EscalateToReview")
    states["ReapplyRestrictions"] = _set_risk_state(
        case_table, state="RESTRICTED", next_state="Done", catch_to="EscalateToReview")
    states["UpdateRiskState"] = _set_risk_state(
        case_table, state="MONITORING", next_state="Done", catch_to="Done")

    states["EscalateToReview"] = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::sqs:sendMessage",
        "Arguments": {
            "QueueUrl": review_queue_url,
            "MessageBody": {
                "phase": 3,
                "reason": "{% $decision.outcome %}",
                "customerId": "{% $riskEvent.customerId %}",
                "caseId": "{% $case.caseId.S %}",
                "eventId": "{% $riskEvent.eventId %}",
                "explanation": "{% $decision.explanation %}",
            },
        },
        "Catch": _catch("Done"),
        "Next": "Done",
    }

    # Terminal states. A duplicate is a Succeed, not a Fail: the decision it would have written
    # already exists, so the system is in the intended state and paging someone would be noise.
    states["DuplicateEventIgnored"] = {"Type": "Succeed"}
    states["Done"] = {"Type": "Succeed"}
    states["EventUnroutable"] = {
        "Type": "Fail",
        "Error": "EventUnroutable",
        "Cause": "No case exists for this customer; the risk event cannot be assessed.",
    }
    states["ReassessFailed"] = {
        "Type": "Fail",
        "Error": "ReassessFailed",
        "Cause": "Re-assessment could not complete. The day-zero decision is unchanged.",
    }

    return {
        "Comment": "Phase 3 — lifecycle re-assessment. Appends a decision; never rewrites day zero.",
        "QueryLanguage": "JSONata",
        "StartAt": "LoadDayZero",
        "States": states,
    }


def _phase3_engine_task(
    function_arn: str, *, evidence_complete: bool, next_state: str, catch_to: str
) -> dict[str, Any]:
    """Invoke the decision engine in Phase-3 mode.

    `evidenceComplete` is passed explicitly rather than inferred from whether a finding happens to
    be present. The engine treats `false` as "the control did not run" and returns SYSTEM_ERROR;
    letting it guess from an absent field would make a parsing failure indistinguishable from a
    completed clean re-screen, which is the F-89 mistake in a different costume.
    """
    payload: dict[str, Any] = {
        "phase": 3,
        # The engine owns the clock for the record; the state machine supplies it because the rule
        # layer is pure and cannot read one (design §8).
        "decidedAt": "{% $states.context.Execution.StartTime %}",
        "evidenceComplete": evidence_complete,
        "riskEvent": "{% $riskEvent %}",
        "day0": {
            "caseId": "{% $case.caseId.S %}",
            # Read from where the day-zero decision actually is. `fullTier`, `decision` and
            # `decidedAt` are attributes nothing ever writes: the decision lives in the `phase1`
            # (or `phase2`) JSON string, and its timestamp in `provisionalDecidedAt`. Unguarded,
            # all three returned undefined and `Reassess` died with States.QueryEvaluationError,
            # so every lifecycle re-assessment ended at `ReassessFailed` — "Re-assessment could
            # not complete. The day-zero decision is unchanged."
            #
            # Phase 2 wins over Phase 1 when present: a full-tier decision supersedes the
            # provisional one, and re-assessing against the superseded tier would compare today's
            # risk to a decision that no longer stands.
            "tier": ("{% $exists($case.phase2) ? $parse($case.phase2.S).tier : "
                     "($exists($case.phase1) ? $parse($case.phase1.S).tier : null) %}"),
            "outcome": ("{% $exists($case.phase2) ? $parse($case.phase2.S).outcome : "
                        "($exists($case.phase1) ? $parse($case.phase1.S).outcome : null) %}"),
            "decidedAt": ("{% $exists($case.fullDecidedAt) ? $case.fullDecidedAt.S : "
                          "($exists($case.provisionalDecidedAt) ? "
                          "$case.provisionalDecidedAt.S : null) %}"),
            "version": "{% $day0Version %}",
        },
    }
    if evidence_complete:
        payload.update({
            "jurisdictionRisk": (
            # Guarded, and defaulted to LOW to match `edge_cases.py`'s own
            # `risk_by_code.get(code, RiskTier.LOW)` precedent for an unrecorded jurisdiction.
            #
            # Unguarded, this single expression made Phase 1 incapable of ever auto-approving.
            # Nothing in the system writes `jurisdictionRisk` onto the case record — it is read
            # here and by the decision engine, and written nowhere — so JSONata returned
            # undefined, `PreliminaryTier` died with States.QueryEvaluationError, the catch sent
            # every case to DegradedPath -> ManualReview, and the 90-second path was
            # unreachable for *all* applicants. Confirmed live: a clean case whose passport MRZ
            # parsed with valid check digits and whose liveness returned PASS still degraded.
            #
            # The neighbouring `edgeFacts` line already uses this `$exists` form; this one did
            # not, which is the whole defect.
            #
            # NOTE (needs compliance sign-off): defaulting an absent jurisdiction risk to LOW
            # is a fail-OPEN. It is safe only because `CheckJurisdiction` has already rejected
            # anything outside the four supported jurisdictions before this state is reached.
            # The durable fix is for case creation to set `jurisdictionRisk` from a maintained
            # jurisdiction risk list; this guard stops the crash, it does not supply real risk.
            "{% $exists($case.jurisdictionRisk) ? $case.jurisdictionRisk.S : 'LOW' %}"),
            "productApplied": "{% $case.productApplied.S %}",
            "findings": (
                "{% {'fraud': $exists($case.findingFraud) ? $case.findingFraud.S : null} %}"),
            "pepStatus": (
                "{% $exists($case.pepStatus) ? $case.pepStatus.S : 'NONE' %}"),
            "adverseMediaSeverity": (
                "{% $exists($case.adverseMediaSeverity) ? "
                "$case.adverseMediaSeverity.S : 'NONE' %}"),
            "screeningHits": (
                "{% $exists($case.screeningHits) ? $case.screeningHits : [] %}"),
            # Guarded. `customerAge` is written by nothing in this system and read by nothing
            # in the decision engine either — it is a vestigial payload field. Unguarded it
            # was fatal: JSONata returned undefined, `Reassess` died with
            # States.QueryEvaluationError, and every Phase 3 re-assessment ended at
            # `ReassessFailed` ("Re-assessment could not complete"). The one run that ever
            # succeeded used a hand-seeded case that happened to carry the attribute, which is
            # exactly why a green result on seeded data is not evidence about real data.
            "customerAge": ("{% $exists($case.customerAge) ? "
                            "$number($case.customerAge.N) : null %}"),
            # `.Bool`, not `.BOOL`. Step Functions' aws-sdk DynamoDB integration returns
            # AttributeValue members in PascalCase on the way OUT as well as in: a real getItem
            # returned `{'Bool': False}`. Reading `.BOOL` yields undefined, and JSONata fails the
            # whole state on undefined (not null), so the case died at QueryEvaluationError. The
            # same latent spelling existed in `_decision_engine_task` for velocityCritical and is
            # fixed there too — it only fires when that attribute is present on the case.
                        # Guarded, and null rather than false. Nothing writes `preScreenMatched` to the
            # case record, so unguarded this was the next QueryEvaluationError waiting
            # behind `customerAge` — found by extending the unguarded-read test to phase 3
            # rather than by another failed live run. Null so the engine treats an
            # unknown screening result as undetermined; `false` would assert "no sanctions
            # match" on no evidence, which is a fail-OPEN on the one check that must not.
            "preScreenMatched": ("{% $exists($case.preScreenMatched) ? "
                                 "$case.preScreenMatched.Bool : null %}"),
            "documentConfidence": "{% $number($case.documentConfidence.N) %}",
            "velocitySignals": "{% {'critical': false, 'detail': []} %}",
        })
    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Arguments": {"FunctionName": function_arn, "Payload": payload},
        "Assign": {"decision": "{% $states.result.Payload %}"},
        "Catch": _catch(catch_to),
        "Next": next_state,
    }


def _set_risk_state(
    case_table: str, *, state: str, next_state: str, catch_to: str
) -> dict[str, Any]:
    """Move the CURRENT risk state on the case. Never touches a decision row.

    This is the one legitimate update in Phase 3 and it is deliberately narrow: it writes
    `riskState` and `updatedAt` and nothing else. The decision history is append-only and lives in
    a separate table, so this cannot reach it even by accident.
    """
    return {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::aws-sdk:dynamodb:updateItem",
        "Arguments": {
            "TableName": case_table,
            "Key": {
                "applicantId": {"S": "{% $riskEvent.customerId %}"},
                "sk": {"S": "CASE"},
            },
            "UpdateExpression": (
                "SET riskState = :s, decisionVersion = :v, updatedAt = :t"),
            "ExpressionAttributeValues": {
                ":s": {"S": state},
                ":v": {"N": "{% $string($decision.version) %}"},
                ":t": {"S": "{% $states.context.Execution.StartTime %}"},
            },
        },
        # Explicit rather than defaulting to `next_state`. Reusing next_state here is how a
        # restriction-lifting path hid: a failed ReapplyRestrictions fell through into the state
        # that writes MONITORING. A Catch must never land somewhere less restrictive than the
        # state that failed.
        "Catch": _catch(catch_to),
        "Next": next_state,
    }
