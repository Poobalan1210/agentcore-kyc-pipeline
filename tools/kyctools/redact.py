"""Structured logging that cannot leak PII.

Task 2.1 requires "structured logging, no PII in logs". A denylist of field names is the only
approach that survives contact with reality here: an allowlist of *values* is impossible
because names and addresses are unbounded, and free-form log messages are where PII actually
escapes.

So the rule is inverted. Nothing is logged unless it is explicitly declared safe, and any
declared-sensitive key is replaced by a stable fingerprint. The fingerprint is deliberately
truncated and salted per invocation so it correlates within one case and is useless across
cases — enough to debug a single applicant's journey, not enough to build a shadow record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from typing import Any

#: Keys whose values never reach a log, at any nesting depth.
SENSITIVE_KEYS = frozenset({
    "name", "fullname", "firstname", "lastname", "middlename", "surname",
    "givenname", "familyname", "maidenname", "marriedname",
    "dob", "dateofbirth", "birthdate",
    "address", "addressline1", "addressline2", "postcode", "zipcode", "city",
    "idnumber", "documentnumber", "passportnumber", "nationalid", "ssn",
    "socialsecuritynumber", "taxid", "mrz",
    "email", "phone", "phonenumber", "msisdn",
    "cardnumber", "creditcard", "iban", "accountnumber", "sortcode",
    "referenceimage", "referenceimagebytes", "auditimages", "imagebytes",
    "faceimage", "selfie", "video",
    "sessionid",   # liveness session ids are short-lived but still identifying
})

#: Keys that are safe to log verbatim. Anything not here and not sensitive is logged as a type.
SAFE_KEYS = frozenset({
    "tool", "status", "reason", "reasoncode", "source", "listversion", "retrievedat",
    "latencyms", "jurisdiction", "applicantid", "caseid", "phase", "outcome", "tier",
    "classification", "confidence", "hitcount", "matched", "live", "region",
    "modelid", "inferenceoption", "agent", "rulefired", "documenttype", "expired",
    "relationship", "severity", "count", "durationms", "attempt", "version",
})

_SALT = os.environ.get("LOG_FINGERPRINT_SALT") or uuid.uuid4().hex


def fingerprint(value: Any) -> str:
    """Stable within one process, useless outside it."""
    h = hashlib.sha256((_SALT + repr(value)).encode()).hexdigest()
    return "fp:" + h[:12]


def scrub(obj: Any, _depth: int = 0) -> Any:
    """Recursively replace sensitive values with fingerprints and unknown values with types."""
    if _depth > 8:
        return "<max-depth>"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            norm = str(k).replace("_", "").replace("-", "").lower()
            if norm in SENSITIVE_KEYS:
                out[k] = fingerprint(v)
            elif norm in SAFE_KEYS:
                out[k] = scrub(v, _depth + 1) if isinstance(v, (dict, list)) else v
            elif isinstance(v, (dict, list)):
                out[k] = scrub(v, _depth + 1)
            elif isinstance(v, bool) or v is None:
                out[k] = v
            elif isinstance(v, (int, float)):
                out[k] = v
            else:
                # Unknown string field. Do not log the value — log its shape.
                out[k] = "<%s len=%d>" % (type(v).__name__, len(str(v)))
        return out
    if isinstance(obj, list):
        return [scrub(v, _depth + 1) for v in obj[:20]]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(scrub(extra))
        if record.exc_info:
            # Exception *messages* can quote the failing payload, so they stay out. The type and
            # the innermost source location cannot contain applicant data and are what actually
            # make an error diagnosable.
            #
            # Added after a live failure logged `"exc_info": true` and nothing else: three tools
            # were failing identically and the log said only that something had raised. Finding
            # the cause took reproducing the call locally, and a `file:line` would have named it
            # immediately. Type-only redaction was right; type-and-nothing was not.
            exc_type, _, exc_tb = record.exc_info
            payload["exc_type"] = exc_type.__name__ if exc_type else "?"
            frame = exc_tb
            while frame is not None and frame.tb_next is not None:
                frame = frame.tb_next
            if frame is not None:
                code = frame.tb_frame.f_code
                payload["exc_at"] = "%s:%d in %s" % (
                    os.path.basename(code.co_filename), frame.tb_lineno, code.co_name)
        return json.dumps(payload, default=str)


def get_logger(name: str = "kyctools") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
        logger.propagate = False
    return logger


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Structured log line. `exc_info` is routed to the logging call, not into the fields.

    Passed as a field it became the literal JSON `"exc_info": true` while `record.exc_info` stayed
    empty, so `JsonFormatter` never ran its exception branch and even the exception *type* was lost.
    A live failure therefore logged that something had raised and nothing about what — see the
    comment in `JsonFormatter.format`.
    """
    exc_info = fields.pop("exc_info", None)
    logger.log(level, msg, exc_info=exc_info, extra={"fields": fields})
