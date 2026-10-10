"""Device and network velocity, captured at intake (D-41).

`velocity_check`, the Fraud agent's tool (`tools/kyctools/tools_impl.py`), counts the rows the
velocity store holds for an application's device and for its network over the last 24 hours. This
module writes those rows when an application is submitted, and clears them for the presenter's
Reset (`demo.reset`, `scripts/prepare_demo.py reset`).

Three choices, each for a reason:

* **Keyed hashes, never the values.** The browser's device id and the caller's address are hashed
  with a KMS HMAC key before anything is stored, so neither the case record nor the velocity store
  holds an IP address. An unkeyed hash of an IPv4 address is reversed by trying all 2^32 of them;
  a key that never leaves KMS cannot be tried offline.
* **One row per person.** A row's sort key is a keyed hash of the declared identity, so one person
  applying again from the same device counts once. Many different people from one device is the
  synthetic-identity pattern velocity exists to catch; one person retrying is not. It also means a
  presenter restarting a persona on stage does not move the count.
* **Both identifiers or neither.** The tool reports a missing identifier as zero, and zero means
  "we looked". So the case carries the device and the network together or not at all, and with
  neither the tool records NOT_ESTABLISHED (D-29).

Capture never stops an application. If KMS or the store cannot be reached, the case is submitted
without identifiers and velocity is NOT_ESTABLISHED for it: what the build did before this
existed, and never a clean result nobody observed.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Mapping

LOG = logging.getLogger(__name__)

#: The window `velocity_check` counts (`VELOCITY_WINDOW_HOURS`). A row counts while its
#: `expiresAt` is in the future; the table's TTL deletes it some time after.
WINDOW_SECONDS = 24 * 3600

#: What the console sends: `crypto.randomUUID()`, kept in the browser's local storage. Anything
#: else is ignored rather than hashed, so a caller cannot put arbitrary text into a key.
_DEVICE_ID = re.compile(r"^[A-Za-z0-9-]{16,64}$")

_TABLE_ENV = "VELOCITY_TABLE_NAME"
_KEY_ENV = "INTAKE_HASH_KEY_ID"

#: BatchWriteItem's limit, and how many times unprocessed deletes are retried.
_BATCH = 25
_BATCH_ATTEMPTS = 5


def table_name() -> str:
    return os.environ.get(_TABLE_ENV, "")


def source_network(event: Mapping[str, Any]) -> str | None:
    """The caller's network as the API saw it: an IPv4 address, or an IPv6 /64.

    IPv6 clients rotate the host half of their address (privacy addresses), so one device shows a
    new address every few hours. The /64 is what stays put, so that is what is counted.
    """
    context = event.get("requestContext") if isinstance(event, Mapping) else None
    context = context if isinstance(context, Mapping) else {}
    http = context.get("http") if isinstance(context.get("http"), Mapping) else {}
    rest = context.get("identity") if isinstance(context.get("identity"), Mapping) else {}
    raw = str(http.get("sourceIp") or rest.get("sourceIp") or "").strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def identity_key(declared: Any, applicant_id: str) -> str:
    """Who applied, as the details form submits it: names upper-cased, spaces collapsed.

    With no declared name or date of birth (an API caller that sent none), the application itself
    stands for the person, so it still counts once.
    """
    source = declared if isinstance(declared, Mapping) else {}
    parts = [" ".join(str(source.get(field) or "").upper().split())
             for field in ("givenNames", "surname", "dateOfBirth")]
    if not any(parts):
        return f"application:{applicant_id}"
    return "identity:" + "|".join(parts)


def capture(boto3: Any, event: Mapping[str, Any], *, device_id: Any, declared: Any,
            applicant_id: str, now: datetime) -> dict[str, Any] | None:
    """Count this application against its device and its network.

    Returns the case's `intake` attribute (the two keyed hashes `velocity_check` reads), or None
    when velocity cannot be established for this application. Why is logged, never raised.
    """
    table, key_id = table_name(), os.environ.get(_KEY_ENV, "")
    if not (table and key_id):
        return None
    device = str(device_id or "").strip()
    network = source_network(event)
    if not _DEVICE_ID.match(device) or network is None:
        LOG.warning("velocity not captured applicantId=%s reason=%s", applicant_id,
                    "NO_SOURCE_IP" if _DEVICE_ID.match(device) else "NO_DEVICE_ID")
        return None
    try:
        kms = boto3.client("kms")
        device_hash = _mac(kms, key_id, f"device:{device}")
        network_hash = _mac(kms, key_id, f"ip:{network}")
        person = _mac(kms, key_id, identity_key(declared, applicant_id))
        expires = str(int(now.timestamp()) + WINDOW_SECONDS)
        ddb = boto3.client("dynamodb")
        # The partition keys are the ones `velocity_check` queries: `device#…` and `ip#…`.
        for label, value in (("device", device_hash), ("ip", network_hash)):
            ddb.put_item(TableName=table, Item={
                "pk": {"S": f"{label}#{value}"},
                "sk": {"S": person},
                "expiresAt": {"N": expires},
            })
    except Exception as exc:  # noqa: BLE001 - capture never stops an application
        LOG.warning("velocity not captured applicantId=%s reason=%s", applicant_id,
                    error_code(exc))
        return None
    return {"M": {
        "deviceHash": {"S": device_hash},
        "ipHash": {"S": network_hash},
        "capturedAt": {"S": now.isoformat().replace("+00:00", "Z")},
    }}


def count(clients: Any, *, table: str | None = None) -> int | None:
    """How many rows the store holds now, expired or not. None when no store is configured."""
    table = table or table_name()
    if not table:
        return None
    ddb = clients.client("dynamodb")
    total = 0
    kwargs: dict[str, Any] = {"TableName": table, "Select": "COUNT"}
    while True:
        page = ddb.scan(**kwargs)
        total += int(page.get("Count", 0))
        if not page.get("LastEvaluatedKey"):
            return total
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def clear(clients: Any, *, table: str | None = None) -> int | None:
    """Delete every row: the presenter's Reset. Returns how many were deleted, or None when no
    store is configured. Raises if any delete could not be completed, so Reset can say so."""
    table = table or table_name()
    if not table:
        return None
    ddb = clients.client("dynamodb")
    deleted = 0
    kwargs: dict[str, Any] = {"TableName": table, "ProjectionExpression": "#pk, #sk",
                              "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk"}}
    while True:
        page = ddb.scan(**kwargs)
        keys = [{"pk": item["pk"], "sk": item["sk"]} for item in page.get("Items", [])]
        for start in range(0, len(keys), _BATCH):
            _delete(ddb, table, keys[start:start + _BATCH])
        deleted += len(keys)
        if not page.get("LastEvaluatedKey"):
            return deleted
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _delete(ddb: Any, table: str, keys: list[dict[str, Any]]) -> None:
    requests = [{"DeleteRequest": {"Key": key}} for key in keys]
    for attempt in range(_BATCH_ATTEMPTS):
        resp = ddb.batch_write_item(RequestItems={table: requests})
        requests = (resp.get("UnprocessedItems") or {}).get(table) or []
        if not requests:
            return
        time.sleep(0.1 * 2 ** attempt)
    raise RuntimeError(f"{len(requests)} velocity entries could not be deleted")


def _mac(kms: Any, key_id: str, message: str) -> str:
    resp = kms.generate_mac(KeyId=key_id, Message=message.encode("utf-8"),
                            MacAlgorithm="HMAC_SHA_256")
    return bytes(resp["Mac"]).hex()


def error_code(exc: Exception) -> str:
    """The AWS error code, else the exception's type: what a log line or the presenter is told."""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Code", ""))
    return type(exc).__name__
