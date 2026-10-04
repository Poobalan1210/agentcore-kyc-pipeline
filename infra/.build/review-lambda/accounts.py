"""`GET /accounts/{applicantId}`, `GET|POST /accounts/{applicantId}/transactions`.

The account as the platform holds it — the stub in `account-stub/`, which refuses a transaction
its restrictions forbid and says which restriction refused (R31.2). This is where the demo shows a
restriction is real: the applicant tries to move money out of a provisional account and is told
no, by the system that would actually move it.

Who sees what:

* the applicant sees their own account's status, restrictions in plain language, limits and
  balance, and may try transactions on it;
* a reviewer or presenter sees the full platform view, including holds and the platform's own
  event log, and a presenter may try transactions on stage.

The applicant's view leaves out holds and platform events on purpose. Both name the risk event that
caused them — a hold placed by a sanctions list update carries that event's id and type — and
telling a customer why their account is limited can itself be an offence (`notifications.py`).
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

import auth

#: The applicant's view of an account: what they can do, not why.
APPLICANT_FIELDS = ("applicantId", "state", "restrictions", "restrictionDetails", "limits",
                    "balance", "cumulativeDeposits", "openedAt", "updatedAt")

#: Fields of a transaction record an applicant may see.
_TRANSACTION_FIELDS = ("transactionId", "at", "type", "amount", "currency", "allowed",
                       "refusedBy", "reasonCode", "counterparty")

#: Platform error class -> HTTP status. Anything else is the platform failing, not the caller.
_ERROR_STATUS = {
    "NoSuchAccount": 404,
    "BadArgument": 400,
    "AccountFrozen": 409,
    "AccountOnHold": 409,
    "ConcurrentUpdate": 409,
}


def _invoke(operation: str, arguments: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    import boto3

    resp = boto3.client("lambda").invoke(
        FunctionName=os.environ["ACCOUNT_FUNCTION_NAME"],
        Payload=json.dumps({"operation": operation, "arguments": dict(arguments)}).encode())
    payload = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError"):
        error = str(payload.get("errorType") or "Unknown")
        status = _ERROR_STATUS.get(error, 502)
        return status, {"error": error if status != 502 else "ACCOUNT_PLATFORM_UNAVAILABLE"}
    return 200, payload


def _privileged(event: Mapping[str, Any]) -> bool:
    return bool(auth.groups(event) & {auth.REVIEWERS, auth.PRESENTERS})


def applicant_view(account: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(account, Mapping):
        return None
    return {k: account.get(k) for k in APPLICANT_FIELDS if k in account}


def _shape(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """The platform's answer as this caller may see it."""
    out = dict(payload)
    out.pop("status", None)
    out.pop("operation", None)
    if _privileged(event):
        return out
    if "account" in out:
        out["account"] = applicant_view(out["account"])
    if isinstance(out.get("transaction"), Mapping):
        out["transaction"] = {k: out["transaction"].get(k) for k in _TRANSACTION_FIELDS}
    if isinstance(out.get("transactions"), list):
        out["transactions"] = [{k: t.get(k) for k in _TRANSACTION_FIELDS}
                               for t in out["transactions"] if isinstance(t, Mapping)]
    return out


def get_account(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    applicant_id = _applicant(event)
    if not applicant_id or not auth.may_read(event, applicant_id):
        # 404, not 403: the route must not reveal which applicant ids have accounts.
        return _response(404, {"error": "ACCOUNT_NOT_FOUND"})
    status, payload = _invoke("getAccount", {"applicantId": applicant_id})
    if status != 200:
        return _response(status, payload)
    return _response(200, _shape(event, payload))


def list_transactions(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    applicant_id = _applicant(event)
    if not applicant_id or not auth.may_read(event, applicant_id):
        return _response(404, {"error": "ACCOUNT_NOT_FOUND"})
    status, payload = _invoke("listTransactions", {"applicantId": applicant_id, "limit": 20})
    if status != 200:
        return _response(status, payload)
    return _response(200, _shape(event, payload))


def create_transaction(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """Try to move money. A refusal is a 200 with `allowed: false`: it is the answer, not an
    error, and it names the restriction that refused."""
    applicant_id = _applicant(event)
    owner = auth.owns(event, applicant_id)
    if not applicant_id or not (owner or auth.is_member(event, auth.PRESENTERS)):
        return _response(404, {"error": "ACCOUNT_NOT_FOUND"})
    body = _body(event)
    arguments = {
        "applicantId": applicant_id,
        "type": str(body.get("type") or "").upper()[:32],
        "amount": body.get("amount", 0),
    }
    if body.get("counterparty"):
        arguments["counterparty"] = str(body["counterparty"])[:64]
    status, payload = _invoke("transact", arguments)
    if status != 200:
        return _response(status, payload)
    return _response(200, _shape(event, payload))


def _applicant(event: Mapping[str, Any]) -> str:
    return str((event.get("pathParameters") or {}).get("applicantId") or "")


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


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    route = (event.get("requestContext") or {}).get("routeKey", "")
    if route.startswith("GET /accounts/") and route.endswith("/transactions"):
        return list_transactions(event, context)
    if route.startswith("POST /accounts/") and route.endswith("/transactions"):
        return create_transaction(event, context)
    if route.startswith("GET /accounts/"):
        return get_account(event, context)
    return _response(404, {"error": "NOT_FOUND", "route": route})
