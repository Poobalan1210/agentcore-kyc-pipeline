# Measurements

Every number here was measured against the live service. Nothing on this page is an
estimate. Where a figure is still an estimate it lives in the Assumption Register in
`.kiro/specs/kyc-90-seconds/requirements.md`, not here.

| Field | Value |
|---|---|
| **Account** | `633898796092` |
| **Primary Region** | `us-east-1` |
| **Spike harness** | `arn:aws:bedrock-agentcore:us-east-1:633898796092:harness/kycspike_kyc_spike_fraud-wFNik3Ok0E` |
| **Created via** | `agentcore` CLI `1.0.0-preview.13` → CDK stack `AgentCore-kycspike-default` |
| **boto3 / botocore** | 1.43.67 |
| **Raw data** | `spike/spike_results.json`, `spike/reasoning_effort.json`, `spike/cold_start.json` |

---

## Task 0.1 — CloudWatch Transaction Search  · GATE G0 · PASS

| Check | Result |
|---|---|
| Trace segment destination | `CloudWatchLogs` |
| Status | `ACTIVE` |
| Indexing rule | `Default`, probabilistic, **100%** desired sampling |
| Rule last modified | 2026-06-17 |
| `aws/spans` log group | present, **889,933 bytes** stored — segments are genuinely ingesting |
| Per-agent log groups | 6 existing `/aws/bedrock-agentcore/runtimes/*` groups |

Verified 2026-08-09. It was already enabled in this account, so no change was made. The
evidence that matters is the stored bytes: an enabled-but-empty configuration looks identical
to a working one on the console (F-19), and only ingestion volume distinguishes them.

Re-verify weekly through 2026-11-29 (R19.2) and again within 24 hours of the session (R26.8).

---

## Task 0.2 — Nova model access  · GATE G1 · PASS

All three base models required by design §4.2 are `ACTIVE` in `us-east-1`:

| Model ID | Used by |
|---|---|
| `amazon.nova-micro-v1:0` | A1 Router, A5 Pre-Screen, A7 Notify |
| `amazon.nova-lite-v1:0` | A2 Documents, A3 Identity |
| `amazon.nova-2-lite-v1:0` | A4 Fraud, A6 Compliance Risk |

63 inference profiles visible in `us-east-1`; 6 are Nova.

### Per-jurisdiction availability — evidence for R12.10, R12.11, F-50, F-69

Probed across five Regions on 2026-08-02, then confirmed by invocation.

| Jurisdiction | Region | Nova 2 Lite geo | Nova Lite V1 | Resulting inference option |
|---|---|---|---|---|
| US | `us-east-1` | `us.` ✓ | ✓ | **Geo Cross-Region** |
| UK | `eu-west-2` | none — `global.` only | ✓ | **Global** for 2 Lite, or In-Region Lite V1 |
| EU | `eu-west-1` | `eu.` ✓ | ✓ | **Geo Cross-Region** |
| AU | `ap-southeast-2` | none — `global.` only | ✓ + `apac.` | **Geo via `apac.` Nova Lite V1** |
| JP | `ap-northeast-1` | `jp.` ✓ | ✓ + `apac.` | **Geo Cross-Region** |

**Three results that correct or sharpen the spec:**

- `apac.` geo profiles **do** exist — for Nova Lite V1, Micro and Pro. The original claim
  "no APAC geo" was too broad. The precise, defensible claim is narrower and stronger:
  **`apac.amazon.nova-2-lite-v1:0` does not exist at all**, failing with *"The provided model
  identifier is invalid."*
- `amazon.nova-2-lite-v1:0` cannot be invoked In-Region anywhere: *"Invocation of model ID
  amazon.nova-2-lite-v1:0 with on-demand throughput isn't supported."* F-21 confirmed by
  invocation, not just by the model card.
- So **AU has a residency-respecting path, but not on Nova 2 Lite.** It runs
  `apac.amazon.nova-lite-v1:0` and forgoes extended thinking (R12.10).

### Latency cost of global routing

| Call | Region | Latency |
|---|---|---|
| `apac.amazon.nova-lite-v1:0` | `ap-southeast-2` | **250 ms** |
| `amazon.nova-lite-v1:0` In-Region | `ap-southeast-2` | **260 ms** |
| `global.amazon.nova-2-lite-v1:0` | `ap-southeast-2` | **802 ms** |

Global routing from Sydney costs **3.2×** the latency of staying in APAC. Global inference is
not only a residency question; it is a performance question, and both point the same way.

---

## Task 0.3 — Nova IDs through `InvokeHarness` · PASS

Single-turn invocation, document-classification prompt, live harness.

| Model ID | Result | TTFT | Total |
|---|---|---|---|
| `us.amazon.nova-2-lite-v1:0` | OK, clean fenced JSON | 6841 ms | 7235 ms |
| `us.amazon.nova-lite-v1:0` | OK | 4875 ms | 5947 ms |
| `us.amazon.nova-micro-v1:0` | OK | 3618 ms | 4040 ms |
| `global.amazon.nova-2-lite-v1:0` | OK, clean fenced JSON | 5604 ms | 5965 ms |

Two observations with design consequences:

- **Nova Lite V1 and Micro emit visible `<thinking>` blocks** in ordinary output with no
  reasoning enabled; Nova 2 Lite returned fenced JSON without them (F-68). D-04's premise —
  that `<thinking>` content exists and must be retained as evidence under two-tier retention —
  is confirmed, and it applies to the *cheap* models too, not only the reasoning ones.
- These are **single-agent, single-turn** figures on a warm-ish path. They are not a Phase 1
  budget. Phase 1 runs five agents in parallel plus tool calls; task 12.1 measures that (T2)
  and task 14.10 measures the actual claim (T1).

---

## Task 0.6 — reasoning effort · F-22 CONFIRMED

8 runs per arm, identical sanctions-matching prompt, `us.amazon.nova-2-lite-v1:0`.

| Arm | OK | TTFT p50 | TTFT max | Total p50 | Total max |
|---|---|---|---|---|---|
| `medium`, temperature + maxTokens pinned | 8/8 | **2 788 ms** | 4 320 ms | 5 224 ms | 6 503 ms |
| `medium`, unpinned | 8/8 | 4 621 ms | 6 357 ms | 8 505 ms | 11 917 ms |
| `high`, temperature + maxTokens unset | 8/8 | **21 716 ms** | **48 304 ms** | 21 883 ms | **48 539 ms** |
| `high`, pinned | **0/8** | — | — | — | — |

**high versus medium, like for like (both unpinned): TTFT 4.70×, total 2.57×.**
Against the pinned medium configuration the design actually uses, high is **7.8× worse on
TTFT**.

Four conclusions, three of which change the spec:

1. **F-22 confirmed, and the mechanism is visible.** For `high`, TTFT p50 21 716 ms is
   effectively equal to total p50 21 883 ms — there is almost no streaming. That is precisely
   the documented behaviour of buffering all reasoning into a final chunk, now measured.
2. **D-08 was not merely defensible, it was necessary.** A single agent at `high` has a
   **48.5-second** worst case. Inside a 90-second Phase 1 budget with five parallel agents,
   that alone would end the claim. Compliance Risk belongs in Phase 2, and this is the number
   that proves it.
3. **`high` and pinned sampling are mutually exclusive** (F-62). All 8 pinned runs failed:
   *"`temperature` and `maxTokens` must be unset when reasoning is enabled."* The one agent
   that runs at `high` therefore **cannot pin its temperature**. The determinism argument in
   design §8 is not a stylistic preference — the API forbids the alternative.
4. **Pinning is faster at `medium`**, 2 788 ms versus 4 621 ms TTFT. Constraining the model
   helps latency as well as reproducibility, so there is no trade to make on the Phase 1 agents.

### Shape defect found here

Design §7.3 previously showed `AdditionalParams: { reasoningConfig: {...} }`. That is
rejected (F-61). The accepted shape nests one level deeper:

```json
"AdditionalParams": {
  "additionalModelRequestFields": {
    "reasoningConfig": { "type": "enabled", "maxReasoningEffort": "medium" }
  }
}
```

`AdditionalParams` is spliced into a Bedrock **Converse** request, so it accepts only
Converse's surface: `modelId`, `messages`, `system`, `inferenceConfig`, `toolConfig`,
`guardrailConfig`, `additionalModelRequestFields`.

Reasoning also consumes the output token budget (F-63): at `maxTokens: 600` runs failed with
*"Model stopped generating due to maximum token limit."*

---

## Task 0.4 — Nova through Step Functions · GATE G4 · PASS · closes A-03

Method: `TestState` against a real Task on
`arn:aws:states:::bedrockagentcore:invokeHarness`, one state per model configuration, role
`KycSpikeSfnRole`. This is the gap P-10 does not close — P-10 invoked through the
`agentcore` CLI, never through Step Functions.

| Model ID | effort | sampling pinned | Result |
|---|---|---|---|
| `us.amazon.nova-micro-v1:0` | — | yes | **SUCCEEDED** |
| `us.amazon.nova-lite-v1:0` | — | yes | **SUCCEEDED** |
| `us.amazon.nova-2-lite-v1:0` | — | yes | **SUCCEEDED** |
| `us.amazon.nova-2-lite-v1:0` | `medium` | yes | **SUCCEEDED** |
| `us.amazon.nova-2-lite-v1:0` | `high` | no (required) | **SUCCEEDED** |
| `global.amazon.nova-2-lite-v1:0` | — | yes | **SUCCEEDED** |

**6/6. A-03 is closed and design §4.2 stands unchanged.** The corrected reasoning shape from
F-61 works through Step Functions as well as through the raw API.

Two prerequisites had to be discovered by failing first, and both would have blocked the
build:

- **`"QueryLanguage": "JSONata"` is mandatory** on any state using `Arguments` or `{% … %}`
  (F-71). Without it the definition does not validate at all: *"field 'Arguments' is only
  supported for the 'JSONata' QueryLanguage."*
- **The IAM action is `bedrock-agentcore:InvokeAgentRuntime`, not `InvokeHarness`** (F-70).
  The denial names it explicitly. Note the collision with R16: the action to *withhold* is
  `InvokeAgentRuntimeCommand`, because it bypasses `allowedTools`. Grant one, withhold the
  other, and do not confuse them.

The observed output confirms F-13's shape directly —
`{"Output":{"Message":{"Role":"assistant","Content":[…]}},"StopReason":…}` — a single final
assistant message with no tool-use or reasoning blocks.

One anomaly worth carrying forward: the `global.` profile SUCCEEDED but returned
`"Content": []`, an empty content array. It is a success with no answer. Any aggregation step
that assumes `Content[0]` exists will throw on it, so the decision engine must treat empty
content as a failed agent result rather than as an absent finding.

---

## Task 0.5 — cold versus warm · closes A-01

`us.amazon.nova-micro-v1:0`, trivial JSON prompt, `idleRuntimeSessionTimeout` 120 s.

| Arm | n | p50 | p95 | min | max |
|---|---|---|---|---|---|
| Warm — one session reused | 12 | **2 327 ms** | 3 132 ms | 2 077 ms | 3 164 ms |
| Fresh session each call, no idle gap | 8 | **4 940 ms** | 5 267 ms | 3 548 ms | 6 605 ms |
| Fresh session after a 130 s idle gap | 4 | **3 357 ms** | 3 556 ms | 2 982 ms | 5 408 ms |

**A-01 is closed, and its premise was wrong.** There is no container cold start to fear. What
costs time is **establishing a session**, and idleness adds nothing on top of that: a fresh
session after a 130-second gap (3 357 ms) was *faster* than a fresh session created under
back-to-back pressure (4 940 ms).

The plausible reading of that inversion is contention, not warmth — the 8 no-gap calls created
eight sessions in quick succession, while the cold calls were 130 seconds apart. Which points
at the pattern that actually matters here: **Phase 1 creates five sessions at once**, so the
no-gap arm, not the idle arm, is the honest model for it. Budget roughly **+2.6 s per agent**
for session establishment against a warm baseline.

That makes the pre-warm strategy in R26.1 worth real money rather than being cosmetic, and it
reframes what pre-warming means: hold sessions open, do not warm containers.

Caveat stated plainly: n is 12 / 8 / 4, not the 100 warm and 20 cold that task 12.1 requires.
These are spike figures for a single agent on the cheapest model. They bound the problem; they
do not replace T2.

---

## Toolchain findings

These cost real time to discover and are recorded so nobody repeats them.

| Finding | Detail |
|---|---|
| **No `CreateHarness` API** | The Harness control-plane operations are `CreateLoopyAgent`, `Get/List/Update/DeleteLoopyAgent` — internally the Harness is a "LoopyAgent" |
| **Those operations are not served** | Every `*LoopyAgent` call returns `AccessDeniedException: Unable to determine service/operation name to be authorized`, while `ListGateways`, `ListAgentRuntimes`, `ListPolicyEngines`, `ListMemories`, `ListBrowsers` and `ListCodeInterpreters` all succeed on the same client and credentials. The route exists in the SDK model but not on the endpoint (F-64) |
| **AWS CLI 2.33.11 is too old** | Its bundled `bedrock-agentcore-control` model has 82 operations and **no** Harness operations. botocore 1.43.67 has 135. Neither can create a harness |
| **The working path** | `agentcore` CLI → `harness.json` → `agentcore deploy` → CDK. Invocation then works fine from boto3 via `bedrock-agentcore.invoke_harness` |
| **`InvokeHarness` streams** | The response is an event stream (F-65). The Converse-shaped single body in F-13 is produced by Step Functions, not by the API |
| **Memory really is on by default** | `agentcore add harness --no-memory` exists to *"Skip auto-creating memory"* (F-66). With the flag, `harness.json` omits the `memory` key entirely |
| **`lifecycleConfig` is settable at create** | `idleRuntimeSessionTimeout` and `maxLifetime` both accepted, so R11.14's 900-second cap is implementable today |
| **Two packages, one name** | `agentcore` CLI ships CDK constructs at `v0.1.0-alpha.36`; `@aws-cdk/aws-bedrock-agentcore-alpha` is separately at `2.263.0-alpha.0` with 55 published versions. Pin one and say which (A-16) |

---

## Branch `reinvent-ready` — rehearsal figures · NOT YET MEASURED

This branch has not been deployed, so it has no live figures yet, and this section stays empty
until it does: nothing here may be an estimate. After the first rehearsal in the deployed
account, paste the output of:

```bash
make measure                                   # time to decision p50/p95, agent latency, tokens
./.venv/bin/python scripts/measure.py --price-per-mtok <blended USD per 1M tokens>   # with cost
make export                                    # per-state durations: where the seconds go
make check-policies                            # Cedar decisions before any ENFORCE switch
```

and, for figures under load, `scripts/loadtest.py` (it writes; read its docstring first) followed
by `scripts/measure.py --prefix APP-LOADTEST-<run>`.

What *is* measured on this branch, offline (`make evals`): each persona reaches its act's outcome
through the real tools and rules; 1,000 replays of each recorded event give one output; misleading
agent replies change no decision; a tool forced unavailable, a face that does not match and a
contradicted date of birth each route to a person. These are properties of the code, not
latencies, and say nothing about time to decision.
