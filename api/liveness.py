"""Liveness endpoints — task 14.2, R33.1, R33.2, R33.5, R33.6, F-40, F-41, F-44.

`POST /liveness/session` -> `CreateFaceLivenessSession`. `GET /liveness/{sessionId}/result` ->
`GetFaceLivenessSessionResults`, threshold applied here, boolean only returned.

Two properties this module exists to hold, both because getting them wrong is invisible until
the specific failure mode occurs:

* **A fresh SessionId every call, never a cached one** (F-41). There is deliberately no
  memoisation here — reusing a SessionId does not error loudly, it just fails the check, so the
  fix is "never build the capability to reuse one" rather than "remember not to".
* **The raw confidence score never leaves this module** (F-44, R33.2). `_is_live` returns a
  bool, and the boolean is the only field of the Rekognition response this module returns to
  the caller — reference/audit image keys are persisted to the case record, not echoed back.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Mapping

# Shared with `POST /applicants` on purpose: the liveness result and the case record must land on
# the same DynamoDB key, and two independent derivations of that key is precisely the bug that
# sent every liveness PASS to an orphan row. Both Lambdas are packaged from `api/`, so this is a
# flat-package import in the deployment package as well as locally.
from applicants import applicant_id_from  # noqa: E402

LOG = logging.getLogger(__name__)
if not LOG.handlers:
    LOG.setLevel(logging.INFO)

#: R33.4/F-27 — Face Liveness does not exist in every jurisdiction's home Region. This is the
#: single source of truth for which Region actually runs the Rekognition call per jurisdiction;
#: `infra/kyc/config.py`'s `Jurisdiction.liveness_region` is the CDK-time mirror of this table,
#: kept here too because the API Lambda must not import the CDK app to make a runtime decision.
LIVENESS_REGION_BY_JURISDICTION: dict[str, str] = {
    "us": "us-east-1",
    "uk": "eu-west-1",       # F-27: not available in London
    "au": "ap-southeast-5",  # F-27: not available in Sydney; nearest supported Region
    "jp": "ap-northeast-1",
}

#: R33.2 — the pass/fail line, applied server-side and never returned to the browser as a
#: number. Rekognition returns 0-100; this is the score confirmed live/CLOSED against F-44 in
#: the same style as the sanctions-screening thresholds (decision-engine/thresholds.py) rather
#: than invented per-call.
LIVE_CONFIDENCE_THRESHOLD = 80.0

#: D-36 — the two Face Liveness challenges, as `CreateFaceLivenessSession` names them.
#: `FaceMovementAndLightChallenge` (the service default) has the user move toward the camera and
#: then hold still while the screen flashes a colour sequence, and AWS recommends it for the
#: highest accuracy. `FaceMovementChallenge` drops the flashes and is several seconds faster.
LIGHT_CHALLENGE = "FaceMovementAndLightChallenge"
MOVEMENT_CHALLENGE = "FaceMovementChallenge"
LIVENESS_CHALLENGES = (LIGHT_CHALLENGE, MOVEMENT_CHALLENGE)

#: F-40 — a SessionId is unusable 3 minutes after CreateFaceLivenessSession. The Client App is
#: expected to complete steps 3-7 inside this window; the backend does not enforce it directly
#: (Rekognition itself returns SessionNotFoundException past expiry) but names the distinct
#: error so the state machine routes to STEP_UP rather than REJECT (R33.6).
SESSION_EXPIRED_ERRORS = ("SessionNotFoundException", "InvalidParameterException")


def _clients(region: str) -> Any:
    import boto3

    return boto3.client("rekognition", region_name=region)


def _ddb():
    import boto3

    return boto3.client("dynamodb")


def _challenge() -> str:
    """The challenge this deployment asks for (`LIVENESS_CHALLENGE`, set by the stack).

    Anything unset or unrecognised gets the service default, the light challenge: a typo must not
    quietly select the less accurate check.
    """
    configured = os.environ.get("LIVENESS_CHALLENGE", "")
    if configured in LIVENESS_CHALLENGES:
        return configured
    if configured:
        LOG.warning("unknown LIVENESS_CHALLENGE %r; using %s", configured, LIGHT_CHALLENGE)
    return LIGHT_CHALLENGE


#: The case states in which liveness evidence may still be written: before the case is submitted
#: (no state yet) and while the applicant has been asked for more. Once a case is submitted the
#: evidence it is decided on must not change underneath it.
ACCEPTING_STATE = "AWAITING_APPLICANT"
_ACCEPTING_CONDITION = "(attribute_not_exists(#s) OR #s = :accepting)"


def create_session(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`POST /liveness/session`. Body: `{"jurisdiction": "us", "applicationId": "..."}`.

    No caching, no reuse — every call creates and returns a brand new SessionId (F-41). The
    KMS key, if the jurisdiction has one already used for evidence, encrypts the reference and
    audit images Rekognition stores for the life of the session.

    The session is bound to the caller's case as it is created. Only a bound session's result can
    be written to that case, and only once, so a liveness PASS cannot be carried from one case to
    another or replayed onto a case that has since failed.
    """
    body = _body(event)
    jurisdiction = str(body.get("jurisdiction") or "").lower()
    region = LIVENESS_REGION_BY_JURISDICTION.get(jurisdiction)
    if region is None:
        return _response(400, {"error": "UNSUPPORTED_JURISDICTION", "jurisdiction": jurisdiction})
    applicant_id = applicant_id_from(event, body)
    if not applicant_id:
        return _response(400, {"error": "MISSING_APPLICATION_ID"})

    kms_key_id = os.environ.get(f"LIVENESS_KMS_KEY_{jurisdiction.upper()}") or None
    challenge = _challenge()
    settings: dict[str, Any] = {
        "AuditImagesLimit": 1,
        # Always sent, so the session's challenge is this deployment's choice rather than
        # whatever the service default happens to be. The browser component reads the challenge
        # from the session itself, so nothing client-side has to agree with this.
        "ChallengePreferences": [{"Type": challenge}],
    }

    # `GetFaceLivenessSessionResults` only returns `ReferenceImage.S3Object`/`AuditImages[].
    # S3Object` when this session was created with `Settings.OutputConfig.S3Bucket` set — without
    # it, the images exist only inside Rekognition's own service-managed storage for the life of
    # the session and `_persist_image` (in `get_result`) has nothing to copy, so
    # `referenceImageKey` ends up empty. That in turn is why the Identity agent reports "no
    # liveness reference frame to compare against" even on a genuinely successful liveness pass —
    # a real gap this closes, found live.
    #
    # AWS requires the OutputConfig bucket to be in the *same region* as the Face Liveness
    # endpoint being called (`region`, from `LIVENESS_REGION_BY_JURISDICTION`), and the biometric
    # bucket is provisioned once, in `PRIMARY_REGION` (`us-east-1`). That covers the `us`
    # jurisdiction, whose liveness endpoint already is `us-east-1`. UK/AU/JP route to eu-west-1/
    # ap-southeast-5/ap-northeast-1, where no biometric bucket currently exists, so OutputConfig
    # is only set when the two regions actually match — setting a cross-region bucket would be
    # rejected by the API outright, and this is more honest than pretending to fix jurisdictions
    # it cannot fix without a regional bucket per jurisdiction (a larger infra change, not a
    # bug-fix-sized one).
    biometric_bucket = os.environ.get("BIOMETRIC_BUCKET_NAME")
    biometric_bucket_region = os.environ.get("BIOMETRIC_BUCKET_REGION", "us-east-1")
    if biometric_bucket and region == biometric_bucket_region:
        settings["OutputConfig"] = {
            "S3Bucket": biometric_bucket,
            "S3KeyPrefix": "liveness-sessions",
        }

    kwargs: dict[str, Any] = {"Settings": settings}
    if kms_key_id:
        kwargs["KmsKeyId"] = kms_key_id

    client = _clients(region)
    resp = client.create_face_liveness_session(**kwargs)
    session_id = resp["SessionId"]

    try:
        _ddb().update_item(
            TableName=os.environ["CASE_TABLE_NAME"],
            Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
            UpdateExpression=(
                "SET pendingLivenessSessionId = :sid, pendingLivenessRegion = :region, "
                "pendingLivenessCreatedAt = :t"),
            ConditionExpression=_ACCEPTING_CONDITION,
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":sid": {"S": session_id}, ":region": {"S": region}, ":t": {"S": _now_iso()},
                ":accepting": {"S": ACCEPTING_STATE}},
        )
    except Exception as exc:  # noqa: BLE001 - narrowed immediately by error code
        if _error_code(exc) != "ConditionalCheckFailedException":
            raise
        # The unused session expires on its own within three minutes (F-40).
        return _response(409, {"error": "CASE_NOT_ACCEPTING_LIVENESS"})

    LOG.info("liveness session created jurisdiction=%s region=%s challenge=%s",
             jurisdiction, region, challenge)
    # The challenge is returned only so the page can say what to expect (flashing colours, or
    # none); the browser component takes the challenge it runs from the session.
    return _response(200, {"sessionId": session_id, "region": region, "challenge": challenge})


def get_result(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /liveness/{sessionId}/result?jurisdiction=us&applicationId=...`.

    Persists the boolean result (and the Region actually used, R33.4) to the case record
    **immediately** — F-40's 3-minute window means the reference/audit images and the raw
    session are gone by the time anyone might come back to re-derive this, so this call is the
    only chance to record it (R33.5).

    The write lands on the same item `POST /applicants` will later fill in, which is why that
    endpoint uses a conditional `update_item` rather than a `put_item`: this call legitimately
    creates the item first, holding nothing but liveness evidence.
    """
    path = event.get("pathParameters") or {}
    session_id = str(path.get("sessionId") or "")
    query = event.get("queryStringParameters") or {}
    jurisdiction = str(query.get("jurisdiction") or "").lower()
    # Identity is derived from the verified JWT claims, composed with the caller's
    # `applicationId` — never taken as a client-supplied `applicantId`. An earlier version read
    # `applicantId` straight from the query string, which meant any signed-in user could write a
    # liveness PASS onto *another* applicant's case: forging the strongest single piece of
    # identity evidence in the system. Sharing `applicant_id_from` with `applicants.py` also
    # guarantees both endpoints derive byte-identical keys, which is what makes the liveness
    # result and the case record meet at all.
    applicant_id = applicant_id_from(event, {
        "applicationId": query.get("applicationId"),
        "applicantId": query.get("applicantId"),
    })

    region = LIVENESS_REGION_BY_JURISDICTION.get(jurisdiction)
    if region is None or not session_id or not applicant_id:
        return _response(400, {"error": "MISSING_PARAMETERS"})

    # Checked before anything is copied or written. A retry of a result already recorded is
    # answered from the record; a session this case did not create is refused.
    current = _ddb().get_item(
        TableName=os.environ["CASE_TABLE_NAME"],
        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
        ConsistentRead=True,
    ).get("Item") or {}
    recorded = _recorded_result(current, session_id)
    if recorded is not None:
        return _response(200, {"isLive": recorded})
    if (current.get("pendingLivenessSessionId") or {}).get("S") != session_id:
        return _response(409, {"error": "SESSION_NOT_BOUND"})

    client = _clients(region)
    try:
        resp = client.get_face_liveness_session_results(SessionId=session_id)
    except Exception as exc:  # noqa: BLE001
        code = _error_code(exc)
        if code in SESSION_EXPIRED_ERRORS:
            # R33.6 — distinct error, and the case must emit STEP_UP, never REJECT. Persisting
            # nothing here is deliberate: there is nothing valid to persist, and a case with no
            # liveness result already fails CheckPreconditions to STEP_UP (asl.py).
            return _response(410, {"error": "SESSION_EXPIRED"})
        raise

    confidence = float(resp.get("Confidence", 0.0))
    # F-44 / R33.2 — the score is used here and discarded. It is never assigned to a variable
    # that survives past this line into the response body.
    is_live = confidence >= LIVE_CONFIDENCE_THRESHOLD

    reference_image = resp.get("ReferenceImage") or {}
    audit_images = resp.get("AuditImages") or []
    ref_key = _persist_image(
        reference_image, jurisdiction=jurisdiction, applicant_id=applicant_id, kind="reference")
    audit_keys = [
        _persist_image(img, jurisdiction=jurisdiction, applicant_id=applicant_id,
                       kind=f"audit-{i}")
        for i, img in enumerate(audit_images)
    ]

    try:
        _persist_liveness_result(
            applicant_id=applicant_id, session_id=session_id, is_live=is_live, region=region,
            reference_image_key=ref_key, audit_image_keys=[k for k in audit_keys if k],
        )
    except Exception as exc:  # noqa: BLE001 - narrowed immediately by error code
        if _error_code(exc) != "ConditionalCheckFailedException":
            raise
        # A concurrent retry won the write, or the case was submitted in between.
        again = _ddb().get_item(
            TableName=os.environ["CASE_TABLE_NAME"],
            Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
            ConsistentRead=True,
        ).get("Item") or {}
        recorded = _recorded_result(again, session_id)
        if recorded is not None:
            return _response(200, {"isLive": recorded})
        return _response(409, {"error": "CASE_NOT_ACCEPTING_LIVENESS"})

    LOG.info("liveness result persisted applicantId=%s isLive=%s region=%s",
             applicant_id, is_live, region)
    return _response(200, {"isLive": is_live})


def _recorded_result(item: Mapping[str, Any], session_id: str) -> bool | None:
    """The result already recorded for this session, if this session's result is on the case."""
    liveness = (item.get("liveness") or {}).get("M") or {}
    if (liveness.get("sessionId") or {}).get("S") != session_id:
        return None
    return (liveness.get("result") or {}).get("S") == "PASS"


def _persist_image(image: Mapping[str, Any], *, jurisdiction: str, applicant_id: str,
                   kind: str) -> str | None:
    """Write the reference/audit image to the demo biometric bucket (R36, task 14.8) — not the
    Object-Lock evidence bucket. Object Lock in COMPLIANCE mode forbids deletion for years; a
    real human face must remain deletable within 24 hours (R36.1-R36.3)."""
    s3_bucket = image.get("S3Object")
    if not isinstance(s3_bucket, Mapping):
        return None
    bucket_name = os.environ.get("BIOMETRIC_BUCKET_NAME")
    if not bucket_name:
        return None
    import boto3

    s3 = boto3.client("s3")
    key = f"{applicant_id}/{kind}-{uuid.uuid4().hex[:8]}.jpg"
    src_bucket = s3_bucket.get("Bucket")
    src_key = s3_bucket.get("Name")
    if not src_bucket or not src_key:
        return None
    # Rekognition stores the image in its own service bucket; copy it into ours so the 24-hour
    # lifecycle rule (task 14.8) governs it, rather than depending on Rekognition's own
    # retention which is scoped to the session window, not a documented deletion SLA.
    s3.copy_object(
        Bucket=bucket_name, Key=key,
        CopySource={"Bucket": src_bucket, "Key": src_key},
    )
    return key


def _persist_liveness_result(*, applicant_id: str, session_id: str, is_live: bool, region: str,
                             reference_image_key: str | None,
                             audit_image_keys: list[str]) -> None:
    """Record the result and consume the session, in one conditional write.

    A failure is counted (`livenessFailures`), because R30.2 routes the third one to a reviewer
    with an alternative path instead of asking again — and a count that could be reset by
    re-running a session would make that limit decorative.
    """
    values: dict[str, Any] = {
        ":l": {"M": {
            "result": {"S": "PASS" if is_live else "FAILED"},
            "sessionId": {"S": session_id},
            "region": {"S": region},
            "retrievedAt": {"S": _now_iso()},
            "referenceImageKey": {"S": reference_image_key or ""},
            "auditImageKeys": {"L": [{"S": k} for k in audit_image_keys]},
        }},
        ":sid": {"S": session_id},
        ":accepting": {"S": ACCEPTING_STATE},
    }
    update = "SET liveness = :l REMOVE pendingLivenessSessionId"
    if not is_live:
        update += " ADD livenessFailures :one"
        values[":one"] = {"N": "1"}
    _ddb().update_item(
        TableName=os.environ["CASE_TABLE_NAME"],
        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
        UpdateExpression=update,
        ConditionExpression=f"pendingLivenessSessionId = :sid AND {_ACCEPTING_CONDITION}",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues=values,
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _body(event: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = event.get("body")
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    if isinstance(raw, Mapping):
        return raw
    return {}


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Code", ""))
    return type(exc).__name__


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Routed by HTTP API method/path (see `api_stack.py`)."""
    route = (event.get("requestContext") or {}).get("routeKey", "")
    if route.startswith("POST /liveness/session"):
        return create_session(event, context)
    if route.startswith("GET /liveness/"):
        return get_result(event, context)
    return _response(404, {"error": "NOT_FOUND", "route": route})
