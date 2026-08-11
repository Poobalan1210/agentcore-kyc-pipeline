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
import uuid
from typing import Any, Mapping

LOG = logging.getLogger(__name__)
if not LOG.handlers:
    LOG.setLevel(logging.INFO)

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
    document_type = str(body.get("documentType") or "")
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
    "documents": [{"bucket": ..., "key": ..., "type": "PASSPORT", "issuingCountry": "US"}]}`.

    This is the applicant-facing half of task 7.1-7.3's ingress path — it writes the same case
    record shape `spike/live_run.py` seeds (jurisdiction, documents, gatewayArn) and enqueues on
    the real `kyc90-applicants.fifo` queue, so a submission through this endpoint takes the
    identical path already live-verified this session (`NormalizeInput` → `LoadCase` → ...).
    """
    body = _body(event)
    jurisdiction = str(body.get("jurisdiction") or "").lower()
    if jurisdiction not in _BUCKET_ENV_BY_JURISDICTION:
        return _response(400, {"error": "UNSUPPORTED_JURISDICTION", "jurisdiction": jurisdiction})

    applicant_id = applicant_id_from(event, body) or f"APP-{uuid.uuid4().hex[:12].upper()}"
    event_id = f"EVT-{uuid.uuid4().hex[:12].upper()}"
    documents = body.get("documents") or []
    if not isinstance(documents, list) or not documents:
        return _response(400, {"error": "NO_DOCUMENTS"})

    account = os.environ.get("AWS_ACCOUNT_ID", "")
    region = os.environ.get("AWS_REGION", "us-east-1")

    import boto3
    from datetime import datetime, timezone

    ddb = boto3.client("dynamodb")
    # `update_item`, not `put_item`, and the guard is on `caseId` rather than `applicantId`.
    #
    # The liveness result is written to this same item *before* this endpoint is ever called —
    # F-40 gives a 3-minute window in which the Rekognition session still exists, so
    # `liveness.py` persists immediately rather than waiting for the applicant to finish
    # uploading documents (R33.5). That write creates the item. A `put_item` would therefore
    # either overwrite the liveness evidence, or, with `attribute_not_exists(applicantId)`, fail
    # outright — which is exactly what happened: every case reached Phase 1 with no liveness
    # attribute and `CheckPreconditions` correctly emitted STEP_UP / LIVENESS_REQUIRED.
    #
    # `attribute_not_exists(caseId)` keeps the property that actually matters (R21.4: a retry must
    # not clobber an in-flight or decided case) because `caseId` is written here and nowhere else,
    # so its presence means "this case has already been intaken" — while still tolerating an item
    # that so far holds nothing but liveness evidence.
    ddb.update_item(
        TableName=os.environ["CASE_TABLE_NAME"],
        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
        UpdateExpression=(
            "SET caseId = :caseId, #state = :state, createdAt = :createdAt, "
            "jurisdiction = :jurisdiction, productApplied = :productApplied, "
            "policyEngineVersion = :policyEngineVersion, gatewayArn = :gatewayArn, "
            "documents = :documents, declaredIdentity = :declaredIdentity"
        ),
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={
            ":declaredIdentity": _declared_identity_attr(body.get("declaredIdentity")),
            ":caseId": {"S": f"CASE-{applicant_id}"},
            ":state": {"S": "RECEIVED"},
            ":createdAt": {"S": datetime.now(timezone.utc).isoformat()},
            ":jurisdiction": {"S": jurisdiction},
            ":productApplied": {"S": str(body.get("productApplied") or "current_account")},
            ":policyEngineVersion": {"S": "1"},
            ":gatewayArn": {"S": (
                f"arn:aws:bedrock-agentcore:{region}:{account}:"
                f"gateway/kycgateway-{jurisdiction}")},
            ":documents": {"L": [
                {"M": {
                    "type": {"S": str(d.get("type") or "")},
                    "key": {"S": str(d.get("key") or "")},
                    "bucket": {"S": str(d.get("bucket") or "")},
                    "issuingCountry": {"S": str(d.get("issuingCountry") or "")},
                }} for d in documents
            ]},
        },
        ConditionExpression="attribute_not_exists(caseId)",
    )

    sqs = boto3.client("sqs")
    sqs.send_message(
        QueueUrl=os.environ["APPLICANTS_QUEUE_URL"],
        MessageBody=json.dumps({"applicantId": applicant_id, "eventId": event_id}),
        MessageGroupId=applicant_id,
    )

    LOG.info("applicant enqueued applicantId=%s jurisdiction=%s", applicant_id, jurisdiction)
    return _response(202, {"applicantId": applicant_id, "eventId": event_id})


#: The declared-identity fields stored on the case record. Fixed list rather than "whatever the
#: client sent": every Phase 1 agent receives the whole case record as its prompt
#: (`asl.py` passes `$string($case)`), so an unbounded client-supplied map would let a caller
#: inject arbitrary text straight into an agent's context.
_DECLARED_FIELDS = ("givenNames", "surname", "dateOfBirth", "nationality", "address")

#: Ceiling per field, for the same reason. Long enough for a real compound name or address.
_DECLARED_MAX_LEN = 256


def _declared_identity_attr(declared: Any) -> dict[str, Any]:
    """The applicant's declared identity, as a DynamoDB map.

    This is the input `sanctions_exact_match` needs — it raises `MISSING_NAME` and refuses to run
    without a name, so before this existed the Phase 1 pre-screen had nothing to screen. It is
    also the left-hand side of the declared-vs-document comparison, which is a fraud control in
    its own right.

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
    """`GET /cases/{applicantId}`. Returns case state and phase progress, never raw agent
    prose — `state`, `phase1.outcome` (if present), `phase2Pending`, `ingressLatencyMs`."""
    path = event.get("pathParameters") or {}
    applicant_id = str(path.get("applicantId") or "")
    if not applicant_id:
        return _response(400, {"error": "MISSING_APPLICANT_ID"})

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


def _project_case(item: Mapping[str, Any]) -> dict[str, Any]:
    """Only the fields the applicant is entitled to see — never the raw phase1Evidence blob,
    which carries agent reasoning text (R18.2's audience is auditors, not applicants)."""
    out: dict[str, Any] = {"state": _s(item.get("state"))}
    phase1 = item.get("phase1", {}).get("S")
    if phase1:
        try:
            decoded = json.loads(phase1)
            out["outcome"] = decoded.get("outcome")
            out["reasonCode"] = decoded.get("reasonCode")
            out["requiredItem"] = decoded.get("requiredItem")
        except (json.JSONDecodeError, AttributeError):
            pass
    if "phase2Pending" in item:
        out["phase2Pending"] = _s(item.get("phase2Pending"))
    if "ingressLatencyMs" in item:
        n = item["ingressLatencyMs"]
        out["ingressLatencyMs"] = float(n["N"]) if "N" in n else None
    return out


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
    context = event.get("requestContext")
    if not isinstance(context, Mapping):
        return {}
    authorizer = context.get("authorizer")
    if not isinstance(authorizer, Mapping):
        return {}
    jwt = authorizer.get("jwt")
    if isinstance(jwt, Mapping) and isinstance(jwt.get("claims"), Mapping):
        return jwt["claims"]
    claims = authorizer.get("claims")
    if isinstance(claims, Mapping):
        return claims
    return {}


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
