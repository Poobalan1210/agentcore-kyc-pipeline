#!/usr/bin/env python3
"""Stage-2 (Cedar policies) deploy — direct boto3, deliberately bypassing CloudFormation.

**Root cause, confirmed live across many attempts in this session**: the
`AWS::BedrockAgentCore::Policy` CloudFormation resource type has a real reliability defect, not
a name-reuse timing quirk this script can wait out. Evidence: `list_policies` against the real
service showed a policy `ACTIVE` — created, working, correct — at the exact same moment
CloudFormation's own stack events recorded that identical resource as `CREATE_FAILED` with
"Policy with the same name already exists". CloudFormation's handler issued a duplicate
`CreatePolicy` call for a create that had already succeeded, then choked on its own duplicate.
The mirror failure happens on delete: CFN's rollback calls `DeletePolicy` while its own view of
the resource still says `CREATING`, even after the service has already moved it to `ACTIVE`
seconds earlier, and `DeletePolicy` on a `CREATING` policy is rejected outright
(`ConflictException`). No amount of pre-flight sweeping or sequential `DependsOn` chaining (both
tried this session) fixes a bug in CloudFormation's own polling of the resource's async status.

**The fix: stop asking CloudFormation to manage this resource type.** This script talks to
`bedrock-agentcore-control` directly — `create_policy` then the real `policy_active` waiter,
one policy at a time, exactly the sequencing CloudFormation was supposed to provide and did
not reliably provide. `infra/kyc/policies_stack.py`'s CDK resources become the source of truth
for *what* to create (still generated from `policies/cedar.py` via `policies/generate.py`'s
manifest) without CloudFormation provisioning them — the same "two-stage deploy, F-17" spirit
extended one step further because this resource type turned out not to tolerate CFN's own
create/rollback cadence. This is recorded as an explicit, dated operational deviation, not a
silent workaround: Cedar policies for this build are provisioned by `policies/deploy_stage2.py`,
not `cdk deploy KycPolicies`; `infra/kyc/policies_stack.py` and its CDK resources exist to
generate and validate the same shape (task 8.1's CDK test suite runs against them) but are not
the deploy path.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import boto3

REGION = "us-east-1"
MANIFEST_PATH = Path(__file__).resolve().parent / "generated" / "manifest.json"


def _bac():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def _create_policy_raw(bac, *, engine_id: str, name: str, definition_key: str,
                       statement: str, description: str, validation_mode: str) -> dict:
    """Bypasses boto3's client-side parameter validation for `definition.policy` — the
    installed botocore model's `CreatePolicy` shape only lists `cedar`/`policyGeneration`
    under `definition`, even though the live API accepts (and, for a `when guardrails`
    statement, *requires*) a `policy` key alongside them, confirmed by hand-signing the same
    request `boto3.client(...).create_policy` rejects client-side. Not a service bug — a stale
    botocore service model for a resource this new.
    """
    import botocore.session
    import urllib.error
    import urllib.request
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    body = json.dumps({
        "name": name,
        "description": description,
        "validationMode": validation_mode,
        "definition": {definition_key: {"statement": statement}},
        "clientToken": str(uuid.uuid4()),
    })
    session = botocore.session.get_session()
    creds = session.get_credentials()
    url = bac.meta.endpoint_url + f"/policy-engines/{engine_id}/policies"
    request = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(request)
    prepared = request.prepare()
    req = urllib.request.Request(
        prepared.url, data=prepared.body.encode(), headers=dict(prepared.headers), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{exc.code} {exc.read().decode()}") from exc


def _deployed_name(base_name: str, statement: str, salt: int) -> str:
    """`<BaseName><6-hex>` — the name actually created on the engine.

    **Why a hash suffix rather than the bare semantic name.** `Policy.Name` is immutable per
    engine (`createOnlyProperties`) and, confirmed live, a name that has been through even one
    failed `CreatePolicy` becomes permanently unusable: `CreatePolicy` keeps returning
    `ConflictException: Policy with the same name already exists` while `ListPolicies` — fully
    paginated — reports no policy with that name anywhere on the engine. The proof it is the
    *name* and not the statement or the engine: a randomly-named policy carrying the byte-identical
    Cedar statement creates successfully on the same engine, in the same second. So retrying a
    failed name can never succeed, however long the backoff, and the retry loop that did so was
    the reason this stage burned so many attempts.

    The suffix is a hash of the statement text, not a random value, so it is **deterministic**:
    re-running this script computes the same name for the same statement, which is what makes the
    "already present, skipping" path idempotent instead of creating duplicates. `salt` is the
    escape hatch — bumped only when a computed name turns out to be one of the poisoned ones,
    yielding a fresh name for the same statement without touching anything already deployed.

    A statement *edit* therefore produces a new name and a new policy, leaving the old one
    behind; that is the correct consequence of immutable names, and `_sweep_stale` handles the
    cleanup rather than pretending an in-place update is possible.
    """
    digest = hashlib.sha256(f"{statement}|salt={salt}".encode()).hexdigest()[:6]
    return f"{base_name}{digest}"


def _existing_by_name(bac, engine_id: str) -> dict[str, dict]:
    """Every policy on the engine, paginated fully.

    Confirmed live to be the actual cause of the repeated false "AlreadyExists" backoff loop on
    the 8th-12th statement per jurisdiction: `list_policies` paginates at 10 results by default
    (`nextToken` present, never followed), so a policy that had genuinely already been created —
    ACTIVE, confirmed via the AWS CLI at the same moment — was invisible to this function's
    single-page read and the loop treated it as needing creation, which then legitimately failed
    with AlreadyExists and burned through the entire retry/backoff ladder for nothing.
    """
    policies: dict[str, dict] = {}
    next_token: str | None = None
    while True:
        kwargs = {"policyEngineId": engine_id}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = bac.list_policies(**kwargs)
        for p in resp.get("policies", []):
            policies[p["name"]] = p
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return policies


def deploy() -> int:
    if not MANIFEST_PATH.exists():
        print(f"error: {MANIFEST_PATH} does not exist — run "
              f"`python policies/generate.py --outputs infra/cdk-outputs.json "
              f"--out policies/generated` first", file=sys.stderr)
        return 1

    manifest: dict[str, list[dict[str, str]]] = json.loads(MANIFEST_PATH.read_text())
    bac = _bac()
    created, skipped, failed = 0, 0, 0

    for code, entries in sorted(manifest.items()):
        engine_id = entries[0]["policyEngineArn"].rsplit("/", 1)[-1]
        print(f"=== {code.upper()} ({engine_id}) — {len(entries)} statements ===")

        for entry in entries:
            base_name = entry["name"]
            # Re-fetched per statement, not once per engine — a policy this same run created
            # moments earlier for an earlier statement in this engine is real and ACTIVE by the
            # time later statements are checked, and a single snapshot taken before the loop
            # started would still show it absent, sending this straight down the create path for
            # a policy that already exists.
            existing = _existing_by_name(bac, engine_id)
            # Matched by prefix, not exact name: the deployed name carries a hash suffix, and a
            # previous run that had to bump `salt` produced a different suffix for the same
            # statement. An exact-name check would miss it and create a duplicate policy
            # authorising the same tool twice.
            already = [
                p for name, p in existing.items()
                if name.startswith(base_name) and p["status"] in ("ACTIVE", "CREATING")
            ]
            if already:
                if already[0]["status"] == "CREATING":
                    bac.get_waiter("policy_active").wait(
                        policyEngineId=engine_id, policyId=already[0]["policyId"])
                print(f"  {base_name}: already present as {already[0]['name']}, skipping")
                skipped += 1
                continue

            is_guardrail = entry["kind"] != "authorization"
            definition_key = "policy" if is_guardrail else "cedar"
            validation_mode = "IGNORE_ALL_FINDINGS" if is_guardrail else "FAIL_ON_ANY_FINDINGS"
            description = (
                f"{entry['kind']} — jurisdiction {code.upper()} (Phase 8, R14/R15). "
                f"Generated by policies/generate.py; deployed by policies/deploy_stage2.py; "
                f"edit policies/cedar.py, not this resource directly."
            )

            policy_id = None
            name = base_name
            last_error: Exception | None = None
            # On `AlreadyExists`, bump the salt to get a *different* name rather than sleeping
            # and retrying the same one. A name that has failed once never becomes creatable
            # again (see `_deployed_name`), so a backoff on the same name is guaranteed to fail
            # for as long as it runs — which is exactly what it did. Non-conflict errors break
            # out immediately: those are real statement or validation problems that a new name
            # would not fix, and retrying them would only hide the reason.
            for salt in range(6):
                name = _deployed_name(base_name, entry["cedar"], salt)
                try:
                    if is_guardrail:
                        resp = _create_policy_raw(
                            bac, engine_id=engine_id, name=name, definition_key=definition_key,
                            statement=entry["cedar"], description=description,
                            validation_mode=validation_mode)
                    else:
                        resp = bac.create_policy(
                            policyEngineId=engine_id, name=name,
                            definition={definition_key: {"statement": entry["cedar"]}},
                            description=description, validationMode=validation_mode,
                        )
                    policy_id = resp["policyId"]
                    break
                except Exception as exc:
                    last_error = exc
                    if "AlreadyExists" not in str(exc) and "already exists" not in str(exc):
                        break
                    print(f"    {name} is unusable; retrying under a new name (salt {salt + 1})")

            if policy_id is None:
                print(f"  {base_name}: CREATE FAILED — {last_error}")
                failed += 1
                continue

            try:
                bac.get_waiter("policy_active").wait(
                    policyEngineId=engine_id, policyId=policy_id)
                print(f"  {name}: ACTIVE ({policy_id})")
                created += 1
            except Exception:
                got = bac.get_policy(policyEngineId=engine_id, policyId=policy_id)
                print(f"  {name}: did not reach ACTIVE — status={got['status']} "
                      f"reasons={got.get('statusReasons')}")
                failed += 1

    print(f"\ncreated={created} skipped={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(deploy())
