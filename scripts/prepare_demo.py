"""Get a deployed environment ready for the talk, and check that it is.

    ./.venv/bin/python scripts/prepare_demo.py check
    ./.venv/bin/python scripts/prepare_demo.py users --user-pool-id us-east-1_X \\
        --presenter presenter@example.com --reviewer reviewer@example.com          # dry run
    ./.venv/bin/python scripts/prepare_demo.py users ... --apply
    ./.venv/bin/python scripts/prepare_demo.py reset --apply

* `check` (read-only) — every stack deployed, demo controls clear, both console groups present,
  and where each demo persona stands: which already has an open account (Act 3 needs DEMO-US-03
  open before the list update) and what is waiting for a reviewer.
* `users` — creates the presenter and reviewer accounts if missing and puts each in its group.
  Group membership is granted only here, by hand, never by sign-up: a presenter can put any open
  account on hold.
* `reset` — clears the fault flags, the published list update and the device and network counts
  the Fraud check reads (D-41), as the Demo Panel's Reset does.

Every command that changes anything prints its plan and does nothing without `--apply`.

**There is no command that seeds a decided case**, on purpose. Phase 1 refuses a case without a
live face check, so the only way to an open account is the real flow — which is the claim. Seed
the demo by running the personas through the applicant app at rehearsal (`docs/RUNBOOK.md`),
then run `check` to confirm they are where the acts need them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "demo-assets"))
# Appended, not prepended: api/ holds modules with plain names (`auth`, `demo`) that must not
# shadow anything else on the path.
sys.path.append(str(REPO / "api"))

from gen_documents import DEMO_IDENTITIES  # noqa: E402
import velocity  # noqa: E402  (api/velocity.py: the same clear the Demo Panel's Reset runs)

#: KycPolicies is not here: stage 2 creates the Cedar policies with boto3
#: (policies/deploy_stage2.py), so `check` reads them off the engines instead.
STACKS = ("KycFoundation", "KycTools", "KycGateways", "KycHarnesses", "KycOrchestration",
          "KycIngress", "KycLifecycle", "KycAuth", "KycApi")
POLICY_ENGINE_PREFIX = "kyc90_policy_"
POLICY_ENGINES = 4
GROUPS = ("reviewers", "presenters")
FAULTS_PARAMETER = "/kyc90/demo/faults"
OVERLAY_PARAMETER = "/kyc90/screening/list-overlay"
CASE_TABLE = "kyc90-cases"
VELOCITY_TABLE = "kyc90-velocity"
CASE_STATES = ("RECEIVED", "IN_PROGRESS", "AWAITING_APPLICANT", "AWAITING_REVIEW", "PROVISIONAL",
               "MONITORING", "RESTRICTED", "FROZEN", "DECIDED")

#: Where each persona must be before the act that uses it.
READY_STATES = {
    "DEMO-US-03": ("MONITORING", "Act 3 re-screens open accounts; hers must be open"),
}


# --------------------------------------------------------------------------- pure planning
def persona_status(cases: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each persona's newest case, matched on the declared name and date of birth."""
    rows = []
    for identity in DEMO_IDENTITIES:
        mine = [c for c in cases
                if c.get("surname") == identity.surname
                and c.get("givenNames") == identity.given_names
                and c.get("dateOfBirth") == identity.dob]
        mine.sort(key=lambda c: c.get("createdAt") or "", reverse=True)
        newest = mine[0] if mine else None
        need = READY_STATES.get(identity.persona_id)
        rows.append({
            "persona": identity.persona_id,
            "name": identity.full_name,
            "cases": len(mine),
            "state": newest.get("state") if newest else None,
            "applicantId": newest.get("applicantId") if newest else None,
            "ready": (newest is not None and newest.get("state") == need[0]) if need else None,
            "needs": need[1] if need else None,
        })
    return rows


def user_plan(existing: Mapping[str, set[str] | None], wanted: Mapping[str, set[str]]
              ) -> list[str]:
    """`existing`: email -> groups the user is in, or None if the user does not exist.
    `wanted`: email -> the groups it must be in. Returns the steps, in order. One person may be
    both presenter and reviewer when a single presenter runs both consoles."""
    steps = []
    for email, groups_wanted in wanted.items():
        groups = existing.get(email)
        if groups is None:
            steps.append(f"create user {email}")
            groups = set()
        for group in sorted(groups_wanted - groups):
            steps.append(f"add {email} to {group}")
    return steps


# ----------------------------------------------------------------------------------- AWS
def _cases(ddb: Any) -> list[dict[str, Any]]:
    out = []
    for state in CASE_STATES:
        kwargs: dict[str, Any] = {
            "TableName": CASE_TABLE, "IndexName": "state-index",
            "KeyConditionExpression": "#s = :s", "ExpressionAttributeNames": {"#s": "state"},
            "ExpressionAttributeValues": {":s": {"S": state}}}
        while True:
            resp = ddb.query(**kwargs)
            for item in resp.get("Items", []):
                declared = (item.get("declaredIdentity") or {}).get("M") or {}
                out.append({
                    "applicantId": item.get("applicantId", {}).get("S"),
                    "state": state, "createdAt": item.get("createdAt", {}).get("S"),
                    "reviewReason": item.get("reviewReason", {}).get("S"),
                    **{k: (declared.get(k) or {}).get("S")
                       for k in ("surname", "givenNames", "dateOfBirth")}})
            if not resp.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return out


def _check_policies(session: Any) -> int:
    """Stage 2: every engine has policies and all of them are ACTIVE. Prints the enforcement
    mode they are in, which must match the Gateways' (`make deploy ENFORCEMENT=...`)."""
    print("\nCedar policies")
    try:
        bac = session.client("bedrock-agentcore-control")
        engines, token = [], None
        while True:
            resp = bac.list_policy_engines(**({"nextToken": token} if token else {}))
            engines += [e for e in resp.get("policyEngines", [])
                        if str(e.get("name", "")).startswith(POLICY_ENGINE_PREFIX)]
            token = resp.get("nextToken")
            if not token:
                break
        problems = 0 if len(engines) == POLICY_ENGINES else 1
        if problems:
            print(f"  BAD {len(engines)} policy engines, expected {POLICY_ENGINES}")
        for engine in sorted(engines, key=lambda e: e["name"]):
            policies, token = [], None
            while True:
                kwargs = {"policyEngineId": engine["policyEngineId"]}
                if token:
                    kwargs["nextToken"] = token
                resp = bac.list_policies(**kwargs)
                policies += resp.get("policies", [])
                token = resp.get("nextToken")
                if not token:
                    break
            active = [p for p in policies if p.get("status") == "ACTIVE"]
            modes = ",".join(sorted({str(p.get("enforcementMode")) for p in active})) or "-"
            ok = bool(active) and len(active) == len(policies)
            problems += 0 if ok else 1
            print(f"  {'ok ' if ok else 'BAD'} {engine['name']:<18} "
                  f"{len(active)}/{len(policies)} active  {modes}")
        return problems
    except Exception as exc:  # noqa: BLE001 - unreadable is the finding
        print(f"  BAD unreadable ({type(exc).__name__}: {exc})")
        return 1


def check(session: Any, *, user_pool_id: str | None) -> int:
    problems = 0
    cfn = session.client("cloudformation")
    print("Stacks")
    for name in STACKS:
        try:
            status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        except Exception:  # noqa: BLE001 - a missing stack is the finding
            status = "NOT DEPLOYED"
        ok = status.endswith("_COMPLETE") and "ROLLBACK" not in status
        problems += 0 if ok else 1
        print(f"  {'ok ' if ok else 'BAD'} {name:<18} {status}")

    problems += _check_policies(session)

    ssm = session.client("ssm")
    print("\nDemo controls")
    for name, clear in ((FAULTS_PARAMETER, lambda v: not v.get("unavailableTools")
                         and not v.get("agentTimeouts")),
                        (OVERLAY_PARAMETER, lambda v: not v.get("entries"))):
        try:
            value = json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"] or "{}")
            is_clear = clear(value)
        except Exception as exc:  # noqa: BLE001
            value, is_clear = f"unreadable ({type(exc).__name__})", False
        problems += 0 if is_clear else 1
        print(f"  {'ok ' if is_clear else 'SET'} {name} = {value}")
    # Information, not a problem: one person applying again counts once, so the run of show's
    # four personas never reach the five-people signal. Other identities applied from the same
    # browser since the last reset do count.
    try:
        entries = velocity.count(session, table=VELOCITY_TABLE)
        print(f"  --- {VELOCITY_TABLE}: {entries} device and network count entries "
              "(reset clears them)")
    except Exception as exc:  # noqa: BLE001 - unreadable is the finding
        print(f"  --- {VELOCITY_TABLE}: unreadable ({type(exc).__name__})")

    if user_pool_id:
        idp = session.client("cognito-idp")
        print("\nConsole groups")
        for group in GROUPS:
            try:
                members = idp.list_users_in_group(UserPoolId=user_pool_id,
                                                  GroupName=group).get("Users", [])
                print(f"  {'ok ' if members else 'BAD'} {group}: {len(members)} member(s)")
                problems += 0 if members else 1
            except Exception as exc:  # noqa: BLE001
                problems += 1
                print(f"  BAD {group}: {type(exc).__name__}")

    cases = _cases(session.client("dynamodb"))
    print("\nPersonas")
    for row in persona_status(cases):
        flag = {True: "ok ", False: "NOT", None: "   "}[row["ready"]]
        print(f"  {flag} {row['persona']} {row['name']:<22} {row['state'] or 'no case':<18}"
              f"{'  ' + row['needs'] if row['ready'] is False else ''}")
        problems += 1 if row["ready"] is False else 0
    waiting = [c for c in cases if c["state"] == "AWAITING_REVIEW"]
    print(f"\nWaiting for a reviewer: {len(waiting)}")
    for c in waiting:
        print(f"  {c['applicantId']}  {c.get('reviewReason') or ''}")
    print(f"\n{'Ready.' if not problems else f'{problems} item(s) need attention.'}")
    return 0 if not problems else 1


def users(session: Any, *, user_pool_id: str, wanted: dict[str, set[str]], apply: bool) -> int:
    idp = session.client("cognito-idp")
    existing: dict[str, set[str] | None] = {}
    for email in wanted:
        try:
            idp.admin_get_user(UserPoolId=user_pool_id, Username=email)
            groups = idp.admin_list_groups_for_user(UserPoolId=user_pool_id, Username=email)
            existing[email] = {g["GroupName"] for g in groups.get("Groups", [])}
        except idp.exceptions.UserNotFoundException:
            existing[email] = None
    steps = user_plan(existing, wanted)
    for step in steps or ["nothing to do"]:
        print(("  " if apply else "  would ") + step)
    if not apply or not steps:
        if steps:
            print("\nDry run. Re-run with --apply to make these changes.")
        return 0
    for email, groups_wanted in wanted.items():
        if existing.get(email) is None:
            # Cognito emails a temporary password; the user sets their own at first sign-in.
            idp.admin_create_user(UserPoolId=user_pool_id, Username=email, UserAttributes=[
                {"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])
        for group in sorted(groups_wanted - (existing.get(email) or set())):
            idp.admin_add_user_to_group(UserPoolId=user_pool_id, Username=email,
                                        GroupName=group)
    return 0


def reset(session: Any, *, apply: bool) -> int:
    plan = [(FAULTS_PARAMETER, {}), (OVERLAY_PARAMETER, {"entries": []})]
    for name, value in plan:
        print(f"  {'' if apply else 'would '}set {name} = {json.dumps(value)}")
    if not apply:
        entries = velocity.count(session, table=VELOCITY_TABLE)
        print(f"  would delete {entries} device and network count entries from {VELOCITY_TABLE}")
        print("\nDry run. Re-run with --apply. Frozen accounts stay frozen either way: nothing "
              "in this build unfreezes one.")
        return 0
    ssm = session.client("ssm")
    for name, value in plan:
        ssm.put_parameter(Name=name, Value=json.dumps(value, sort_keys=True), Type="String",
                          Overwrite=True)
    deleted = velocity.clear(session, table=VELOCITY_TABLE)
    print(f"  deleted {deleted} device and network count entries from {VELOCITY_TABLE}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--user-pool-id")
    u = sub.add_parser("users")
    u.add_argument("--user-pool-id", required=True)
    u.add_argument("--presenter", action="append", default=[])
    u.add_argument("--reviewer", action="append", default=[])
    u.add_argument("--apply", action="store_true")
    r = sub.add_parser("reset")
    r.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    import boto3

    session = boto3.session.Session()
    if args.command == "check":
        return check(session, user_pool_id=args.user_pool_id)
    if args.command == "users":
        wanted: dict[str, set[str]] = {}
        for email in args.presenter:
            wanted.setdefault(email, set()).add("presenters")
        for email in args.reviewer:
            wanted.setdefault(email, set()).add("reviewers")
        if not wanted:
            parser.error("name at least one --presenter or --reviewer")
        return users(session, user_pool_id=args.user_pool_id, wanted=wanted, apply=args.apply)
    return reset(session, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
