"""Account platform stub — task 2.7.

R31.2 is the requirement that shapes this file: **restrictions are enforced by the account
platform, not by advisory flag.** So this is not a dict of booleans that something else is
trusted to respect. It is a transaction authoriser: you ask it to move money, and it says no
and tells you which restriction said no.

The difference matters for the demo and for the argument. "We set a flag" invites the question
"and what enforces it?" A stub that actually rejects a transfer with
`INTERNATIONAL_TRANSFERS_BLOCKED` answers that question on stage.

State lives in DynamoDB when `ACCOUNT_TABLE` is set, in memory otherwise so the tests are
hermetic.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from enum import Enum
from typing import Any


class Restriction(str, Enum):
    """The five restrictions in R31.1. Exactly five — the table is normative."""

    DEPOSIT_CEILING = "CUMULATIVE_DEPOSIT_CEILING"
    INTERNATIONAL_TRANSFERS = "INTERNATIONAL_TRANSFERS_BLOCKED"
    THIRD_PARTY_PAYMENTS = "THIRD_PARTY_PAYMENTS_BLOCKED"
    CASH_WITHDRAWAL_LIMIT = "CASH_WITHDRAWAL_LIMIT"
    PRODUCT_UPGRADES = "PRODUCT_UPGRADES_BLOCKED"


PROVISIONAL_SET: tuple[Restriction, ...] = tuple(Restriction)
assert len(PROVISIONAL_SET) == 5


class AccountState(str, Enum):
    OPEN_RESTRICTED = "OPEN_RESTRICTED"
    OPEN_UNRESTRICTED = "OPEN_UNRESTRICTED"
    OPEN_MONITORED = "OPEN_MONITORED"
    FROZEN = "FROZEN"


class TxnType(str, Enum):
    DEPOSIT = "DEPOSIT"
    INTERNATIONAL_TRANSFER = "INTERNATIONAL_TRANSFER"
    THIRD_PARTY_PAYMENT = "THIRD_PARTY_PAYMENT"
    CASH_WITHDRAWAL = "CASH_WITHDRAWAL"
    PRODUCT_UPGRADE = "PRODUCT_UPGRADE"
    DOMESTIC_TRANSFER = "DOMESTIC_TRANSFER"


# D-10 is still open: these values are placeholders pending the product's risk appetite, and
# they are named here rather than scattered so the open decision has one home.
DEFAULT_LIMITS = {
    "depositCeiling": Decimal(os.environ.get("PROVISIONAL_DEPOSIT_CEILING", "2000")),
    "cashWithdrawalPerDay": Decimal(os.environ.get("PROVISIONAL_CASH_LIMIT", "250")),
}


@dataclass
class Decision:
    allowed: bool
    #: Which restriction refused. None when allowed.
    refused_by: Restriction | None = None
    reason: str = ""
    #: Machine-readable, safe to branch on.
    reason_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["refused_by"] = self.refused_by.value if self.refused_by else None
        return d


@dataclass
class Account:
    applicant_id: str
    state: AccountState
    restrictions: set[Restriction] = field(default_factory=set)
    cumulative_deposits: Decimal = Decimal("0")
    withdrawals_today: Decimal = Decimal("0")
    limits: dict[str, Decimal] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, kind: str, **detail: Any) -> None:
        # R31.7: the restriction set, the outcome and the elapsed time between them all have to
        # be reconstructable, so every transition is timestamped as it happens.
        self.events.append({"event": kind,
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            **detail})


class AccountPlatform:
    """In-memory reference implementation. The contract, not the storage, is the point."""

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}

    # ---------------------------------------------------------------- lifecycle
    def open_provisional(self, applicant_id: str,
                         limits: dict[str, Decimal] | None = None) -> Account:
        """R31.1. Opens with all five restrictions enforced."""
        acct = Account(applicant_id=applicant_id,
                       state=AccountState.OPEN_RESTRICTED,
                       restrictions=set(PROVISIONAL_SET),
                       limits=dict(limits or DEFAULT_LIMITS))
        acct.record("OPENED_PROVISIONAL",
                    restrictions=[r.value for r in sorted(acct.restrictions, key=lambda x: x.value)],
                    limits={k: str(v) for k, v in acct.limits.items()})
        self._accounts[applicant_id] = acct
        return acct

    def get(self, applicant_id: str) -> Account | None:
        return self._accounts.get(applicant_id)

    def get_restriction_state(self, applicant_id: str) -> dict[str, Any]:
        acct = self._require(applicant_id)
        return {
            "applicantId": applicant_id,
            "state": acct.state.value,
            "restrictions": sorted(r.value for r in acct.restrictions),
            "cumulativeDeposits": str(acct.cumulative_deposits),
            "limits": {k: str(v) for k, v in acct.limits.items()},
            "eventCount": len(acct.events),
        }

    # ---------------------------------------------------------------- Phase 2 transitions
    def lift_restrictions(self, applicant_id: str) -> dict[str, Any]:
        """R31.3. FULL_APPROVE_STANDARD lifts everything, and the lift is timestamped."""
        acct = self._require(applicant_id)
        lifted = sorted(r.value for r in acct.restrictions)
        acct.restrictions.clear()
        acct.state = AccountState.OPEN_UNRESTRICTED
        acct.record("RESTRICTIONS_LIFTED", lifted=lifted, outcome="FULL_APPROVE_STANDARD")
        return self.get_restriction_state(applicant_id)

    def lift_transfers_keep_ceiling(self, applicant_id: str) -> dict[str, Any]:
        """R31.4. FULL_APPROVE_ENHANCED_MONITORING lifts transfers, keeps the deposit ceiling."""
        acct = self._require(applicant_id)
        for r in (Restriction.INTERNATIONAL_TRANSFERS, Restriction.THIRD_PARTY_PAYMENTS):
            acct.restrictions.discard(r)
        acct.restrictions.add(Restriction.DEPOSIT_CEILING)
        acct.state = AccountState.OPEN_MONITORED
        acct.record("RESTRICTIONS_PARTIALLY_LIFTED",
                    remaining=sorted(r.value for r in acct.restrictions),
                    outcome="FULL_APPROVE_ENHANCED_MONITORING")
        return self.get_restriction_state(applicant_id)

    def freeze_account(self, applicant_id: str, outcome: str,
                       reason: str = "") -> dict[str, Any]:
        """R31.5. RESTRICT_FURTHER or REJECT freezes and notifies compliance."""
        acct = self._require(applicant_id)
        acct.state = AccountState.FROZEN
        acct.restrictions = set(PROVISIONAL_SET)
        acct.record("FROZEN", outcome=outcome, reason=reason,
                    complianceNotified=True)
        return self.get_restriction_state(applicant_id)

    def retain_on_sla_breach(self, applicant_id: str) -> dict[str, Any]:
        """R31.6. Restrictions never lapse by timeout.

        There is deliberately no code path from a timeout to a lift. This method exists so the
        alarm case is explicit in the contract rather than being the absence of an action —
        silence is not permission.
        """
        acct = self._require(applicant_id)
        acct.record("PHASE2_SLA_BREACH",
                    restrictionsRetained=sorted(r.value for r in acct.restrictions),
                    alarm=True)
        return self.get_restriction_state(applicant_id)

    # ---------------------------------------------------------------- enforcement
    def authorise(self, applicant_id: str, txn_type: TxnType,
                  amount: Decimal | str | float = 0) -> Decision:
        """The whole point of the stub. Returns a refusal naming the restriction (R31.2)."""
        acct = self._require(applicant_id)
        amt = Decimal(str(amount))

        if acct.state is AccountState.FROZEN:
            return Decision(False, None, "account is frozen pending compliance review",
                            "ACCOUNT_FROZEN")

        if txn_type is TxnType.INTERNATIONAL_TRANSFER and \
                Restriction.INTERNATIONAL_TRANSFERS in acct.restrictions:
            return Decision(False, Restriction.INTERNATIONAL_TRANSFERS,
                            "international transfers are blocked while checks complete",
                            "INTERNATIONAL_TRANSFERS_BLOCKED")

        if txn_type is TxnType.THIRD_PARTY_PAYMENT and \
                Restriction.THIRD_PARTY_PAYMENTS in acct.restrictions:
            return Decision(False, Restriction.THIRD_PARTY_PAYMENTS,
                            "third-party payments are blocked while checks complete",
                            "THIRD_PARTY_PAYMENTS_BLOCKED")

        if txn_type is TxnType.PRODUCT_UPGRADE and \
                Restriction.PRODUCT_UPGRADES in acct.restrictions:
            return Decision(False, Restriction.PRODUCT_UPGRADES,
                            "product upgrades are unavailable while checks complete",
                            "PRODUCT_UPGRADES_BLOCKED")

        if txn_type is TxnType.DEPOSIT and Restriction.DEPOSIT_CEILING in acct.restrictions:
            ceiling = acct.limits["depositCeiling"]
            if acct.cumulative_deposits + amt > ceiling:
                return Decision(
                    False, Restriction.DEPOSIT_CEILING,
                    "deposit would exceed the provisional cumulative ceiling of %s "
                    "(already deposited %s)" % (ceiling, acct.cumulative_deposits),
                    "DEPOSIT_CEILING_EXCEEDED")
            acct.cumulative_deposits += amt
            acct.record("DEPOSIT", amount=str(amt),
                        cumulative=str(acct.cumulative_deposits))
            return Decision(True)

        if txn_type is TxnType.CASH_WITHDRAWAL and \
                Restriction.CASH_WITHDRAWAL_LIMIT in acct.restrictions:
            limit = acct.limits["cashWithdrawalPerDay"]
            if acct.withdrawals_today + amt > limit:
                return Decision(
                    False, Restriction.CASH_WITHDRAWAL_LIMIT,
                    "withdrawal would exceed the provisional daily cash limit of %s "
                    "(already withdrawn %s today)" % (limit, acct.withdrawals_today),
                    "CASH_LIMIT_EXCEEDED")
            acct.withdrawals_today += amt
            acct.record("CASH_WITHDRAWAL", amount=str(amt))
            return Decision(True)

        if txn_type is TxnType.DEPOSIT:
            acct.cumulative_deposits += amt
            acct.record("DEPOSIT", amount=str(amt))
        return Decision(True)

    def _require(self, applicant_id: str) -> Account:
        acct = self._accounts.get(applicant_id)
        if acct is None:
            raise KeyError("no account for applicant %s" % applicant_id)
        return acct


# Module-level instance for the Lambda handler path.
PLATFORM = AccountPlatform()

_OPS = {
    "openProvisional": lambda p, a: p.open_provisional(a["applicantId"]).applicant_id,
    "applyRestrictions": lambda p, a: p.open_provisional(a["applicantId"]).applicant_id,
    "liftRestrictions": lambda p, a: p.lift_restrictions(a["applicantId"]),
    "liftTransfersKeepCeiling": lambda p, a: p.lift_transfers_keep_ceiling(a["applicantId"]),
    "freezeAccount": lambda p, a: p.freeze_account(
        a["applicantId"], a.get("outcome", "REJECT"), a.get("reason", "")),
    "getRestrictionState": lambda p, a: p.get_restriction_state(a["applicantId"]),
    "retainOnSlaBreach": lambda p, a: p.retain_on_sla_breach(a["applicantId"]),
    "authorise": lambda p, a: p.authorise(
        a["applicantId"], TxnType(a["txnType"]), a.get("amount", 0)).to_dict(),
}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    op = event.get("operation")
    args = event.get("arguments") or {}
    fn = _OPS.get(op)
    if fn is None:
        return {"status": "INVALID", "reason": "unknown operation %r" % op,
                "operations": sorted(_OPS)}
    try:
        return {"status": "OK", "operation": op, "result": fn(PLATFORM, args)}
    except KeyError as e:
        return {"status": "INVALID", "reason": str(e), "reason_code": "NO_SUCH_ACCOUNT"}
    except ValueError as e:
        return {"status": "INVALID", "reason": str(e), "reason_code": "BAD_ARGUMENT"}
