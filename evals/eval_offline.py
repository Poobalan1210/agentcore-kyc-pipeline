"""The pipeline's decision path, offline: the real tools and the real rules, fake AWS services.

What runs is the code that runs in production. Each evidence tool is invoked through the same
Lambda handler the Gateway invokes (`tools/handler.py`), reads its inputs from a case record and
records its fact there, exactly as deployed; the rules are the decision engine's own Lambda
handler, given the event the state machine builds (`infra/kyc/asl.py`: `PreliminaryTier`,
`FullTier`, `Reassess`), with the same fact names, list version and dispositions.

What is faked is what the code calls out to, and only that:

* **DynamoDB** — an in-memory case record in the wire format (`{"S": ...}`), so the tools'
  serialisation is exercised rather than bypassed.
* **S3 and Textract** — the persona's document "reads" as the lines printed on it, including the
  real two-line machine-readable zone `demo-assets/gen_documents.py` prints, check digits and all.
  The MRZ is then parsed and verified by the tool's own parser.
* **Rekognition** — the document portrait matches the liveness frame at 99%. That is an
  assumption, and the demo makes it true by printing the presenter's own photo on the persona's
  document (`demo-assets/photos/README.md`). `face_match=False` runs the other case.
* **Parameter Store** — the published list update and the demo faults, read through the tools'
  own `controls.py`.

What is *not* here: the agents, the Gateway, Cedar, Step Functions and the clock-driven parts of
the state machine. None of them decides anything — the agents call the tools and explain the
facts; the rules decide — which is the property these evals exist to demonstrate.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

REPO = Path(__file__).resolve().parents[1]
for _path in ("tools", "decision-engine", "review", "infra", "demo-assets"):
    if str(REPO / _path) not in sys.path:
        sys.path.insert(0, str(REPO / _path))

import decision_handler  # noqa: E402
import handler as tool_handler  # noqa: E402  (tools/handler.py, the Gateway's entry point)
from gen_documents import ACT3_BATCH, DemoIdentity, build_mrz  # noqa: E402
from kyc.config import (  # noqa: E402
    CASE_FACT_ATTRIBUTES,
    DECIDED_JURISDICTIONS,
    DEMO_FAULTS_PARAMETER,
    LIST_OVERLAY_PARAMETER,
    SCREENING_LIST_BASE_VERSION,
)

CASE_TABLE = "kyc90-cases"
EVIDENCE_BUCKET = "kyc90-evidence-us-000000000000"
BIOMETRIC_BUCKET = "kyc90-demo-biometric-000000000000"

PHASE1_AGENTS = ("documents", "identity", "fraud", "prescreen")


class AwsError(Exception):
    """Shaped like a botocore ClientError: the tools read the code structurally."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


# ----------------------------------------------------------------------------- fake services
class CaseStore:
    """DynamoDB, as the tools call it: `get_item`, `update_item` with SET clauses, `query`."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, *, TableName: str, Key: Mapping[str, Any], **_: Any) -> dict[str, Any]:
        item = self.items.get((Key["applicantId"]["S"], Key["sk"]["S"]))
        return {"Item": json.loads(json.dumps(item))} if item else {}

    def update_item(self, *, TableName: str, Key: Mapping[str, Any], UpdateExpression: str,
                    ExpressionAttributeValues: Mapping[str, Any],
                    ExpressionAttributeNames: Mapping[str, str] | None = None,
                    ConditionExpression: str | None = None, **_: Any) -> dict[str, Any]:
        key = (Key["applicantId"]["S"], Key["sk"]["S"])
        if ConditionExpression == "attribute_exists(applicantId)" and key not in self.items:
            raise AwsError("ConditionalCheckFailedException")
        if not UpdateExpression.startswith("SET "):
            raise AssertionError(f"unsupported update: {UpdateExpression}")
        item = self.items.setdefault(key, {"applicantId": Key["applicantId"], "sk": Key["sk"]})
        for clause in UpdateExpression[len("SET "):].split(","):
            name, placeholder = (part.strip() for part in clause.split("="))
            name = (ExpressionAttributeNames or {}).get(name, name)
            value = ExpressionAttributeValues[placeholder]
            if not (isinstance(value, dict) and len(value) == 1):
                raise AssertionError(f"not an AttributeValue: {value!r}")
            item[name] = value
        return {}

    def query(self, **_: Any) -> dict[str, Any]:
        return {"Count": 0}

    # ---- helpers for the harness, not part of the client surface
    def put(self, applicant_id: str, attributes: Mapping[str, Any]) -> None:
        item = {"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}}
        item.update({k: _to_av(v) for k, v in attributes.items()})
        self.items[(applicant_id, "CASE")] = item

    def case(self, applicant_id: str) -> dict[str, Any]:
        return self.items[(applicant_id, "CASE")]


class DocumentStore:
    """S3 and Textract together: a stored object, and the lines a text reader returns for it."""

    def __init__(self) -> None:
        self.lines: dict[str, list[str]] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    def add(self, key: str, lines: list[str], metadata: Mapping[str, str]) -> None:
        self.lines[key] = list(lines)
        self.metadata[key] = dict(metadata)

    # s3
    def head_object(self, *, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:
        if Key not in self.lines:
            raise AwsError("404")
        return {"ContentLength": 180_000, "ContentType": "image/jpeg",
                "Metadata": dict(self.metadata[Key])}

    def get_object(self, *, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:
        # The bytes are a handle for `detect_document_text`, which looks them up below.
        return {"Body": io.BytesIO(Key.encode())}

    # textract
    def detect_document_text(self, *, Document: Mapping[str, Any], **_: Any) -> dict[str, Any]:
        key = Document["Bytes"].decode()
        return {"Blocks": [{"BlockType": "LINE", "Text": line} for line in self.lines[key]]}


class FaceService:
    """Rekognition `compare_faces`. `match` is the assumption every run states."""

    def __init__(self, *, match: bool) -> None:
        self.match = match

    def compare_faces(self, **_: Any) -> dict[str, Any]:
        if self.match:
            return {"FaceMatches": [{"Similarity": 99.1}], "UnmatchedFaces": []}
        return {"FaceMatches": [], "UnmatchedFaces": [{"Confidence": 99.0}]}


class Parameters:
    """Parameter Store: the list overlay and the demo faults."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_parameter(self, *, Name: str, **_: Any) -> dict[str, Any]:
        if Name not in self.values:
            raise AwsError("ParameterNotFound")
        return {"Parameter": {"Name": Name, "Value": self.values[Name]}}


class FakeBoto3:
    def __init__(self, clients: Mapping[str, Any]) -> None:
        self._clients = dict(clients)

    def client(self, name: str, **_: Any) -> Any:
        if name not in self._clients:
            raise AssertionError(f"the offline pipeline has no {name!r} client")
        return self._clients[name]


def _to_av(value: Any) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, Mapping):
        return {"M": {k: _to_av(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"L": [_to_av(v) for v in value]}
    raise TypeError(type(value))


# --------------------------------------------------------------------------------- the run
def iso(moment: datetime) -> str:
    """Step Functions' `$now()` / `Execution.StartTime` shape: milliseconds and a Z."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def document_lines(identity: DemoIdentity) -> list[str]:
    """What a text reader returns for the generated document: printed fields, then the MRZ."""
    line1, line2 = build_mrz(identity)
    return [
        "SPECIMEN - NOT A REAL TRAVEL DOCUMENT", "PASSPORT", identity.nationality,
        "SURNAME / NOM", identity.surname, "GIVEN NAMES / PRENOMS", identity.given_names,
        "DOCUMENT NO. / N° DU DOCUMENT", identity.document_number,
        "DATE OF BIRTH / DATE DE NAISSANCE", identity.dob,
        "DATE OF EXPIRY / DATE D'EXPIRATION", identity.expiry,
        line1, line2,
    ]


@dataclass
class PhaseRun:
    """One phase's decision and the event the rules were given."""

    phase: int
    event: dict[str, Any]
    decision: dict[str, Any]

    @property
    def outcome(self) -> str:
        return str(self.decision.get("outcome"))


@dataclass
class Applicant:
    """One persona's journey through the offline pipeline."""

    identity: DemoIdentity
    applicant_id: str
    dispositions: list[dict[str, Any]] = field(default_factory=list)
    phases: dict[str, PhaseRun] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return f"CASE-{self.applicant_id}"


class OfflinePipeline:
    """The tools and the rules over fake AWS, one persona at a time.

    Use as a context manager: it installs the fake `boto3` and the tools' environment for the
    duration, and restores both afterwards, so it can run inside a test session or a script.
    """

    def __init__(self, *, face_match: bool = True) -> None:
        self.cases = CaseStore()
        self.documents = DocumentStore()
        self.parameters = Parameters()
        self.boto3 = FakeBoto3({
            "dynamodb": self.cases, "s3": self.documents, "textract": self.documents,
            "rekognition": FaceService(match=face_match), "ssm": self.parameters,
        })
        self._saved: list[tuple[str, Any]] = []

    # ---- context management -------------------------------------------------------------
    def __enter__(self) -> "OfflinePipeline":
        self._saved = [("boto3", sys.modules.get("boto3"))]
        sys.modules["boto3"] = self.boto3  # type: ignore[assignment]
        env = {"CASE_TABLE": CASE_TABLE, "BIOMETRIC_BUCKET": BIOMETRIC_BUCKET,
               "LIST_OVERLAY_PARAMETER": LIST_OVERLAY_PARAMETER,
               "DEMO_FAULTS_PARAMETER": DEMO_FAULTS_PARAMETER,
               "CONTROL_TTL_SECONDS": "0", "FORCE_UNAVAILABLE": ""}
        self._env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.parameters.values[LIST_OVERLAY_PARAMETER] = json.dumps({"entries": []})
        self.parameters.values[DEMO_FAULTS_PARAMETER] = "{}"
        self._reset_tool_cache()
        # The tools log every call as JSON to stdout, which is right for Lambda and noise here.
        self._log_levels = {name: logging.getLogger(name).level
                            for name in ("kyctools", "kyctools.handler")}
        for name in self._log_levels:
            logging.getLogger(name).setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc: Any) -> None:
        for name, module in self._saved:
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name, level in getattr(self, "_log_levels", {}).items():
            logging.getLogger(name).setLevel(level)
        self._reset_tool_cache()

    @staticmethod
    def _reset_tool_cache() -> None:
        from kyctools import controls

        controls.reset_cache()

    @staticmethod
    def now() -> datetime:
        """Wall-clock time, never a simulated one. The tools stamp each fact with the real time
        (second resolution), and the rules treat a fact recorded more than five seconds before
        its phase started as stale, so every timestamp the harness hands the rules has to come
        from the same clock the tools read."""
        return datetime.now(timezone.utc)

    # ---- runtime controls -----------------------------------------------------------------
    def publish_list_update(self, entries: list[Mapping[str, Any]],
                            version: str = "ACT3") -> None:
        self.parameters.values[LIST_OVERLAY_PARAMETER] = json.dumps(
            {"version": version, "entries": list(entries)})
        self._reset_tool_cache()

    def set_faults(self, faults: Mapping[str, Any]) -> None:
        self.parameters.values[DEMO_FAULTS_PARAMETER] = json.dumps(dict(faults))
        self._reset_tool_cache()

    def current_list_version(self) -> str:
        """`asl.CURRENT_LIST_VERSION`: the base list, plus the update's version when it has
        entries."""
        overlay = json.loads(self.parameters.values.get(LIST_OVERLAY_PARAMETER) or "{}")
        if overlay.get("entries"):
            return f"{SCREENING_LIST_BASE_VERSION}+{overlay.get('version')}"
        return SCREENING_LIST_BASE_VERSION

    # ---- intake -----------------------------------------------------------------------------
    def submit(self, identity: DemoIdentity, *, applicant_id: str | None = None,
               declared: Mapping[str, str] | None = None) -> Applicant:
        """What `POST /applicants` and the liveness result leave on the case before Phase 1.

        The document always carries `identity`. `declared` overrides what the applicant typed,
        which is how a declared-versus-document mismatch is staged.
        """
        applicant = Applicant(identity=identity,
                              applicant_id=applicant_id or f"APP-EVAL-{identity.persona_id}")
        key = f"{applicant.applicant_id}/{identity.document_type}-eval"
        self.documents.add(key, document_lines(identity), {
            "documenttype": identity.document_type,
            "issuingcountry": identity.issuing_country, "synthetic": "true"})
        typed = {"givenNames": identity.given_names, "surname": identity.surname,
                 "dateOfBirth": identity.dob, "nationality": identity.mrz_country,
                 "address": identity.address, **dict(declared or {})}
        self.cases.put(applicant.applicant_id, {
            "caseId": applicant.case_id, "state": "RECEIVED", "jurisdiction":
            identity.jurisdiction, "productApplied": "current_account",
            "createdAt": iso(self.now()),
            "declaredIdentity": typed,
            "documents": [{"type": identity.document_type, "bucket": EVIDENCE_BUCKET,
                           "key": key, "issuingCountry": identity.issuing_country}],
            "liveness": {"result": "PASS", "region": "us-east-1",
                         "referenceImageKey": f"{applicant.applicant_id}/reference.jpg"},
        })
        return applicant

    # ---- tools --------------------------------------------------------------------------
    def call_tool(self, applicant: Applicant, tool: str) -> dict[str, Any]:
        """Through the Lambda handler the Gateway invokes, so dispatch and the envelope run too."""
        return tool_handler.handler({"toolName": tool,
                                "arguments": {"applicantId": applicant.applicant_id,
                                              "name": applicant.identity.full_name}})

    def _facts(self, applicant: Applicant) -> dict[str, Any]:
        item = self.cases.case(applicant.applicant_id)
        return {name: (item.get(attr) or {}).get("S") for name, attr in
                CASE_FACT_ATTRIBUTES.items()}

    def _declared(self, applicant: Applicant) -> dict[str, str]:
        """`asl._DECLARED_IDENTITY`: the four fields, read from the case record."""
        declared = self.cases.case(applicant.applicant_id)["declaredIdentity"]["M"]
        return {k: declared[k]["S"]
                for k in ("surname", "givenNames", "dateOfBirth", "nationality")}

    @staticmethod
    def _agent_run(agent: str, narrative: str) -> dict[str, Any]:
        """`asl._ok_record`: an agent that ran and replied. The rules read only that it did."""
        return {"agent": agent, "status": "OK", "content": [{"Text": narrative}],
                "stopReason": "end_turn", "latencyMs": 0, "totalTokens": 0}

    # ---- the phases ---------------------------------------------------------------------
    def phase1(self, applicant: Applicant, *, narrative: str = "Checked.") -> PhaseRun:
        """`CheckPreconditions` (jurisdiction), the four evidence tools, then `PreliminaryTier`."""
        started = self.now()
        if applicant.identity.jurisdiction not in DECIDED_JURISDICTIONS:
            run = PhaseRun(phase=1, event={}, decision={"outcome": "JURISDICTION_NOT_DECIDED"})
            applicant.phases["phase1"] = run
            return run
        for tool in ("s3_get_document", "rekognition_compare_faces", "velocity_check",
                     "sanctions_screen"):
            self.call_tool(applicant, tool)
        event = {
            "phase": 1,
            "asOf": iso(self.now()),
            "phaseStartedAt": iso(started),
            "currentListVersion": self.current_list_version(),
            "productApplied": "current_account",
            "declaredIdentity": self._declared(applicant),
            "screeningDispositions": list(applicant.dispositions),
            "livenessFailures": 0,
            "facts": self._facts(applicant),
            "agentResults": {a: self._agent_run(a, narrative) for a in PHASE1_AGENTS},
        }
        return self._decide(applicant, "phase1", event)

    def review(self, applicant: Applicant, *, action: str = "APPROVE",
               reviewer: str = "eval-reviewer") -> list[str]:
        """A reviewer's APPROVE, as the review handler records it: the PROBABLE and POSSIBLE
        alerts on the recorded screening become dispositions. A CONFIRMED one never does."""
        import decisions

        if action != "APPROVE":
            return []
        case = {"factScreening": self.cases.case(applicant.applicant_id)
                .get("factScreening", {}).get("S")}
        if decisions.confirmed_matches(case):
            raise ValueError("the review handler refuses to approve a confirmed match")
        screening = json.loads(case["factScreening"] or "{}")
        by_id = {h["entryId"]: h for h in screening.get("hits") or ()}
        cleared = list(decisions.clearable_alerts(case))
        for entry_id in cleared:
            applicant.dispositions.append({
                "entryId": entry_id, "classification": by_id[entry_id]["classification"],
                "listVersion": screening.get("listVersion"), "phase": "phase1",
                "reviewerId": reviewer, "decidedAt": iso(self.now())})
        return cleared

    def phase2(self, applicant: Applicant, *, narrative: str = "Checked.") -> PhaseRun:
        """The two Phase 2 tools, a re-screen if the list moved, then `FullTier`."""
        started = self.now()
        for tool in ("pep_lookup", "adverse_media"):
            self.call_tool(applicant, tool)
        agents = {"compliance": self._agent_run("compliance", narrative)}
        screening = json.loads(self._facts(applicant)["screening"] or "{}")
        if screening.get("listVersion") != self.current_list_version():
            self.call_tool(applicant, "sanctions_screen")
            agents["prescreen"] = self._agent_run("prescreen", narrative)
        event = {
            "phase": 2,
            "asOf": iso(self.now()),
            "phaseStartedAt": iso(started),
            "productApplied": "current_account",
            "declaredIdentity": self._declared(applicant),
            "screeningDispositions": list(applicant.dispositions),
            "facts": self._facts(applicant),
            "currentListVersion": self.current_list_version(),
            "agentResults": agents,
        }
        return self._decide(applicant, "phase2", event)

    def phase3(self, applicant: Applicant, *, event_id: str = "LIST-UPDATE-ACT3",
               severity: str = "HIGH") -> PhaseRun:
        """A sanctions list update reaching an open account: re-screen, then `Reassess`."""
        started = self.now()
        self.call_tool(applicant, "sanctions_screen")
        day0_record = (applicant.phases.get("phase2") or applicant.phases["phase1"]).decision
        risk_event = {
            "eventId": event_id, "eventType": "SANCTIONS_LIST_UPDATE", "severity": severity,
            "customerId": applicant.applicant_id, "occurredAt": iso(started),
            "detail": {"listUpdate": "ACT3", "entryIds": sorted(e["entryId"] for e in ACT3_BATCH)},
        }
        event = {
            "phase": 3,
            "asOf": iso(self.now()),
            "decidedAt": iso(started),
            "phaseStartedAt": iso(started),
            "currentListVersion": self.current_list_version(),
            "productApplied": "current_account",
            "declaredIdentity": self._declared(applicant),
            "screeningDispositions": list(applicant.dispositions),
            "facts": self._facts(applicant),
            "riskEvent": risk_event,
            "day0": {"caseId": applicant.case_id, "tier": day0_record.get("tier") or "LOW",
                     "outcome": day0_record.get("outcome"), "decidedAt": iso(started),
                     "version": 1},
        }
        return self._decide(applicant, "phase3", event)

    def _decide(self, applicant: Applicant, phase: str, event: dict[str, Any]) -> PhaseRun:
        decision = decision_handler.lambda_handler(json.loads(json.dumps(event)))
        run = PhaseRun(phase=int(phase[-1]), event=event, decision=decision)
        applicant.phases[phase] = run
        return run

    def fact(self, applicant: Applicant, name: str) -> dict[str, Any] | None:
        raw = self._facts(applicant).get(name)
        return json.loads(raw) if raw else None


@contextlib.contextmanager
def pipeline(**kwargs: Any) -> Iterator[OfflinePipeline]:
    with OfflinePipeline(**kwargs) as running:
        yield running
