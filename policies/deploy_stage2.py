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
the deploy path. `make stage2` runs this script.

**Policy names are unique per account, not per engine.** The API reference says so for
`CreatePolicy`'s `name`, and a fresh account showed it: the first `cdk deploy KycPolicies` there
asked all four engines for the same bare name at the same moment, one call went through and the
other three got `ConflictException: Policy with the same name already exists`. That likely
explains the "unusable name" reports below as well, where `ListPolicies` on one engine showed no
such name: the name was held on another engine. The hash suffix avoids the clash because every
statement names its own jurisdiction's Gateway, so no two jurisdictions share a statement, and
so none share a name.

**Enforcement mode** (`--enforcement`, default `LOG_ONLY`) uses the Gateway's spelling and is
mapped to the policy's (`ENFORCE` -> `ACTIVE`), as `infra/kyc/policies_stack.py` does. New
policies are created in that mode and existing ones are moved to it, so `make deploy
ENFORCEMENT=ENFORCE` switches the Gateway attachment (stage 1) and every policy (here) together.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import boto3

REGION = "us-east-1"
MANIFEST_PATH = Path(__file__).resolve().parent / "generated" / "manifest.json"

#: Gateway attachment mode -> per-policy `enforcementMode`. Mirrors
#: `infra/kyc/policies_stack.py`'s POLICY_MODE_FOR_GATEWAY_MODE: the Gateway says ENFORCE, the
#: policy says ACTIVE.
POLICY_MODE_FOR_GATEWAY_MODE = {"LOG_ONLY": "LOG_ONLY", "ENFORCE": "ACTIVE"}


def _bac():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def _require_policy_definition(bac) -> None:
    """Guardrail statements (`when guardrails { ... }`) must be sent as `definition.policy`.

    They used to go out as a hand-signed request through `urllib`, because the botocore model of
    the day listed only `cedar` and `policyGeneration` under `definition`. The pinned botocore
    (1.43.67) has `policy`, and the hand-signed request failed certificate verification on a
    python.org Python whose own CA store is empty (boto3 brings its own bundle), so every
    statement now goes through the client. This stops a stale botocore with a clear message
    instead of a client-side validation error per statement.
    """
    shape = bac.meta.service_model.operation_model("CreatePolicy").input_shape
    if "policy" not in shape.members["definition"].members:
        raise SystemExit("botocore is too old for guardrail policies (no definition.policy); "
                         "install infra/requirements.txt (`make install`)")


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
    behind; that is the correct consequence of immutable names. `deploy` creates the new one and
    reports the old one rather than deleting live authorisation as a side effect.
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


def _statement_of(policy: dict) -> str | None:
    """The Cedar text a policy holds (stored byte-identical to what was sent), or None when the
    listing does not carry it."""
    definition = policy.get("definition") or {}
    for key in ("cedar", "policy"):
        if isinstance(definition.get(key), dict):
            return definition[key].get("statement")
    return None


def _delete(bac, engine_id: str, policy: dict) -> None:
    bac.delete_policy(policyEngineId=engine_id, policyId=policy["policyId"])
    bac.get_waiter("policy_deleted").wait(policyEngineId=engine_id, policyId=policy["policyId"])


def deploy(enforcement: str = "LOG_ONLY") -> int:
    if enforcement not in POLICY_MODE_FOR_GATEWAY_MODE:
        print(f"error: --enforcement must be one of {tuple(POLICY_MODE_FOR_GATEWAY_MODE)}, "
              f"got {enforcement!r}", file=sys.stderr)
        return 1
    mode = POLICY_MODE_FOR_GATEWAY_MODE[enforcement]
    if not MANIFEST_PATH.exists():
        print(f"error: {MANIFEST_PATH} does not exist — run "
              f"`python policies/generate.py --outputs infra/cdk-outputs.json "
              f"--out policies/generated` first", file=sys.stderr)
        return 1

    manifest: dict[str, list[dict[str, str]]] = json.loads(MANIFEST_PATH.read_text())
    bac = _bac()
    _require_policy_definition(bac)
    created, skipped, updated, failed, older = 0, 0, 0, 0, 0

    for code, entries in sorted(manifest.items()):
        engine_id = entries[0]["policyEngineArn"].rsplit("/", 1)[-1]
        print(f"=== {code.upper()} ({engine_id}) — {len(entries)} statements, "
              f"enforcementMode {mode} ===")

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
            # authorising the same tool twice. Exactly the base name plus the 6-hex suffix, so a
            # base name that happens to be a prefix of another statement's name never matches.
            mine = [p for name, p in existing.items()
                    if name.startswith(base_name) and len(name) == len(base_name) + 6]
            # A failed attempt has no effect, but it holds its name (names are unique per
            # account) and a re-run would trip over it. Remove it before trying again.
            for p in [p for p in mine if p["status"] == "CREATE_FAILED"]:
                _delete(bac, engine_id, p)
                print(f"  {p['name']}: removed an earlier failed attempt")
            live = [p for p in mine if p["status"] in ("ACTIVE", "CREATING", "UPDATING")]
            # Present only with this exact statement. Under the same base name with other text
            # it is an older version: reported, not counted, so an edited statement is created
            # instead of being skipped as "already present".
            already = [p for p in live if _statement_of(p) in (entry["cedar"], None)]
            for p in live:
                if p not in already:
                    print(f"  {p['name']}: an older version of {base_name} is still on the "
                          f"engine; delete it once the new one is ACTIVE")
                    older += 1
            if already:
                present = already[0]
                if present["status"] in ("CREATING", "UPDATING"):
                    bac.get_waiter("policy_active").wait(
                        policyEngineId=engine_id, policyId=present["policyId"])
                if present.get("enforcementMode") == mode:
                    print(f"  {base_name}: already present as {present['name']}, skipping")
                    skipped += 1
                    continue
                # Same statement, other mode: move it rather than create a second policy for
                # the same tool. The Gateway attachment moves in stage 1 with the same switch.
                try:
                    bac.update_policy(policyEngineId=engine_id, policyId=present["policyId"],
                                      enforcementMode=mode)
                    bac.get_waiter("policy_active").wait(
                        policyEngineId=engine_id, policyId=present["policyId"])
                    print(f"  {present['name']}: enforcementMode "
                          f"{present.get('enforcementMode')} -> {mode}")
                    updated += 1
                except Exception as exc:  # noqa: BLE001 - reported and counted, then exit 1
                    print(f"  {present['name']}: MODE CHANGE FAILED — {exc}")
                    failed += 1
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
                    resp = bac.create_policy(
                        policyEngineId=engine_id, name=name,
                        definition={definition_key: {"statement": entry["cedar"]}},
                        description=description, validationMode=validation_mode,
                        enforcementMode=mode,
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
                if got["status"] == "CREATE_FAILED":
                    # It has no effect and would hold the name: leave the engine as it was.
                    _delete(bac, engine_id, got)

        # Policies of a statement the manifest no longer has (a removed kind or tool). Reported,
        # not deleted, for the same reason as an older version above.
        bases = [e["name"] for e in entries]
        for name in sorted(_existing_by_name(bac, engine_id)):
            if not any(name.startswith(b) and len(name) == len(b) + 6 for b in bases):
                print(f"  {name}: on the engine but not in the manifest")
                older += 1

    print(f"\ncreated={created} skipped={skipped} updated={updated} failed={failed} "
          f"older-versions-left={older}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the Cedar policies in the manifest.")
    parser.add_argument("--enforcement", default="LOG_ONLY",
                        choices=tuple(POLICY_MODE_FOR_GATEWAY_MODE),
                        help="the Gateway's mode; every policy is put in the matching mode")
    return deploy(parser.parse_args(argv).enforcement)


if __name__ == "__main__":
    sys.exit(main())
