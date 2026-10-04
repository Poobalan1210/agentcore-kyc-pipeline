"""Account platform stub tests — R31, AC-23.

The assertion that matters is not "the flag is set". It is "the transaction was refused and the
refusal named the restriction". R31.2 requires enforcement rather than advice, and a test that only
checks state would pass against an advisory implementation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import account_stub
from account_stub import (
    MONEY_OUT,
    POSTURES,
    AccountFrozen,
    AccountPlatform,
    AccountState,
    BadArgument,
    DynamoStore,
    MemoryStore,
    NoSuchAccount,
    Posture,
    Restriction,
    TxnType,
    handler,
)


@pytest.fixture
def platform() -> AccountPlatform:
    p = AccountPlatform(MemoryStore())
    p.open_provisional("A-1", decision_id="D-1")
    return p


def _refused_by(result: dict[str, Any]) -> str | None:
    assert result["allowed"] is False, result
    return result["refusedBy"]


class TestProvisionalPosture:
    def test_no_money_can_leave_a_provisional_account(self, platform):
        """The window in which the checks are unfinished is the window in which nothing moves
        out. The earlier five-restriction set left domestic transfers open and allowed cash up to a
        daily limit — enough to empty the ceiling in a week."""
        platform.transact("A-1", TxnType.DEPOSIT, 500)
        for kind in (TxnType.DOMESTIC_TRANSFER, TxnType.INTERNATIONAL_TRANSFER,
                     TxnType.THIRD_PARTY_PAYMENT, TxnType.CASH_WITHDRAWAL):
            result = platform.transact("A-1", kind, 10)
            assert _refused_by(result) == account_stub.TXN_RESTRICTION[kind].value, kind

    def test_every_money_out_route_has_a_restriction(self):
        """If a new way to move money is added, it must be blocked provisionally too."""
        debits = {TxnType.DOMESTIC_TRANSFER, TxnType.INTERNATIONAL_TRANSFER,
                  TxnType.THIRD_PARTY_PAYMENT, TxnType.CASH_WITHDRAWAL}
        assert {account_stub.TXN_RESTRICTION[k] for k in debits} == set(MONEY_OUT)
        assert MONEY_OUT <= POSTURES[Posture.PROVISIONAL]

    def test_deposits_are_accepted_up_to_the_ceiling(self, platform):
        """The account is usable from minute one — the premise of the two-phase design."""
        assert platform.transact("A-1", TxnType.DEPOSIT, 1500)["allowed"] is True
        assert platform.transact("A-1", TxnType.DEPOSIT, 500)["allowed"] is True
        refused = platform.transact("A-1", TxnType.DEPOSIT, "0.01")
        assert _refused_by(refused) == Restriction.DEPOSIT_CEILING.value
        assert refused["reasonCode"] == "DEPOSIT_CEILING_EXCEEDED"
        assert refused["account"]["balance"] == "2000.00"

    def test_product_upgrades_are_blocked(self, platform):
        result = platform.transact("A-1", TxnType.PRODUCT_UPGRADE, 0)
        assert _refused_by(result) == Restriction.PRODUCT_UPGRADES.value

    def test_the_view_describes_each_limit_in_plain_language(self, platform):
        """What the Notify agent is given: what is limited and by how much, never why."""
        view = platform.get_account("A-1")
        assert view["state"] == AccountState.OPEN_RESTRICTED.value
        assert view["posture"] == "PROVISIONAL"
        details = {d["code"]: d["description"] for d in view["restrictionDetails"]}
        assert "$2,000" in details[Restriction.DEPOSIT_CEILING.value]
        for text in details.values():
            for word in ("sanction", "fraud", "screen", "risk", "compliance"):
                assert word not in text.lower(), text

    def test_opening_is_idempotent(self, platform):
        """A replayed Phase 1 must not reset a balance or reopen a frozen account."""
        platform.transact("A-1", TxnType.DEPOSIT, 100)
        platform.freeze("A-1", outcome="REJECT")
        again = platform.open_provisional("A-1")
        assert again["state"] == AccountState.FROZEN.value
        assert again["balance"] == "100.00"

    def test_opening_is_recorded(self, platform):
        view = platform.get_account("A-1")
        opened = [e for e in view["recentEvents"] if e["event"] == "OPENED_PROVISIONAL"]
        assert opened and opened[0]["at"].endswith("Z")
        assert view["decisionId"] == "D-1"


class TestLiftAndRestrict:
    def test_standard_lifts_everything(self, platform):
        view = platform.lift("A-1", Posture.STANDARD, outcome="FULL_APPROVE_STANDARD")
        assert view["restrictions"] == []
        assert view["state"] == AccountState.OPEN_STANDARD.value
        platform.transact("A-1", TxnType.DEPOSIT, 5000)
        assert platform.transact("A-1", TxnType.INTERNATIONAL_TRANSFER, 10)["allowed"] is True

    def test_enhanced_lets_money_move_and_keeps_the_ceiling(self, platform):
        """R31.4, as re-specified: MEDIUM lifts transfers and cash, keeps the ceiling."""
        view = platform.lift("A-1", Posture.ENHANCED)
        assert set(view["restrictions"]) == {Restriction.DEPOSIT_CEILING.value,
                                             Restriction.PRODUCT_UPGRADES.value}
        assert view["state"] == AccountState.OPEN_ENHANCED.value
        platform.transact("A-1", TxnType.DEPOSIT, 100)
        assert platform.transact("A-1", TxnType.DOMESTIC_TRANSFER, 50)["allowed"] is True
        assert _refused_by(platform.transact("A-1", TxnType.DEPOSIT, 5000)) == \
            Restriction.DEPOSIT_CEILING.value

    def test_restrict_never_removes_a_restriction(self, platform):
        """An automated re-assessment can add restrictions; only an approval can lift them. From
        PROVISIONAL, restricting to ENHANCED must leave every provisional restriction in place."""
        before = set(platform.get_account("A-1")["restrictions"])
        after = set(platform.restrict("A-1", Posture.ENHANCED)["restrictions"])
        assert before <= after

    def test_restrict_reapplies_what_a_lift_removed(self, platform):
        platform.lift("A-1", Posture.STANDARD)
        view = platform.restrict("A-1", Posture.PROVISIONAL, outcome="RISK_INCREASED_REVIEW")
        assert set(view["restrictions"]) == {r.value for r in POSTURES[Posture.PROVISIONAL]}

    def test_tightened_also_pauses_deposits(self, platform):
        view = platform.restrict("A-1", Posture.TIGHTENED, outcome="RESTRICT_FURTHER")
        assert Restriction.DEPOSITS.value in view["restrictions"]
        assert _refused_by(platform.transact("A-1", TxnType.DEPOSIT, 1)) == \
            Restriction.DEPOSITS.value

    def test_a_frozen_account_cannot_be_lifted(self, platform):
        """A Phase 2 approval landing after a Phase 3 freeze must not undo the freeze."""
        platform.freeze("A-1", outcome="FREEZE")
        with pytest.raises(AccountFrozen):
            platform.lift("A-1", Posture.STANDARD)
        assert platform.get_account("A-1")["state"] == AccountState.FROZEN.value

    def test_restricting_a_frozen_account_leaves_it_frozen(self, platform):
        platform.freeze("A-1")
        assert platform.restrict("A-1", Posture.ENHANCED)["state"] == AccountState.FROZEN.value


class TestHolds:
    """A re-assessment's restriction cannot be lifted by a decision made on older evidence."""

    def test_postures_are_ordered_by_what_they_restrict(self):
        """`at least as restrictive` is a rank comparison only because each posture contains the
        one below it."""
        ordered = sorted(account_stub.POSTURE_RANK, key=account_stub.POSTURE_RANK.get)
        for lower, higher in zip(ordered, ordered[1:]):
            assert POSTURES[lower] < POSTURES[higher], (lower, higher)

    def test_a_lift_below_an_outstanding_hold_is_refused_and_changes_nothing(self, platform):
        """The Phase 2 vs Phase 3 race: a list update restricts the account for review while a
        Phase 2 computed on the old list is about to lift it."""
        platform.restrict("A-1", Posture.PROVISIONAL, outcome="RISK_INCREASED_REVIEW",
                          hold="EVT-LIST-1")
        before = platform.get_account("A-1")
        with pytest.raises(account_stub.AccountOnHold, match="EVT-LIST-1"):
            platform.lift("A-1", Posture.STANDARD, outcome="FULL_APPROVE_STANDARD")
        with pytest.raises(account_stub.AccountOnHold):
            platform.lift("A-1", Posture.ENHANCED, outcome="FULL_APPROVE_ENHANCED_MONITORING")
        after = platform.get_account("A-1")
        assert after["restrictions"] == before["restrictions"]
        assert after["holds"] == [{"holdId": "EVT-LIST-1", "posture": "PROVISIONAL"}]

    def test_the_reviewers_clearance_releases_its_own_hold(self, platform):
        platform.restrict("A-1", Posture.PROVISIONAL, hold="EVT-LIST-1")
        view = platform.lift("A-1", Posture.STANDARD, release_holds=["EVT-LIST-1"])
        assert view["posture"] == "STANDARD" and view["holds"] == []

    def test_releasing_one_hold_does_not_release_another(self, platform):
        platform.restrict("A-1", Posture.PROVISIONAL, hold="EVT-1")
        platform.restrict("A-1", Posture.PROVISIONAL, hold="EVT-2")
        with pytest.raises(account_stub.AccountOnHold, match="EVT-2"):
            platform.lift("A-1", Posture.STANDARD, release_holds=["EVT-1"])
        assert {h["holdId"] for h in platform.get_account("A-1")["holds"]} == {"EVT-1", "EVT-2"}

    def test_a_lift_to_the_holds_own_posture_is_allowed(self, platform):
        """A hold is a floor, not a lock: MEDIUM risk keeps the enhanced posture."""
        platform.lift("A-1", Posture.STANDARD)
        platform.restrict("A-1", Posture.ENHANCED, outcome="RISK_INCREASED_RESTRICT",
                          hold="EVT-TXN-9")
        assert platform.lift("A-1", Posture.ENHANCED)["posture"] == "ENHANCED"
        with pytest.raises(account_stub.AccountOnHold):
            platform.lift("A-1", Posture.STANDARD)

    def test_a_second_restriction_by_the_same_event_only_raises_the_floor(self, platform):
        platform.restrict("A-1", Posture.ENHANCED, hold="EVT-3")
        platform.restrict("A-1", Posture.PROVISIONAL, hold="EVT-3")
        platform.restrict("A-1", Posture.ENHANCED, hold="EVT-3")
        assert platform.get_account("A-1")["holds"] == [
            {"holdId": "EVT-3", "posture": "PROVISIONAL"}]

    def test_holds_survive_the_dynamodb_round_trip(self):
        table = _FakeTable()
        p = AccountPlatform(DynamoStore("kyc90-accounts", table=table))
        p.open_provisional("D-9")
        p.restrict("D-9", Posture.PROVISIONAL, hold="EVT-7")
        with pytest.raises(account_stub.AccountOnHold):
            p.lift("D-9", Posture.STANDARD)
        assert p.lift("D-9", Posture.STANDARD, release_holds=["EVT-7"])["holds"] == []

    def test_the_handler_passes_holds_through(self):
        account_stub._PLATFORM = AccountPlatform(MemoryStore())
        handler({"operation": "openProvisional", "arguments": {"applicantId": "H-2"}})
        handler({"operation": "restrict", "arguments": {
            "applicantId": "H-2", "posture": "PROVISIONAL", "hold": "EVT-1"}})
        with pytest.raises(account_stub.AccountOnHold):
            handler({"operation": "lift", "arguments": {"applicantId": "H-2",
                                                        "posture": "STANDARD"}})
        lifted = handler({"operation": "lift", "arguments": {
            "applicantId": "H-2", "posture": "STANDARD", "releaseHolds": ["EVT-1"]}})
        assert lifted["account"]["holds"] == []


class TestFreeze:
    def test_a_frozen_account_refuses_everything(self, platform):
        platform.transact("A-1", TxnType.DEPOSIT, 100)
        platform.freeze("A-1", outcome="FREEZE", reason="SANCTIONS_LIST_UPDATE")
        for kind in TxnType:
            amount = 0 if kind is TxnType.PRODUCT_UPGRADE else 1
            result = platform.transact("A-1", kind, amount)
            assert result["allowed"] is False, kind
            assert result["reasonCode"] == "ACCOUNT_FROZEN"

    def test_freezing_notifies_compliance_and_is_idempotent(self, platform):
        platform.freeze("A-1", outcome="FREEZE")
        platform.freeze("A-1", outcome="FREEZE")
        events = [e for e in platform.get_account("A-1")["recentEvents"]
                  if e["event"] == "FROZEN"]
        assert len(events) == 1 and events[0]["complianceNotified"] is True


class TestTransactions:
    def test_every_attempt_is_recorded_including_refusals(self, platform):
        platform.transact("A-1", TxnType.DEPOSIT, 100, counterparty="payroll")
        platform.transact("A-1", TxnType.DOMESTIC_TRANSFER, 10)
        history = platform.transactions("A-1")["transactions"]
        assert [t["allowed"] for t in history] == [False, True]  # newest first
        assert history[0]["reasonCode"] == Restriction.DOMESTIC_TRANSFERS.value

    def test_insufficient_funds_is_refused_once_money_may_move(self, platform):
        platform.lift("A-1", Posture.STANDARD)
        result = platform.transact("A-1", TxnType.DOMESTIC_TRANSFER, 10)
        assert result["allowed"] is False and result["reasonCode"] == "INSUFFICIENT_FUNDS"

    @pytest.mark.parametrize("amount", [-1, 0, "abc", "NaN", 10_000_000])
    def test_bad_amounts_are_rejected(self, platform, amount):
        with pytest.raises(BadArgument):
            platform.transact("A-1", TxnType.DEPOSIT, amount)


class TestHandler:
    def test_operations_return_the_account(self):
        account_stub._PLATFORM = AccountPlatform(MemoryStore())
        opened = handler({"operation": "openProvisional",
                          "arguments": {"applicantId": "H-1", "decisionId": "D-9"}})
        assert opened["status"] == "OK"
        assert opened["account"]["posture"] == "PROVISIONAL"
        lifted = handler({"operation": "lift",
                          "arguments": {"applicantId": "H-1", "posture": "ENHANCED"}})
        assert lifted["account"]["posture"] == "ENHANCED"

    def test_failures_raise_so_a_catch_sees_them(self):
        """A success-shaped refusal from a state-changing call would read to Step Functions as
        success. The error class name is what the Catch matches on."""
        account_stub._PLATFORM = AccountPlatform(MemoryStore())
        with pytest.raises(NoSuchAccount):
            handler({"operation": "freeze", "arguments": {"applicantId": "missing"}})
        with pytest.raises(BadArgument):
            handler({"operation": "retainOnSlaBreach", "arguments": {"applicantId": "x"}})
        with pytest.raises(BadArgument):
            handler({"operation": "lift", "arguments": {"applicantId": "x", "posture": "VIP"}})

    def test_the_graphs_operations_all_exist(self):
        """Every operation asl.py invokes is served by the handler."""
        for op in ("openProvisional", "getAccount", "restrict", "lift", "freeze"):
            assert op in account_stub.OPERATIONS, op


# --------------------------------------------------------------------------- DynamoDB store


class _ConditionFailed(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


def _key_value(condition: Any, attribute: str) -> Any:
    """The value a boto3 key condition compares `attribute` with (`eq` or `begins_with`)."""
    expression = condition.get_expression()
    if expression["operator"] == "AND":
        for part in expression["values"]:
            found = _key_value(part, attribute)
            if found is not None:
                return found
        return None
    key, value = expression["values"]
    return value if key.name == attribute else None


class _FakeTable:
    """Just enough of a DynamoDB Table resource: the two conditions the store uses."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803 - boto3 casing
        item = self.items.get((Key["applicantId"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None,  # noqa: N803
                 ExpressionAttributeValues=None):  # noqa: N803
        key = (Item["applicantId"], Item["sk"])
        current = self.items.get(key)
        if ConditionExpression == "attribute_not_exists(applicantId)" and current:
            raise _ConditionFailed()
        if ConditionExpression == "#v = :v":
            if not current or current["version"] != ExpressionAttributeValues[":v"]:
                raise _ConditionFailed()
        self.items[key] = {k: (Decimal(str(v)) if isinstance(v, Decimal) else v)
                           for k, v in Item.items()}

    def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=None):  # noqa: N803
        applicant = _key_value(KeyConditionExpression, "applicantId")
        prefix = _key_value(KeyConditionExpression, "sk") or ""
        rows = sorted((v for (a, s), v in self.items.items()
                       if a == applicant and s.startswith(prefix)),
                      key=lambda r: r["sk"], reverse=not ScanIndexForward)
        return {"Items": rows[:Limit] if Limit else rows}

    def delete_item(self, Key):  # noqa: N803
        self.items.pop((Key["applicantId"], Key["sk"]), None)


class TestDynamoStore:
    def test_round_trip_keeps_amounts_exact(self):
        table = _FakeTable()
        p = AccountPlatform(DynamoStore("kyc90-accounts", table=table))
        p.open_provisional("D-1")
        p.transact("D-1", TxnType.DEPOSIT, "123.45")
        view = p.get_account("D-1")
        assert view["balance"] == "123.45"
        assert table.items[("D-1", "ACCOUNT")]["version"] == 2

    def test_a_stale_write_is_retried_against_the_latest_version(self):
        """Two concurrent changes cannot both land on the same starting state."""
        table = _FakeTable()
        store = DynamoStore("kyc90-accounts", table=table)
        p = AccountPlatform(store)
        p.open_provisional("D-2")
        stale = store.get("D-2")
        p.freeze("D-2")
        assert store.save(stale, expected_version=1) is False
        assert p.get_account("D-2")["state"] == AccountState.FROZEN.value

    def test_opening_twice_does_not_overwrite(self):
        table = _FakeTable()
        p = AccountPlatform(DynamoStore("kyc90-accounts", table=table))
        p.open_provisional("D-3")
        p.transact("D-3", TxnType.DEPOSIT, 10)
        assert p.open_provisional("D-3")["balance"] == "10.00"

    def test_transactions_are_their_own_items_newest_first(self):
        table = _FakeTable()
        p = AccountPlatform(DynamoStore("kyc90-accounts", table=table))
        p.open_provisional("D-4")
        p.transact("D-4", TxnType.DEPOSIT, 10)
        p.transact("D-4", TxnType.CASH_WITHDRAWAL, 5)
        rows = p.transactions("D-4")["transactions"]
        assert [r["type"] for r in rows] == ["CASH_WITHDRAWAL", "DEPOSIT"]
        assert all(k not in rows[0] for k in ("applicantId", "sk"))

    def test_reset_removes_the_account_and_its_transactions(self):
        table = _FakeTable()
        p = AccountPlatform(DynamoStore("kyc90-accounts", table=table))
        p.open_provisional("D-5")
        p.transact("D-5", TxnType.DEPOSIT, 10)
        p.reset("D-5")
        assert not [k for k in table.items if k[0] == "D-5"]
        with pytest.raises(NoSuchAccount):
            p.get_account("D-5")
