"""`POST /documents/presign`, `POST /applicants`, `GET /cases/{applicantId}` — task 14.3.

R23.1/R23.2/F-23 are enforced **client-side before upload** per R32.6 — this module's job is
narrower: it only ever hands out a presigned PUT into the jurisdiction's evidence bucket, and it
only enqueues an applicant onto the real ingress path (task 7.1/7.2), the same SQS FIFO queue and
EventBridge Pipe already live-verified this session, not a shortcut around it. A malformed
document that slips past the browser still fails inside the state machine, visibly, per §10.2 —
this handler does not attempt a second copy of the client-side validation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any, Mapping

import auth
import notifications
import velocity

LOG = logging.getLogger(__name__)
if not LOG.handlers:
    LOG.setLevel(logging.INFO)

#: At most this many documents per submission. The tools read the first; more is never needed and
#: an unbounded list is an unbounded write into the case record.
MAX_DOCUMENTS = 3

#: The states in which an applicant may resubmit. Everything else is either in progress, with a
#: person, or decided — a second submission there would race the first.
CONTINUABLE_STATES = ("AWAITING_APPLICANT",)

_DOCUMENT_TYPE = re.compile(r"[^A-Za-z0-9_]")

#: Jurisdiction -> evidence bucket env var name, mirroring `foundation_stack.py`'s per-
#: jurisdiction buckets. Kept as an explicit map (not a format string) so an unsupported
#: jurisdiction fails with a clear 400 rather than presigning into a bucket that does not exist.
_BUCKET_ENV_BY_JURISDICTION = {
    "us": "EVIDENCE_BUCKET_US", "uk": "EVIDENCE_BUCKET_UK",
    "au": "EVIDENCE_BUCKET_AU", "jp": "EVIDENCE_BUCKET_JP",
}

#: R33.1 — the presign expiry. Short enough that a leaked URL is a narrow window, long enough
#: to cover a slow upload from a mobile connection.
PRESIGN_EXPIRY_SECONDS = 300


def presign_document(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`POST /documents/presign`. Body: `{"jurisdiction": "us", "documentType": "PASSPORT",
    "contentType": "image/jpeg", "checksumSha256": "<base64 SHA-256 of the file>"}`.

    Two properties of the evidence buckets (`foundation_stack.py`) dictate how this URL must be
    signed, and both were found by a real presigned PUT failing against the live bucket rather
    than by reading the docs:

    1. **SSE-KMS requires SigV4.** The buckets are encrypted with a customer-managed key, and S3
       rejects a presigned PUT signed with any earlier signature version outright
       (`InvalidArgument: Requests specifying Server Side Encryption with AWS KMS managed keys
       require AWS Signature Version 4`). boto3's default for S3 is not guaranteed to be SigV4 in
       every environment, so it is pinned explicitly instead of relied upon.

    2. **Object Lock requires an integrity header.** Every PUT into a bucket with a default
       retention must carry `Content-MD5` or an `x-amz-checksum-*` header, or S3 returns
       `InvalidRequest`. SHA-256 is used rather than MD5 because the browser computes it natively
       with `crypto.subtle.digest` (Web Crypto exposes no MD5), so the applicant app needs no
       hashing dependency. The checksum must be part of the signature here for the browser's
       header to be accepted, which is why it is a request parameter rather than something the
       client can attach unilaterally.
    """
    body = _body(event)
    jurisdiction = str(body.get("jurisdiction") or "").lower()
    # Into an S3 key, so letters, digits and underscore only. A `/` here could place an upload
    # under the decision records' `{applicantId}/{phase}/` prefix in the same bucket.
    document_type = _DOCUMENT_TYPE.sub("", str(body.get("documentType") or ""))[:32]
    content_type = str(body.get("contentType") or "application/octet-stream")
    checksum_sha256 = str(body.get("checksumSha256") or "")
    applicant_id = applicant_id_from(event, body)

    bucket_env = _BUCKET_ENV_BY_JURISDICTION.get(jurisdiction)
    if bucket_env is None or not applicant_id:
        return _response(400, {"error": "MISSING_OR_UNSUPPORTED_PARAMETERS"})
    bucket_name = os.environ.get(bucket_env)
    if not bucket_name:
        return _response(500, {"error": "BUCKET_NOT_CONFIGURED", "jurisdiction": jurisdiction})
    if not checksum_sha256:
        # Refused rather than defaulted: a URL signed without the checksum is guaranteed to fail
        # at PUT time against an Object Lock bucket, and failing here names the missing field
        # instead of surfacing S3's InvalidRequest to the applicant as a generic upload error.
        return _response(400, {"error": "MISSING_CHECKSUM_SHA256"})

    key = f"{applicant_id}/{document_type or 'document'}-{uuid.uuid4().hex[:8]}"

    import boto3
    from botocore.config import Config

    # S3 user metadata, signed here and sent back as `x-amz-meta-*` headers by the browser.
    #
    # This is the channel `kyctools`' `s3_get_document` reads: it calls `head_object` and returns
    # `metadata` to the Documents agent. With nothing stamped here, that map is empty and the agent
    # has only a size and a content type — which is exactly the state a live run exposed, with the
    # agent correctly but uselessly reporting every field "not available in the document metadata".
    # It could not evaluate expiry or an unsupported issuer because it had no inputs for either.
    #
    # `synthetic` is the important one for a public demo: it marks the object as fabricated at the
    # storage layer, so the audit record can never present a demo document as a real one. It is
    # derived from the request rather than assumed, so a genuine document would not inherit it.
    metadata = {
        "documenttype": document_type or "UNKNOWN",
        "issuingcountry": str(body.get("issuingCountry") or "").upper(),
        "synthetic": "true" if body.get("synthetic") else "false",
        "uploadedvia": "applicant-web",
    }

    s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket_name,
            "Key": key,
            "ContentType": content_type,
            "ChecksumSHA256": checksum_sha256,
            "Metadata": metadata,
        },
        ExpiresIn=PRESIGN_EXPIRY_SECONDS,
    )
    return _response(200, {
        "uploadUrl": url, "bucket": bucket_name, "key": key,
        "expiresInSeconds": PRESIGN_EXPIRY_SECONDS,
        # Echoed back so the browser sends exactly the value that was signed — recomputing it on
        # the client after the presign would risk a mismatch that S3 reports as a signature error.
        "checksumSha256": checksum_sha256,
        # The browser must replay these verbatim as `x-amz-meta-*` headers: they are part of the
        # signature, so an added, missing or altered value is rejected by S3 as a signature
        # mismatch rather than silently ignored.
        "metadata": metadata,
    })


def create_applicant(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`POST /applicants`. Body: `{"jurisdiction": "us", "productApplied": "current_account",
    "applicationId": "...", "declaredIdentity": {...},
    "documents": [{"bucket": ..., "key": ..., "type": "PASSPORT", "issuingCountry": "US"}]}`.

    Writes the case record and enqueues the applicant on the real ingress queue, so a submission
    through this endpoint takes the identical path the pipeline was proven on (`NormalizeInput` ->
    `LoadCase` -> ...).

    Three outcomes for a case that already exists:

    * **AWAITING_APPLICANT** — the applicant was asked for something (a clearer document, another
      liveness check). This is the continuation: the previous Phase 1 decision moves into
      `phase1History`, the case returns to RECEIVED with a new event id, and Phase 1 runs again
      on the new evidence. The facts the tools recorded last time are older than the new run, so
      the rules ignore them.
    * **any other state** — 409. The case is in progress, with a reviewer, or decided, and a second
      submission would race the first.
    * **liveness only** — the liveness endpoint creates the item before this call; that is not a
      case yet, and intake proceeds normally.
    """
    body = _body(event)
    jurisdiction = str(body.get("jurisdiction") or "").lower()
    if jurisdiction not in _BUCKET_ENV_BY_JURISDICTION:
        return _response(400, {"error": "UNSUPPORTED_JURISDICTION", "jurisdiction": jurisdiction})

    applicant_id = applicant_id_from(event, body) or f"APP-{uuid.uuid4().hex[:12].upper()}"
    documents = body.get("documents") or []
    if not isinstance(documents, list) or not documents:
        return _response(400, {"error": "NO_DOCUMENTS"})
    if len(documents) > MAX_DOCUMENTS:
        return _response(400, {"error": "TOO_MANY_DOCUMENTS", "limit": MAX_DOCUMENTS})

    # Only documents this applicant uploaded, through this API, to this jurisdiction's bucket.
    # The tools read whatever the case lists, so a case that could list another applicant's object
    # could be screened against someone else's passport.
    bucket = os.environ.get(_BUCKET_ENV_BY_JURISDICTION[jurisdiction], "")
    for document in documents:
        if not isinstance(document, Mapping):
            return _response(400, {"error": "MALFORMED_DOCUMENT"})
        if (str(document.get("bucket") or "") != bucket
                or not str(document.get("key") or "").startswith(f"{applicant_id}/")):
            return _response(403, {"error": "DOCUMENT_NOT_OWNED"})

    declared_body = body.get("declaredIdentity")
    declared_body = declared_body if isinstance(declared_body, Mapping) else {}
    raw_tax_id = str(declared_body.get("taxId") or "").strip()
    tax_id = _tax_id(raw_tax_id) if raw_tax_id else None
    if raw_tax_id and tax_id is None:
        return _response(400, {"error": "INVALID_TAX_ID"})

    import boto3
    from datetime import datetime, timezone

    ddb = boto3.client("dynamodb")
    table = os.environ["CASE_TABLE_NAME"]
    key = {"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}}
    tin_key = {"applicantId": {"S": applicant_id}, "sk": {"S": TIN_SORT_KEY}}
    # A step-up resubmits from what the browser kept, and the browser never keeps the number: one
    # already on file stands.
    if (tax_id is None and _tax_id_required(jurisdiction, declared_body)
            and "Item" not in ddb.get_item(TableName=table, Key=tin_key,
                                           ProjectionExpression="applicantId")):
        return _response(400, {"error": "TAX_ID_REQUIRED"})
    moment = datetime.now(timezone.utc)
    now = moment.isoformat().replace("+00:00", "Z")
    event_id = f"EVT-{uuid.uuid4().hex[:12].upper()}"
    documents_attr = {"L": [
        {"M": {
            "type": {"S": _DOCUMENT_TYPE.sub("", str(d.get("type") or ""))[:32]},
            "key": {"S": str(d.get("key") or "")},
            "bucket": {"S": str(d.get("bucket") or "")},
            "issuingCountry": {"S": str(d.get("issuingCountry") or "")[:3].upper()},
        }} for d in documents
    ]}
    declared = _declared_identity_attr(body.get("declaredIdentity"))
    # D-41. The device and network this application came from, as keyed hashes, for the Fraud
    # agent's velocity tool. None when either is missing or capture failed: velocity is then
    # NOT_ESTABLISHED for this case, and the application goes ahead regardless.
    intake = velocity.capture(boto3, event, device_id=body.get("deviceId"),
                              declared=body.get("declaredIdentity"), applicant_id=applicant_id,
                              now=moment)

    values: dict[str, Any] = {
        ":declaredIdentity": declared,
        ":caseId": {"S": f"CASE-{applicant_id}"},
        ":state": {"S": "RECEIVED"},
        ":createdAt": {"S": now},
        ":jurisdiction": {"S": jurisdiction},
        ":productApplied": {"S": str(body.get("productApplied") or "current_account")},
        ":documents": documents_attr,
        ":eventId": {"S": event_id},
        ":empty": {"L": []},
        ":tr": _transition("NONE", "RECEIVED", now, "application submitted", event),
    }
    if intake is not None:
        values[":intake"] = intake

    # `update_item`, not `put_item`, and the guard is on `caseId` rather than `applicantId`: the
    # liveness result is written to this item *before* this endpoint runs (F-40's three-minute
    # window), so the item can already exist holding nothing but liveness evidence. `caseId` is
    # written here and nowhere else, so its absence is the real "not yet intaken" signal (R21.4).
    try:
        ddb.update_item(
            TableName=table,
            Key=key,
            UpdateExpression=(
                "SET caseId = :caseId, #state = :state, createdAt = :createdAt, "
                "jurisdiction = :jurisdiction, productApplied = :productApplied, "
                "documents = :documents, declaredIdentity = :declaredIdentity, "
                "lastEventId = :eventId, "
                + ("intake = :intake, " if intake is not None else "")
                + "transitions = list_append(if_not_exists(transitions, :empty), :tr)"),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_not_exists(caseId)",
        )
    except Exception as exc:  # noqa: BLE001 - narrowed immediately by error code
        if _error_code(exc) != "ConditionalCheckFailedException":
            raise
        resubmitted = _continue_case(ddb, table=table, key=key, applicant_id=applicant_id,
                                     event_id=event_id, documents=documents_attr,
                                     declared=declared, now=now, event=event, intake=intake)
        if resubmitted is not None:
            return resubmitted

    if tax_id is not None:
        # After the case write, so a submission refused above never replaces the number on file.
        kind, digits = tax_id
        ddb.update_item(
            TableName=table,
            Key=tin_key,
            UpdateExpression="SET taxId = :taxId, kind = :kind, last4 = :last4, recordedAt = :at",
            ExpressionAttributeValues={":taxId": {"S": digits}, ":kind": {"S": kind},
                                       ":last4": {"S": digits[-4:]}, ":at": {"S": now}},
        )

    _enqueue(boto3, applicant_id, event_id)
    LOG.info("applicant enqueued applicantId=%s jurisdiction=%s", applicant_id, jurisdiction)
    return _response(202, {"applicantId": applicant_id, "eventId": event_id})


def _continue_case(ddb: Any, *, table: str, key: Mapping[str, Any], applicant_id: str,
                   event_id: str, documents: Mapping[str, Any], declared: Mapping[str, Any],
                   now: str, event: Mapping[str, Any],
                   intake: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Resubmission after a step-up. Returns a response to send, or None to go on and enqueue.

    The previous attempt is kept, not overwritten: its decision, any review of it and the identity
    it was decided on move into `phase1History`, so a reviewer of the new attempt can see the old
    one, including a changed name or date of birth. The review slot is cleared because a new
    attempt can be reviewed afresh; first-writer-wins applies per attempt.

    `intake` is this attempt's device and network, when captured; without it the case keeps the
    ones the first submission recorded.
    """
    current = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item") or {}
    state = _s(current.get("state")) or "UNKNOWN"
    if state not in CONTINUABLE_STATES:
        return _response(409, {"error": "CASE_ALREADY_SUBMITTED", "state": state,
                               "applicantId": applicant_id})

    previous = {"M": {
        "phase1": current.get("phase1") or {"NULL": True},
        "phase1Review": current.get("phase1Review") or {"NULL": True},
        "declaredIdentity": current.get("declaredIdentity") or {"NULL": True},
        "documents": current.get("documents") or {"L": []},
        "eventId": current.get("lastEventId") or {"NULL": True},
        "closedAt": {"S": now},
    }}
    values: dict[str, Any] = {
        ":received": {"S": "RECEIVED"},
        ":awaiting": {"S": state},
        ":documents": documents,
        ":declaredIdentity": declared,
        ":eventId": {"S": event_id},
        ":now": {"S": now},
        ":empty": {"L": []},
        ":previous": {"L": [previous]},
        ":tr": _transition(state, "RECEIVED", now, "resubmitted after a step-up", event),
    }
    if intake is not None:
        values[":intake"] = intake
    try:
        ddb.update_item(
            TableName=table,
            Key=key,
            UpdateExpression=(
                "SET #state = :received, documents = :documents, "
                "declaredIdentity = :declaredIdentity, lastEventId = :eventId, "
                "resubmittedAt = :now, "
                + ("intake = :intake, " if intake is not None else "")
                + "phase1History = list_append(if_not_exists(phase1History, :empty), :previous), "
                "transitions = list_append(if_not_exists(transitions, :empty), :tr) "
                "REMOVE phase1, phase1Review, reviewTaskToken, reviewTaskTokenAt"),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=values,
            # The state read above must still hold: two resubmissions racing each other must not
            # both enqueue.
            ConditionExpression="#state = :awaiting",
        )
    except Exception as exc:  # noqa: BLE001 - narrowed immediately by error code
        if _error_code(exc) != "ConditionalCheckFailedException":
            raise
        return _response(409, {"error": "CASE_ALREADY_SUBMITTED", "applicantId": applicant_id})
    return None


def _enqueue(boto3: Any, applicant_id: str, event_id: str) -> None:
    boto3.client("sqs").send_message(
        QueueUrl=os.environ["APPLICANTS_QUEUE_URL"],
        MessageBody=json.dumps({"applicantId": applicant_id, "eventId": event_id}),
        MessageGroupId=applicant_id,
        # FIFO dedupes on this for five minutes, so a browser retry of one submission is one run.
        MessageDeduplicationId=event_id,
    )


def _transition(from_state: str, to_state: str, at: str, reason: str,
                event: Mapping[str, Any]) -> dict[str, Any]:
    """R2.5 — the applicant is the actor of their own submission."""
    return {"L": [{"M": {
        "from": {"S": from_state},
        "to": {"S": to_state},
        "at": {"S": at},
        "actor": {"S": f"applicant:{auth.caller_sub(event) or 'unauthenticated'}"},
        "reason": {"S": reason},
    }}]}


#: The declared-identity fields stored on the case record. Fixed list rather than "whatever the
#: client sent": the declared names reach agent prompts and the screening tool, and an unbounded
#: client-supplied map would let a caller put arbitrary text in front of a model.
_DECLARED_FIELDS = ("givenNames", "surname", "dateOfBirth", "nationality", "address")

#: Ceiling per field, for the same reason. Long enough for a real compound name or address.
_DECLARED_MAX_LEN = 256

#: CIP (31 CFR 1020.220(a)(2)(i)(A)(4)): a U.S. person gives a taxpayer identification number. It
#: is kept on its own item, never on the case: the state machines read the whole case item, and
#: Step Functions keeps what they read in execution history for 90 days, where Backstage shows it.
#: Nothing in the pipeline needs the number; a reviewer sees its last four digits.
TIN_SORT_KEY = "TIN"

#: ITIN group numbers (the fourth and fifth digits), per the IRS. An ITIN always starts with 9;
#: an SSN never does.
_ITIN_GROUPS = frozenset(
    [*range(50, 66), *range(70, 89), *range(90, 93), *range(94, 100)])


def _tax_id(raw: Any) -> tuple[str, str] | None:
    """`(kind, digits)` for a well-formed SSN or ITIN, or None.

    Format only. Whether the number belongs to this person is a question for SSA's consent-based
    verification service (eCBSV), which this demo does not call.
    """
    digits = re.sub(r"[\s-]", "", str(raw or ""))
    if len(digits) != 9 or not digits.isdigit():
        return None
    area, group, serial = digits[:3], int(digits[3:5]), digits[5:]
    if area.startswith("9"):
        return ("ITIN", digits) if group in _ITIN_GROUPS else None
    if area in ("000", "666") or group == 0 or serial == "0000":
        return None
    return "SSN", digits


def _tax_id_required(jurisdiction: str, declared: Mapping[str, Any]) -> bool:
    """A U.S. national applying to the U.S. entity. A foreign national may give a passport number
    instead (1020.220(a)(2)(i)(A)(4)(ii)), and the passport is already on the case."""
    return jurisdiction == "us" and str(declared.get("nationality") or "").strip().upper() == "USA"


def _declared_identity_attr(declared: Any) -> dict[str, Any]:
    """The applicant's declared identity, as a DynamoDB map.

    This is the identity `sanctions_screen` screens — it records NO_NAME_ON_RECORD and refuses to
    run without a name, so without it the Phase 1 screening has nothing to screen. It is also the
    left-hand side of the declared-vs-document comparison, which is a fraud control in its own
    right.

    Stored even when empty (as an empty map) rather than omitted, so the difference between "the
    applicant declared nothing" and "this case predates the form" is visible on the record instead
    of being indistinguishable.
    """
    values: dict[str, Any] = {}
    source = declared if isinstance(declared, Mapping) else {}
    for field in _DECLARED_FIELDS:
        raw = source.get(field)
        text = str(raw).strip()[:_DECLARED_MAX_LEN] if raw is not None else ""
        values[field] = {"S": text}
    return {"M": values}


def get_case(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /cases/{applicantId}`. The applicant's own case, or any case for a reviewer or a
    presenter. Anyone else gets a 404, not a 403, so the route cannot be used to learn which
    applicant ids exist."""
    path = event.get("pathParameters") or {}
    applicant_id = str(path.get("applicantId") or "")
    if not applicant_id:
        return _response(400, {"error": "MISSING_APPLICANT_ID"})
    if not auth.may_read(event, applicant_id):
        return _response(404, {"error": "CASE_NOT_FOUND"})

    import boto3

    ddb = boto3.client("dynamodb")
    resp = ddb.get_item(
        TableName=os.environ["CASE_TABLE_NAME"],
        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    if not item:
        return _response(404, {"error": "CASE_NOT_FOUND"})

    return _response(200, _project_case(item))


#: Case state -> what the applicant is shown. Internal states such as AWAITING_REVIEW are not
#: secrets, but their names are; "in review" is what a bank would say.
_APPLICANT_STATES = {
    "RECEIVED": "SUBMITTED",
    "IN_PROGRESS": "CHECKING",
    "AWAITING_APPLICANT": "MORE_INFORMATION_NEEDED",
    "AWAITING_REVIEW": "IN_REVIEW",
    "PROVISIONAL": "OPEN_WITH_LIMITS",
    "MONITORING": "OPEN",
    "RESTRICTED": "OPEN_WITH_LIMITS",
    "FROZEN": "ON_HOLD",
    "DECIDED": "DECLINED",
}


def _project_case(item: Mapping[str, Any]) -> dict[str, Any]:
    """Only what the applicant is entitled to see.

    Never the agents' words, the facts the tools recorded, the tier or the rule that decided —
    R18.2's audience is auditors, not applicants — and never a reason that would say why an
    account is limited (`notifications.py`). Messages are passed through the tipping-off guard on
    every read, so a guard improvement applies to messages already stored.
    """
    state = _s(item.get("state")) or "UNKNOWN"
    out: dict[str, Any] = {
        "state": state,
        "status": _APPLICANT_STATES.get(state, "CHECKING"),
        "canContinue": state in CONTINUABLE_STATES,
    }
    phase1 = _json(item.get("phase1"))
    if state in CONTINUABLE_STATES:
        out.update(notifications.applicant_outcome(phase1))
    if "phase2Pending" in item:
        out["phase2Pending"] = _s(item.get("phase2Pending"))
    if "ingressLatencyMs" in item:
        n = item["ingressLatencyMs"]
        out["ingressLatencyMs"] = float(n["N"]) if "N" in n else None
    liveness = item.get("liveness", {}).get("M") or {}
    if liveness:
        out["liveness"] = {"result": _s(liveness.get("result"))}
    out["notifications"] = notifications.project(_plain_list(item.get("notifications")))
    # `resubmittedAt` too: after a step-up the attempt being decided started then, not at
    # `createdAt`, and the applicant's screen times the decision from whichever is later.
    for stamp in ("createdAt", "resubmittedAt", "provisionalDecidedAt", "fullDecidedAt"):
        if stamp in item:
            out[stamp] = _s(item.get(stamp))
    return out


def _json(attr: Any) -> dict[str, Any] | None:
    raw = attr.get("S") if isinstance(attr, Mapping) else None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _plain_list(attr: Any) -> list[dict[str, Any]]:
    """A DynamoDB list of flat string maps -> plain dicts."""
    if not isinstance(attr, Mapping) or not isinstance(attr.get("L"), list):
        return []
    out = []
    for entry in attr["L"]:
        fields = entry.get("M") if isinstance(entry, Mapping) else None
        if isinstance(fields, Mapping):
            out.append({k: _s(v) for k, v in fields.items()})
    return out


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Code", ""))
    return type(exc).__name__


def _s(attr: Any) -> str | None:
    if isinstance(attr, Mapping):
        return attr.get("S")
    return None


def applicant_id_from(event: Mapping[str, Any], body: Mapping[str, Any]) -> str:
    """`APP-{cognito sub}-{applicationId}`.

    The Cognito `sub` prefix is what makes this safe: it is taken from the verified JWT claims and
    never from the request, so a caller can only ever address a record inside their own namespace.
    A fully client-supplied `applicantId` would let any signed-in user write to another
    applicant's case — the same reason `review_handler.py` takes reviewer identity from claims.

    The `applicationId` suffix is client-supplied *by design*, and is the reason this is not just
    `APP-{sub}`. Two things need it:

    * **Correlating liveness with the case.** Liveness is captured and persisted (R33.5, F-40's
      3-minute window) *before* `POST /applicants` runs, so both calls must independently derive
      the same key. The browser generates one id at the start of the flow and passes it to both.
    * **Repeat applications.** Keyed on `sub` alone, a Cognito user could hold exactly one case
      ever, and every subsequent attempt would fail the conditional write. That makes the system
      untestable and is wrong on its own terms: a rejected applicant may legitimately reapply.

    Sanitised rather than trusted verbatim: the value lands in a DynamoDB key and an S3 key
    prefix, so anything outside `[A-Za-z0-9-]` is dropped instead of being carried into either.
    """
    claims = _claims(event)
    sub = str(claims.get("sub") or "")
    raw_application_id = str(body.get("applicationId") or "")
    application_id = "".join(
        c for c in raw_application_id if c.isalnum() or c == "-")[:36]

    if not sub:
        # Unauthenticated paths (direct invoke, tests) may still pass an explicit id.
        return str(body.get("applicantId") or "")
    if application_id:
        return f"APP-{sub}-{application_id}"
    return f"APP-{sub}"


def _claims(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return auth.claims(event)


def _body(event: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = event.get("body")
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    if isinstance(raw, Mapping):
        return raw
    return {}


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    route = (event.get("requestContext") or {}).get("routeKey", "")
    if route.startswith("POST /documents/presign"):
        return presign_document(event, context)
    if route.startswith("POST /applicants"):
        return create_applicant(event, context)
    if route.startswith("GET /cases/"):
        return get_case(event, context)
    return _response(404, {"error": "NOT_FOUND", "route": route})
