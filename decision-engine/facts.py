"""Tier inputs and edge facts, read from the facts the tools recorded — the engine half of "code
decides on facts".

Pure. No clock, no network, no I/O, no model. Every timestamp is supplied by the caller.

## What changed, and why

The engine used to derive its inputs from what the agents *said*: document confidence out of the
Documents agent's reply, "matched" out of the Pre-Screen agent's prose, velocity out of the Fraud
agent's JSON when it produced JSON and out of phrase matching when it did not. That made every
outcome depend on a model's wording. Measured in this build: an agent told a screening source was
UNAVAILABLE replied "which means no match was found" (spike/sp.txt), and the same finding arrived as
`{"confidence": 0.96}` in one run and as a sentence in the next (F-89).

Now each evidence tool records its own result on the case record (`tools/kyctools/case_record.py`)
and this module reads those records. An agent's reply is what a reviewer reads. It is not an input.

## The reading rules

* **A mandatory fact that is missing, stale, or not performed routes to a human.** "Not performed"
  and "performed and found nothing" are different facts, and only the second can approve anyone.
* **Freshness is checked per phase.** A fact recorded before the phase started belongs to an earlier
  run of the same case — a resubmission after a step-up, for instance — and is ignored.
* **Document confidence is verified, not reported.** 1.0 when the machine-readable zone was read and
  every check digit passed, 0.0 otherwise. The model's opinion of its own extraction is no longer a
  number anyone tiers on.
* **Only verified document fields feed the person rules.** Expiry and age come from the passport's
  machine-readable zone when its check digits pass; an unverified zone contributes nothing.
* **A reviewer's disposition clears a PROBABLE or POSSIBLE alert, never a CONFIRMED one.** A
  confirmed match prohibits whatever else is true (R4.3).
* **A screening of an outdated list is not a screening.** When the caller says which list version
  is current, a screening fact recorded against any other version is treated as absent. Without
  this, a list update published while a case waited for a reviewer would never reach that case:
  the account would open, and its restrictions lift, on a screening of the old list.

Dates are compared as integers here rather than through `datetime`, because the rule layer may not
import a clock module at all (`test_tiering.TestPurity`). A date parser is small; a clock import is
exactly how a pure layer stops being one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

import config
from models import EdgeVerdict, Phase1Outcome

# ------------------------------------------------------------------ dates, without a clock
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def parse_date(value: Any) -> tuple[int, int, int] | None:
    """`YYYY-MM-DD` (or the date part of a timestamp) to `(y, m, d)`, or None if not a real date."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    text = value[:10]
    if text[4] != "-" or text[7] != "-":
        return None
    try:
        y, m, d = int(text[0:4]), int(text[5:7]), int(text[8:10])
    except ValueError:
        return None
    if not 1 <= m <= 12:
        return None
    limit = 29 if (m == 2 and _is_leap(y)) else _DAYS_IN_MONTH[m - 1]
    if not 1 <= d <= limit:
        return None
    return y, m, d


def age_on(dob: Any, as_of: Any) -> int | None:
    """Whole years between two ISO dates, or None if either cannot be read."""
    born, today = parse_date(dob), parse_date(as_of)
    if born is None or today is None:
        return None
    years = today[0] - born[0] - ((today[1], today[2]) < (born[1], born[2]))
    return years if years >= 0 else None


def _days_from_civil(y: int, m: int, d: int) -> int:
    """Days since 1970-01-01 (Howard Hinnant's algorithm)."""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def epoch_seconds(value: Any) -> int | None:
    """Seconds since the epoch for a UTC ISO timestamp, or None if it cannot be read.

    Accepts `YYYY-MM-DDTHH:MM:SS`, optional fractional seconds, and a `Z` or `+00:00` suffix —
    the two shapes that meet here: the tools' `2026-12-02T18:04:05Z` and JSONata's `$now()`,
    `2026-12-02T18:04:05.123Z`. Anything carrying a non-UTC offset is refused rather than guessed.
    """
    date_part = parse_date(value)
    if date_part is None or len(value) < 19 or value[10] not in "Tt ":
        return None
    clock, rest = value[11:19], value[19:]
    if clock[2] != ":" or clock[5] != ":":
        return None
    try:
        hh, mm, ss = int(clock[0:2]), int(clock[3:5]), int(clock[6:8])
    except ValueError:
        return None
    if rest.startswith("."):
        rest = rest.lstrip(".0123456789")
    if rest not in ("", "Z", "z", "+00:00"):
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 61):
        return None
    return _days_from_civil(*date_part) * 86400 + hh * 3600 + mm * 60 + ss


# ------------------------------------------------------------------ reading one fact
#: Fact name -> the agent whose tool records it. A missing mandatory fact marks that agent
#: UNAVAILABLE, which is how it reaches `select_phase*_outcome`'s fail-closed branch.
FACT_OWNER = {
    "documentExtraction": "documents",
    "faceMatch": "identity",
    "screening": "prescreen",
    "velocity": "fraud",
    "pep": "compliance",
    "adverseMedia": "compliance",
}


def load_fact(raw: Any) -> dict[str, Any] | None:
    """A fact as stored (canonical JSON), DynamoDB-typed (`{"S": ...}`), or already decoded."""
    if isinstance(raw, Mapping) and len(raw) == 1 and "S" in raw:
        raw = raw["S"]
    if isinstance(raw, Mapping) and len(raw) == 1 and "NULL" in raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return dict(raw) if isinstance(raw, Mapping) else None


def fact_status(fact: Mapping[str, Any] | None, *, fresh_after: int | None) -> str:
    """OK, ABSENT, STALE, UNAVAILABLE, INVALID or NOT_ESTABLISHED."""
    if fact is None:
        return "ABSENT"
    if fresh_after is not None:
        recorded = epoch_seconds(fact.get("recordedAt"))
        # A fact with no readable timestamp cannot be shown to be fresh, so it is not.
        if recorded is None or recorded < fresh_after:
            return "STALE"
    if fact.get("performed") is False:
        status = str(fact.get("status") or "UNAVAILABLE").upper()
        return status if status in ("INVALID", "NOT_ESTABLISHED") else "UNAVAILABLE"
    return "OK"


# ------------------------------------------------------------------ the reading
@dataclass(frozen=True, kw_only=True)
class FactReading:
    #: The document extraction the identity comparison may use: fresh in Phase 1, as recorded after.
    extraction: dict[str, Any] | None
    #: Tier inputs, camelCase, in the shape `decision_handler` parses.
    inputs: dict[str, Any]
    #: Agents whose mandatory fact could not be used. Each becomes UNAVAILABLE -> a human.
    undetermined: tuple[str, ...]
    #: EdgeFacts keys (camelCase) derived from verified facts.
    edge_facts: dict[str, Any]
    #: Verdicts raised directly by a fact, such as a face mismatch.
    verdicts: tuple[EdgeVerdict, ...]
    #: What was read and how, for the decision record (R4.9, R18.2).
    summary: dict[str, Any] = field(default_factory=dict)


def _verified_mrz(extraction: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not extraction or not extraction.get("documentReadable") or not extraction.get("mrzPresent"):
        return None
    mrz = extraction.get("mrz")
    if not isinstance(mrz, Mapping) or mrz.get("checkDigitsValid") is not True:
        return None
    return mrz


def _document_confidence(extraction: Mapping[str, Any] | None) -> float:
    return 1.0 if _verified_mrz(extraction) is not None else 0.0


def _dispositioned(dispositions: Any) -> set[str]:
    out: set[str] = set()
    for d in dispositions or ():
        if isinstance(d, Mapping) and d.get("entryId"):
            out.add(str(d["entryId"]))
        elif isinstance(d, str):
            out.add(d)
    return out


def _screening_inputs(fact: Mapping[str, Any], cleared: set[str]) -> tuple[list[dict], dict]:
    """Alerts in the handler's `screeningHits` shape, minus reviewer-cleared ones."""
    hits, removed = [], []
    for hit in fact.get("hits") or ():
        if not isinstance(hit, Mapping):
            continue
        strength = str(hit.get("classification") or "").upper()
        entry = str(hit.get("entryId") or "")
        if strength == "NO_MATCH" or not entry:
            continue
        if strength in ("PROBABLE", "POSSIBLE") and entry in cleared:
            removed.append(entry)
            continue
        matched_on = ["name"]
        if hit.get("dobMatch"):
            matched_on.append("dob")
        if hit.get("countryMatch"):
            matched_on.append("country")
        hits.append({"listId": entry, "strength": strength, "matchedOn": matched_on,
                     "score": hit.get("score")})
    strongest = "NO_MATCH"
    for level in ("CONFIRMED", "PROBABLE", "POSSIBLE"):
        if any(h["strength"] == level for h in hits):
            strongest = level
            break
    return hits, {"strongestUncleared": strongest, "clearedByReviewer": sorted(removed),
                  "listVersion": fact.get("listVersion"),
                  "excluded": [e.get("entryId") for e in fact.get("excluded") or ()
                               if isinstance(e, Mapping)]}


def _velocity_inputs(status: str, fact: Mapping[str, Any] | None) -> dict[str, Any]:
    if status == "OK" and fact is not None:
        signals = fact.get("signals") or {}
        detail = [f"deviceApplications={fact.get('deviceApplications')}",
                  f"ipApplications={fact.get('ipApplications')}"]
        critical = bool(signals.get("criticalDeviceReuse") or signals.get("criticalIpReuse"))
        return {"critical": critical, "detail": detail}
    # NOT_ESTABLISHED: the check ran and there was nothing to check with. Recorded, not hidden.
    return {"critical": False,
            "detail": ["velocity not established: no device or IP identifier captured"]}


def _nationality(mrz: Mapping[str, Any] | None, declared: Mapping[str, Any]) -> tuple[str, str]:
    if mrz and mrz.get("nationality"):
        return str(mrz["nationality"]).upper(), "MRZ"
    value = str(declared.get("nationality") or "").strip().upper()
    return (value, "DECLARED") if value else ("", "NOT_ESTABLISHED")


def _jurisdiction_risk(nationality: str) -> str:
    return "HIGH" if nationality in config.HIGH_RISK_NATIONALITIES else "LOW"


def _face_verdicts(status: str, fact: Mapping[str, Any] | None) -> tuple[EdgeVerdict, ...]:
    """Face-match results the rules act on. Only a performed comparison can raise one."""
    if status == "INVALID":
        return (EdgeVerdict(
            case_id="IDV-3", requirement="IDV/face-match",
            outcome=Phase1Outcome.STEP_UP,
            reason_code="FACE_IMAGE_UNREADABLE",
            required_item="A clear photo of the identity document's photo page",
            explanation=("The document portrait or the liveness frame could not be read, so the "
                         "face comparison could not run. A clearer image resolves it."),
            signals={"faceMatchPerformed": False},
        ),)
    if status != "OK" or fact is None:
        return ()
    if fact.get("faceDetected") is False:
        return (EdgeVerdict(
            case_id="IDV-2", requirement="IDV/face-match",
            outcome=Phase1Outcome.STEP_UP,
            reason_code="NO_FACE_ON_DOCUMENT",
            required_item="A clear photo of the identity document's photo page, face visible",
            explanation=("No face could be found in the document image, so it could not be "
                         "compared with the liveness check."),
            signals={"faceDetected": False},
        ),)
    if fact.get("match") is not True:
        return (EdgeVerdict(
            case_id="IDV-1", requirement="IDV/face-match",
            outcome=Phase1Outcome.MANUAL_REVIEW,
            reason_code="FACE_MISMATCH",
            explanation=("The face on the identity document did not match the live selfie. A "
                         "reviewer compares them; this is not a rejection, because lighting, age "
                         "and image quality all produce the same result."),
            signals={"faceMatch": False, "similarityBand": fact.get("similarityBand")},
        ),)
    return ()


def _document_verdicts(status: str, fact: Mapping[str, Any] | None) -> tuple[EdgeVerdict, ...]:
    if status == "INVALID":
        return (EdgeVerdict(
            case_id="DOC-1", requirement="R38",
            outcome=Phase1Outcome.STEP_UP,
            reason_code=str((fact or {}).get("reasonCode") or "DOCUMENT_UNUSABLE"),
            required_item="A readable identity document",
            explanation="No usable identity document was found on the application.",
        ),)
    return ()


def read_facts(*, phase: int, facts: Mapping[str, Any], declared: Mapping[str, Any] | None,
               as_of: str | None, phase_started_at: str | None,
               dispositions: Any = None, live_failures: Any = None,
               current_list_version: str | None = None) -> FactReading:
    """Everything the tier and outcome rules need, read from the recorded facts of one phase.

    Phase 1 owns the document, face, screening and velocity facts. Phase 2 owns PEP and adverse
    media and reads the Phase 1 facts as recorded. Phase 3 owns a fresh screening fact (a list
    update) and reads everything else as recorded.

    `current_list_version` is the screening list in force when the caller is deciding. A screening
    fact recorded against another version is LIST_OUTDATED, which no phase can decide on.
    """
    declared = declared if isinstance(declared, Mapping) else {}
    started = epoch_seconds(phase_started_at) if phase_started_at else None
    fresh_after = (started - config.FACT_FRESHNESS_TOLERANCE_SECONDS
                   if started is not None else None)

    owned = {1: ("documentExtraction", "faceMatch", "screening", "velocity"),
             2: ("pep", "adverseMedia"),
             3: ("screening",)}[phase]

    loaded = {name: load_fact(facts.get(name)) for name in FACT_OWNER}
    status = {name: fact_status(loaded[name],
                                fresh_after=fresh_after if name in owned else None)
              for name in FACT_OWNER}
    screened_against = (loaded["screening"] or {}).get("listVersion")
    if (current_list_version is not None and status["screening"] == "OK"
            and screened_against != current_list_version):
        status["screening"] = "LIST_OUTDATED"

    undetermined: list[str] = []
    inputs: dict[str, Any] = {}
    edge: dict[str, Any] = {}
    verdicts: list[EdgeVerdict] = []
    summary: dict[str, Any] = {"phase": phase, "facts": {
        name: {"status": status[name],
               "recordedAt": (loaded[name] or {}).get("recordedAt")}
        for name in FACT_OWNER}}
    summary["listCurrency"] = {"screenedAgainst": screened_against,
                               "currentListVersion": current_list_version,
                               "checked": current_list_version is not None}

    def require(name: str, *, allowed: tuple[str, ...] = ("OK",)) -> bool:
        if status[name] in allowed:
            return True
        owner = FACT_OWNER[name]
        if owner not in undetermined:
            undetermined.append(owner)
        return False

    extraction = loaded["documentExtraction"] if status["documentExtraction"] == "OK" else None
    mrz = _verified_mrz(extraction)

    # ---- documents
    if phase == 1:
        if status["documentExtraction"] == "INVALID":
            verdicts.extend(_document_verdicts("INVALID", loaded["documentExtraction"]))
        elif require("documentExtraction"):
            inputs["documentConfidence"] = _document_confidence(extraction)
    else:
        # Carried: the phase that owned it has already acted on it.
        inputs["documentConfidence"] = _document_confidence(extraction)
    summary["documentConfidenceSource"] = "MRZ_CHECK_DIGITS"

    # ---- screening
    # Mandatory in every phase. Phases 1 and 3 own it and check it is fresh; Phase 2 reads what
    # Phase 1 recorded. No phase may approve a case whose screening fact cannot be read.
    cleared = _dispositioned(dispositions)
    if require("screening"):
        hits, screening_summary = _screening_inputs(loaded["screening"], cleared)
        inputs["screeningHits"] = hits
        inputs["preScreenMatched"] = any(h["strength"] == "CONFIRMED" for h in hits)
        summary["screening"] = screening_summary

    # ---- identity (face)
    if phase == 1:
        face_status = status["faceMatch"]
        if face_status in ("OK", "INVALID"):
            verdicts.extend(_face_verdicts(face_status, loaded["faceMatch"]))
        else:
            require("faceMatch")
        face = loaded["faceMatch"] or {}
        summary["faceMatch"] = {"status": face_status, "match": face.get("match"),
                                "faceDetected": face.get("faceDetected"),
                                "similarityBand": face.get("similarityBand")}

    # ---- velocity
    velocity_status = status["velocity"]
    if phase == 1:
        require("velocity", allowed=("OK", "NOT_ESTABLISHED"))
    if velocity_status in ("OK", "NOT_ESTABLISHED") or phase != 1:
        inputs["velocitySignals"] = _velocity_inputs(velocity_status, loaded["velocity"])
    summary["velocity"] = {"status": velocity_status}

    # ---- Phase 2 screening of politically exposed persons and media
    if phase == 2:
        if require("pep"):
            inputs["pepStatus"] = str(loaded["pep"].get("pepStatus") or "NONE").upper()
        if require("adverseMedia"):
            inputs["adverseMediaSeverity"] = str(
                loaded["adverseMedia"].get("maxSeverity") or "NONE").upper()
    elif phase == 3:
        # Phase 3 re-screens sanctions only. PEP and media are read as Phase 2 recorded them;
        # a customer re-screened before Phase 2 finished has none yet, which is recorded.
        pep = loaded["pep"] if status["pep"] == "OK" else None
        media = loaded["adverseMedia"] if status["adverseMedia"] == "OK" else None
        inputs["pepStatus"] = str((pep or {}).get("pepStatus") or "NONE").upper()
        inputs["adverseMediaSeverity"] = str((media or {}).get("maxSeverity") or "NONE").upper()
        summary["phase2ScreeningAvailable"] = pep is not None and media is not None

    # ---- country risk
    nationality, nationality_source = _nationality(mrz, declared)
    inputs["jurisdictionRisk"] = _jurisdiction_risk(nationality)
    summary["jurisdictionRisk"] = {"nationality": nationality or None,
                                   "source": nationality_source,
                                   "list": config.HIGH_RISK_NATIONALITIES_SOURCE}

    # ---- person and document rules, from verified fields only (Phase 1)
    if phase == 1:
        if mrz is not None:
            expiry, today = parse_date(mrz.get("expiryDate")), parse_date(as_of)
            if expiry is not None and today is not None:
                edge["documentExpired"] = expiry < today
            issuer = str(mrz.get("issuingState") or "").upper()
            if issuer:
                edge["issuingCountrySupported"] = issuer in config.SUPPORTED_DOCUMENT_ISSUERS
        dob = mrz.get("dateOfBirth") if mrz is not None else declared.get("dateOfBirth")
        age = age_on(dob, as_of)
        if age is not None:
            edge["age"] = age
        if isinstance(live_failures, int) and live_failures > 0:
            edge["livenessFailureCount"] = live_failures
        summary["personFactsSource"] = "MRZ" if mrz is not None else "DECLARED"

    return FactReading(extraction=extraction, inputs=inputs, undetermined=tuple(undetermined),
                       edge_facts=edge, verdicts=tuple(verdicts), summary=summary)
