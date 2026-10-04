"""Presenter controls for the live demo — `/demo/*`, `presenters` group only.

Three things a presenter does on stage, each through the same runtime controls production code
reads, so the demo exercises the real paths rather than a demo-only branch:

* **Faults** — `PUT /demo/faults` writes `/kyc90/demo/faults`: tools reported unavailable, agents
  given less time. Both can only send a case to a person; neither can approve one.
* **A sanctions list update** — `POST /demo/list-update` adds listings to
  `/kyc90/screening/list-overlay`, then publishes one `SanctionsListUpdate` event per open account
  to the lifecycle bus. Phase 3 re-screens each account against the updated list. This is Act 3.
* **Reset** — `POST /demo/reset` clears both. It does not unfreeze anything: a freeze is only ever
  undone by a person, and resetting a demo is not a person deciding.

And one read: `GET /demo/cases`, the newest cases with the time each took to decide, measured from
the case record's own timestamps.

These routes are a demo capability and would not exist in a bank: a presenter can publish a
listing that freezes any open account. They are deployed only when the stack is synthesised with
`-c demoControls=true` (the default for this repository), and the group's membership is granted by
hand, never by sign-up.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping

import auth

#: The six evidence tools a fault may mark unavailable (`tools/tool_schemas.py`).
TOOLS = frozenset({"s3_get_document", "rekognition_compare_faces", "velocity_check",
                   "sanctions_screen", "pep_lookup", "adverse_media"})

#: The four agents whose time a fault may shorten — the Phase 1 evidence agents. The state
#: machine applies `min(fault, default)`, so a fault can never lengthen one.
AGENTS = frozenset({"documents", "identity", "fraud", "prescreen"})
MAX_AGENT_TIMEOUT = 90

#: Ids a list update may use. The 09xx range is reserved for updates, so an update can never
#: shadow a base-list designation — the screening tool treats a clash as an unreadable list.
_ENTRY_ID = re.compile(r"^SYN-09\d{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Account states that have an account to re-screen. Phase 3 re-checks this itself.
OPEN_STATES = ("PROVISIONAL", "MONITORING", "RESTRICTED", "AWAITING_REVIEW")

#: Most events one publish sends. A deeper book is not a demo.
MAX_FANOUT = 500


class Invalid(ValueError):
    pass


def _ssm():
    import boto3

    return boto3.client("ssm")


def _read(name: str, default: Any) -> Any:
    """For display: an unreadable control is shown as its default."""
    try:
        return _read_strict(name)
    except Exception:  # noqa: BLE001 - display only; the write paths read strictly
        return default


def _read_strict(name: str) -> Any:
    raw = _ssm().get_parameter(Name=name)["Parameter"]["Value"]
    return json.loads(raw)


def _write(name: str, value: Mapping[str, Any]) -> None:
    _ssm().put_parameter(Name=name, Value=json.dumps(value, sort_keys=True), Type="String",
                         Overwrite=True)


def _faults_name() -> str:
    return os.environ["DEMO_FAULTS_PARAMETER"]


def _overlay_name() -> str:
    return os.environ["LIST_OVERLAY_PARAMETER"]


def get_controls(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    return _response(200, {"faults": _read(_faults_name(), {}),
                           "listOverlay": _read(_overlay_name(), {"entries": []})})


def validate_faults(body: Mapping[str, Any]) -> dict[str, Any]:
    tools = body.get("unavailableTools") or []
    timeouts = body.get("agentTimeouts") or {}
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise Invalid("unavailableTools must be a list of tool names")
    unknown = sorted(set(tools) - TOOLS)
    if unknown:
        raise Invalid(f"unknown tools: {', '.join(unknown)}")
    if not isinstance(timeouts, Mapping):
        raise Invalid("agentTimeouts must be an object of agent -> seconds")
    clean: dict[str, int] = {}
    for agent, seconds in timeouts.items():
        if agent not in AGENTS:
            raise Invalid(f"unknown agent {agent!r}; one of {', '.join(sorted(AGENTS))}")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise Invalid(f"agentTimeouts.{agent} must be a number of seconds")
        if not 1 <= seconds <= MAX_AGENT_TIMEOUT:
            raise Invalid(f"agentTimeouts.{agent} must be between 1 and {MAX_AGENT_TIMEOUT}")
        clean[agent] = int(seconds)
    return {"unavailableTools": sorted(set(tools)), "agentTimeouts": clean}


def put_faults(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    try:
        faults = validate_faults(_body(event))
    except Invalid as exc:
        return _response(400, {"error": "INVALID_FAULTS", "detail": str(exc)})
    _write(_faults_name(), faults)
    return _response(200, {"faults": faults})


def validate_entries(entries: Any) -> list[dict[str, Any]]:
    """The same rules the screening tool applies to the overlay (`kyctools.controls`), plus the
    reserved id range, so a published update can never make screening unavailable."""
    if not isinstance(entries, list) or not entries:
        raise Invalid("entries must be a non-empty list")
    clean, seen = [], set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise Invalid(f"entry {i} is not an object")
        entry_id = str(entry.get("entryId") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not _ENTRY_ID.match(entry_id):
            raise Invalid(f"entry {i}: entryId must be in the update range SYN-0900..SYN-0999")
        if entry_id in seen:
            raise Invalid(f"entry {i}: duplicate entryId {entry_id}")
        seen.add(entry_id)
        if not name or len(name) > 128:
            raise Invalid(f"entry {i}: name is required and at most 128 characters")
        item: dict[str, Any] = {"entryId": entry_id, "name": name,
                                "programme": str(entry.get("programme") or "SYN-UPDATE")[:32]}
        dob = entry.get("dob")
        if dob:
            if not _DATE.match(str(dob)):
                raise Invalid(f"entry {i}: dob must be YYYY-MM-DD")
            item["dob"] = str(dob)
        country = str(entry.get("country") or "").upper()
        if country:
            if not _COUNTRY.match(country):
                raise Invalid(f"entry {i}: country must be a two-letter code")
            item["country"] = country
        aliases = entry.get("aliases") or []
        if not isinstance(aliases, list) or len(aliases) > 5:
            raise Invalid(f"entry {i}: aliases must be a list of at most five names")
        if aliases:
            item["aliases"] = [str(a).strip()[:128] for a in aliases if str(a).strip()]
        clean.append(item)
    return clean


def publish_list_update(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    body = _body(event)
    try:
        entries = validate_entries(body.get("entries"))
    except Invalid as exc:
        return _response(400, {"error": "INVALID_LIST_UPDATE", "detail": str(exc)})
    now = datetime.now(timezone.utc)
    version = str(body.get("version") or f"LIST-{now.strftime('%Y%m%dT%H%M%SZ')}")
    if not _VERSION.match(version):
        return _response(400, {"error": "INVALID_LIST_UPDATE",
                               "detail": "version must be letters, digits, '.', '_' or '-'"})

    # Read strictly. An update adds to what is already published; reading "nothing" because
    # Parameter Store blinked would write the new listing alone and silently delist the others.
    try:
        current = _read_strict(_overlay_name())
    except Exception:  # noqa: BLE001 - nothing is written; the presenter retries
        return _response(503, {"error": "LIST_OVERLAY_UNREADABLE",
                               "detail": "the published list could not be read, so it was not "
                                         "changed"})
    if not isinstance(current, Mapping):
        current = {}
    merged = {e["entryId"]: e for e in current.get("entries") or ()
              if isinstance(e, Mapping) and e.get("entryId")}
    merged.update({e["entryId"]: e for e in entries})
    overlay = {"version": version, "entries": [merged[k] for k in sorted(merged)]}
    _write(_overlay_name(), overlay)

    # The screening tool caches the overlay for a few seconds. Publishing the events before that
    # cache expires would re-screen against the list from just before the update; the rules would
    # catch it (a screening of an outdated list is a SYSTEM_ERROR, not "unchanged"), but on stage
    # that is a review instead of a freeze.
    time.sleep(float(os.environ.get("LIST_UPDATE_SETTLE_SECONDS", "4")))

    customers = _open_accounts()
    sent, failed = _publish(customers, version=version, entry_ids=sorted(merged), now=now)
    return _response(200, {"version": version, "entries": overlay["entries"],
                           "accounts": len(customers), "eventsSent": sent,
                           "eventsFailed": failed})


def _open_accounts() -> list[str]:
    import boto3

    ddb = boto3.client("dynamodb")
    found: list[str] = []
    for state in OPEN_STATES:
        kwargs: dict[str, Any] = {
            "TableName": os.environ["CASE_TABLE_NAME"],
            "IndexName": "state-index",
            "KeyConditionExpression": "#s = :state",
            "ExpressionAttributeNames": {"#s": "state"},
            "ExpressionAttributeValues": {":state": {"S": state}},
        }
        while len(found) < MAX_FANOUT:
            resp = ddb.query(**kwargs)
            for item in resp.get("Items", []):
                applicant = (item.get("applicantId") or {}).get("S")
                if applicant and "provisionalDecidedAt" in item and applicant not in found:
                    found.append(applicant)
            if not resp.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return found[:MAX_FANOUT]


def _publish(customers: list[str], *, version: str, entry_ids: list[str],
             now: datetime) -> tuple[int, int]:
    """One event per open account. HIGH, never CRITICAL: a CRITICAL event freezes regardless of
    what the screening finds, and the point of the act is that the screening decides."""
    import boto3

    events = boto3.client("events")
    occurred_at = now.isoformat().replace("+00:00", "Z")
    sent = failed = 0
    for start in range(0, len(customers), 10):
        batch = [{
            "EventBusName": os.environ["LIFECYCLE_BUS_NAME"],
            "Source": "kyc90.screening",
            "DetailType": "SanctionsListUpdate",
            "Detail": json.dumps({
                "eventId": f"LIST-UPDATE-{version}",
                "eventType": "SANCTIONS_LIST_UPDATE",
                "severity": "HIGH",
                "customerId": customer,
                "occurredAt": occurred_at,
                "detail": {"listUpdate": version, "entryIds": entry_ids},
            }),
        } for customer in customers[start:start + 10]]
        resp = events.put_events(Entries=batch)
        failed += int(resp.get("FailedEntryCount", 0))
        sent += len(batch) - int(resp.get("FailedEntryCount", 0))
    return sent, failed


#: Every state a case can be in, for the presenter's board.
BOARD_STATES = ("RECEIVED", "IN_PROGRESS", "AWAITING_APPLICANT", "AWAITING_REVIEW", "PROVISIONAL",
                "MONITORING", "RESTRICTED", "FROZEN", "DECIDED")
BOARD_LIMIT = 25


def list_cases(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /demo/cases` — the newest cases, for the presenter's board.

    The time to a decision is computed from two timestamps the case record already carries: when
    the attempt was submitted (`createdAt`, or `resubmittedAt` after a step-up) and when the state
    machine wrote the provisional decision (`provisionalDecidedAt`, in the same update that opens
    the case). So the "90 seconds" on stage is measured by the system, not by a stopwatch.

    Names are shown because every demo persona is synthetic and this route exists only in demo
    deployments, for the presenters group.
    """
    import boto3

    ddb = boto3.client("dynamodb")
    items: list[Mapping[str, Any]] = []
    for state in BOARD_STATES:
        resp = ddb.query(
            TableName=os.environ["CASE_TABLE_NAME"],
            IndexName="state-index",
            KeyConditionExpression="#s = :state",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":state": {"S": state}},
            # Newest first on createdAt: the board only ever needs the top of each state.
            ScanIndexForward=False,
            Limit=BOARD_LIMIT,
        )
        items.extend(resp.get("Items", []))
    items.sort(key=lambda i: _s(i.get("createdAt")) or "", reverse=True)
    return _response(200, {"cases": [_board_row(i) for i in items[:BOARD_LIMIT]]})


def _board_row(item: Mapping[str, Any]) -> dict[str, Any]:
    declared = (item.get("declaredIdentity") or {}).get("M") or {}
    name = " ".join(p for p in (_s(declared.get("givenNames")), _s(declared.get("surname"))) if p)
    submitted = _s(item.get("resubmittedAt")) or _s(item.get("createdAt"))
    provisional = _s(item.get("provisionalDecidedAt"))
    row: dict[str, Any] = {
        "applicantId": _s(item.get("applicantId")),
        "caseId": _s(item.get("caseId")),
        "name": name or None,
        "state": _s(item.get("state")),
        "riskState": _s(item.get("riskState")),
        "submittedAt": submitted,
        "provisionalDecidedAt": provisional,
        "fullDecidedAt": _s(item.get("fullDecidedAt")),
        # A resubmission after the provisional decision cannot happen (only AWAITING_APPLICANT
        # resubmits), so a provisional stamp always belongs to the attempt that was submitted.
        "secondsToProvisional": _seconds_between(submitted, provisional),
    }
    if row["state"] == "AWAITING_REVIEW":
        row["reviewPhase"] = _s(item.get("reviewPhase"))
        row["reviewReason"] = _s(item.get("reviewReason"))
    return row


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        began = datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = (ended - began).total_seconds()
    return round(seconds, 1) if seconds >= 0 else None


def _s(attr: Any) -> str | None:
    return attr.get("S") if isinstance(attr, Mapping) else None


def reset(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    _write(_faults_name(), {})
    _write(_overlay_name(), {"entries": []})
    return _response(200, {"faults": {}, "listOverlay": {"entries": []},
                           "note": "frozen accounts stay frozen; only a reviewer changes that"})


def _body(event: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = event.get("body")
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return raw if isinstance(raw, Mapping) else {}


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


_ROUTES = {
    "GET /demo/cases": list_cases,
    "GET /demo/controls": get_controls,
    "PUT /demo/faults": put_faults,
    "POST /demo/list-update": publish_list_update,
    "POST /demo/reset": reset,
}


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    if not auth.is_member(event, auth.PRESENTERS):
        return _response(403, {"error": "PRESENTERS_ONLY"})
    route = (event.get("requestContext") or {}).get("routeKey", "")
    handler = _ROUTES.get(route)
    if handler is None:
        return _response(404, {"error": "NOT_FOUND", "route": route})
    return handler(event, context)
