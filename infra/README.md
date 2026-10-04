# infra — Python CDK

Python CDK app for the eleven stacks in design §12.3.

## Which AgentCore package this repo pins, and why

A-16 records a trap worth stating up front: **two different packages are both called "the
AgentCore CDK constructs"**, they use incompatible version schemes, and mixing them produces
errors that look like your code is wrong.

| Package | Version seen 2026-08-09 | Used here |
|---|---|---|
| `agentcore` CLI, which ships its own CDK constructs | `v0.1.0-alpha.36` (CLI `1.0.0-preview.13`) | **No — see below** |
| `@aws-cdk/aws-bedrock-agentcore-alpha` (npm / PyPI) | `2.263.0-alpha.0`, 55 published versions | **No** |

**Everything, harnesses included, is created by this one CDK app.**

An earlier version of this file said harnesses had to come from the `agentcore` CLI. That was
wrong, and the reasoning is worth keeping because the first half of it still holds. The Harness
control plane genuinely is unreachable from the SDK: the operation is `CreateLoopyAgent` rather
than `CreateHarness`, and every `*LoopyAgent` call returns `AccessDeniedException: Unable to
determine service/operation name to be authorized` while six of seven sibling operations on the
same client succeed (F-64). The mistake was concluding the CLI was therefore the only path.
**CloudFormation is a third path, and it works** — it supports 31 AgentCore types including
`Harness` and `HarnessEndpoint` (F-74). Harnesses are declared here as L1 `CfnResource`.

So there is no split. This app owns KMS, S3, DynamoDB, IAM, Gateways, policy engines, the seven
harnesses and their endpoints, state machines, ingress, observability, auth, API and frontend
hosting.

Two traps in the CFN path, both of which cost real deploys:

* **The CFN shapes are not the API shapes.** Gateway target tool definitions need PascalCase
  `Name`/`Description`/`InputSchema` while MCP and the Lambda use camelCase; rate limits need
  `DimensionKeys: ["targetName"]` and `Requests` as a single-element array (F-78).
* **A control missing from a schema's top-level `properties` may still be settable.** Session
  lifetime looked absent, and that conclusion was recorded as fact F-75 and acted on. It is in
  fact nested at `Environment.AgentCoreRuntimeEnvironment.LifecycleConfiguration` (F-83). Walk
  the `$ref` graph before concluding a property does not exist — believing it did not left every
  harness free to bill for the default 8 hours after its Step Functions Task had already given up.

Do not add `@aws-cdk/aws-bedrock-agentcore-alpha` or the CLI constructs. Mixing either with
`aws-cdk-lib` produces version errors that read as though your own code is at fault.

## Language policy

Python for the CDK app, every Lambda handler, the decision engine, the router, the account
stub, the evaluators and all tests. `aws-cdk-lib` for Python, `pytest`, `boto3`.

React appears in exactly one place — the console (`frontend/apps/console`: the applicant flow,
review queue and demo controls) — because Face Liveness requires the Amplify JS SDK and `FaceLivenessDetector` is a
React component (F-42). Claiming "Python only" without that carve-out would be a claim that
breaks on contact with the liveness requirement. No other language enters the repo.

## Layout

```
infra/
├── app.py                    CDK entry point, two-stage aware
├── cdk.json
├── requirements.txt          exact pins, no ranges (A-16)
├── pytest.ini
├── verify_foundation.py      asserts the LIVE account matches the spec
├── kyc/
│   ├── config.py             jurisdictions, agents, quotas — the single source of truth
│   └── foundation_stack.py   KMS, S3 + Object Lock, DynamoDB, IAM ×10
└── tests/
    └── test_foundation_stack.py
```

`kyc/config.py` is deliberately the only place a model ID or Region appears. R12.8 requires the
jurisdiction matrix to be generated from code rather than maintained by hand, so it cannot go
stale as availability shifts — and it shifted twice during Phase 0 alone.

## Running it

```bash
make install     # venv + pinned deps
make test        # 20 assertions against the synthesised template
make synth
make stage1      # deploy KycFoundation
make verify      # assertions against the live account. Writes a probe object to each evidence
                 # bucket: removed again under the DEMO lock, kept 7 years under COMPLIANCE
```

## Two things here cannot be undone

**Object Lock must be enabled at bucket creation.** It cannot be retrofitted. If it is missed,
the only remedy is to delete and recreate the bucket. `tests/test_foundation_stack.py` asserts
it on the synthesised template before any deploy, and `verify_foundation.py` proves it against
the live account by writing an object and confirming the delete is refused. A configuration
that is enabled-but-not-enforcing looks identical to a working one until somebody tries to
delete evidence, which is the wrong moment to find out.

**The KMS key-per-jurisdiction split defines the residency boundary.** Merging keys later means
re-encrypting evidence that Object Lock forbids you to delete.

Retention is **7 years, not 5**. Australia and Japan both require seven (B-12, B-13), so one
floor of seven satisfies all four markets and is the binding requirement in two of them rather
than a safety margin.

## Verified state, 2026-08-09

`KycFoundation` = `CREATE_COMPLETE`. `make verify` reports **61 checks, 61 passed**, including
a real delete attempt against each of the four evidence buckets returning `AccessDenied`.
