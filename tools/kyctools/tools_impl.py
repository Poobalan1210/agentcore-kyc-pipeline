"""The seven tools — tasks 2.2, 2.3, 2.5.

Phase 1, latency-critical, provisioned concurrency:
    s3_get_document, rekognition_compare_faces, velocity_check, sanctions_exact_match

Phase 2, accuracy-critical, no provisioned concurrency:
    sanctions_fuzzy, pep_lookup, adverse_media

**There is no liveness tool, and its absence is a design decision, not an omission.** A
liveness SessionId expires 180 seconds after issue and takes every associated artefact with it
(F-40). A tool called by an agent several seconds into a pipeline cannot be relied on to land
inside that window, so the Public API fetches the result the moment `onAnalysisComplete` fires
and persists it to the case (R33.5). Agents read a persisted boolean. Making it a tool would
introduce a race whose failure mode is losing the evidence permanently.

Every screening tool fails closed (R25.7): if the source cannot be consulted the result is
UNAVAILABLE, never "no match".
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Mapping

from .contract import HitClassification, ToolError, ToolResult, _now, ok, unavailable, invalid
from .data.sanctions import (
    ALL_ENTRIES,
    EXACT_MATCH_SEEDS,
    FUZZY_SEEDS,
    FALSE_POSITIVE_DECOYS,
    LIST_SOURCE,
    LIST_VERSION,
    MEDIA_ARTICLES,
    PEP_ENTRIES,
    normalise,
    tokens,
)
from .matching import Candidate, disambiguate_media, exact_match, fuzzy_match
from .redact import get_logger, log

LOG = get_logger()

#: Task 2.6. Off by default; the demo profile turns it on so the throttle and rate-limit
#: failure demos are reproducible on stage rather than dependent on luck.
DEMO_LATENCY_MS = int(os.environ.get("DEMO_LATENCY_MS", "0"))
DEMO_PROFILE = os.environ.get("DEMO_PROFILE", "off").lower() == "on"

#: Fault injection for the fail-closed tests (R25.7 / AC-12). Comma-separated tool names.
FORCE_UNAVAILABLE = {
    t.strip() for t in os.environ.get("FORCE_UNAVAILABLE", "").split(",") if t.strip()}


def _inject_latency(tool: str) -> None:
    if DEMO_PROFILE and DEMO_LATENCY_MS > 0:
        time.sleep(DEMO_LATENCY_MS / 1000.0)
        log(LOG, logging.INFO, "demo latency injected", tool=tool,
            duration_ms=DEMO_LATENCY_MS)


def _guard(tool: str) -> None:
    if tool in FORCE_UNAVAILABLE:
        raise ToolError("fault injection: %s forced unavailable" % tool,
                        "FORCED_UNAVAILABLE")


def _candidate(args: dict[str, Any]) -> Candidate:
    name = (args.get("name") or "").strip()
    if not name:
        raise ToolError("name is required", "MISSING_NAME")
    alt = args.get("alternateNames") or args.get("alternate_names") or []
    if isinstance(alt, str):
        alt = [alt]
    return Candidate(
        name=name,
        dob=(args.get("dob") or None),
        country=(args.get("country") or None),
        alternate_names=tuple(a for a in alt if a),
    )


# ------------------------------------------------------------------ Phase 1 tools
def s3_get_document(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """Read an uploaded document from the jurisdiction bucket.

    Pre-model validation (R23.1 / F-23) happens here as well as client-side, because the
    client is untrusted. Nova rejects CMYK, SVG-in-PDF, JPEG2000 and JBIG2, and a rejection
    at the model boundary is a wasted invocation with a confusing error.
    """
    _guard("s3_get_document")
    _inject_latency("s3_get_document")
    bucket = args.get("bucket")
    key = args.get("key")
    if not bucket or not key:
        return invalid("s3_get_document", "bucket and key are required", "MISSING_LOCATION")

    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey", "NotFound"):
            return invalid("s3_get_document", "document not found", "DOCUMENT_NOT_FOUND")
        raise ToolError("S3 unreachable: %s" % code, "S3_UNAVAILABLE") from e

    size = head["ContentLength"]
    ctype = head.get("ContentType", "application/octet-stream")

    MAX_BYTES = 25 * 1024 * 1024          # F-23 direct-upload ceiling
    BANNED = ("image/jp2", "image/jpx", "image/jbig2", "image/svg+xml")
    if size > MAX_BYTES:
        return invalid("s3_get_document",
                       "document exceeds the 25 MB direct limit", "DOCUMENT_TOO_LARGE")
    if ctype in BANNED:
        return invalid("s3_get_document",
                       "unsupported format %s (F-23)" % ctype, "UNSUPPORTED_FORMAT")

    # S3 user metadata, which `head_object` already returned at no extra cost. This is the channel
    # the upload path stamps validated document attributes onto — issuing country, expiry, MRZ
    # validity — so the agent reads extracted fields rather than inferring them from a filename.
    #
    # Added after a live run showed the Document agent correctly but uselessly reporting every
    # field "not available in the document metadata": it had size and content type and nothing
    # else, so it could not evaluate expiry (R28.1) or unsupported issuer (R28.2) at all. An agent
    # asked to judge conditions it has no inputs for will either abstain or invent, and abstaining
    # was the good outcome.
    #
    # Keys arrive lowercased by S3. `synthetic` marks fixture objects so the audit record can never
    # confuse a demo document for a real one.
    metadata = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}

    data: dict[str, Any] = {
        "bucket": bucket, "key": key, "sizeBytes": size, "contentType": ctype,
        "documentType": args.get("documentType", metadata.get("documenttype", "UNKNOWN")),
        "metadata": metadata,
        "synthetic": metadata.get("synthetic") == "true",
    }
    extraction = _extract_document_fields(
        s3, bucket=bucket, key=key, content_type=ctype, size_bytes=size)
    data.update(extraction)

    # Persisted straight to the case record, deliberately bypassing the model.
    #
    # The MRZ check digits make this the one part of document extraction that is *verified* rather
    # than merely trusted, and the decision engine compares it against the applicant's declared
    # details. Routing that through the agent's own finding would turn a checkable fact into a
    # paraphrase — and the whole reason the output contracts exist (R11.13, F-89) is that prose is
    # not a data interface.
    #
    # Best effort: `applicantId` is not required by this tool's schema, so an agent that omits it
    # still gets its extraction back. The comparison then reports `performed: False`, which is
    # visible in the audit record rather than silently absent.
    applicant_id = args.get("applicantId") or (ctx or {}).get("applicantId")
    if applicant_id:
        _persist_document_extraction(str(applicant_id), extraction)

    return ok("s3_get_document", data, source="s3")


#: Case-record attribute for the verified extraction. A flat attribute for the same reason
#: `FINDING_ATTRIBUTES` are flat: a nested `SET` into a map that may not exist yet is rejected by
#: DynamoDB with overlapping document paths, and it fails at invoke time where the agent reports it
#: as an unhandled error and the case degrades to review.
DOCUMENT_EXTRACTION_ATTRIBUTE = "documentExtraction"


def _persist_document_extraction(applicant_id: str, extraction: dict[str, Any]) -> None:
    """Write the verified extraction to the case record.

    Non-fatal by design, unlike `_persist_finding`. A finding that cannot be recorded means an agent
    result is lost, so that raises. Here the extraction has already been returned to the agent and
    the pipeline can still reach a decision without the cross-check; failing the tool call would
    turn a degraded control into a failed document read, which is strictly worse.
    """
    table_name = os.environ.get("CASE_TABLE")
    if not table_name:
        return

    import json

    import boto3

    ddb = boto3.client("dynamodb")
    try:
        ddb.update_item(
            TableName=table_name,
            Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
            UpdateExpression="SET #e = :e, updatedAt = :t",
            ExpressionAttributeNames={"#e": DOCUMENT_EXTRACTION_ATTRIBUTE},
            ExpressionAttributeValues={
                # Canonical JSON so a retried call writes byte-identical content, which makes the
                # write idempotent rather than merely repeatable.
                ":e": {"S": json.dumps(extraction, sort_keys=True)},
                ":t": {"S": _now()},
            },
        )
    except Exception:  # noqa: BLE001 - see the docstring: degraded control, not a failed read
        LOG.warning("could not persist document extraction for %s", applicant_id)


#: Textract's synchronous limit for inline bytes. A larger object would be rejected by the service,
#: so the call is skipped rather than made and failed.
_TEXTRACT_MAX_BYTES = 10 * 1024 * 1024

#: Formats Textract's synchronous text detection accepts as inline bytes. PDF is *not* among them,
#: which is why the demo documents are generated as JPEG as well: a PDF-only document can be
#: neither read nor face-matched, so it produces no identity evidence at all.
_TEXTRACT_CONTENT_TYPES = ("image/jpeg", "image/png")

#: OCR lines returned to the agent. Capped because the full page text is not evidence the agent
#: needs, and an unbounded blob of it is both a prompt-size risk and a place where text printed on
#: the document could read as an instruction to the model.
_MAX_TEXT_LINES = 60


def _extract_document_fields(s3, *, bucket: str, key: str, content_type: str,
                             size_bytes: int) -> dict[str, Any]:
    """Read the document and report what it actually says.

    Before this existed the Documents agent received only a size and a content type, and a live run
    showed it correctly but uselessly reporting every field "not available in the document
    metadata" — it had no input from which to judge expiry (R28.1) or an unsupported issuer
    (R28.2), so it abstained. Abstaining was the good outcome; inventing values would have been the
    bad one.

    Two layers, answering different questions:

    * **OCR text** — what a human would read off the document.
    * **The machine-readable zone**, parsed and check-digit verified by `kyctools.mrz`. This is the
      only part of a document whose extraction can be *verified* rather than merely trusted: an
      altered printed date reads back as a plausible date, while an altered MRZ date fails its
      check digit arithmetically.

    Extraction failures are deliberately **not** fatal, and the distinction is the whole design of
    this function. A document Textract cannot process is a finding *about the document* — wrong
    format, too large, unreadable — and is returned as `documentReadable: False` with a reason, so
    the decision engine and the reviewer can weigh it. A Textract *outage* is a different thing
    entirely and raises `ToolError`, because reporting "nothing on the document" when the source
    was never consulted is the same fail-open mistake R25.7 forbids for screening.
    """
    if content_type not in _TEXTRACT_CONTENT_TYPES:
        return {
            "documentReadable": False,
            "unreadableReason": (
                f"{content_type} cannot be read directly; a JPEG or PNG is required "
                "(synchronous text detection does not accept PDF)"),
        }
    if size_bytes > _TEXTRACT_MAX_BYTES:
        return {"documentReadable": False,
                "unreadableReason": "document exceeds the 10 MB synchronous extraction limit"}

    import boto3

    from .mrz import parse_from_lines

    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 - a fetch failure is reported, never raised blind
        return {"documentReadable": False,
                "unreadableReason": f"document could not be fetched ({type(exc).__name__})"}

    textract = boto3.client("textract")
    try:
        blocks = textract.detect_document_text(Document={"Bytes": body})["Blocks"]
    except Exception as exc:  # noqa: BLE001 - narrowed immediately by error code
        response = getattr(exc, "response", None)
        code = type(exc).__name__
        if isinstance(response, Mapping):
            code = str((response.get("Error") or {}).get("Code") or code)
        if code in ("UnsupportedDocumentException", "BadDocumentException",
                    "DocumentTooLargeException", "InvalidParameterException"):
            return {"documentReadable": False,
                    "unreadableReason": f"text extraction rejected the document ({code})"}
        raise ToolError(f"Textract unavailable: {code}", "TEXTRACT_UNAVAILABLE") from exc

    lines = [b["Text"] for b in blocks if b.get("BlockType") == "LINE"]
    extracted: dict[str, Any] = {
        "documentReadable": True,
        "lineCount": len(lines),
        "textLines": lines[:_MAX_TEXT_LINES],
    }

    parsed = parse_from_lines(lines)
    extracted["mrzPresent"] = parsed is not None
    extracted["mrz"] = parsed.to_dict() if parsed is not None else None
    return extracted


def rekognition_compare_faces(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """Compare the document portrait against the liveness reference frame.

    The similarity threshold is applied here, server side, and only a boolean plus a bucketed
    band crosses back. F-44 makes the same point about liveness: the caller decides, not the
    service, and the raw score is not the agent's business.
    """
    _guard("rekognition_compare_faces")
    _inject_latency("rekognition_compare_faces")
    src = args.get("sourceKey")
    tgt = args.get("targetKey")

    # One bucket per image, because the two images deliberately live in different buckets.
    #
    # The document sits in the jurisdiction evidence bucket (Object Lock, seven years). The liveness
    # reference frame sits in the biometric bucket (no Object Lock, 24-hour lifecycle) precisely
    # because a real human face must remain deletable (R36.1-R36.3). An earlier version accepted a
    # single `bucket` for both, which made this comparison impossible to perform at all: it could
    # only ever compare two objects in the same bucket, and no such pair exists in this system.
    # Rekognition's own API has always allowed an independent `S3Object` per image — the limitation
    # was ours, and it left the strongest identity control inert.
    #
    # `bucket` is still honoured as a fallback for both sides so an existing caller keeps working.
    fallback_bucket = args.get("bucket")
    source_bucket = args.get("sourceBucket") or fallback_bucket
    # Defaulted from the environment rather than demanded from the agent. A model that guessed the
    # wrong bucket would get an unreadable-image finding, which reads as a bad document rather than
    # as a misrouted lookup — a misleading diagnosis for an operator to chase.
    target_bucket = (args.get("targetBucket") or fallback_bucket
                     or os.environ.get("BIOMETRIC_BUCKET"))

    if not (src and tgt and source_bucket and target_bucket):
        return invalid(
            "rekognition_compare_faces",
            "sourceKey, targetKey and a bucket for each image are required",
            "MISSING_LOCATION")

    threshold = float(args.get("threshold", 90.0))

    import boto3
    from botocore.exceptions import ClientError

    rek = boto3.client("rekognition")
    try:
        resp = rek.compare_faces(
            SourceImage={"S3Object": {"Bucket": source_bucket, "Name": src}},
            TargetImage={"S3Object": {"Bucket": target_bucket, "Name": tgt}},
            SimilarityThreshold=threshold,
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "InvalidParameterException":
            # Rekognition raises this when it finds no face at all, which is a real finding
            # about the document, not a transport failure.
            return ok("rekognition_compare_faces",
                      {"match": False, "faceDetected": False,
                       "reason": "no detectable face in one of the images"},
                      source="rekognition")
        if code in ("InvalidS3ObjectException", "InvalidImageFormatException",
                    "ImageTooLargeException"):
            # A missing or unreadable image is a precondition failure, not an outage. The
            # distinction is operational, not pedantic: UNAVAILABLE is retryable and INVALID
            # is not, so mislabelling this makes the pipeline retry a permanent failure until
            # it exhausts its budget.
            return invalid("rekognition_compare_faces",
                           "image missing or unreadable (%s)" % code,
                           "IMAGE_UNREADABLE")
        raise ToolError("Rekognition unreachable: %s" % code,
                        "REKOGNITION_UNAVAILABLE") from e

    matches = resp.get("FaceMatches", [])
    best = max((m["Similarity"] for m in matches), default=0.0)
    return ok("rekognition_compare_faces",
              {"match": bool(matches),
               "faceDetected": True,
               # Bucketed, not raw. Enough for a reviewer, not a score to re-threshold on.
               "similarityBand": ("HIGH" if best >= 95 else
                                  "MEDIUM" if best >= threshold else "LOW"),
               "unmatchedFaces": len(resp.get("UnmatchedFaces", []))},
              source="rekognition")


def velocity_check(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """Device and IP velocity. R29.6 synthetic-identity signal.

    Returns signals, never a verdict. Whether a signal is disqualifying is a decision-engine
    question (§8.4), and putting that judgement in a tool would move a control out of the pure
    layer where it can be tested deterministically.
    """
    _guard("velocity_check")
    _inject_latency("velocity_check")
    device_id = args.get("deviceId")
    ip = args.get("ipAddress")
    if not (device_id or ip):
        return invalid("velocity_check", "deviceId or ipAddress is required",
                       "MISSING_IDENTIFIER")

    table_name = os.environ.get("VELOCITY_TABLE")
    if not table_name:
        # No table configured is a configuration fault, not "no velocity observed".
        raise ToolError("VELOCITY_TABLE is not configured", "NOT_CONFIGURED")

    import boto3
    from botocore.exceptions import ClientError

    ddb = boto3.client("dynamodb")
    counts: dict[str, int] = {}
    try:
        for label, value in (("device", device_id), ("ip", ip)):
            if not value:
                continue
            r = ddb.query(
                TableName=table_name,
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={":pk": {"S": f"{label}#{value}"}},
                Select="COUNT",
            )
            counts[label] = int(r.get("Count", 0))
    except ClientError as e:
        raise ToolError("velocity store unreachable: %s" % e.response["Error"]["Code"],
                        "VELOCITY_UNAVAILABLE") from e

    device_n = counts.get("device", 0)
    ip_n = counts.get("ip", 0)
    return ok("velocity_check",
              {"deviceApplications": device_n,
               "ipApplications": ip_n,
               "signals": {
                   "criticalDeviceReuse": device_n >= 5,
                   "elevatedIpReuse": 3 <= ip_n < 10,
                   "criticalIpReuse": ip_n >= 10,
               }},
              source="velocity-store")


def sanctions_exact_match(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """Phase 1 pre-screen. Exact match only (R11.2), fail closed (R25.7).

    Deliberately cannot find the fuzzy cases. Phase 1 is a blocking gate on a 90-second
    budget, and a confirmed exact hit is enough to stop a provisional account (R4.3). Anything
    subtler is Phase 2's job, where there is no clock pressure.
    """
    _guard("sanctions_exact_match")
    _inject_latency("sanctions_exact_match")
    try:
        candidate = _candidate(args)
    except ToolError as e:
        return invalid("sanctions_exact_match", e.reason, e.reason_code)

    hits = exact_match(candidate, ALL_ENTRIES)
    return ok("sanctions_exact_match",
              {"matched": bool(hits),
               "hitCount": len(hits),
               "hits": [
                   {"entryId": h.entry_id, "classification": h.classification.value,
                    "programme": h.programme, "reason": h.reason,
                    "dobMatch": h.dob_match, "countryMatch": h.country_match}
                   for h in hits],
               "syntheticData": True},
              source=LIST_SOURCE, list_version=LIST_VERSION)


# ------------------------------------------------------------------ Phase 2 tools
def sanctions_fuzzy(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """R27.1-R27.4. Transliteration, diacritics, name order, married/maiden."""
    _guard("sanctions_fuzzy")
    _inject_latency("sanctions_fuzzy")
    try:
        candidate = _candidate(args)
    except ToolError as e:
        return invalid("sanctions_fuzzy", e.reason, e.reason_code)

    hits = [h for h in fuzzy_match(candidate, ALL_ENTRIES)
            if h.classification is not HitClassification.NO_MATCH]
    excluded = [h for h in fuzzy_match(candidate, ALL_ENTRIES)
                if h.classification is HitClassification.NO_MATCH]

    return ok("sanctions_fuzzy",
              {"matched": bool(hits),
               "hitCount": len(hits),
               "hits": [
                   {"entryId": h.entry_id, "matchedName": h.matched_name,
                    "classification": h.classification.value, "score": h.score,
                    "programme": h.programme, "reason": h.reason,
                    "dobMatch": h.dob_match, "nameReordered": h.name_reordered}
                   for h in hits],
               # Exclusions are reported, not hidden. A reviewer seeing "we looked at this and
               # ruled it out, here is why" is being given evidence; silence is not.
               "excluded": [
                   {"entryId": h.entry_id, "reason": h.reason} for h in excluded],
               "syntheticData": True},
              source=LIST_SOURCE, list_version=LIST_VERSION)


def pep_lookup(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """R27.6. PEP by association — spouse, child, business partner — with the relationship."""
    _guard("pep_lookup")
    _inject_latency("pep_lookup")
    try:
        candidate = _candidate(args)
    except ToolError as e:
        return invalid("pep_lookup", e.reason, e.reason_code)

    ct = tokens(candidate.name)
    found = []
    for p in PEP_ENTRIES:
        pt = tokens(p.name)
        if not pt or not ct:
            continue
        overlap = len(ct & pt) / max(len(ct), len(pt))
        if overlap < 0.6:
            continue
        dob_match = bool(candidate.dob and p.dob and candidate.dob == p.dob)
        if not dob_match and normalise(candidate.name) != normalise(p.name):
            continue
        found.append({
            "entryId": p.entry_id,
            "position": p.position,
            "relationship": p.relationship,
            "relatedTo": p.related_to,
            "dobMatch": dob_match,
            # R4: SELF and ASSOCIATE both drive HIGH, but the tier rule needs to know which.
            "pepStatus": "SELF" if p.relationship == "SELF" else "ASSOCIATE",
        })

    status = "NONE"
    if any(f["pepStatus"] == "SELF" for f in found):
        status = "SELF"
    elif found:
        status = "ASSOCIATE"

    return ok("pep_lookup",
              {"pepStatus": status, "hitCount": len(found), "hits": found,
               "syntheticData": True},
              source="synthetic-pep-list", list_version=LIST_VERSION)


def adverse_media(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """R27.7. Entity disambiguation — a name collision is not a match."""
    _guard("adverse_media")
    _inject_latency("adverse_media")
    try:
        candidate = _candidate(args)
    except ToolError as e:
        return invalid("adverse_media", e.reason, e.reason_code)

    attributed, rejected = [], []
    for a in MEDIA_ARTICLES:
        is_match, reason = disambiguate_media(
            candidate, a.subject_name or "", a.subject_dob, a.subject_country)
        record = {"articleId": a.article_id, "headline": a.headline,
                  "severity": a.severity, "reason": reason}
        (attributed if is_match else rejected).append(record)

    severity_rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    worst_sev = max((severity_rank[r["severity"]] for r in attributed), default=0)
    return ok("adverse_media",
              {"matched": bool(attributed),
               "hitCount": len(attributed),
               "maxSeverity": {0: "NONE", 1: "LOW", 2: "MEDIUM", 3: "HIGH"}[worst_sev],
               "attributed": attributed,
               "rejectedCollisions": rejected,
               "syntheticData": True},
              source="synthetic-media-index", list_version=LIST_VERSION)


# --- output contracts (R11.13, F-89) ---------------------------------------------------------
#
# These exist because an agent's prose is not a data interface. Across two consecutive live runs
# with an identical prompt, fixture and model configuration, the Document agent expressed the same
# finding once as `{"confidence": 0.96}` and once as a sentence; the derived signals differed, and
# R3.5's "identical outcome on every evaluation" cannot hold on a foundation like that.
#
# A submit_* call is validated against a JSON Schema by the service before it reaches here, so the
# shape is not the model's choice. The finding is then persisted on the case record, which makes it
# the *state machine* that reads structured data rather than parsing text.


#: Case-record attribute per agent. Flat rather than nested — see `_persist_finding`.
FINDING_ATTRIBUTES: dict[str, str] = {
    "router": "findingRouter",
    "documents": "findingDocuments",
    "identity": "findingIdentity",
    "prescreen": "findingPrescreen",
    "fraud": "findingFraud",
    "compliance": "findingCompliance",
    "notify": "findingNotify",
}


def _persist_finding(agent: str, applicant_id: str, finding: dict[str, Any]) -> None:
    """Write one agent's structured finding to the case record under `findings.<agent>`.

    A conditional-free `SET` on a nested map: each agent owns exactly one key, so two agents
    writing concurrently cannot collide, and a retried tool call is naturally idempotent because
    it writes the same value to the same place.
    """
    table_name = os.environ.get("CASE_TABLE")
    if not table_name:
        # A configuration fault, not "no finding". Raising keeps it distinguishable from an agent
        # that legitimately had nothing to report.
        raise ToolError("CASE_TABLE is not configured", "NOT_CONFIGURED")
    import json
    import boto3

    ddb = boto3.client("dynamodb")
    ddb.update_item(
        TableName=table_name,
        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
        # One flat attribute per agent, e.g. `findingDocuments`.
        #
        # NOT a nested map. `SET findings = if_not_exists(findings, :empty), findings.#a = :f` is
        # rejected by DynamoDB — the two document paths overlap — and it fails at *invoke* time,
        # where the agent reports it as "an unhandled error prevented submission" and the case
        # degrades to manual review. Flat attributes need no pre-creation, cannot overlap, and
        # still give each agent sole ownership of its own key so four concurrent branches never
        # contend.
        UpdateExpression="SET #f = :f, updatedAt = :t",
        ExpressionAttributeNames={"#f": FINDING_ATTRIBUTES[agent]},
        ExpressionAttributeValues={
            # Canonical JSON so a retried call writes byte-identical content, which is what makes
            # the write idempotent rather than merely repeatable.
            ":f": {"S": json.dumps(finding, sort_keys=True)},
            ":t": {"S": _now()},
        },
    )


def _submit(agent: str, tool: str, args: dict[str, Any], fields: tuple[str, ...],
            ctx: dict[str, Any] | None = None) -> ToolResult:
    # The invocation context is a fallback source for the applicant id. The agent is asked for it
    # in the tool arguments, but an agent that loses track of it mid-conversation would otherwise
    # be unable to record a finding at all — and a lost finding degrades the case to review.
    applicant_id = args.get("applicantId") or (ctx or {}).get("applicantId")
    if not applicant_id:
        return invalid(tool, "applicantId is required to record a finding", "MISSING_APPLICANT")

    args = dict(args)
    missing = [f for f in fields if args.get(f) is None]
    if missing:
        # Schema validation should already have caught this. Re-checked because a finding with a
        # hole in it is worse than no finding: the decision engine would tier on a partial record.
        return invalid(tool, "missing required contract fields: %s" % ", ".join(missing),
                       "INCOMPLETE_CONTRACT")

    # Bounds are enforced here rather than declared in the schema: the Gateway's inline MCP schema
    # rejects JSON Schema numeric constraints, so `minimum`/`maximum` cannot travel with the tool
    # definition. Checking them here keeps the guarantee — an out-of-range confidence would
    # otherwise reach the tier calculation and silently skew it.
    # Every conversion is guarded. An unguarded `float()` here raised on whatever the agent
    # actually sent — the schema says `number` but a model may still send "0.96", "high", or an
    # empty string, and the raise surfaced as UNHANDLED_ERROR, which the agent then reported to
    # itself as "an unhandled error prevented submission" and gave up. A malformed field must come
    # back as a named INVALID the agent can act on, never as a crash.
    confidence = args.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            return invalid(tool, "confidence must be a number, got %r" % (confidence,),
                           "CONFIDENCE_NOT_A_NUMBER")
        if not 0.0 <= confidence <= 1.0:
            return invalid(tool, "confidence must be within 0.0..1.0, got %r" % (confidence,),
                           "CONFIDENCE_OUT_OF_RANGE")
        args["confidence"] = confidence

    for count_field in ("hitCount", "sharedAttributeCount"):
        value = args.get(count_field)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            return invalid(tool, "%s must be a number, got %r" % (count_field, value),
                           "COUNT_NOT_A_NUMBER")
        if number < 0:
            return invalid(tool, "%s cannot be negative, got %r" % (count_field, value),
                           "NEGATIVE_COUNT")
        args[count_field] = number

    # Booleans travel as real booleans or as the strings a model may substitute. Coerced rather
    # than rejected, because "false" arriving as a truthy string is the dangerous reading.
    for flag in ("determined", "matched", "expired", "screenPhotograph", "unsupportedIssuer",
                 "velocityCritical", "thinFile"):
        value = args.get(flag)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "false"):
                args[flag] = lowered == "true"
            else:
                return invalid(tool, "%s must be a boolean, got %r" % (flag, value),
                               "FLAG_NOT_A_BOOLEAN")

    finding = {k: v for k, v in args.items() if k != "applicantId"}
    _persist_finding(agent, str(applicant_id), finding)
    return ok(tool, {"recorded": True, "agent": agent, "finding": finding}, source="case-record")


# Every tool takes `(args, ctx)` — the dispatcher calls `fn(args, ctx)` positionally. Omitting
# `ctx` raised TypeError inside the dispatcher, which the catch-all reported as UNHANDLED_ERROR;
# the agent read that as "an unhandled error prevented submission", abandoned the contract, and the
# case degraded to manual review. Three tools failed identically for one missing parameter.


def submit_document_analysis(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> ToolResult:
    return _submit("documents", "submit_document_analysis", args,
                   ("confidence", "expired", "screenPhotograph", "unsupportedIssuer"), ctx)


def submit_prescreen(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> ToolResult:
    return _submit("prescreen", "submit_prescreen", args,
                   ("determined", "matched", "hitCount"), ctx)


def submit_fraud_assessment(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> ToolResult:
    return _submit("fraud", "submit_fraud_assessment", args,
                   ("determined", "velocityCritical"), ctx)


def submit_jurisdiction(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> ToolResult:
    return _submit("router", "submit_jurisdiction", args,
                   ("determined", "jurisdiction"), ctx)


def submit_identity(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> ToolResult:
    return _submit("identity", "submit_identity", args, ("determined", "faceMatch"), ctx)


def submit_compliance_risk(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> ToolResult:
    return _submit("compliance", "submit_compliance_risk", args,
                   ("determined", "pepStatus", "adverseMediaSeverity"), ctx)


def submit_notification(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> ToolResult:
    return _submit("notify", "submit_notification", args, ("determined", "message"), ctx)


TOOLS = {
    "s3_get_document": s3_get_document,
    "rekognition_compare_faces": rekognition_compare_faces,
    "velocity_check": velocity_check,
    "sanctions_exact_match": sanctions_exact_match,
    "sanctions_fuzzy": sanctions_fuzzy,
    "pep_lookup": pep_lookup,
    "adverse_media": adverse_media,
    "submit_document_analysis": submit_document_analysis,
    "submit_prescreen": submit_prescreen,
    "submit_fraud_assessment": submit_fraud_assessment,
    "submit_jurisdiction": submit_jurisdiction,
    "submit_identity": submit_identity,
    "submit_compliance_risk": submit_compliance_risk,
    "submit_notification": submit_notification,
}

PHASE1_TOOLS = ("s3_get_document", "rekognition_compare_faces",
                "velocity_check", "sanctions_exact_match",
                "submit_document_analysis", "submit_prescreen", "submit_fraud_assessment",
                "submit_jurisdiction", "submit_identity")
PHASE2_TOOLS = ("sanctions_fuzzy", "pep_lookup", "adverse_media",
                "submit_compliance_risk", "submit_notification")

#: Tools whose failure must never be read as a clean result (R25.7).
SCREENING_TOOLS = ("sanctions_exact_match", "sanctions_fuzzy", "pep_lookup", "adverse_media")

#: The three output contracts (R11.13). Separated from the evidence tools because they are a
#: different kind of thing: an evidence tool *fetches* a fact from a source, a contract tool
#: *records* the agent's finding. Only evidence tools can be UNAVAILABLE in the R25.7 sense.
CONTRACT_TOOLS = ("submit_document_analysis", "submit_prescreen", "submit_fraud_assessment",
                  "submit_jurisdiction", "submit_identity", "submit_compliance_risk",
                  "submit_notification")

#: Evidence-gathering tools — the seven the talk describes as "one Lambda, seven tools".
EVIDENCE_TOOLS = tuple(t for t in TOOLS if t not in CONTRACT_TOOLS)

assert set(TOOLS) == set(PHASE1_TOOLS) | set(PHASE2_TOOLS)
assert len(EVIDENCE_TOOLS) == 7, "the seven evidence tools are a fixed set"
# One contract per agent, which is what `OutputContractNames` always listed. Building only three
# left four agents instructed by their prompt to call a tool that did not exist.
assert len(CONTRACT_TOOLS) == 7
assert len(FINDING_ATTRIBUTES) == 7
assert len(TOOLS) == 14
assert not set(CONTRACT_TOOLS) & set(SCREENING_TOOLS)
