"""Cedar policies — Phase 8, stage 2 of the two-stage deploy (task 8.1, F-17).

One `AWS::BedrockAgentCore::Policy` resource per Cedar *statement*, never per document. This is
not a stylistic choice: `CreatePolicy` was tested live against a `Statement` containing several
concatenated `permit(...)` blocks and rejected it with `unexpected token 'permit'` — the parser
stops after the first `;`. Splitting one statement per resource, confirmed live the same way,
succeeds. `policies/generate.py` already emits one statement per manifest entry for exactly this
reason; this stack does no further splitting, only creation.

Deployed as `KycPolicies`, the second of the two stages `make deploy` runs (R14.8). It cannot be
synthesised, let alone deployed, before stage 1: it reads `policies/generated/manifest.json`,
which does not exist until `policies/generate.py` has read stage 1's captured Gateway and policy
engine ARNs. Cedar forbids wildcard resources (F-17), so there is no version of this stack that
could reference a Gateway before that Gateway exists.

Every statement starts life in `LOG_ONLY` (R14.6): the policy engine is attached to its Gateway
in `LOG_ONLY` mode already (`gateways_stack.py`), and the `EnforcementMode` on each individual
`Policy` resource is set to `LOG_ONLY` here to match, so a bad statement is visible in CloudWatch
before it can deny a real call. Promotion to `ENFORCE` (R14.6's 2026-10-24 deadline) is a
follow-up change to this one flag, not a new deploy.

**`ValidationMode` differs by statement kind, confirmed live.** `FAIL_ON_ANY_FINDINGS` runs
Cedar's semantic analysis against every action in the Gateway's schema (not just the one the
statement scopes to). A guardrail statement's scope is deliberately unconditioned —
`forbid(principal, action, resource == Gateway::"...")` — because it is meant to gate every tool
call, not one; the analyser reads that as "Overly Restrictive: will deny every request" for each
action/principal pair and rejects the create outright with `CREATE_FAILED`. Every one of the
four guardrail kinds hit this on first deploy. `IGNORE_ALL_FINDINGS` is correct there — schema
checks (valid Cedar/Dogwood, references a real action) still run regardless of validation mode,
only the semantic overly-permissive/-restrictive/ineffective analysis is skipped. Plain `permit`
authorisation statements keep `FAIL_ON_ANY_FINDINGS`, where that same analysis is doing real
work: an always-allow or always-deny authorisation statement there would be a genuine defect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import RemovalPolicy, Stack
from constructs import Construct


#: Gateway attachment mode -> per-policy `EnforcementMode`. The two resources spell the same
#: decision differently: the Gateway says ENFORCE, the policy says ACTIVE (CloudFormation schema).
POLICY_MODE_FOR_GATEWAY_MODE = {"LOG_ONLY": "LOG_ONLY", "ENFORCE": "ACTIVE"}


class PoliciesStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 manifest_path: Path,
                 policy_enforcement: str = "LOG_ONLY",
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if policy_enforcement not in POLICY_MODE_FOR_GATEWAY_MODE:
            raise ValueError(f"policy_enforcement must be one of "
                             f"{tuple(POLICY_MODE_FOR_GATEWAY_MODE)}, got {policy_enforcement!r}")
        self.enforcement_mode = POLICY_MODE_FOR_GATEWAY_MODE[policy_enforcement]

        if not manifest_path.exists():
            raise FileNotFoundError(
                f"{manifest_path} does not exist. Run stage 1 (KycFoundation..KycIngress), "
                f"then `python policies/generate.py --outputs infra/cdk-outputs.json "
                f"--out policies/generated` before synthesising KycPolicies (F-17)."
            )

        manifest: dict[str, list[dict[str, str]]] = json.loads(manifest_path.read_text())
        self.policy_count = 0

        for code, entries in sorted(manifest.items()):
            # Chained sequentially within one jurisdiction's policy engine, not created in
            # parallel. Confirmed live: CloudFormation's default ~12-wide parallel resource
            # creation, all targeting the same policy engine, produced `CreatePolicy ...
            # Policy with the same name already exists` on names that had never been created
            # before in that run — a concurrency/lock-contention fault in the service, not a
            # stale-name issue (ruled out by starting from a verified-empty engine each time).
            # An explicit `DependsOn` chain forces one `CreatePolicy` call at a time per engine;
            # the four engines still create in parallel across each other.
            previous: cdk.CfnResource | None = None
            for entry in entries:
                resource = self._policy(code=code, entry=entry)
                if previous is not None:
                    resource.add_dependency(previous)
                previous = resource
                self.policy_count += 1

        cdk.CfnOutput(self, "PolicyCount", value=str(self.policy_count))

    def _policy(self, *, code: str, entry: dict[str, str]) -> cdk.CfnResource:
        # PolicyEngineId (not the full ARN) is what AWS::BedrockAgentCore::Policy's
        # PolicyEngineId property expects — the last path segment of the ARN
        # ".../policy-engine/<id>".
        engine_id = entry["policyEngineArn"].rsplit("/", 1)[-1]
        # A statement using `when guardrails { ... }` (every kind except plain authorisation)
        # must be submitted under `Definition.Policy.Statement`, never `Definition.Cedar`.
        # Confirmed live: the two fields look identical from the outside — both carry plain
        # Cedar/Dogwood text — but submitting a guardrails statement as `Cedar` fails the
        # *parser* with `unexpected token 'guardrails'`, which reads like a syntax error in the
        # statement rather than the wrong field in the request.
        is_guardrail = entry["kind"] != "authorization"
        definition_key = "Policy" if is_guardrail else "Cedar"
        # `FAIL_ON_ANY_FINDINGS` semantic analysis runs against every action in the gateway
        # schema (policy-create-update-validation.html). A guardrail statement's scope is
        # deliberately unconditioned — `forbid(principal, action, resource == Gateway::"...")` —
        # because the guardrail is meant to gate every tool call on this Gateway, not one. Cedar's
        # own analyzer reads that as "Overly Restrictive: will deny every request" for each
        # action/principal pair and REJECTS THE CREATE — confirmed live, `CREATE_FAILED` with
        # that exact reason on all four guardrail kinds. `IGNORE_ALL_FINDINGS` (schema checks
        # still run; only the semantic overly-permissive/overly-restrictive/ineffective analysis
        # is skipped) is correct here: the "restriction" is the intended behaviour, not a defect
        # to catch. Plain `permit` authorisation statements keep FAIL_ON_ANY_FINDINGS — for those,
        # an always-allow or always-deny finding IS a real defect worth blocking on.
        validation_mode = "IGNORE_ALL_FINDINGS" if is_guardrail else "FAIL_ON_ANY_FINDINGS"
        resource = cdk.CfnResource(
            self, f"Policy{code.upper()}{entry['name']}",
            type="AWS::BedrockAgentCore::Policy",
            properties={
                "PolicyEngineId": engine_id,
                # Policy `Name` is immutable and unique per engine (createOnlyProperties) —
                # `<Kind><Detail>` per statement, matching `policies/cedar.py`'s `Statement.name`
                # so a human reading a CloudWatch LOG_ONLY denial can find the source `.cedar`
                # file by the same name.
                "Name": entry["name"],
                "Description": (
                    f"{entry['kind']} — jurisdiction {code.upper()} (Phase 8, R14/R15). "
                    f"Generated by policies/generate.py; edit the source in policies/cedar.py, "
                    f"not this resource."
                ),
                "Definition": {definition_key: {"Statement": entry["cedar"]}},
                # R14.6: LOG_ONLY until the logged decisions have been checked, then ACTIVE —
                # set together with the Gateway's attachment mode by `-c policyEnforcement`.
                "EnforcementMode": self.enforcement_mode,
                "ValidationMode": validation_mode,
            },
        )
        resource.apply_removal_policy(RemovalPolicy.DESTROY)
        return resource
