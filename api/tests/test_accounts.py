"""Account routes — `api/accounts.py`. The account as the platform holds it, shown to whoever may
see it, in the detail they may see it in.

The fake Lambda runs the real account stub in memory, so a refusal here is the stub's own refusal
and not a canned payload: what the applicant is told on stage is what these tests pin.
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

import account_stub
import accounts

SUB = "8f14e45f-ceea-467f-a0e6-1f2b3c4d5e6f"
APPLICANT = f"APP-{SUB}-run-1"


class StubLambda:
    """Invokes `account_stub.handler` the way Lambda does: an exception becomes a FunctionError
    with the exception's class name as `errorType`."""

    def __init__(self, platform):
        self.platform = platform
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs):
        payload = json.loads(kwargs["Payload"])
        self.calls.append(payload)
        try:
            result = account_stub.handler(payload)
        except Exception as exc:  # noqa: BLE001 - mirrors the Lambda runtime
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(json.dumps(
                {"errorType": type(exc).__name__, "errorMessage": str(exc)}).encode())}
        return {"Payload": io.BytesIO(json.dumps(result, default=str).encode())}


class FakeBoto3:
    def __init__(self, lam):
        self._lambda = lam

    def client(self, name, **_):
        assert name == "lambda"
        return self._lambda


@pytest.fixture
def platform(monkeypatch):
    plat = account_stub.AccountPlatform(account_stub.MemoryStore())
    monkeypatch.setattr(account_stub, "platform", lambda: plat)
    lam = StubLambda(plat)
    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3(lam))
    monkeypatch.setenv("ACCOUNT_FUNCTION_NAME", "kyc90-account")
    plat.open_provisional(APPLICANT, decision_id="DEC-1")
    return plat


def event(groups: Any = "[applicants]", sub: str = SUB, applicant: str = APPLICANT,
          body: Any = None, route: str = "GET /accounts/{applicantId}") -> dict[str, Any]:
    ev: dict[str, Any] = {
        "requestContext": {"routeKey": route, "authorizer": {"jwt": {"claims": {
            "sub": sub, "cognito:groups": groups}}}},
        "pathParameters": {"applicantId": applicant},
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def call(fn, ev):
    resp = fn(ev)
    return resp["statusCode"], json.loads(resp["body"])


class TestWhoMaySee:
    def test_the_applicant_sees_their_own_account(self, platform):
        status, body = call(accounts.get_account, event())
        assert status == 200
        assert body["account"]["applicantId"] == APPLICANT
        assert body["account"]["state"] == "OPEN_RESTRICTED"

    @pytest.mark.parametrize("sub, groups", [
        ("someone-else", "[applicants]"),
        ("someone-else", "[notreviewers]"),
        ("", "[applicants]"),
    ])
    def test_anyone_else_gets_a_404_not_a_403(self, platform, sub, groups):
        """A 403 would confirm the account exists."""
        status, body = call(accounts.get_account, event(groups=groups, sub=sub))
        assert (status, body) == (404, {"error": "ACCOUNT_NOT_FOUND"})

    def test_a_refusal_is_decided_before_the_platform_is_asked(self, platform):
        lam = sys.modules["boto3"]._lambda
        call(accounts.get_account, event(sub="someone-else"))
        call(accounts.list_transactions, event(sub="someone-else"))
        call(accounts.create_transaction, event(sub="someone-else", body={"type": "DEPOSIT"}))
        assert lam.calls == []

    @pytest.mark.parametrize("groups", ["[reviewers]", "[presenters]"])
    def test_reviewers_and_presenters_see_any_account(self, platform, groups):
        status, _ = call(accounts.get_account, event(groups=groups, sub="staff-1"))
        assert status == 200

    def test_no_account_is_a_404(self, platform):
        status, body = call(accounts.get_account, event(applicant=f"APP-{SUB}-run-2"))
        assert (status, body) == (404, {"error": "NoSuchAccount"})


class TestWhatTheApplicantSees:
    """What they can do, never why. A hold carries the id and type of the event that placed it —
    `LIST-UPDATE-...` says "a sanctions list changed" to anyone who reads it."""

    @pytest.fixture
    def held(self, platform):
        platform.restrict(APPLICANT, "TIGHTENED", outcome="RISK_INCREASED_REVIEW",
                          reason="SANCTIONS_LIST_UPDATE", hold="LIST-UPDATE-L1")
        return platform

    def test_holds_and_platform_events_are_left_out(self, held):
        raw = accounts.get_account(event())["body"]
        account = json.loads(raw)["account"]
        assert set(account) <= set(accounts.APPLICANT_FIELDS)
        assert "holds" not in account and "recentEvents" not in account
        assert "decisionId" not in account and "posture" not in account
        assert "LIST-UPDATE" not in raw
        assert "SANCTIONS" not in raw.upper()

    def test_the_restrictions_are_shown_in_plain_language(self, held):
        account = call(accounts.get_account, event())[1]["account"]
        assert "DEPOSITS_BLOCKED" in account["restrictions"]
        assert any(d["description"] == "New deposits are paused for now."
                   for d in account["restrictionDetails"])

    def test_a_reviewer_sees_the_hold_and_who_placed_it(self, held):
        account = call(accounts.get_account, event(groups="[reviewers]", sub="r-1"))[1]["account"]
        assert account["holds"] == [{"holdId": "LIST-UPDATE-L1", "posture": "TIGHTENED"}]
        assert account["posture"] == "TIGHTENED"

    def test_the_transaction_history_is_trimmed_the_same_way(self, held):
        call(accounts.create_transaction,
             event(route="POST /accounts/{applicantId}/transactions",
                   body={"type": "DEPOSIT", "amount": 50}))
        status, body = call(accounts.list_transactions,
                            event(route="GET /accounts/{applicantId}/transactions"))
        assert status == 200
        assert set(body["account"]) <= set(accounts.APPLICANT_FIELDS)
        assert all(set(t) == set(accounts._TRANSACTION_FIELDS) for t in body["transactions"])


class TestTryingToMoveMoney:
    def test_a_refusal_is_the_answer_not_an_error_and_names_the_restriction(self, platform):
        """The demo beat: money out of a provisional account, refused by the platform itself."""
        status, body = call(accounts.create_transaction, event(
            route="POST /accounts/{applicantId}/transactions",
            body={"type": "international_transfer", "amount": 200, "counterparty": "GB-ACME"}))
        assert status == 200
        assert body["allowed"] is False
        assert body["refusedBy"] == "INTERNATIONAL_TRANSFERS_BLOCKED"
        assert body["transaction"]["allowed"] is False
        assert body["transaction"]["counterparty"] == "GB-ACME"

    def test_an_allowed_deposit_moves_the_balance(self, platform):
        status, body = call(accounts.create_transaction, event(
            route="POST /accounts/{applicantId}/transactions",
            body={"type": "DEPOSIT", "amount": 100}))
        assert status == 200 and body["allowed"] is True
        assert float(body["account"]["balance"]) == 100.0

    def test_a_reviewer_may_look_but_not_transact(self, platform):
        status, _ = call(accounts.create_transaction, event(
            groups="[reviewers]", sub="r-1", route="POST /accounts/{applicantId}/transactions",
            body={"type": "DEPOSIT", "amount": 1}))
        assert status == 404

    def test_a_presenter_may_transact_on_stage(self, platform):
        status, body = call(accounts.create_transaction, event(
            groups="[presenters]", sub="p-1", route="POST /accounts/{applicantId}/transactions",
            body={"type": "CASH_WITHDRAWAL", "amount": 20}))
        assert status == 200 and body["allowed"] is False

    def test_a_frozen_account_refuses_everything(self, platform):
        platform.freeze(APPLICANT, outcome="FREEZE", reason="test")
        status, body = call(accounts.create_transaction, event(
            route="POST /accounts/{applicantId}/transactions",
            body={"type": "DEPOSIT", "amount": 1}))
        assert status == 200
        assert body["allowed"] is False and body["reasonCode"] == "ACCOUNT_FROZEN"

    @pytest.mark.parametrize("body", [{"type": "TELEPORT", "amount": 1},
                                      {"type": "DEPOSIT", "amount": -5},
                                      {"type": "DEPOSIT", "amount": "lots"}])
    def test_a_malformed_attempt_is_a_400(self, platform, body):
        status, payload = call(accounts.create_transaction, event(
            route="POST /accounts/{applicantId}/transactions", body=body))
        assert (status, payload) == (400, {"error": "BadArgument"})

    def test_an_unparseable_body_is_an_empty_attempt(self, platform):
        ev = event(route="POST /accounts/{applicantId}/transactions")
        ev["body"] = "{not json"
        status, _ = call(accounts.create_transaction, ev)
        assert status == 400


class TestPlatformFailures:
    def test_an_unexpected_platform_error_is_a_502_without_its_detail(self, monkeypatch):
        class Broken:
            def invoke(self, **_):
                return {"FunctionError": "Unhandled", "Payload": io.BytesIO(json.dumps(
                    {"errorType": "KeyError", "errorMessage": "table kyc90-accounts"}).encode())}

        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3(Broken()))
        monkeypatch.setenv("ACCOUNT_FUNCTION_NAME", "kyc90-account")
        status, body = call(accounts.get_account, event())
        assert (status, body) == (502, {"error": "ACCOUNT_PLATFORM_UNAVAILABLE"})


class TestRouting:
    @pytest.mark.parametrize("route, handler", [
        ("GET /accounts/{applicantId}", "get_account"),
        ("GET /accounts/{applicantId}/transactions", "list_transactions"),
        ("POST /accounts/{applicantId}/transactions", "create_transaction"),
    ])
    def test_each_route_reaches_its_handler(self, monkeypatch, route, handler):
        called = []
        monkeypatch.setattr(accounts, handler, lambda e, c=None: called.append(handler) or {})
        accounts.lambda_handler({"requestContext": {"routeKey": route}})
        assert called == [handler]

    def test_an_unknown_route_is_a_404(self):
        resp = accounts.lambda_handler({"requestContext": {"routeKey": "DELETE /accounts/x"}})
        assert resp["statusCode"] == 404
