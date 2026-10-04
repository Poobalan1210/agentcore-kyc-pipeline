

class TestSpanLogGroupFanOut:
    """`SPAN_LOG_GROUP` is a comma-separated list of per-agent AgentCore trace groups.

    Pins the live defect where it was a single non-existent group
    (`/aws/bedrock-agentcore/kyc90`): the span query returned nothing on every run, so every
    decision record was refused as incomplete and every applicant was fail-closed to a human.
    """

    class _FakeLogs:
        def __init__(self, by_group):
            self.by_group = by_group
            self.queried = []

        def filter_log_events(self, *, logGroupName, filterPattern, limit):
            self.queried.append(logGroupName)
            if logGroupName not in self.by_group:
                raise RuntimeError("ResourceNotFoundException")
            return {"events": [{"message": m} for m in self.by_group[logGroupName]]}

    def test_queries_every_group_and_merges(self):
        from audit_lambda import _spans_for
        client = self._FakeLogs({
            "g-documents": ['{"traceId":"t1","name":"AGENT_RUN"}'],
            "g-identity": ['{"traceId":"t1","name":"TOOL_CALL"}'],
        })
        spans = _spans_for("t1", log_group="g-documents,g-identity", client=client)
        assert [s["name"] for s in spans] == ["AGENT_RUN", "TOOL_CALL"]
        assert client.queried == ["g-documents", "g-identity"]

    def test_one_missing_group_does_not_hide_the_others(self):
        from audit_lambda import _spans_for
        client = self._FakeLogs({"g-ok": ['{"traceId":"t1","name":"DECISION"}']})
        spans = _spans_for("t1", log_group="g-missing,g-ok", client=client)
        assert [s["name"] for s in spans] == ["DECISION"], (
            "a non-existent group must not suppress spans from the groups that do exist"
        )

    def test_blank_and_whitespace_entries_ignored(self):
        from audit_lambda import _spans_for
        client = self._FakeLogs({"g-ok": ['{"traceId":"t1","name":"DECISION"}']})
        assert _spans_for("t1", log_group=" , g-ok , ", client=client)
        assert _spans_for("t1", log_group="  ,  ", client=client) == []


class TestOtelTraceIdDerivation:
    """Spans are keyed by a 32-hex OTEL trace id, not by anything merely populated.

    Pins the live defect where the ASL passed the Step Functions execution name as `traceId`:
    always present, never an id anything is indexed by, so the span query matched nothing and
    every decision was fail-closed as MISSING_SPANS.
    """

    XRAY = "Root=1-6a9bfd6a-6ecd4731cde923a5b99cebd4;Parent=53995c3f42cd8ad8;Sampled=1"
    EXPECTED = "6a9bfd6a6ecd4731cde923a5b99cebd4"

    def test_derives_from_xray_header_when_supplied_value_is_not_a_trace_id(self, monkeypatch):
        from audit_lambda import _otel_trace_id
        monkeypatch.setenv("_X_AMZN_TRACE_ID", self.XRAY)
        assert _otel_trace_id("fresh-clean-1788609131") == self.EXPECTED

    def test_prefers_a_real_supplied_trace_id(self, monkeypatch):
        from audit_lambda import _otel_trace_id
        monkeypatch.setenv("_X_AMZN_TRACE_ID", self.XRAY)
        other = "aaaabbbbccccddddeeeeffff00001111"
        assert _otel_trace_id(other) == other

    def test_normalises_case(self, monkeypatch):
        from audit_lambda import _otel_trace_id
        monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)
        assert _otel_trace_id(self.EXPECTED.upper()) == self.EXPECTED

    def test_empty_when_no_trace_available(self, monkeypatch):
        from audit_lambda import _otel_trace_id
        monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)
        assert _otel_trace_id("not-a-trace") == ""

    def test_execution_name_alone_never_becomes_a_trace_id(self, monkeypatch):
        """The specific regression: a populated non-id must not be passed through."""
        from audit_lambda import _otel_trace_id
        monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)
        assert _otel_trace_id("clean-run3-1788608953") != "clean-run3-1788608953"


class TestCaseRecordEvidenceFallback:
    """The audit record must be satisfiable from what the orchestrator persisted.

    Pins the live defect where `WriteDecisionRecord` fail-closed every single decision with
    MISSING_SPANS=[AGENT_RUN, TOOL_CALL, DECISION]. Every documented prerequisite was already in
    place (Transaction Search ACTIVE, 100% sampling, UNIFIED_TRACES_DESTINATION_ENABLED=true,
    X-Ray Active) and no span carrying agent_name/tool_name/outcome existed anywhere, so the 90
    second auto-approve path was unreachable for every applicant.
    """

    CASE = {
        "phase1Evidence": {"S": '[{"agent":"documents","content":[{"Text":"<thinking>x</thinking>"}]},'
                                '{"agent":"identity","content":[]},'
                                '{"agent":"fraud","content":[]},'
                                '{"agent":"prescreen","content":[]}]'},
        "documentExtraction": {"S": '{"mrzPresent":true,"documentReadable":true}'},
        "findingFraud": {"S": '{"velocityCritical":false}'},
    }
    DECISION = {"outcome": "PROVISIONAL_APPROVE_RESTRICTED",
                "rule": "O1.provisional_approve", "tier": "LOW"}

    def test_case_record_yields_complete_evidence(self):
        from spans import evidence_from_case_record
        ev = evidence_from_case_record(self.CASE, decision=self.DECISION)
        assert ev.complete, ev.missing
        assert [a.agent_name for a in ev.agent_runs] == \
            ["documents", "identity", "fraud", "prescreen"]
        assert ev.decision.outcome == "PROVISIONAL_APPROVE_RESTRICTED"

    def test_records_agent_order(self):
        """R18.1 wants the order agents ran in, not just the set."""
        from spans import evidence_from_case_record
        ev = evidence_from_case_record(self.CASE, decision=self.DECISION)
        assert [a.agent_name for a in ev.agent_runs][0] == "documents"

    def test_no_decision_still_reports_incomplete(self):
        from spans import evidence_from_case_record
        ev = evidence_from_case_record(self.CASE, decision=None)
        assert not ev.complete and "DECISION" in ev.missing

    def test_no_phase_evidence_reports_missing_agent_run(self):
        from spans import evidence_from_case_record
        ev = evidence_from_case_record({"documentExtraction": {"S": "{}"}},
                                       decision=self.DECISION)
        assert "AGENT_RUN" in ev.missing

    def test_arguments_are_not_invented(self):
        """The tool request was never persisted; None states that instead of fabricating it."""
        from spans import evidence_from_case_record
        ev = evidence_from_case_record(self.CASE, decision=self.DECISION)
        assert all(t.arguments is None for t in ev.tool_calls)

    def test_source_is_disclosed_in_notes(self):
        from spans import evidence_from_case_record
        ev = evidence_from_case_record(self.CASE, decision=self.DECISION)
        assert any("case record" in n for n in ev.notes), (
            "the record must say where its evidence came from"
        )


class TestRecordedFactsAreToolEvidence:
    """The evidence tools record their results on the case, and the rules decide on those facts.
    The same facts are the record's TOOL_CALL evidence; the old `finding*` attributes are no longer
    written, so reading only them left every new record without a tool call."""

    CASE = {
        "phase1Evidence": {"S": '[{"agent":"prescreen","content":[{"Text":"screened"}]}]'},
        "factScreening": {"S": '{"performed":true,"strongest":"NO_MATCH","hits":[]}'},
        "factFaceMatch": {"S": '{"performed":true,"match":true}'},
    }

    def test_facts_are_named_by_their_gateway_action(self):
        from spans import evidence_from_case_record
        ev = evidence_from_case_record(self.CASE, decision={"outcome": "REJECT"})
        assert ev.complete, ev.missing
        names = {t.tool_name for t in ev.tool_calls}
        assert names == {"SanctionsScreen___sanctions_screen",
                         "FaceCompare___rekognition_compare_faces"}

    def test_every_fact_attribute_the_tools_write_is_read(self):
        """If a tool starts recording a new fact, the audit record must know about it."""
        from kyctools.case_record import FACT_ATTRIBUTES
        from spans import _TOOL_RESULT_ATTRIBUTES
        read = {attribute for attribute, _ in _TOOL_RESULT_ATTRIBUTES}
        assert set(FACT_ATTRIBUTES.values()) <= read

    def test_phase_three_reads_its_own_evidence(self):
        from spans import evidence_from_case_record
        case = {"phase3Evidence": {"S": '[{"agent":"prescreen","content":[]}]'},
                "factScreening": {"S": "{}"}}
        ev = evidence_from_case_record(case, decision={"outcome": "FREEZE"}, phase="phase3")
        assert [a.agent_name for a in ev.agent_runs] == ["prescreen"]


class TestEngineResponseFieldsReachTheRecord:
    """The state machine passes the engine's response as it is, in camelCase. Reading only the
    snake_case names dropped the tier inputs, thresholds and reason code from every record."""

    def test_camel_case_fields_are_kept(self):
        from record import EvidenceSource, VersionPins, build_decision_record
        from spans import evidence_from_case_record

        case = {"applicantId": "APP-1", "caseId": "CASE-APP-1", "jurisdiction": "us"}
        raw = {"phase1Evidence": {"S": '[{"agent":"prescreen","content":[]}]'},
               "factScreening": {"S": "{}"}}
        decision = {"outcome": "MANUAL_REVIEW", "rule": "O1.preliminary_tier_high",
                    "reasonCode": "PRELIMINARY_RISK_HIGH", "tier": "HIGH",
                    "tierRule": "P1.screening_hit_unresolved",
                    "tierInputs": {"document_confidence": 1.0},
                    "thresholdsInForce": {"document_confidence_threshold": 0.8},
                    "edgeCasesMatched": ["IDV-1(R2.1)"]}
        record = build_decision_record(
            case_record=case,
            evidence=evidence_from_case_record(raw, decision=decision),
            outcome=decision,
            versions=VersionPins(harness_version="documents=3", endpoint_name="live",
                                 policy_engine_version="pe#LOG_ONLY", gateway_arn="gw"),
            decision_id="d-1", decided_at="2026-10-01T00:00:00Z", phase="phase1",
            actor="reviewer-7",
            sources=[EvidenceSource.OTEL_SPANS, EvidenceSource.CASE_RECORD])
        assert record.reason_code == "PRELIMINARY_RISK_HIGH"
        assert record.tier_rule == "P1.screening_hit_unresolved"
        assert dict(record.tier_inputs) == {"document_confidence": 1.0}
        assert dict(record.thresholds_in_force) == {"document_confidence_threshold": 0.8}
        assert record.edge_cases == ("IDV-1(R2.1)",)
        assert record.actor == "reviewer-7"


class TestTheSealedRecordLeadsBackToItsExecution:
    """End to end through the handler: the state machine sends its execution name as `traceId`
    (`asl._audit_task`), and the sealed record carries it as `executionName` — the key the
    reviewer console looks the timeline up by."""

    def test_execution_name_and_review_fields_are_sealed(self, monkeypatch):
        import audit_lambda

        written = {}

        class FakeDdb:
            def get_item(self, **_):
                return {"Item": {
                    "applicantId": {"S": "APP-1"}, "caseId": {"S": "CASE-APP-1"},
                    "jurisdiction": {"S": "us"},
                    "phase2Evidence": {"S": '[{"agent":"compliance","content":[]}]'},
                    "factPep": {"S": '{"performed":true,"pepStatus":"NONE"}'}}}

        class FakeLogs:
            def filter_log_events(self, **_):
                return {"events": []}

        class FakeBoto3:
            @staticmethod
            def client(name, **_):
                return {"dynamodb": FakeDdb(), "logs": FakeLogs()}[name]

        def put(record, **_):
            written["record"] = record

        monkeypatch.setattr(audit_lambda, "boto3", FakeBoto3)
        monkeypatch.setattr(audit_lambda, "put_decision_record", put)
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        monkeypatch.delenv("SPAN_LOG_GROUP", raising=False)

        result = audit_lambda.lambda_handler({
            "applicantId": "APP-1", "phase": "phase2",
            "traceId": "DEC-P1",
            "actor": "reviewer-7",
            "decision": {"outcome": "FULL_APPROVE_STANDARD", "rule": "REVIEW.approved",
                         "engineOutcome": "MANUAL_REVIEW",
                         "reviewReason": "ENGINE_MANUAL_REVIEW", "clearedAlerts": ["SYN-0106"],
                         "supersedesDecisionId": "DEC-P1"},
            "harnessVersion": "compliance=2", "endpointName": "live",
            "policyEngineVersion": "pe#LOG_ONLY", "gatewayArn": "arn:gw",
        })
        record = written["record"]
        assert result["phase"] == "phase2"
        assert record.execution_name == "DEC-P1"
        assert record.engine_outcome == "MANUAL_REVIEW"
        assert record.cleared_alerts == ("SYN-0106",)
        assert record.supersedes_decision_id == "DEC-P1"
        assert record.actor == "reviewer-7"


class TestTheLockIsTheDeployments:
    """D-35. Each record is locked the way the deployment says: GOVERNANCE for a day in a demo,
    COMPLIANCE for seven years when nothing says otherwise."""

    def _write(self, monkeypatch, **env):
        import audit_lambda

        captured = {}

        class FakeDdb:
            def get_item(self, **_):
                return {"Item": {
                    "applicantId": {"S": "APP-1"}, "caseId": {"S": "CASE-APP-1"},
                    "jurisdiction": {"S": "us"},
                    "phase2Evidence": {"S": '[{"agent":"compliance","content":[]}]'},
                    "factPep": {"S": '{"performed":true,"pepStatus":"NONE"}'}}}

        class FakeLogs:
            def filter_log_events(self, **_):
                return {"events": []}

        class FakeBoto3:
            @staticmethod
            def client(name, **_):
                return {"dynamodb": FakeDdb(), "logs": FakeLogs()}[name]

        def put(record, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(audit_lambda, "boto3", FakeBoto3)
        monkeypatch.setattr(audit_lambda, "put_decision_record", put)
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        monkeypatch.delenv("SPAN_LOG_GROUP", raising=False)
        for name in ("AUDIT_LOCK_MODE", "AUDIT_RETENTION_DAYS"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        audit_lambda.lambda_handler({
            "applicantId": "APP-1", "phase": "phase2", "traceId": "DEC-P1",
            "decision": {"outcome": "FULL_APPROVE_STANDARD", "rule": "P2.standard"},
            "harnessVersion": "compliance=2", "endpointName": "live",
            "policyEngineVersion": "pe#LOG_ONLY", "gatewayArn": "arn:gw",
        })
        return captured

    def test_a_demo_deployment_locks_for_one_day_in_governance(self, monkeypatch):
        written = self._write(monkeypatch, AUDIT_LOCK_MODE="GOVERNANCE", AUDIT_RETENTION_DAYS="1")
        assert (written["lock_mode"], written["retention_days"]) == ("GOVERNANCE", 1.0)

    def test_nothing_set_means_compliance_for_seven_years(self, monkeypatch):
        written = self._write(monkeypatch)
        assert (written["lock_mode"], written["retention_days"]) == ("COMPLIANCE", None)

    def test_the_writer_refuses_a_mode_s3_does_not_have(self):
        from datetime import datetime, timezone

        import pytest

        from writer import put_decision_record, retain_until

        with pytest.raises(ValueError):
            put_decision_record(object(), bucket="b", lock_mode="NONE",
                                decided_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        assert (retain_until(decided_at=start, days=1) - start).days == 1
