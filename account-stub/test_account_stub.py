"""Account platform stub tests — R31, AC-23.

The assertion that matters is not "the flag is set". It is "the transaction was refused and the
refusal named the restriction". R31.2 requires enforcement rather than advice, and a test that
only checks state would pass against an advisory implementation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from account_stub import (
    PROVISIONAL_SET,
    AccountPlatform,
    AccountState,
    Restriction,
    TxnType,
    handler,
)


@pytest.fixture
def platform() -> AccountPlatform:
    p = AccountPlatform()
    p.open_provisional("A-1")
    return p


class TestProvisionalOpening:
    def test_exactly_five_restrictions_are_applied(self, platform):
        """R31.1. The table in the requirement is normative — five, not four, not six."""
        state = platform.get_restriction_state("A-1")
        assert len(state["restrictions"]) == 5
        assert set(state["restrictions"]) == {r.value for r in PROVISIONAL_SET}
        assert state["state"] == AccountState.OPEN_RESTRICTED.value

    def test_opening_is_recorded_with_a_timestamp(self, platform):
        acct = platform.get("A-1")
        assert acct.events[0]["event"] == "OPENED_PROVISIONAL"
        assert acct.events[0]["at"].endswith("Z")


class TestEnforcementNotAdvice:
    """R31.2 / AC-23. Each of these proves a real refusal, with a reason."""

    def test_international_transfer_is_refused_by_name(self, platform):
        d = platform.authorise("A-1", TxnType.INTERNATIONAL_TRANSFER, 10)
        assert d.allowed is False
        assert d.refused_by is Restriction.INTERNATIONAL_TRANSFERS
        assert d.reason_code == "INTERNATIONAL_TRANSFERS_BLOCKED"
        assert "blocked" in d.reason

    def test_third_party_payment_is_refused(self, platform):
        d = platform.authorise("A-1", TxnType.THIRD_PARTY_PAYMENT, 10)
        assert d.allowed is False
        assert d.refused_by is Restriction.THIRD_PARTY_PAYMENTS

    def test_product_upgrade_is_refused(self, platform):
        d = platform.authorise("A-1", TxnType.PRODUCT_UPGRADE)
        assert d.allowed is False
        assert d.refused_by is Restriction.PRODUCT_UPGRADES

    def test_deposit_under_ceiling_is_allowed(self, platform):
        assert platform.authorise("A-1", TxnType.DEPOSIT, 500).allowed is True

    def test_deposit_over_ceiling_is_refused_and_says_how_much(self, platform):
        platform.authorise("A-1", TxnType.DEPOSIT, 1900)
        d = platform.authorise("A-1", TxnType.DEPOSIT, 500)
        assert d.allowed is False
        assert d.refused_by is Restriction.DEPOSIT_CEILING
        assert "1900" in d.reason  # the reviewer needs the running total, not just a no

    def test_ceiling_is_cumulative_not_per_transaction(self, platform):
        """The restriction is a cumulative ceiling (R31.1). Twenty small deposits must be
        refused as surely as one large one, or the control is trivially bypassed."""
        for _ in range(20):
            platform.authorise("A-1", TxnType.DEPOSIT, 100)
        d = platform.authorise("A-1", TxnType.DEPOSIT, 100)
        assert d.allowed is False
        assert d.refused_by is Restriction.DEPOSIT_CEILING

    def test_cash_withdrawal_limit_is_daily_and_cumulative(self, platform):
        platform.authorise("A-1", TxnType.DEPOSIT, 1000)
        assert platform.authorise("A-1", TxnType.CASH_WITHDRAWAL, 200).allowed is True
        d = platform.authorise("A-1", TxnType.CASH_WITHDRAWAL, 100)
        assert d.allowed is False
        assert d.refused_by is Restriction.CASH_WITHDRAWAL_LIMIT

    def test_domestic_transfer_is_not_restricted(self, platform):
        """The five restrictions are exactly five. A provisional account is usable — that is the
        premise of the whole two-phase design, so over-restricting is also a defect."""
        assert platform.authorise("A-1", TxnType.DOMESTIC_TRANSFER, 50).allowed is True


class TestPhase2Transitions:
    def test_full_approve_standard_lifts_everything(self, platform):
        state = platform.lift_restrictions("A-1")
        assert state["restrictions"] == []
        assert state["state"] == AccountState.OPEN_UNRESTRICTED.value
        assert platform.authorise("A-1", TxnType.INTERNATIONAL_TRANSFER, 10).allowed is True

    def test_lift_is_recorded_with_a_timestamp(self, platform):
        """R31.3 requires the lifting event to be timestamped."""
        platform.lift_restrictions("A-1")
        ev = [e for e in platform.get("A-1").events if e["event"] == "RESTRICTIONS_LIFTED"]
        assert ev and ev[0]["at"].endswith("Z")
        assert len(ev[0]["lifted"]) == 5

    def test_enhanced_monitoring_lifts_transfers_and_keeps_the_ceiling(self, platform):
        """R31.4."""
        state = platform.lift_transfers_keep_ceiling("A-1")
        assert Restriction.DEPOSIT_CEILING.value in state["restrictions"]
        assert Restriction.INTERNATIONAL_TRANSFERS.value not in state["restrictions"]
        assert platform.authorise("A-1", TxnType.INTERNATIONAL_TRANSFER, 10).allowed is True
        platform.authorise("A-1", TxnType.DEPOSIT, 1999)
        assert platform.authorise("A-1", TxnType.DEPOSIT, 500).allowed is False

    def test_reject_freezes_and_notifies_compliance(self, platform):
        """R31.5."""
        state = platform.freeze_account("A-1", "REJECT", "confirmed sanctions match")
        assert state["state"] == AccountState.FROZEN.value
        ev = [e for e in platform.get("A-1").events if e["event"] == "FROZEN"]
        assert ev[0]["complianceNotified"] is True

    def test_frozen_account_refuses_everything(self, platform):
        platform.freeze_account("A-1", "REJECT")
        for t in TxnType:
            assert platform.authorise("A-1", t, 1).allowed is False, t

    def test_sla_breach_retains_restrictions_and_raises_an_alarm(self, platform):
        """R31.6. The property under test is that nothing lapses."""
        before = set(platform.get_restriction_state("A-1")["restrictions"])
        state = platform.retain_on_sla_breach("A-1")
        assert set(state["restrictions"]) == before
        ev = [e for e in platform.get("A-1").events if e["event"] == "PHASE2_SLA_BREACH"]
        assert ev[0]["alarm"] is True

    def test_no_method_lifts_restrictions_on_timeout(self):
        """R31.6, structurally. If a future edit adds a timeout-driven lift, this catches it:
        the only methods that reduce the restriction set must be the two explicit Phase 2
        outcomes."""
        import inspect
        import account_stub

        src = inspect.getsource(account_stub.AccountPlatform.retain_on_sla_breach)
        for forbidden in ("clear()", "discard(", "remove(", "OPEN_UNRESTRICTED"):
            assert forbidden not in src, (
                "retain_on_sla_breach must not weaken restrictions: found %r" % forbidden)


class TestHandler:
    def test_all_four_contract_operations_exist(self):
        """Task 2.7 names applyRestrictions, liftRestrictions, freezeAccount,
        getRestrictionState."""
        out = handler({"operation": "nope"})
        ops = set(out["operations"])
        assert {"applyRestrictions", "liftRestrictions", "freezeAccount",
                "getRestrictionState"} <= ops

    def test_round_trip_through_the_handler(self):
        handler({"operation": "applyRestrictions", "arguments": {"applicantId": "H-1"}})
        state = handler({"operation": "getRestrictionState",
                         "arguments": {"applicantId": "H-1"}})["result"]
        assert len(state["restrictions"]) == 5

        refused = handler({"operation": "authorise",
                           "arguments": {"applicantId": "H-1",
                                         "txnType": "INTERNATIONAL_TRANSFER",
                                         "amount": 25}})["result"]
        assert refused["allowed"] is False
        assert refused["refused_by"] == "INTERNATIONAL_TRANSFERS_BLOCKED"

        handler({"operation": "liftRestrictions", "arguments": {"applicantId": "H-1"}})
        allowed = handler({"operation": "authorise",
                           "arguments": {"applicantId": "H-1",
                                         "txnType": "INTERNATIONAL_TRANSFER",
                                         "amount": 25}})["result"]
        assert allowed["allowed"] is True

    def test_unknown_account_is_reported_not_crashed(self):
        out = handler({"operation": "getRestrictionState",
                       "arguments": {"applicantId": "does-not-exist"}})
        assert out["status"] == "INVALID"
        assert out["reason_code"] == "NO_SUCH_ACCOUNT"

    def test_bad_transaction_type_is_reported(self):
        handler({"operation": "applyRestrictions", "arguments": {"applicantId": "H-2"}})
        out = handler({"operation": "authorise",
                       "arguments": {"applicantId": "H-2", "txnType": "CRYPTO_YOLO"}})
        assert out["status"] == "INVALID"
        assert out["reason_code"] == "BAD_ARGUMENT"
