

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
