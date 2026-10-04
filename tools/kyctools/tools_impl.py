"""The six evidence tools — tasks 2.2, 2.3, 2.5, reworked so that code decides on facts.

Phase 1, latency-critical, provisioned concurrency:
    s3_get_document, rekognition_compare_faces, velocity_check, sanctions_screen

Phase 2, accuracy-critical:
    pep_lookup, adverse_media

Phase 3 (a sanctions list update) re-runs sanctions_screen.

**Every evidence tool reads its inputs from the case record and records its result there**
(`case_record.py`). The agent supplies an `applicantId` and chooses when to call; it cannot choose
which name is screened, which document is read, which face is compared or what threshold applies,
and the decision engine reads what the tool recorded rather than what the agent said about it.

**Screening is one deterministic tool.** Phase 1 used to run exact matching only and leave
transliteration and name order to Phase 2, minutes later — but the account is already open by then,
and sanctions breaches are strict liability. The variant matcher is plain code and takes
milliseconds, so it now runs in Phase 1. What stays in Phase 2 is the part that is genuinely
slower: politically exposed persons by association and adverse media disambiguation.

**There is no liveness tool, and its absence is a design decision, not an omission.** A
liveness SessionId expires 180 seconds after issue and takes every associated artefact with it
(F-40). A tool called by an agent several seconds into a pipeline cannot be relied on to land
inside that window, so the Public API fetches the result the moment `onAnalysisComplete` fires
and persists it to the case (R33.5).

Every screening tool fails closed (R25.7): if the source cannot be consulted the result is
UNAVAILABLE, never "no match" — and the UNAVAILABLE is recorded on the case, so the rules and the
reviewer see that the check did not run rather than a gap.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Mapping

from .case_record import (
    FACT_ATTRIBUTES,
    aws_error_code,
    documents_on_case,
    load_case,
    record_fact,
    record_unavailable,
    screening_identity,
)
from .contract import ToolError, ToolResult, _now, ok, unavailable, invalid
from .controls import list_overlay, unavailable_tools
from .data.sanctions import (
    ALL_ENTRIES,
    LIST_SOURCE,
    LIST_VERSION,
    MEDIA_ARTICLES,
    PEP_ENTRIES,
    Entry,
    normalise,
    tokens,
)
from .matching import Candidate, disambiguate_media, names_overlap, screen, worst
from .redact import get_logger, log

LOG = get_logger()

#: Task 2.6. Off by default; the demo profile turns it on so the throttle and rate-limit
#: failure demos are reproducible on stage rather than dependent on luck.
DEMO_LATENCY_MS = int(os.environ.get("DEMO_LATENCY_MS", "0"))
DEMO_PROFILE = os.environ.get("DEMO_PROFILE", "off").lower() == "on"

#: The face-match threshold, applied here and only here (F-44). An earlier version accepted a
#: `threshold` argument from the agent, which meant a prompt-injected agent could pass 0 and make
#: any detected face a "match". The argument is now ignored.
FACE_MATCH_THRESHOLD = 90.0

#: Velocity window. Counts only applications recorded within it.
VELOCITY_WINDOW_HOURS = 24


def _inject_latency(tool: str) -> None:
    if DEMO_PROFILE and DEMO_LATENCY_MS > 0:
        time.sleep(DEMO_LATENCY_MS / 1000.0)
        log(LOG, logging.INFO, "demo latency injected", tool=tool,
            duration_ms=DEMO_LATENCY_MS)


def _guard(tool: str) -> None:
    """Fault injection for the fail-closed tests (R25.7 / AC-12) and the on-stage failure demo.

    Read on every call — see `controls.py` for why an import-time environment read could never be
    flipped on stage.
    """
    if tool in unavailable_tools():
        raise ToolError("fault injection: %s forced unavailable" % tool, "FORCED_UNAVAILABLE")


def _applicant_id(args: Mapping[str, Any]) -> str:
    return str(args.get("applicantId") or "").strip()


def _missing_applicant(tool: str) -> ToolResult:
    return invalid(tool, "applicantId is required: this tool reads its inputs from the case "
                         "record and records its result there", "MISSING_APPLICANT")


def _case_or_invalid(tool: str, applicant_id: str) -> tuple[dict[str, Any] | None,
                                                            ToolResult | None]:
    case = load_case(applicant_id)
    if case is None:
        return None, invalid(tool, "no case record exists for this applicantId",
                             "CASE_NOT_FOUND")
    return case, None


def _not_performed(applicant_id: str, attribute: str, *, status: str, reason_code: str,
                   reason: str) -> dict[str, Any]:
    return record_fact(applicant_id, attribute, {
        "performed": False, "status": status, "reasonCode": reason_code, "reason": reason})


# ------------------------------------------------------------------ Phase 1 tools
def s3_get_document(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """Read the identity document listed on the case record and record what it says.

    The document comes from the case record. An agent may name a bucket and key, but only one the
    case already lists; anything else is refused, so a steered agent cannot substitute a clean
    document for the applicant's own.

    Pre-model validation (R23.1 / F-23) happens here as well as client-side, because the client is
    untrusted. Nova rejects CMYK, SVG-in-PDF, JPEG2000 and JBIG2, and a rejection at the model
    boundary is a wasted invocation with a confusing error.
    """
    tool = "s3_get_document"
    attribute = FACT_ATTRIBUTES["document"]
    applicant_id = _applicant_id(args)
    if not applicant_id:
        return _missing_applicant(tool)
    try:
        _guard(tool)
        _inject_latency(tool)
        case, refused = _case_or_invalid(tool, applicant_id)
        if refused is not None:
            return refused
        documents = documents_on_case(case)
        if not documents:
            _not_performed(applicant_id, attribute, status="INVALID",
                           reason_code="NO_DOCUMENT_ON_CASE",
                           reason="no identity document is listed on the case record")
            return invalid(tool, "no document is listed on the case record",
                           "NO_DOCUMENT_ON_CASE")

        requested_bucket, requested_key = args.get("bucket"), args.get("key")
        if requested_bucket or requested_key:
            doc = next((d for d in documents
                        if d["bucket"] == requested_bucket and d["key"] == requested_key), None)
            if doc is None:
                return invalid(tool, "that document is not listed on this applicant's case "
                                     "record; omit bucket and key to read the listed document",
                               "DOCUMENT_NOT_ON_CASE")
        else:
            doc = documents[0]
        return _read_and_record_document(tool, applicant_id, attribute, doc)
    except ToolError as error:
        record_unavailable(applicant_id, attribute, error)
        raise


def _read_and_record_document(tool: str, applicant_id: str, attribute: str,
                              doc: Mapping[str, str]) -> ToolResult:
    import boto3

    bucket, key = doc["bucket"], doc["key"]
    s3 = boto3.client("s3")
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001 - narrowed immediately by error code
        code = aws_error_code(e)
        if code in ("404", "NoSuchKey", "NotFound"):
            _record_document(applicant_id, attribute, doc, {
                "documentReadable": False, "unreadableReason": "document not found in storage"})
            return invalid(tool, "document not found", "DOCUMENT_NOT_FOUND")
        raise ToolError("S3 unreachable: %s" % code, "S3_UNAVAILABLE") from e

    size = head["ContentLength"]
    ctype = head.get("ContentType", "application/octet-stream")
    # S3 user metadata stamped at upload. Keys arrive lowercased. `synthetic` marks fixture objects
    # so the audit record can never confuse a demo document for a real one.
    metadata = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}

    MAX_BYTES = 25 * 1024 * 1024          # F-23 direct-upload ceiling
    BANNED = ("image/jp2", "image/jpx", "image/jbig2", "image/svg+xml")
    if size > MAX_BYTES:
        extraction = {"documentReadable": False,
                      "unreadableReason": "document exceeds the 25 MB direct limit"}
        _record_document(applicant_id, attribute, doc, extraction, size=size, ctype=ctype,
                         metadata=metadata)
        return invalid(tool, extraction["unreadableReason"], "DOCUMENT_TOO_LARGE")
    if ctype in BANNED:
        extraction = {"documentReadable": False,
                      "unreadableReason": "unsupported format %s (F-23)" % ctype}
        _record_document(applicant_id, attribute, doc, extraction, size=size, ctype=ctype,
                         metadata=metadata)
        return invalid(tool, extraction["unreadableReason"], "UNSUPPORTED_FORMAT")

    extraction = _extract_document_fields(
        s3, bucket=bucket, key=key, content_type=ctype, size_bytes=size)
    recorded = _record_document(applicant_id, attribute, doc, extraction, size=size,
                                ctype=ctype, metadata=metadata)
    data: dict[str, Any] = {
        "bucket": bucket, "key": key, "sizeBytes": size, "contentType": ctype,
        "documentType": recorded["document"]["documentType"],
        "metadata": metadata,
        "synthetic": recorded["document"]["synthetic"],
        "recorded": True,
    }
    data.update(extraction)
    return ok(tool, data, source="s3")


def _record_document(applicant_id: str, attribute: str, doc: Mapping[str, str],
                     extraction: Mapping[str, Any], *, size: int | None = None,
                     ctype: str | None = None,
                     metadata: Mapping[str, str] | None = None) -> dict[str, Any]:
    metadata = metadata or {}
    fact = {
        "performed": True,
        **extraction,
        "document": {
            "bucket": doc["bucket"],
            "key": doc["key"],
            # The type recorded at intake wins over anything read later; the agent's opinion of
            # the document type is not an input.
            "documentType": doc.get("type") or metadata.get("documenttype") or "UNKNOWN",
            "issuingCountry": doc.get("issuingCountry") or metadata.get("issuingcountry"),
            "contentType": ctype,
            "sizeBytes": size,
            "synthetic": metadata.get("synthetic") == "true",
        },
    }
    return record_fact(applicant_id, attribute, fact)


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
        code = aws_error_code(exc)
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
    """Compare the document portrait on the case against the liveness reference frame on the case.

    Both images come from the case record: the first listed document (jurisdiction evidence
    bucket, Object Lock) and `liveness.referenceImageKey` (biometric bucket, 24-hour lifecycle,
    because a real face must stay deletable — R36.1-R36.3). The agent cannot name either image and
    cannot set the threshold.

    Only a boolean and a bucketed band cross back (F-44): the raw score is not the agent's
    business, and nothing downstream may re-threshold it.
    """
    tool = "rekognition_compare_faces"
    attribute = FACT_ATTRIBUTES["face"]
    applicant_id = _applicant_id(args)
    if not applicant_id:
        return _missing_applicant(tool)
    try:
        _guard(tool)
        _inject_latency(tool)
        case, refused = _case_or_invalid(tool, applicant_id)
        if refused is not None:
            return refused
        documents = documents_on_case(case)
        if not documents:
            _not_performed(applicant_id, attribute, status="INVALID",
                           reason_code="NO_DOCUMENT_ON_CASE",
                           reason="no identity document is listed on the case record")
            return invalid(tool, "no document is listed on the case record",
                           "NO_DOCUMENT_ON_CASE")
        liveness = case.get("liveness") if isinstance(case.get("liveness"), Mapping) else {}
        frame_key = str(liveness.get("referenceImageKey") or "")
        if not frame_key:
            # Ours to fix, not the applicant's: today the frame is only captured where the
            # biometric bucket shares the liveness Region (api/liveness.py). UNAVAILABLE, so it
            # routes to a human rather than asking the applicant for something they cannot give.
            _not_performed(applicant_id, attribute, status="UNAVAILABLE",
                           reason_code="NO_REFERENCE_FRAME",
                           reason="the case record carries no liveness reference frame")
            return invalid(tool, "no liveness reference frame on the case record",
                           "NO_REFERENCE_FRAME")
        target_bucket = os.environ.get("BIOMETRIC_BUCKET")
        if not target_bucket:
            raise ToolError("BIOMETRIC_BUCKET is not configured", "NOT_CONFIGURED")
        source = documents[0]
        return _compare_and_record(tool, applicant_id, attribute, source, target_bucket,
                                   frame_key)
    except ToolError as error:
        record_unavailable(applicant_id, attribute, error)
        raise


def _compare_and_record(tool: str, applicant_id: str, attribute: str,
                        source: Mapping[str, str], target_bucket: str,
                        frame_key: str) -> ToolResult:
    import boto3

    rek = boto3.client("rekognition")
    provenance = {"documentKey": source["key"], "referenceFramePresent": True,
                  "threshold": FACE_MATCH_THRESHOLD}
    try:
        resp = rek.compare_faces(
            SourceImage={"S3Object": {"Bucket": source["bucket"], "Name": source["key"]}},
            TargetImage={"S3Object": {"Bucket": target_bucket, "Name": frame_key}},
            SimilarityThreshold=FACE_MATCH_THRESHOLD,
        )
    except Exception as e:  # noqa: BLE001 - narrowed immediately by error code
        code = aws_error_code(e)
        if code == "InvalidParameterException":
            # Rekognition raises this when it finds no face at all: a real finding about the
            # document, not a transport failure.
            fact = record_fact(applicant_id, attribute, {
                "performed": True, "faceDetected": False, "match": False,
                "similarityBand": "NONE",
                "reason": "no detectable face in one of the images", **provenance})
            return ok(tool, _face_view(fact), source="rekognition")
        if code in ("InvalidS3ObjectException", "InvalidImageFormatException",
                    "ImageTooLargeException"):
            # Applicant-fixable and not retryable: INVALID routes to a step-up for a better image.
            _not_performed(applicant_id, attribute, status="INVALID",
                           reason_code="IMAGE_UNREADABLE",
                           reason="image missing or unreadable (%s)" % code)
            return invalid(tool, "image missing or unreadable (%s)" % code, "IMAGE_UNREADABLE")
        raise ToolError("Rekognition unreachable: %s" % code, "REKOGNITION_UNAVAILABLE") from e

    matches = resp.get("FaceMatches", [])
    best = max((m["Similarity"] for m in matches), default=0.0)
    fact = record_fact(applicant_id, attribute, {
        "performed": True,
        "match": bool(matches),
        "faceDetected": True,
        # Bucketed, not raw. Enough for a reviewer, not a score to re-threshold on.
        "similarityBand": ("HIGH" if best >= 95 else
                           "MEDIUM" if best >= FACE_MATCH_THRESHOLD else "LOW"),
        "unmatchedFaces": len(resp.get("UnmatchedFaces", [])),
        **provenance,
    })
    return ok(tool, _face_view(fact), source="rekognition")


def _face_view(fact: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("match", "faceDetected", "similarityBand", "unmatchedFaces", "reason")
    view = {k: fact[k] for k in keys if k in fact}
    view["recorded"] = True
    return view


def velocity_check(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """Device and IP velocity from identifiers captured at intake. R29.6 synthetic-identity signal.

    Returns signals, never a verdict. Whether a signal is disqualifying is a decision-engine
    question (§8.4).

    This build does not capture device or IP identifiers at intake (plan v2 defers it: only the
    mule-pattern variant of the risk-event act needs them). When the case carries none, the tool
    records `performed: false, status: NOT_ESTABLISHED` rather than zero counts — "we did not look"
    and "we looked and saw nothing" are different facts, and only the first is true here.
    """
    tool = "velocity_check"
    attribute = FACT_ATTRIBUTES["velocity"]
    applicant_id = _applicant_id(args)
    if not applicant_id:
        return _missing_applicant(tool)
    try:
        _guard(tool)
        _inject_latency(tool)
        case, refused = _case_or_invalid(tool, applicant_id)
        if refused is not None:
            return refused
        intake = case.get("intake") if isinstance(case.get("intake"), Mapping) else {}
        device = str(intake.get("deviceHash") or "")
        ip = str(intake.get("ipHash") or "")
        if not (device or ip):
            fact = _not_performed(
                applicant_id, attribute, status="NOT_ESTABLISHED",
                reason_code="NO_DEVICE_OR_IP_CAPTURED",
                reason="no device or IP identifier was captured at intake, so velocity "
                       "could not be assessed")
            return ok(tool, {"performed": False, "reasonCode": fact["reasonCode"],
                             "reason": fact["reason"], "recorded": True},
                      source="velocity-store")

        table_name = os.environ.get("VELOCITY_TABLE")
        if not table_name:
            # No table configured is a configuration fault, not "no velocity observed".
            raise ToolError("VELOCITY_TABLE is not configured", "NOT_CONFIGURED")
        counts = _velocity_counts(table_name, {"device": device, "ip": ip})
        device_n, ip_n = counts.get("device", 0), counts.get("ip", 0)
        fact = record_fact(applicant_id, attribute, {
            "performed": True,
            "deviceApplications": device_n,
            "ipApplications": ip_n,
            "windowHours": VELOCITY_WINDOW_HOURS,
            "signals": {
                "criticalDeviceReuse": device_n >= 5,
                "elevatedIpReuse": 3 <= ip_n < 10,
                "criticalIpReuse": ip_n >= 10,
            },
        })
        view = {k: fact[k] for k in ("deviceApplications", "ipApplications", "windowHours",
                                     "signals")}
        view.update({"performed": True, "recorded": True})
        return ok(tool, view, source="velocity-store")
    except ToolError as error:
        record_unavailable(applicant_id, attribute, error)
        raise


def _velocity_counts(table_name: str, identifiers: Mapping[str, str]) -> dict[str, int]:
    import boto3

    ddb = boto3.client("dynamodb")
    now_epoch = int(time.time())
    counts: dict[str, int] = {}
    try:
        for label, value in identifiers.items():
            if not value:
                continue
            total = 0
            kwargs: dict[str, Any] = {
                "TableName": table_name,
                "KeyConditionExpression": "pk = :pk",
                # TTL deletion lags by up to 48 hours, so expiry is filtered explicitly.
                "FilterExpression": "expiresAt > :now",
                "ExpressionAttributeValues": {":pk": {"S": f"{label}#{value}"},
                                              ":now": {"N": str(now_epoch)}},
                "Select": "COUNT",
            }
            while True:
                r = ddb.query(**kwargs)
                total += int(r.get("Count", 0))
                if not r.get("LastEvaluatedKey"):
                    break
                kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
            counts[label] = total
    except Exception as e:  # noqa: BLE001 - narrowed to a named outage
        raise ToolError("velocity store unreachable: %s" % aws_error_code(e),
                        "VELOCITY_UNAVAILABLE") from e
    return counts


def current_list() -> tuple[str, tuple[Entry, ...]]:
    """The base list plus any published list update, and the version that describes both."""
    overlay_version, overlay_entries = list_overlay()
    if not overlay_entries:
        return LIST_VERSION, ALL_ENTRIES
    base_ids = {e.entry_id for e in ALL_ENTRIES}
    clash = sorted(e.entry_id for e in overlay_entries if e.entry_id in base_ids)
    if clash:
        # A list update that reuses an existing id would silently shadow a designation.
        raise ToolError("list update reuses existing entry ids: %s" % ", ".join(clash),
                        "LIST_UNAVAILABLE")
    return "%s+%s" % (LIST_VERSION, overlay_version), ALL_ENTRIES + overlay_entries


def sanctions_screen(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """Sanctions screening of the identity on the case record. Exact and variant, fail closed.

    Screens the applicant's declared identity (see `ScreeningIdentity` for why declared) against
    the current list, including any published update. Every alert is classified CONFIRMED,
    PROBABLE or POSSIBLE with a reason; every exclusion is reported with its reason (R27.5).

    `name` is what the agent read off the case record. It is not what gets screened. It is recorded
    beside the result so a reviewer can see when the agent's view of the applicant and the record
    disagree — which is what a prompt-injected agent looks like from here.
    """
    tool = "sanctions_screen"
    attribute = FACT_ATTRIBUTES["screening"]
    applicant_id = _applicant_id(args)
    if not applicant_id:
        return _missing_applicant(tool)
    agent_name = str(args.get("name") or "").strip()
    try:
        _guard(tool)
        _inject_latency(tool)
        case, refused = _case_or_invalid(tool, applicant_id)
        if refused is not None:
            return refused
        identity = screening_identity(case)
        if not identity.name:
            _not_performed(applicant_id, attribute, status="UNAVAILABLE",
                           reason_code="NO_NAME_ON_RECORD",
                           reason="the case record carries no declared name to screen")
            return invalid(tool, "the case record carries no declared name to screen",
                           "NO_NAME_ON_RECORD")

        version, entries = current_list()
        candidate = Candidate(name=identity.name, dob=identity.dob, country=identity.country,
                              alternate_names=identity.alternate_names)
        alerts, exclusions = screen(candidate, entries)
        strongest = worst(alerts)
        fact = record_fact(applicant_id, attribute, {
            "performed": True,
            "listVersion": version,
            "source": LIST_SOURCE,
            "syntheticData": True,
            "strongest": strongest.value,
            "hits": [h.to_dict() for h in alerts],
            "excluded": [{"entryId": h.entry_id, "matchedName": h.matched_name,
                          "reason": h.reason} for h in exclusions],
            "screened": identity.to_dict(),
            "agentSuppliedName": agent_name or None,
            "agentNameMatchesRecord": (tokens(agent_name) == tokens(identity.name)
                                       if agent_name else None),
        })
        return ok(tool, {
            "matched": bool(alerts),
            "strongest": fact["strongest"],
            "hitCount": len(alerts),
            "hits": fact["hits"],
            "excluded": fact["excluded"],
            "screenedName": identity.name,
            "syntheticData": True,
            "recorded": True,
        }, source=LIST_SOURCE, list_version=version)
    except ToolError as error:
        record_unavailable(applicant_id, attribute, error)
        raise


# ------------------------------------------------------------------ Phase 2 tools
def pep_lookup(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """R27.6. PEP by association — spouse, child, business partner — with the relationship.

    Reads the identity from the case record. A PEP listing whose name overlaps but whose date of
    birth differs from the applicant's is reported as excluded, not matched: the earlier version let
    an exact name alone set `pepStatus` even when the dates conflicted.
    """
    tool = "pep_lookup"
    attribute = FACT_ATTRIBUTES["pep"]
    applicant_id = _applicant_id(args)
    if not applicant_id:
        return _missing_applicant(tool)
    try:
        _guard(tool)
        _inject_latency(tool)
        case, refused = _case_or_invalid(tool, applicant_id)
        if refused is not None:
            return refused
        identity = screening_identity(case)
        if not identity.name:
            _not_performed(applicant_id, attribute, status="UNAVAILABLE",
                           reason_code="NO_NAME_ON_RECORD",
                           reason="the case record carries no declared name to screen")
            return invalid(tool, "the case record carries no declared name to screen",
                           "NO_NAME_ON_RECORD")

        found, excluded = [], []
        for p in PEP_ENTRIES:
            if not names_overlap(identity.name, p.name):
                continue
            if identity.dob and p.dob and identity.dob != p.dob:
                excluded.append({"entryId": p.entry_id,
                                 "reason": "names overlap but the dates of birth differ"})
                continue
            dob_match = bool(identity.dob and p.dob and identity.dob == p.dob)
            if not dob_match and normalise(identity.name) != normalise(p.name):
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

        record_fact(applicant_id, attribute, {
            "performed": True, "pepStatus": status, "hits": found, "excluded": excluded,
            "listVersion": LIST_VERSION, "source": "synthetic-pep-list",
            "syntheticData": True})
        return ok(tool, {"pepStatus": status, "hitCount": len(found), "hits": found,
                         "excluded": excluded, "syntheticData": True, "recorded": True},
                  source="synthetic-pep-list", list_version=LIST_VERSION)
    except ToolError as error:
        record_unavailable(applicant_id, attribute, error)
        raise


def adverse_media(args: dict[str, Any], ctx: dict[str, Any]) -> ToolResult:
    """R27.7. Entity disambiguation — a name collision is not a match.

    Reads the identity from the case record. Only articles whose subject name overlaps the
    applicant's are considered at all; the earlier version reported every unrelated article as a
    "rejected collision", which put other people's headlines on every applicant's record.
    """
    tool = "adverse_media"
    attribute = FACT_ATTRIBUTES["adverse_media"]
    applicant_id = _applicant_id(args)
    if not applicant_id:
        return _missing_applicant(tool)
    try:
        _guard(tool)
        _inject_latency(tool)
        case, refused = _case_or_invalid(tool, applicant_id)
        if refused is not None:
            return refused
        identity = screening_identity(case)
        if not identity.name:
            _not_performed(applicant_id, attribute, status="UNAVAILABLE",
                           reason_code="NO_NAME_ON_RECORD",
                           reason="the case record carries no declared name to screen")
            return invalid(tool, "the case record carries no declared name to screen",
                           "NO_NAME_ON_RECORD")

        candidate = Candidate(name=identity.name, dob=identity.dob, country=identity.country)
        attributed, rejected = [], []
        for a in MEDIA_ARTICLES:
            if not a.subject_name or not names_overlap(identity.name, a.subject_name):
                continue
            is_match, reason = disambiguate_media(
                candidate, a.subject_name, a.subject_dob, a.subject_country)
            record = {"articleId": a.article_id, "headline": a.headline,
                      "severity": a.severity, "reason": reason}
            (attributed if is_match else rejected).append(record)

        severity_rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        worst_sev = max((severity_rank[r["severity"]] for r in attributed), default=0)
        max_severity = {0: "NONE", 1: "LOW", 2: "MEDIUM", 3: "HIGH"}[worst_sev]
        record_fact(applicant_id, attribute, {
            "performed": True, "maxSeverity": max_severity, "attributed": attributed,
            "rejectedCollisions": rejected, "listVersion": LIST_VERSION,
            "source": "synthetic-media-index", "syntheticData": True})
        return ok(tool, {"matched": bool(attributed), "hitCount": len(attributed),
                         "maxSeverity": max_severity, "attributed": attributed,
                         "rejectedCollisions": rejected, "syntheticData": True,
                         "recorded": True},
                  source="synthetic-media-index", list_version=LIST_VERSION)
    except ToolError as error:
        record_unavailable(applicant_id, attribute, error)
        raise


# --- output contracts (R11.13, F-89) ---------------------------------------------------------
#
# Built and tested, but not deployed (`tool_schemas.ENABLE_OUTPUT_CONTRACTS`). They were the first
# answer to "prose is not a data interface": across two consecutive live runs with an identical
# prompt, fixture and model configuration, the Document agent expressed the same finding once as
# `{"confidence": 0.96}` and once as a sentence.
#
# The evidence tools above are the second, stronger answer: they record the facts themselves, so
# the decision engine does not need the agent to report anything. A contract now records only the
# agent's narrative, which is useful to a reviewer and irrelevant to the outcome.


#: Case-record attribute per agent. Flat rather than nested — see `_persist_finding`.
FINDING_ATTRIBUTES: dict[str, str] = {
    "briefing": "findingBriefing",
    "documents": "findingDocuments",
    "identity": "findingIdentity",
    "prescreen": "findingPrescreen",
    "fraud": "findingFraud",
    "compliance": "findingCompliance",
    "notify": "findingNotify",
}


def _persist_finding(agent: str, applicant_id: str, finding: dict[str, Any]) -> None:
    """Write one agent's structured finding to the case record under `finding<Agent>`.

    A conditional-free `SET` of a flat attribute: each agent owns exactly one key, so two agents
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


def submit_briefing(args: dict[str, Any], ctx: dict[str, Any] | None = None) -> ToolResult:
    return _submit("briefing", "submit_briefing", args, ("determined", "briefing"), ctx)


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
    "sanctions_screen": sanctions_screen,
    "pep_lookup": pep_lookup,
    "adverse_media": adverse_media,
    "submit_document_analysis": submit_document_analysis,
    "submit_prescreen": submit_prescreen,
    "submit_fraud_assessment": submit_fraud_assessment,
    "submit_briefing": submit_briefing,
    "submit_identity": submit_identity,
    "submit_compliance_risk": submit_compliance_risk,
    "submit_notification": submit_notification,
}

PHASE1_TOOLS = ("s3_get_document", "rekognition_compare_faces",
                "velocity_check", "sanctions_screen",
                "submit_document_analysis", "submit_prescreen", "submit_fraud_assessment",
                "submit_briefing", "submit_identity")
PHASE2_TOOLS = ("pep_lookup", "adverse_media",
                "submit_compliance_risk", "submit_notification")

#: Tools whose failure must never be read as a clean result (R25.7).
SCREENING_TOOLS = ("sanctions_screen", "pep_lookup", "adverse_media")

#: The output contracts (R11.13). Separated from the evidence tools because they are a different
#: kind of thing: an evidence tool *records* a fact it established, a contract tool *records* the
#: agent's narrative. Only evidence tools can be UNAVAILABLE in the R25.7 sense.
CONTRACT_TOOLS = ("submit_document_analysis", "submit_prescreen", "submit_fraud_assessment",
                  "submit_briefing", "submit_identity", "submit_compliance_risk",
                  "submit_notification")

#: Evidence tools — "one Lambda, six tools". Seven before the two screening tools became one.
EVIDENCE_TOOLS = tuple(t for t in TOOLS if t not in CONTRACT_TOOLS)

assert set(TOOLS) == set(PHASE1_TOOLS) | set(PHASE2_TOOLS)
assert len(EVIDENCE_TOOLS) == 6, "the six evidence tools are a fixed set"
# One contract per agent, which is what `OutputContractNames` always listed. Building only three
# left four agents instructed by their prompt to call a tool that did not exist.
assert len(CONTRACT_TOOLS) == 7
assert len(FINDING_ATTRIBUTES) == 7
assert len(TOOLS) == 13
assert not set(CONTRACT_TOOLS) & set(SCREENING_TOOLS)
# Every evidence tool records exactly one fact attribute.
assert len(FACT_ATTRIBUTES) == len(EVIDENCE_TOOLS)
