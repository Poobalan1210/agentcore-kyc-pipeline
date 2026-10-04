"""The submit_* output contracts — R11.13, F-89.

These tools exist because prose is not a data interface. Measured across two consecutive live runs
with an identical prompt, fixture and model configuration: one run reported `{"confidence": 0.96}`
and the next expressed the same finding in a sentence, so derived signals differed between runs and
R3.5's "identical outcome on every evaluation" could not hold.

R11.13 already required this — "SHALL use tool-use with `toolChoice` and a JSON Schema... SHALL NOT
rely on prompt-only JSON for a compliance decision". The tests below pin the two properties that
make the contract worth having: a finding is either complete or refused, and "could not determine"
is expressible without lying.
"""

from __future__ import annotations

import json

import pytest
from kyctools import tools_impl
from tool_schemas import ALL_OUTPUT_CONTRACTS, ALL_SCHEMAS, ALL_TARGET_NAMES, cedar_action


class FakeDynamo:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        return {}


@pytest.fixture
def ddb(monkeypatch):
    fake = FakeDynamo()
    monkeypatch.setenv("CASE_TABLE", "kyc90-cases")
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    return fake


class TestSchemas:
    def test_every_agent_has_a_contract(self):
        """One per agent — the set `OutputContractNames` always listed. Building only three left
        Router, Identity, Compliance and Notify instructed by their own prompts to "call the submit
        tool" when no such tool existed, and the runtime rejected the tool-use block outright."""
        assert set(ALL_OUTPUT_CONTRACTS) == {
            "briefing", "documents", "identity", "fraud", "prescreen", "compliance", "notify"}
        assert len(set(ALL_OUTPUT_CONTRACTS.values())) == 7

    def test_each_contract_has_a_gateway_target(self):
        """`cedar_action` reads the *deployed* registry, so it is not exercised here — the flag
        removes contract tools from it. `ALL_TARGET_NAMES` keeps the built mapping verified."""
        for tool in ALL_OUTPUT_CONTRACTS.values():
            assert tool in ALL_TARGET_NAMES
            assert ALL_TARGET_NAMES[tool].startswith("Submit")

    def test_each_contract_has_a_schema(self):
        for tool in ALL_OUTPUT_CONTRACTS.values():
            assert ALL_SCHEMAS[tool]["inputSchema"]["type"] == "object"
            assert ALL_SCHEMAS[tool]["inputSchema"]["required"]

    def test_confidence_is_a_number_not_a_string(self):
        """The prose route produced 0.96, 96 and "high confidence" for the same value."""
        prop = ALL_SCHEMAS["submit_document_analysis"]["inputSchema"]["properties"]["confidence"]
        assert prop["type"] == "number"

    def test_no_schema_declares_numeric_constraints(self):
        """The Gateway's inline MCP schema rejects JSON Schema numeric constraints — 12 targets
        failed with "0 subschemas matched instead of one" before these were removed. The bounds
        moved into the tool, which is tested separately below."""
        for tool in ALL_OUTPUT_CONTRACTS.values():
            for name, prop in ALL_SCHEMAS[tool]["inputSchema"]["properties"].items():
                assert "minimum" not in prop, f"{tool}.{name}"
                assert "maximum" not in prop, f"{tool}.{name}"
                assert prop.get("type") != "integer", f"{tool}.{name} must use number"

    def test_screening_can_say_it_did_not_determine(self):
        """Without `determined`, an agent that could not screen has only `matched=false` to
        report — which reads as "no sanctions hits" and approves the applicant."""
        schema = ALL_SCHEMAS["submit_prescreen"]["inputSchema"]
        assert "determined" in schema["required"]

    def test_every_field_the_engine_tiers_on_is_required(self):
        doc = ALL_SCHEMAS["submit_document_analysis"]["inputSchema"]["required"]
        assert {"confidence", "expired", "screenPhotograph", "unsupportedIssuer"} <= set(doc)
        fraud = ALL_SCHEMAS["submit_fraud_assessment"]["inputSchema"]["required"]
        assert {"determined", "velocityCritical"} <= set(fraud)

    def test_descriptions_tell_the_agent_this_is_the_only_channel(self):
        for tool in ALL_OUTPUT_CONTRACTS.values():
            assert "ONLY way" in ALL_SCHEMAS[tool]["description"]


class TestPersistence:
    def test_a_document_finding_is_written_to_the_case_record(self, ddb):
        result = tools_impl.submit_document_analysis({
            "applicantId": "APP-1", "confidence": 0.96, "expired": False,
            "screenPhotograph": False, "unsupportedIssuer": False,
        })
        assert result.status == "OK"
        call, = ddb.calls
        assert call["Key"]["applicantId"]["S"] == "APP-1"
        assert call["ExpressionAttributeNames"]["#f"] == "findingDocuments"
        finding = json.loads(call["ExpressionAttributeValues"][":f"]["S"])
        assert finding["confidence"] == 0.96
        assert "applicantId" not in finding

    def test_each_agent_writes_to_its_own_attribute(self, ddb):
        tools_impl.submit_prescreen({
            "applicantId": "APP-1", "determined": True, "matched": False, "hitCount": 0})
        tools_impl.submit_fraud_assessment({
            "applicantId": "APP-1", "determined": True, "velocityCritical": False})
        assert [c["ExpressionAttributeNames"]["#f"] for c in ddb.calls] == [
            "findingPrescreen", "findingFraud"]

    def test_the_write_uses_flat_attributes_not_overlapping_paths(self, ddb):
        """DynamoDB rejects an UpdateExpression whose document paths overlap, so
        `SET findings = if_not_exists(findings, :empty), findings.#a = :f` is invalid — and it fails
        at *invoke* time, where the agent reports only "an unhandled error prevented submission"
        and the case degrades to review. Flat attributes need no container and cannot overlap."""
        tools_impl.submit_prescreen({
            "applicantId": "APP-1", "determined": True, "matched": False, "hitCount": 0})
        expr = ddb.calls[0]["UpdateExpression"]
        assert expr == "SET #f = :f, updatedAt = :t"
        assert "if_not_exists" not in expr
        assert "." not in expr.split("=")[0]

    def test_every_agent_has_a_distinct_attribute(self, ddb):
        names = set(tools_impl.FINDING_ATTRIBUTES.values())
        assert len(names) == len(tools_impl.FINDING_ATTRIBUTES) == 7

    def test_a_retried_call_is_idempotent(self, ddb):
        """Canonical JSON, so a retry writes byte-identical content to the same place."""
        args = {"applicantId": "APP-1", "determined": True, "matched": False, "hitCount": 0}
        tools_impl.submit_prescreen(dict(args))
        tools_impl.submit_prescreen(dict(args))
        first, second = (c["ExpressionAttributeValues"][":f"]["S"] for c in ddb.calls)
        assert first == second


class TestRefusals:
    def test_a_finding_without_an_applicant_is_refused(self, ddb):
        result = tools_impl.submit_prescreen({
            "determined": True, "matched": False, "hitCount": 0})
        assert result.status == "INVALID"
        assert result.reason_code == "MISSING_APPLICANT"
        assert ddb.calls == []

    @pytest.mark.parametrize("missing", ["determined", "matched", "hitCount"])
    def test_an_incomplete_finding_is_refused_not_partially_written(self, ddb, missing):
        """A finding with a hole is worse than none: the engine would tier on a partial record."""
        args = {"applicantId": "APP-1", "determined": True, "matched": False, "hitCount": 0}
        del args[missing]
        result = tools_impl.submit_prescreen(args)
        assert result.status == "INVALID"
        assert result.reason_code == "INCOMPLETE_CONTRACT"
        assert ddb.calls == []

    def test_false_is_not_treated_as_missing(self, ddb):
        """`matched=False` is a real answer. Rejecting it as absent would make a clean screening
        result impossible to report."""
        result = tools_impl.submit_prescreen({
            "applicantId": "APP-1", "determined": True, "matched": False, "hitCount": 0})
        assert result.status == "OK"

    def test_zero_is_not_treated_as_missing(self, ddb):
        result = tools_impl.submit_document_analysis({
            "applicantId": "APP-1", "confidence": 0.0, "expired": False,
            "screenPhotograph": False, "unsupportedIssuer": False})
        assert result.status == "OK"

    def test_an_unconfigured_table_raises_rather_than_reporting_no_finding(self, monkeypatch):
        """A configuration fault must stay distinguishable from an agent with nothing to report."""
        monkeypatch.delenv("CASE_TABLE", raising=False)
        with pytest.raises(tools_impl.ToolError):
            tools_impl.submit_prescreen({
                "applicantId": "APP-1", "determined": True, "matched": False, "hitCount": 0})


class TestRegistration:
    def test_all_three_are_dispatchable(self):
        for tool in ALL_OUTPUT_CONTRACTS.values():
            assert tool in tools_impl.TOOLS

    def test_each_contract_sits_in_its_agents_phase(self):
        """Compliance and Notify run in Phase 2, so their contracts belong there. Mixing them into
        Phase 1 would offer an agent a tool for work it does not do in that phase."""
        PHASE1_AGENTS = ("briefing", "documents", "identity", "fraud", "prescreen")
        PHASE2_AGENTS = ("compliance", "notify")

        for agent in PHASE1_AGENTS:
            contract = ALL_OUTPUT_CONTRACTS[agent]
            assert contract in tools_impl.PHASE1_TOOLS
            assert contract not in tools_impl.PHASE2_TOOLS
        for agent in PHASE2_AGENTS:
            contract = ALL_OUTPUT_CONTRACTS[agent]
            assert contract in tools_impl.PHASE2_TOOLS
            assert contract not in tools_impl.PHASE1_TOOLS

    def test_the_two_phases_do_not_overlap(self):
        assert not set(tools_impl.PHASE1_TOOLS) & set(tools_impl.PHASE2_TOOLS)


class TestBoundsEnforcedInTheTool:
    """The bounds the Gateway schema cannot carry. Without these an out-of-range confidence would
    reach the tier calculation and skew it silently."""

    @pytest.mark.parametrize("confidence", [-0.1, 1.5, 96])
    def test_out_of_range_confidence_is_refused(self, ddb, confidence):
        result = tools_impl.submit_document_analysis({
            "applicantId": "APP-1", "confidence": confidence, "expired": False,
            "screenPhotograph": False, "unsupportedIssuer": False})
        assert result.status == "INVALID"
        assert result.reason_code == "CONFIDENCE_OUT_OF_RANGE"
        assert ddb.calls == []

    @pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
    def test_in_range_confidence_is_accepted_including_the_boundaries(self, ddb, confidence):
        result = tools_impl.submit_document_analysis({
            "applicantId": "APP-1", "confidence": confidence, "expired": False,
            "screenPhotograph": False, "unsupportedIssuer": False})
        assert result.status == "OK"

    def test_a_negative_hit_count_is_refused(self, ddb):
        result = tools_impl.submit_prescreen({
            "applicantId": "APP-1", "determined": True, "matched": False, "hitCount": -1})
        assert result.status == "INVALID"
        assert result.reason_code == "NEGATIVE_COUNT"

    def test_a_negative_shared_attribute_count_is_refused(self, ddb):
        result = tools_impl.submit_fraud_assessment({
            "applicantId": "APP-1", "determined": True, "velocityCritical": False,
            "sharedAttributeCount": -3})
        assert result.status == "INVALID"
        assert result.reason_code == "NEGATIVE_COUNT"


class TestMalformedValuesAreNamedNotCrashes:
    """The schema says `number`, and a model may still send a string.

    An unguarded `float()` raised, the handler reported UNHANDLED_ERROR, and the agent read that
    back as "an unhandled error prevented submission" and stopped trying — so the finding was never
    recorded and the case degraded to review. A named INVALID is something the agent can act on.
    """

    def doc(self, **over):
        args = {"applicantId": "APP-1", "confidence": 0.9, "expired": False,
                "screenPhotograph": False, "unsupportedIssuer": False}
        args.update(over)
        return args

    @pytest.mark.parametrize("value", ["high", "", "n/a", None if False else "abc"])
    def test_a_non_numeric_confidence_is_named_not_raised(self, ddb, value):
        result = tools_impl.submit_document_analysis(self.doc(confidence=value))
        assert result.status == "INVALID"
        assert result.reason_code == "CONFIDENCE_NOT_A_NUMBER"
        assert ddb.calls == []

    def test_a_numeric_string_confidence_is_accepted_and_normalised(self, ddb):
        result = tools_impl.submit_document_analysis(self.doc(confidence="0.93"))
        assert result.status == "OK"
        finding = json.loads(ddb.calls[0]["ExpressionAttributeValues"][":f"]["S"])
        assert finding["confidence"] == 0.93

    def test_a_non_numeric_hit_count_is_named(self, ddb):
        result = tools_impl.submit_prescreen({
            "applicantId": "APP-1", "determined": True, "matched": False, "hitCount": "many"})
        assert result.reason_code == "COUNT_NOT_A_NUMBER"

    @pytest.mark.parametrize("raw,expected", [("false", False), ("true", True),
                                              ("False", False), (" TRUE ", True)])
    def test_boolean_strings_are_coerced(self, ddb, raw, expected):
        """"false" as a string is truthy in Python. Left uncoerced, an agent reporting
        `matched="false"` would have been read as a sanctions hit."""
        result = tools_impl.submit_prescreen({
            "applicantId": "APP-1", "determined": True, "matched": raw, "hitCount": 0})
        assert result.status == "OK"
        finding = json.loads(ddb.calls[0]["ExpressionAttributeValues"][":f"]["S"])
        assert finding["matched"] is expected

    def test_an_uninterpretable_boolean_is_named(self, ddb):
        result = tools_impl.submit_prescreen({
            "applicantId": "APP-1", "determined": True, "matched": "maybe", "hitCount": 0})
        assert result.reason_code == "FLAG_NOT_A_BOOLEAN"
        assert ddb.calls == []

    def test_the_callers_dict_is_not_mutated(self, ddb):
        args = self.doc(confidence="0.5")
        tools_impl.submit_document_analysis(args)
        assert args["confidence"] == "0.5"


class TestDispatcherSignature:
    """The dispatcher calls `fn(args, ctx)` positionally.

    Omitting `ctx` raised TypeError *inside the dispatcher*, which the catch-all converted to
    UNHANDLED_ERROR. The agent read that back as "an unhandled error prevented submission",
    abandoned the contract, and the case degraded to manual review — three tools broken by one
    missing parameter, and nothing in the tool result named it.
    """

    @pytest.mark.parametrize("tool", [
        "submit_document_analysis", "submit_prescreen", "submit_fraud_assessment"])
    def test_every_contract_tool_accepts_args_and_ctx(self, tool):
        import inspect

        params = list(inspect.signature(tools_impl.TOOLS[tool]).parameters)
        assert len(params) >= 2, f"{tool} must accept (args, ctx)"

    def test_every_tool_in_the_registry_takes_two_positional_arguments(self):
        """Checked across all ten, so the next tool added cannot repeat this."""
        import inspect

        for name, fn in tools_impl.TOOLS.items():
            params = list(inspect.signature(fn).parameters)
            assert len(params) >= 2, f"{name} would raise TypeError in the dispatcher"

    def test_the_applicant_id_can_come_from_the_context(self, ddb):
        """An agent that loses the applicant id mid-conversation can still record its finding;
        otherwise the finding is lost and the case degrades for a bookkeeping reason."""
        result = tools_impl.submit_prescreen(
            {"determined": True, "matched": False, "hitCount": 0},
            {"applicantId": "APP-FROM-CTX"})
        assert result.status == "OK"
        assert ddb.calls[0]["Key"]["applicantId"]["S"] == "APP-FROM-CTX"

    def test_arguments_win_over_context(self, ddb):
        result = tools_impl.submit_prescreen(
            {"applicantId": "APP-EXPLICIT", "determined": True, "matched": False, "hitCount": 0},
            {"applicantId": "APP-FROM-CTX"})
        assert result.status == "OK"
        assert ddb.calls[0]["Key"]["applicantId"]["S"] == "APP-EXPLICIT"
