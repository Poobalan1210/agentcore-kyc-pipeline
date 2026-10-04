"""Account platform stub — the part of the demo that makes a restriction real.

R31.2 is the requirement that shapes this file: **restrictions are enforced by the account
platform, not by an advisory flag.** So this is not a dict of booleans that something else is
trusted to respect. It is a transaction authoriser: ask it to move money and it says no, naming the
restriction that refused. "We set a flag" invites the question "and what enforces it?"; a stub that
refuses a transfer with `DOMESTIC_TRANSFERS_BLOCKED` answers it on stage.

Postures
--------
An account is always in one of four postures, or frozen:

* **PROVISIONAL** — opened by Phase 1. Deposits up to a ceiling, and **no money leaves**: no
  transfers of any kind, no cash, no upgrades. Verification may lawfully complete within a
  reasonable time after the account is opened (31 CFR 1020.220), but a sanctions breach is strict
  liability, so the window in which the checks are unfinished is the window in which nothing can
  be moved out. The earlier set of five restrictions left domestic transfers open and allowed cash
  up to a daily limit, which is enough to move the ceiling's worth out in a week.
* **ENHANCED** — Phase 2 at MEDIUM risk, or a reviewer's approval: money moves, the deposit ceiling
  and the product-upgrade block stay.
* **STANDARD** — Phase 2 at LOW risk: nothing restricted.
* **TIGHTENED** — Phase 2 found a permanently unusable item: the provisional restrictions plus a
  block on new deposits, while the applicant is asked for something that works.
* **FROZEN** — every transaction refused. Only a person's explicit action changes it.

Two operations move restrictions, and they are deliberately different: `restrict` only ever adds
(so an automated re-assessment can never lift one by accident), and `lift` sets the account to
exactly a posture (used only by an approval). A frozen account cannot be lifted.

Holds
-----
A re-assessment that restricts an account places a **hold** naming the event and the posture it
imposed. No lift may go below an outstanding hold's posture unless it releases that hold, and only
the reviewer's clearance of that event releases it. This is what stops two decisions made on
different evidence from undoing each other: a Phase 2 approval computed before a list update
cannot lift restrictions the list update imposed, however the two executions interleave. The
platform is where that has to hold, because it is what decides whether money moves.

State lives in DynamoDB when `ACCOUNT_TABLE` is set, and in memory otherwise so the tests are
hermetic. Every state-changing operation raises on failure, so a Step Functions Catch sees it; a
refused transaction is not a failure, it is the answer.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol


class Restriction(str, Enum):
    DEPOSIT_CEILING = "CUMULATIVE_DEPOSIT_CEILING"
    INTERNATIONAL_TRANSFERS = "INTERNATIONAL_TRANSFERS_BLOCKED"
    THIRD_PARTY_PAYMENTS = "THIRD_PARTY_PAYMENTS_BLOCKED"
    DOMESTIC_TRANSFERS = "DOMESTIC_TRANSFERS_BLOCKED"
    CASH_WITHDRAWALS = "CASH_WITHDRAWALS_BLOCKED"
    PRODUCT_UPGRADES = "PRODUCT_UPGRADES_BLOCKED"
    DEPOSITS = "DEPOSITS_BLOCKED"


class Posture(str, Enum):
    PROVISIONAL = "PROVISIONAL"
    ENHANCED = "ENHANCED"
    STANDARD = "STANDARD"
    TIGHTENED = "TIGHTENED"


#: Every way money can leave an account. A provisional account blocks all of them.
MONEY_OUT: frozenset[Restriction] = frozenset({
    Restriction.INTERNATIONAL_TRANSFERS,
    Restriction.THIRD_PARTY_PAYMENTS,
    Restriction.DOMESTIC_TRANSFERS,
    Restriction.CASH_WITHDRAWALS,
})

POSTURES: dict[Posture, frozenset[Restriction]] = {
    Posture.PROVISIONAL: MONEY_OUT | {Restriction.DEPOSIT_CEILING, Restriction.PRODUCT_UPGRADES},
    Posture.ENHANCED: frozenset({Restriction.DEPOSIT_CEILING, Restriction.PRODUCT_UPGRADES}),
    Posture.STANDARD: frozenset(),
    Posture.TIGHTENED: MONEY_OUT | {Restriction.DEPOSIT_CEILING, Restriction.PRODUCT_UPGRADES,
                                    Restriction.DEPOSITS},
}

#: Kept for callers that still import the name. It is the provisional posture.
PROVISIONAL_SET: tuple[Restriction, ...] = tuple(
    sorted(POSTURES[Posture.PROVISIONAL], key=lambda r: r.value))

#: Least to most restrictive. Each posture's restrictions contain the one before it, which the
#: tests assert, so "at least as restrictive" is a plain comparison of ranks.
POSTURE_RANK: dict[Posture, int] = {
    Posture.STANDARD: 0,
    Posture.ENHANCED: 1,
    Posture.PROVISIONAL: 2,
    Posture.TIGHTENED: 3,
}


class AccountState(str, Enum):
    OPEN_RESTRICTED = "OPEN_RESTRICTED"
    OPEN_ENHANCED = "OPEN_ENHANCED"
    OPEN_STANDARD = "OPEN_STANDARD"
    FROZEN = "FROZEN"


class TxnType(str, Enum):
    DEPOSIT = "DEPOSIT"
    DOMESTIC_TRANSFER = "DOMESTIC_TRANSFER"
    INTERNATIONAL_TRANSFER = "INTERNATIONAL_TRANSFER"
    THIRD_PARTY_PAYMENT = "THIRD_PARTY_PAYMENT"
    CASH_WITHDRAWAL = "CASH_WITHDRAWAL"
    PRODUCT_UPGRADE = "PRODUCT_UPGRADE"


#: The restriction that refuses each kind of transaction. Deposits are handled separately: they
#: are refused by the deposit block, or by the ceiling when the amount would exceed it.
TXN_RESTRICTION: dict[TxnType, Restriction] = {
    TxnType.INTERNATIONAL_TRANSFER: Restriction.INTERNATIONAL_TRANSFERS,
    TxnType.THIRD_PARTY_PAYMENT: Restriction.THIRD_PARTY_PAYMENTS,
    TxnType.DOMESTIC_TRANSFER: Restriction.DOMESTIC_TRANSFERS,
    TxnType.CASH_WITHDRAWAL: Restriction.CASH_WITHDRAWALS,
    TxnType.PRODUCT_UPGRADE: Restriction.PRODUCT_UPGRADES,
}

#: Money leaves the account for these.
_DEBITS = frozenset({TxnType.DOMESTIC_TRANSFER, TxnType.INTERNATIONAL_TRANSFER,
                     TxnType.THIRD_PARTY_PAYMENT, TxnType.CASH_WITHDRAWAL})

#: Plain-language descriptions for the applicant message. They say what is limited, never why.
_DESCRIPTIONS: dict[Restriction, str] = {
    Restriction.DEPOSIT_CEILING: "Total deposits are limited to {ceiling}.",
    Restriction.INTERNATIONAL_TRANSFERS: "International transfers are not available yet.",
    Restriction.THIRD_PARTY_PAYMENTS: "Payments to other people are not available yet.",
    Restriction.DOMESTIC_TRANSFERS: "Transfers out of the account are not available yet.",
    Restriction.CASH_WITHDRAWALS: "Cash withdrawals are not available yet.",
    Restriction.PRODUCT_UPGRADES: "Upgrades to other products are not available yet.",
    Restriction.DEPOSITS: "New deposits are paused for now.",
}

#: D-10 is still open: a placeholder pending the product's risk appetite, named in one place.
DEFAULT_DEPOSIT_CEILING = Decimal(os.environ.get("PROVISIONAL_DEPOSIT_CEILING", "2000"))

CURRENCY = "USD"

#: The account item keeps only its latest events; transactions are their own items.
_MAX_EVENTS = 20


# ---------------------------------------------------------------------------------- errors


class AccountError(Exception):
    """Raised, never returned, so a Step Functions Catch sees it. The class name is the error."""

    code = "ACCOUNT_ERROR"


class NoSuchAccount(AccountError):
    code = "NO_SUCH_ACCOUNT"


class AccountFrozen(AccountError):
    code = "ACCOUNT_FROZEN"


class BadArgument(AccountError):
    code = "BAD_ARGUMENT"


class ConcurrentUpdate(AccountError):
    code = "CONCURRENT_UPDATE"


class AccountOnHold(AccountError):
    """A lift would go below an outstanding hold that it does not release."""

    code = "ACCOUNT_ON_HOLD"


# ---------------------------------------------------------------------------------- model


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Account:
    applicant_id: str
    restrictions: set[Restriction] = field(default_factory=set)
    frozen: bool = False
    balance: Decimal = Decimal("0")
    cumulative_deposits: Decimal = Decimal("0")
    deposit_ceiling: Decimal = DEFAULT_DEPOSIT_CEILING
    opened_at: str = ""
    updated_at: str = ""
    decision_id: str | None = None
    version: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    #: Outstanding holds: hold id (the event that placed it) -> the posture it imposed.
    holds: dict[str, Posture] = field(default_factory=dict)

    def record(self, kind: str, **detail: Any) -> None:
        # R31.7: the restriction set, the outcome and the time between them must be
        # reconstructable, so every transition is timestamped as it happens.
        at = _now()
        self.events.append({"event": kind, "at": at, **detail})
        self.events = self.events[-_MAX_EVENTS:]
        self.updated_at = at

    @property
    def state(self) -> AccountState:
        if self.frozen:
            return AccountState.FROZEN
        if not self.restrictions:
            return AccountState.OPEN_STANDARD
        if self.restrictions & (MONEY_OUT | {Restriction.DEPOSITS}):
            return AccountState.OPEN_RESTRICTED
        return AccountState.OPEN_ENHANCED

    @property
    def posture(self) -> str:
        if self.frozen:
            return "FROZEN"
        for posture, restrictions in POSTURES.items():
            if self.restrictions == restrictions:
                return posture.value
        return "CUSTOM"

    def view(self) -> dict[str, Any]:
        """What callers see. Amounts as strings, so no float ever rounds a balance."""
        ordered = sorted(self.restrictions, key=lambda r: r.value)
        ceiling = f"${self.deposit_ceiling:,.0f}"
        remaining = max(Decimal("0"), self.deposit_ceiling - self.cumulative_deposits)
        return {
            "applicantId": self.applicant_id,
            "state": self.state.value,
            "posture": self.posture,
            "restrictions": [r.value for r in ordered],
            "restrictionDetails": [
                {"code": r.value, "description": _DESCRIPTIONS[r].format(ceiling=ceiling)}
                for r in ordered],
            "limits": {
                "currency": CURRENCY,
                "depositCeiling": str(self.deposit_ceiling),
                "depositsRemaining": (str(remaining)
                                      if Restriction.DEPOSIT_CEILING in self.restrictions
                                      else None),
            },
            "balance": str(self.balance),
            "cumulativeDeposits": str(self.cumulative_deposits),
            "openedAt": self.opened_at,
            "updatedAt": self.updated_at,
            "decisionId": self.decision_id,
            "version": self.version,
            "holds": [{"holdId": k, "posture": v.value} for k, v in sorted(self.holds.items())],
            "recentEvents": list(self.events[-10:]),
        }


# ---------------------------------------------------------------------------------- stores


class Store(Protocol):
    def get(self, applicant_id: str) -> Account | None: ...
    def create(self, account: Account) -> bool: ...
    def save(self, account: Account, *, expected_version: int) -> bool: ...
    def add_transaction(self, applicant_id: str, txn: dict[str, Any]) -> None: ...
    def transactions(self, applicant_id: str, limit: int) -> list[dict[str, Any]]: ...
    def delete(self, applicant_id: str) -> None: ...


class MemoryStore:
    """The same contract as the DynamoDB store, for hermetic tests."""

    def __init__(self) -> None:
        self._accounts: dict[str, dict[str, Any]] = {}
        self._txns: dict[str, list[dict[str, Any]]] = {}

    def get(self, applicant_id: str) -> Account | None:
        item = self._accounts.get(applicant_id)
        return _from_item(item) if item else None

    def create(self, account: Account) -> bool:
        if account.applicant_id in self._accounts:
            return False
        account.version = 1
        self._accounts[account.applicant_id] = _to_item(account)
        return True

    def save(self, account: Account, *, expected_version: int) -> bool:
        current = self._accounts.get(account.applicant_id)
        if current is None or int(current["version"]) != expected_version:
            return False
        account.version = expected_version + 1
        self._accounts[account.applicant_id] = _to_item(account)
        return True

    def add_transaction(self, applicant_id: str, txn: dict[str, Any]) -> None:
        self._txns.setdefault(applicant_id, []).append(dict(txn))

    def transactions(self, applicant_id: str, limit: int) -> list[dict[str, Any]]:
        return list(reversed(self._txns.get(applicant_id, [])))[:limit]

    def delete(self, applicant_id: str) -> None:
        self._accounts.pop(applicant_id, None)
        self._txns.pop(applicant_id, None)


class DynamoStore:
    """One `ACCOUNT` item per applicant, one `TXN#<sequence>` item per transaction attempt.

    Every update is conditional on the version read, so two concurrent changes cannot both land on
    the same starting state — the second is retried against the first's result.
    """

    def __init__(self, table_name: str, table: Any = None) -> None:
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self._table = table

    def get(self, applicant_id: str) -> Account | None:
        item = self._table.get_item(
            Key={"applicantId": applicant_id, "sk": "ACCOUNT"},
            ConsistentRead=True).get("Item")
        return _from_item(item) if item else None

    def create(self, account: Account) -> bool:
        account.version = 1
        try:
            self._table.put_item(Item=_to_item(account),
                                 ConditionExpression="attribute_not_exists(applicantId)")
        except Exception as exc:  # noqa: BLE001 - narrowed by error code
            if _is_conditional_failure(exc):
                return False
            raise
        return True

    def save(self, account: Account, *, expected_version: int) -> bool:
        account.version = expected_version + 1
        try:
            # Aliased: a bare attribute name in an expression fails if DynamoDB reserves it.
            self._table.put_item(Item=_to_item(account),
                                 ConditionExpression="#v = :v",
                                 ExpressionAttributeNames={"#v": "version"},
                                 ExpressionAttributeValues={":v": expected_version})
        except Exception as exc:  # noqa: BLE001 - narrowed by error code
            if _is_conditional_failure(exc):
                account.version = expected_version
                return False
            raise
        return True

    def add_transaction(self, applicant_id: str, txn: dict[str, Any]) -> None:
        self._table.put_item(Item={
            "applicantId": applicant_id,
            "sk": f"TXN#{int(txn['sequence']):010d}",
            **{k: v for k, v in txn.items() if v is not None},
        })

    def transactions(self, applicant_id: str, limit: int) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            KeyConditionExpression=(Key("applicantId").eq(applicant_id)
                                    & Key("sk").begins_with("TXN#")),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [_plain({k: v for k, v in item.items() if k not in ("applicantId", "sk")})
                for item in response.get("Items", [])]

    def delete(self, applicant_id: str) -> None:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(KeyConditionExpression=Key("applicantId").eq(applicant_id))
        for item in response.get("Items", []):
            self._table.delete_item(Key={"applicantId": applicant_id, "sk": item["sk"]})


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code") == "ConditionalCheckFailedException"


def _plain(value: Any) -> Any:
    """DynamoDB returns Decimals; callers get strings for amounts and ints for counters."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    return value


def _to_item(account: Account) -> dict[str, Any]:
    return {
        "applicantId": account.applicant_id,
        "sk": "ACCOUNT",
        "restrictions": sorted(r.value for r in account.restrictions),
        "frozen": account.frozen,
        "balance": account.balance,
        "cumulativeDeposits": account.cumulative_deposits,
        "depositCeiling": account.deposit_ceiling,
        "openedAt": account.opened_at,
        "updatedAt": account.updated_at,
        "decisionId": account.decision_id or "",
        "version": account.version,
        "events": list(account.events),
        "holds": {k: v.value for k, v in account.holds.items()},
    }


def _from_item(item: dict[str, Any]) -> Account:
    return Account(
        applicant_id=str(item["applicantId"]),
        restrictions={Restriction(r) for r in item.get("restrictions") or ()},
        frozen=bool(item.get("frozen")),
        balance=Decimal(str(item.get("balance", "0"))),
        cumulative_deposits=Decimal(str(item.get("cumulativeDeposits", "0"))),
        deposit_ceiling=Decimal(str(item.get("depositCeiling", DEFAULT_DEPOSIT_CEILING))),
        opened_at=str(item.get("openedAt") or ""),
        updated_at=str(item.get("updatedAt") or ""),
        decision_id=str(item.get("decisionId") or "") or None,
        version=int(item.get("version") or 0),
        events=[_plain(e) for e in item.get("events") or ()],
        holds={str(k): Posture(str(v)) for k, v in (item.get("holds") or {}).items()},
    )


# ---------------------------------------------------------------------------------- platform


class AccountPlatform:
    """The contract. The store is swappable; the rules about what may move are not."""

    def __init__(self, store: Store | None = None) -> None:
        self._store = store or MemoryStore()

    # ---------------------------------------------------------------- lifecycle
    def open_provisional(self, applicant_id: str, *, decision_id: str | None = None,
                         deposit_ceiling: Decimal | None = None) -> dict[str, Any]:
        """R31.1. Opens in the provisional posture. Idempotent: an existing account is returned
        unchanged, so a replayed Phase 1 cannot reset a balance or re-open a frozen account."""
        _require_id(applicant_id)
        existing = self._store.get(applicant_id)
        if existing is not None:
            return existing.view()
        now = _now()
        account = Account(applicant_id=applicant_id,
                          restrictions=set(POSTURES[Posture.PROVISIONAL]),
                          deposit_ceiling=deposit_ceiling or DEFAULT_DEPOSIT_CEILING,
                          opened_at=now, updated_at=now, decision_id=decision_id)
        account.record("OPENED_PROVISIONAL", posture=Posture.PROVISIONAL.value,
                       restrictions=sorted(r.value for r in account.restrictions),
                       decisionId=decision_id)
        if not self._store.create(account):
            current = self._store.get(applicant_id)
            if current is None:
                raise ConcurrentUpdate(f"account for {applicant_id} could not be created")
            return current.view()
        return account.view()

    def get_account(self, applicant_id: str) -> dict[str, Any]:
        return self._require(applicant_id).view()

    def restrict(self, applicant_id: str, posture: Posture | str, *, outcome: str = "",
                 reason: str = "", hold: str | None = None) -> dict[str, Any]:
        """Add the posture's restrictions. Never removes one, and a frozen account stays frozen.

        `hold` names the event imposing them. The hold keeps the account at least this restricted
        until a lift releases it by the same id.
        """
        target = _posture(posture)
        hold_id = _hold_id(hold) if hold is not None else None

        def change(account: Account) -> None:
            if hold_id:
                current = account.holds.get(hold_id)
                if current is None or POSTURE_RANK[target] > POSTURE_RANK[current]:
                    account.holds[hold_id] = target
            if account.frozen:
                account.record("RESTRICT_IGNORED_FROZEN", posture=target.value, outcome=outcome,
                               holdId=hold_id)
                return
            added = sorted(r.value for r in POSTURES[target] - account.restrictions)
            account.restrictions |= POSTURES[target]
            account.record("RESTRICTED", posture=target.value, added=added, outcome=outcome,
                           reason=reason, holdId=hold_id)

        return self._update(applicant_id, change)

    def lift(self, applicant_id: str, posture: Posture | str, *, outcome: str = "",
             reason: str = "", decision_id: str | None = None,
             release_holds: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        """R31.3/R31.4. Set exactly the posture's restrictions.

        Refused on a frozen account: only a person unfreezes, and no automated path does. Refused
        too when an outstanding hold this call does not release is more restrictive than the
        target. Nothing is changed by a refused lift, including the holds it names.
        """
        target = _posture(posture)
        releasing = {_hold_id(h) for h in release_holds or ()}

        def change(account: Account) -> None:
            if account.frozen:
                raise AccountFrozen(
                    f"account for {applicant_id} is frozen; restrictions cannot be lifted")
            blocking = sorted(h for h, floor in account.holds.items()
                              if h not in releasing
                              and POSTURE_RANK[floor] > POSTURE_RANK[target])
            if blocking:
                raise AccountOnHold(
                    f"account for {applicant_id} is held at a stricter posture by "
                    f"{', '.join(blocking)}; lifting to {target.value} needs those released")
            released = sorted(h for h in releasing if h in account.holds)
            for h in released:
                del account.holds[h]
            lifted = sorted(r.value for r in account.restrictions - POSTURES[target])
            added = sorted(r.value for r in POSTURES[target] - account.restrictions)
            account.restrictions = set(POSTURES[target])
            account.record("RESTRICTIONS_SET", posture=target.value, lifted=lifted, added=added,
                           outcome=outcome, reason=reason, decisionId=decision_id,
                           releasedHolds=released)

        return self._update(applicant_id, change)

    def freeze(self, applicant_id: str, *, outcome: str = "", reason: str = ""
               ) -> dict[str, Any]:
        """R31.5. Every transaction refused, and compliance notified. Idempotent."""

        def change(account: Account) -> None:
            if account.frozen:
                return
            account.frozen = True
            account.record("FROZEN", outcome=outcome, reason=reason, complianceNotified=True)

        return self._update(applicant_id, change)

    # ---------------------------------------------------------------- enforcement
    def transact(self, applicant_id: str, txn_type: TxnType | str, amount: Any = 0, *,
                 counterparty: str | None = None) -> dict[str, Any]:
        """The whole point of the stub: a decision naming the restriction that refused (R31.2).

        Every attempt is recorded, refused ones included, so the demo can show the refusal and the
        audit can show the attempt.
        """
        kind = _txn_type(txn_type)
        value = _amount(amount, allow_zero=kind is TxnType.PRODUCT_UPGRADE)
        verdict: dict[str, Any] = {}

        def change(account: Account) -> None:
            verdict.clear()
            verdict.update(_authorise(account, kind, value))
            if verdict["allowed"]:
                if kind is TxnType.DEPOSIT:
                    account.balance += value
                    account.cumulative_deposits += value
                elif kind in _DEBITS:
                    account.balance -= value
                account.record("TRANSACTION", type=kind.value, amount=str(value),
                               allowed=True)
            else:
                account.record("TRANSACTION_REFUSED", type=kind.value, amount=str(value),
                               reasonCode=verdict["reasonCode"])

        view = self._update(applicant_id, change)
        txn = {
            "transactionId": uuid.uuid4().hex[:12],
            # The account version this attempt produced. It rises with every change, so it orders
            # transactions exactly; a timestamp at one-second resolution does not.
            "sequence": view["version"],
            "at": _now(),
            "type": kind.value,
            "amount": str(value),
            "currency": CURRENCY,
            "allowed": verdict["allowed"],
            "refusedBy": verdict.get("refusedBy"),
            "reasonCode": verdict.get("reasonCode"),
            "counterparty": (counterparty or "")[:64] or None,
        }
        self._store.add_transaction(applicant_id, txn)
        return {**verdict, "transaction": txn, "account": view}

    def transactions(self, applicant_id: str, *, limit: int = 20) -> dict[str, Any]:
        account = self._require(applicant_id)
        return {"account": account.view(),
                "transactions": self._store.transactions(applicant_id, max(1, min(limit, 100)))}

    def reset(self, applicant_id: str) -> dict[str, Any]:
        """Demo reset: removes the account and its transactions. Not reachable from the graphs."""
        _require_id(applicant_id)
        self._store.delete(applicant_id)
        return {"account": None, "reset": True}

    # ---------------------------------------------------------------- internals
    def _require(self, applicant_id: str) -> Account:
        _require_id(applicant_id)
        account = self._store.get(applicant_id)
        if account is None:
            raise NoSuchAccount(f"no account for applicant {applicant_id}")
        return account

    def _update(self, applicant_id: str, change) -> dict[str, Any]:
        for _ in range(3):
            account = self._require(applicant_id)
            expected = account.version
            change(account)
            if self._store.save(account, expected_version=expected):
                return account.view()
        raise ConcurrentUpdate(f"account for {applicant_id} changed concurrently; retry")


def _authorise(account: Account, kind: TxnType, amount: Decimal) -> dict[str, Any]:
    def refuse(restriction: Restriction | None, code: str, reason: str) -> dict[str, Any]:
        return {"allowed": False, "refusedBy": restriction.value if restriction else None,
                "reasonCode": code, "reason": reason}

    if account.frozen:
        return refuse(None, "ACCOUNT_FROZEN", "the account is on hold")
    if kind is TxnType.DEPOSIT:
        if Restriction.DEPOSITS in account.restrictions:
            return refuse(Restriction.DEPOSITS, Restriction.DEPOSITS.value,
                          "new deposits are paused")
        if (Restriction.DEPOSIT_CEILING in account.restrictions
                and account.cumulative_deposits + amount > account.deposit_ceiling):
            return refuse(Restriction.DEPOSIT_CEILING, "DEPOSIT_CEILING_EXCEEDED",
                          f"the deposit would exceed the {account.deposit_ceiling} ceiling "
                          f"(already deposited {account.cumulative_deposits})")
        return {"allowed": True, "refusedBy": None, "reasonCode": None, "reason": ""}
    restriction = TXN_RESTRICTION[kind]
    if restriction in account.restrictions:
        return refuse(restriction, restriction.value, _DESCRIPTIONS[restriction].rstrip("."))
    if kind in _DEBITS and amount > account.balance:
        return refuse(None, "INSUFFICIENT_FUNDS", "the balance does not cover this amount")
    return {"allowed": True, "refusedBy": None, "reasonCode": None, "reason": ""}


def _require_id(applicant_id: Any) -> None:
    if not isinstance(applicant_id, str) or not applicant_id.strip():
        raise BadArgument("applicantId is required")


def _hold_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128:
        raise BadArgument("a hold id must be a non-empty event id of at most 128 characters")
    return text


def _posture(value: Posture | str) -> Posture:
    try:
        return value if isinstance(value, Posture) else Posture(str(value).upper())
    except ValueError as exc:
        raise BadArgument(f"unknown posture {value!r}") from exc


def _txn_type(value: TxnType | str) -> TxnType:
    try:
        return value if isinstance(value, TxnType) else TxnType(str(value).upper())
    except ValueError as exc:
        raise BadArgument(f"unknown transaction type {value!r}") from exc


def _amount(value: Any, *, allow_zero: bool) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BadArgument(f"amount must be a number, got {value!r}") from exc
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise BadArgument(f"amount must be positive, got {value!r}")
    if amount > Decimal("1000000"):
        raise BadArgument("amount exceeds the stub's single-transaction limit")
    return amount.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------------- handler

_PLATFORM: AccountPlatform | None = None


def platform() -> AccountPlatform:
    """DynamoDB when `ACCOUNT_TABLE` is set; memory otherwise (tests, local runs)."""
    global _PLATFORM
    if _PLATFORM is None:
        table = os.environ.get("ACCOUNT_TABLE")
        _PLATFORM = AccountPlatform(DynamoStore(table) if table else MemoryStore())
    return _PLATFORM


def _id(a: dict[str, Any]) -> str:
    return str(a.get("applicantId") or "")


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise BadArgument("releaseHolds must be a list of hold ids")


_OPS = {
    "openProvisional": lambda p, a: {"account": p.open_provisional(
        _id(a), decision_id=a.get("decisionId") or None)},
    "getAccount": lambda p, a: {"account": p.get_account(_id(a))},
    "restrict": lambda p, a: {"account": p.restrict(
        _id(a), a.get("posture", ""), outcome=str(a.get("outcome") or ""),
        reason=str(a.get("reason") or ""), hold=a.get("hold"))},
    "lift": lambda p, a: {"account": p.lift(
        _id(a), a.get("posture", ""), outcome=str(a.get("outcome") or ""),
        reason=str(a.get("reason") or ""), decision_id=a.get("decisionId") or None,
        release_holds=_list(a.get("releaseHolds")))},
    "freeze": lambda p, a: {"account": p.freeze(
        _id(a), outcome=str(a.get("outcome") or ""), reason=str(a.get("reason") or ""))},
    "transact": lambda p, a: p.transact(
        _id(a), a.get("type", ""), a.get("amount", 0), counterparty=a.get("counterparty")),
    "listTransactions": lambda p, a: p.transactions(_id(a), limit=int(a.get("limit") or 20)),
    "reset": lambda p, a: p.reset(_id(a)),
}

OPERATIONS = tuple(sorted(_OPS))


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """`{"operation": ..., "arguments": {...}}`. Raises on any failure, including an unknown
    operation, so the caller's error handling — a Step Functions Catch, the API's error mapping —
    sees a failure rather than a success-shaped refusal."""
    op = (event or {}).get("operation")
    args = (event or {}).get("arguments") or {}
    fn = _OPS.get(op)
    if fn is None:
        raise BadArgument(f"unknown operation {op!r}; expected one of {', '.join(OPERATIONS)}")
    if not isinstance(args, dict):
        raise BadArgument("arguments must be an object")
    return {"status": "OK", "operation": op, **fn(platform(), args)}
