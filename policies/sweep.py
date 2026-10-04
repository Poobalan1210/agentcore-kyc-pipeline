#!/usr/bin/env python3
"""Delete every policy on every jurisdiction's policy engine.

Companion to `deploy_stage2.py`, for the one case that script deliberately will not do on its
own: wiping an engine clean. `deploy_stage2.py` is additive and idempotent (it skips statements
already present) precisely so that re-running it is safe; destroying live authorisation policy
is not something a deploy should ever do as a side effect, so it lives here as an explicit,
separately-invoked action.

Waits out `CREATING` before deleting — `DeletePolicy` is rejected with `ConflictException` on a
policy that has not reached a terminal state yet, and a delete loop that ignores that leaves
exactly the half-deleted engines this stage kept ending up with.

Only this system's engines (`kyc90_policy_*`): the account may hold other projects' engines, and
`make destroy` runs this before deleting the stacks, because an engine with policies on it
cannot be deleted.
"""

from __future__ import annotations

import sys

import boto3

REGION = "us-east-1"
ENGINE_PREFIX = "kyc90_policy_"


def _engines(bac) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while True:
        resp = bac.list_policy_engines(**({"nextToken": token} if token else {}))
        ids += [e["policyEngineId"] for e in resp.get("policyEngines", [])
                if str(e.get("name", "")).startswith(ENGINE_PREFIX)]
        token = resp.get("nextToken")
        if not token:
            return ids


def sweep(*, failed_only: bool = False) -> int:
    """`failed_only` deletes only CREATE_FAILED policies: they have no effect, but each holds
    its name, and names are unique per account."""
    bac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    engines = _engines(bac)
    total = 0

    for engine_id in engines:
        policies: dict[str, dict] = {}
        token: str | None = None
        while True:
            kwargs = {"policyEngineId": engine_id}
            if token:
                kwargs["nextToken"] = token
            resp = bac.list_policies(**kwargs)
            for p in resp.get("policies", []):
                policies[p["policyId"]] = p
            token = resp.get("nextToken")
            if not token:
                break

        if failed_only:
            policies = {k: p for k, p in policies.items() if p["status"] == "CREATE_FAILED"}
        if not policies:
            print(f"{engine_id}: {'no failed policies' if failed_only else 'already empty'}")
            continue

        print(f"{engine_id}: deleting {len(policies)} policies")
        for policy_id, p in policies.items():
            if p["status"] == "CREATING":
                print(f"  waiting for {policy_id} to leave CREATING")
                try:
                    bac.get_waiter("policy_active").wait(
                        policyEngineId=engine_id, policyId=policy_id)
                except Exception:
                    pass  # CREATE_FAILED is also terminal and also deletable
            try:
                bac.delete_policy(policyEngineId=engine_id, policyId=policy_id)
                print(f"  deleted {p['name']} ({policy_id})")
                total += 1
            except Exception as exc:
                print(f"  FAILED to delete {policy_id}: {exc}")

    print(f"\ndeleted {total} policies across {len(engines)} engines")
    return 0


if __name__ == "__main__":
    sys.exit(sweep(failed_only="--failed-only" in sys.argv[1:]))
