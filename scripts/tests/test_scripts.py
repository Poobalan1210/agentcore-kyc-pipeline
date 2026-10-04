"""The operational scripts' logic, without AWS: every number and verdict they print comes from
these functions, so these are the tests that make the printed figures trustworthy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import check_policy_decisions as cpd
import export_executions
import loadtest
import measure
import measurement
import prepare_demo


def _dt(seconds: float = 0) -> datetime:
    return datetime(2026, 12, 2, 17, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def _iso(seconds: float = 0) -> str:
    return _dt(seconds).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------------------ measurement
class TestPercentile:
    def test_linear_interpolation_between_ranks(self):
        values = [10, 20, 30, 40, 50]
        assert measurement.percentile(values, 50) == 30
        assert measurement.percentile(values, 95) == pytest.approx(48.0)
        assert measurement.percentile(values, 0) == 10
        assert measurement.percentile(values, 100) == 50

    def test_order_does_not_matter_and_one_value_is_itself(self):
        assert measurement.percentile([50, 10, 30], 50) == 30
        assert measurement.percentile([7.5], 95) == 7.5

    def test_no_values_is_no_answer_not_zero(self):
        assert measurement.percentile([], 50) is None

    def test_p_outside_0_100_is_refused(self):
        with pytest.raises(ValueError):
            measurement.percentile([1], 101)


def _case(applicant="APP-1", created=0.0, decided=72.5, resubmitted=None, evidence=None):
    item = {"applicantId": {"S": applicant}, "createdAt": {"S": _iso(created)}}
    if decided is not None:
        item["provisionalDecidedAt"] = {"S": _iso(decided)}
    if resubmitted is not None:
        item["resubmittedAt"] = {"S": _iso(resubmitted)}
    if evidence is not None:
        item["phase1Evidence"] = {"S": json.dumps(evidence)}
    return item


class TestTimeToDecision:
    def test_from_submission_to_the_provisional_decision(self):
        assert measurement.t1_seconds(_case(created=0, decided=72.5)) == 72.5

    def test_a_resubmission_is_timed_from_when_it_was_resubmitted(self):
        assert measurement.t1_seconds(_case(created=0, resubmitted=3600, decided=3665)) == 65

    def test_undecided_or_unreadable_is_none(self):
        assert measurement.t1_seconds(_case(decided=None)) is None
        assert measurement.t1_seconds({"createdAt": {"S": "yesterday"}}) is None

    def test_a_decision_before_submission_is_not_a_negative_duration(self):
        assert measurement.seconds_between(_iso(10), _iso(5)) is None


EVIDENCE = [
    {"agent": "documents", "status": "OK", "latencyMs": 8000, "totalTokens": 1200},
    {"agent": "prescreen", "status": "OK", "latencyMs": 4000, "totalTokens": 800},
    {"agent": "fraud", "status": "DEGRADED", "reason": "EMPTY_OR_FAILED_AGENT_RESULT"},
]


class TestSummary:
    def test_the_distribution_tokens_and_cost(self):
        cases = [_case(f"APP-{i}", decided=d, evidence=EVIDENCE)
                 for i, d in enumerate([40, 60, 80, 100])]
        s = measurement.summarise(cases, price_per_million_tokens=0.5)
        assert s["decided"] == 4
        assert s["t1"]["p50"] == 70
        assert s["t1"]["underTarget"] == 0.75
        assert s["tokensPerCase"] == 2000
        assert s["modelCostPerCase"] == pytest.approx(0.001)
        assert s["agents"]["phase1:documents"]["p50"] == 8.0
        # A degraded run has no latency worth averaging, but it is counted.
        assert "phase1:fraud" not in s["agents"]
        assert s["degradedRuns"] == 4

    def test_no_price_means_no_cost_line_rather_than_an_invented_one(self):
        s = measurement.summarise([_case(evidence=EVIDENCE)])
        assert s["modelCostPerCase"] is None
        assert "Estimated model cost" not in measurement.render(s)

    def test_render_reports_the_share_within_the_target(self):
        text = measurement.render(measurement.summarise([_case(decided=60), _case(decided=120)]))
        assert "| within 90s | 50% |" in text
        assert "| p50 | 90.0s |" in text


class TestStepTimeline:
    EVENTS = [
        {"type": "TaskStateEntered", "timestamp": _dt(0),
         "stateEnteredEventDetails": {"name": "LoadCase"}},
        {"type": "TaskStateExited", "timestamp": _dt(0.4),
         "stateExitedEventDetails": {"name": "LoadCase"}},
        {"type": "ParallelStateEntered", "timestamp": _dt(0.4),
         "stateEnteredEventDetails": {"name": "Evidence"}},
        {"type": "TaskFailed", "timestamp": _dt(30)},
        {"type": "ParallelStateExited", "timestamp": _dt(31),
         "stateExitedEventDetails": {"name": "Evidence"}},
        {"type": "TaskStateEntered", "timestamp": _dt(31),
         "stateEnteredEventDetails": {"name": "PreliminaryTier"}},
    ]

    def test_each_state_with_its_duration_and_status(self):
        steps = measurement.step_timeline(self.EVENTS)
        assert [(s["name"], s["status"], s.get("seconds")) for s in steps] == [
            ("LoadCase", "SUCCEEDED", 0.4), ("Evidence", "FAILED", 30.6),
            ("PreliminaryTier", "RUNNING", None)]

    def test_states_ranked_slowest_first(self):
        executions = [{"steps": [{"name": "A", "seconds": 1.0}, {"name": "B", "seconds": 9.0}]},
                      {"steps": [{"name": "A", "seconds": 2.0}]}]
        durations = measurement.state_durations(executions)
        assert list(durations) == ["B", "A"]
        assert durations["A"]["count"] == 2


class TestMeasureSelection:
    def test_load_test_copies_are_left_out_of_rehearsal_figures(self):
        items = [_case("APP-a"), _case("APP-LOADTEST-1-000")]
        assert [i["applicantId"]["S"] for i in measure.select(
            items, since=None, prefix=None, include_loadtest=False)] == ["APP-a"]
        assert len(measure.select(items, since=None, prefix=None, include_loadtest=True)) == 2
        assert [i["applicantId"]["S"] for i in measure.select(
            items, since=None, prefix="APP-LOADTEST-", include_loadtest=False)] == [
            "APP-LOADTEST-1-000"]

    def test_since_filters_on_creation_time(self):
        items = [_case("APP-old", created=-7200), _case("APP-new", created=0)]
        assert [i["applicantId"]["S"] for i in measure.select(
            items, since=_iso(-60), prefix=None, include_loadtest=False)] == ["APP-new"]

    def test_the_scan_pages_to_the_end(self):
        class Ddb:
            def __init__(self):
                self.calls = []

            def scan(self, **kwargs):
                self.calls.append(kwargs)
                if "ExclusiveStartKey" not in kwargs:
                    return {"Items": [_case("APP-1")], "LastEvaluatedKey": {"k": 1}}
                return {"Items": [_case("APP-2")]}

        ddb = Ddb()
        assert len(list(measure.scan_cases("t", client=ddb))) == 2
        assert "phase1Evidence" in ddb.calls[0]["ProjectionExpression"]
        assert "declaredIdentity" not in ddb.calls[0]["ProjectionExpression"]


class TestExport:
    def test_listing_stops_at_the_window(self):
        class Sfn:
            def list_executions(self, **kwargs):
                if "nextToken" not in kwargs:
                    return {"executions": [{"startDate": _dt(0)}, {"startDate": _dt(-10)}],
                            "nextToken": "t"}
                return {"executions": [{"startDate": _dt(-7200)}, {"startDate": _dt(-9000)}],
                        "nextToken": "t2"}

        found = list(export_executions.executions(Sfn(), "arn", since=_dt(-3600)))
        assert len(found) == 2

    def test_a_row_carries_no_execution_input_or_output(self):
        row = export_executions.export_row(
            "phase1", {"name": "n", "executionArn": "a", "status": "SUCCEEDED",
                       "startDate": _dt(0), "stopDate": _dt(71), "input": "{secret}"},
            TestStepTimeline.EVENTS[:2])
        assert row["seconds"] == 71
        assert "input" not in row and "output" not in row
        assert row["steps"][0]["name"] == "LoadCase"


# ----------------------------------------------------------------------------- Cedar check
class TestPolicyRollup:
    def test_one_decision_published_under_several_dimension_sets_is_counted_once(self):
        series = [
            ({"PolicyEngine": "e", "OperationName": "AuthorizeAction", "Mode": "LOG_ONLY"}, 10),
            ({"PolicyEngine": "e", "OperationName": "AuthorizeAction", "Mode": "LOG_ONLY",
              "TargetResource": "gw"}, 10),
            ({"PolicyEngine": "e", "OperationName": "AuthorizeAction", "Mode": "LOG_ONLY",
              "ToolName": "SanctionsScreen___sanctions_screen"}, 6),
            ({"PolicyEngine": "e", "OperationName": "AuthorizeAction", "Mode": "LOG_ONLY",
              "ToolName": "DocumentStore___s3_get_document"}, 4),
            ({"PolicyEngine": "e", "Policy": "p-1"}, 3),
        ]
        total, tools = cpd.rollup(series)
        assert total == 10
        assert tools == {"SanctionsScreen___sanctions_screen": 6,
                         "DocumentStore___s3_get_document": 4}

    def test_operations_partition_the_rollup(self):
        series = [({"PolicyEngine": "e", "OperationName": "AuthorizeAction"}, 7),
                  ({"PolicyEngine": "e", "OperationName": "PartiallyAuthorizeActions"}, 2)]
        assert cpd.rollup(series)[0] == 9

    def test_reading_from_cloudwatch(self):
        class Cw:
            def list_metrics(self, *, Namespace, MetricName, NextToken=None):
                assert Namespace == "AWS/Bedrock-AgentCore"
                engines = ["kyc90_policy_us-abc", "someone_elses_engine"]
                return {"Metrics": [{"Dimensions": [
                    {"Name": "PolicyEngine", "Value": e},
                    {"Name": "OperationName", "Value": "AuthorizeAction"}]} for e in engines]}

            def get_metric_statistics(self, *, MetricName, **_):
                return {"Datapoints": [{"Sum": 12 if MetricName == "AllowDecisions" else 0}]}

        engines = cpd.read_metrics(Cw(), start=_dt(-3600), end=_dt(0))
        assert [e.engine for e in engines] == ["kyc90_policy_us-abc"]
        assert engines[0].counts["AllowDecisions"] == 12
        assert engines[0].denied == 0


def _engine(name, **counts):
    return cpd.EngineDecisions(name, counts=counts)


class TestPolicyVerdict:
    def test_no_traffic_is_no_evidence_not_an_all_clear(self):
        verdict = cpd.decide([_engine("kyc90_policy_us")])
        assert (verdict.status, verdict.exit_code) == ("NO_EVIDENCE", 2)
        assert cpd.decide([]).status == "NO_EVIDENCE"

    @pytest.mark.parametrize("metric", ["DenyDecisions", "NoDeterminingPolicies",
                                        "MismatchErrors"])
    def test_any_denial_or_policy_failure_is_not_safe(self, metric):
        verdict = cpd.decide([_engine("kyc90_policy_us", AllowDecisions=40, **{metric: 1})])
        assert (verdict.status, verdict.exit_code) == ("NOT_SAFE", 1)

    def test_a_denied_span_is_not_safe_even_when_metrics_look_clean(self):
        verdict = cpd.decide([_engine("kyc90_policy_us", AllowDecisions=40)],
                             [{"gateway": "gw-us", "policies": "permit-documents"}])
        assert verdict.status == "NOT_SAFE"
        assert any("gw-us" in line for line in verdict.lines)

    def test_clean_traffic_is_safe_and_silent_engines_are_named_unverified(self):
        verdict = cpd.decide([_engine("kyc90_policy_us", AllowDecisions=40),
                              _engine("kyc90_policy_uk")])
        assert (verdict.status, verdict.exit_code) == ("SAFE_TO_ENFORCE", 0)
        assert any("UNVERIFIED" in line and "kyc90_policy_uk" in line for line in verdict.lines)

    def test_the_tool_that_was_denied_is_named(self):
        engine = _engine("kyc90_policy_us", AllowDecisions=40, DenyDecisions=2)
        engine.tools = {"SanctionsScreen___sanctions_screen": {"DenyDecisions": 2}}
        assert any("sanctions_screen: 2 denied" in line for line in cpd.decide([engine]).lines)


# ------------------------------------------------------------------------------ demo prep
class TestDemoPreparation:
    def test_user_plan_creates_and_adds_only_what_is_missing(self):
        steps = prepare_demo.user_plan(
            {"p@example.com": None, "r@example.com": {"reviewers"},
             "both@example.com": {"presenters"}},
            {"p@example.com": {"presenters"}, "r@example.com": {"reviewers"},
             "both@example.com": {"presenters", "reviewers"}})
        assert steps == ["create user p@example.com", "add p@example.com to presenters",
                         "add both@example.com to reviewers"]

    def test_persona_status_says_whether_act3s_customer_is_open(self):
        nadia = {"surname": "KORVAL", "givenNames": "NADIA ROSE", "dateOfBirth": "1987-02-11"}
        rows = {r["persona"]: r for r in prepare_demo.persona_status([
            {**nadia, "state": "PROVISIONAL", "createdAt": _iso(0), "applicantId": "APP-n"}])}
        assert rows["DEMO-US-03"]["ready"] is False
        rows = {r["persona"]: r for r in prepare_demo.persona_status([
            {**nadia, "state": "PROVISIONAL", "createdAt": _iso(0), "applicantId": "APP-old"},
            {**nadia, "state": "MONITORING", "createdAt": _iso(60), "applicantId": "APP-new"}])}
        assert rows["DEMO-US-03"]["ready"] is True
        assert rows["DEMO-US-03"]["applicantId"] == "APP-new"
        # Personas no act depends on being in a state carry no readiness flag.
        assert rows["DEMO-US-01"]["ready"] is None and rows["DEMO-US-01"]["cases"] == 0


# ------------------------------------------------------------------------------- load test
SOURCE = {
    "applicantId": {"S": "APP-sub-run1"},
    "jurisdiction": {"S": "us"},
    "productApplied": {"S": "current_account"},
    "documents": {"L": [{"M": {"type": {"S": "PASSPORT"}, "bucket": {"S": "b"},
                               "key": {"S": "APP-sub-run1/PASSPORT-1"}}}]},
    "declaredIdentity": {"M": {"surname": {"S": "REEVES"}}},
    "liveness": {"M": {"result": {"S": "PASS"}, "referenceImageKey": {"S": "APP-sub-run1/r.jpg"}}},
    "phase1": {"S": "{\"outcome\": \"PROVISIONAL_APPROVE_RESTRICTED\"}"},
    "factScreening": {"S": "{}"},
}


class TestLoadTest:
    def test_a_fresh_passing_rehearsal_can_be_replayed(self):
        assert loadtest.validate_source(SOURCE) == []

    @pytest.mark.parametrize("change, problem", [
        ({"liveness": {"M": {"result": {"S": "FAILED"}}}}, "not PASS"),
        ({"liveness": {"M": {"result": {"S": "PASS"}}}}, "reference frame"),
        ({"documents": {"L": []}}, "no document"),
        ({"applicantId": {"S": "APP-LOADTEST-x-001"}}, "itself a load-test copy"),
    ])
    def test_a_source_that_would_measure_the_wrong_path_is_refused(self, change, problem):
        assert any(problem in p for p in loadtest.validate_source({**SOURCE, **change}))
        assert loadtest.validate_source(None) == ["no case with that applicant id"]

    def test_a_copy_is_a_fresh_submission_of_the_same_evidence(self):
        item, message = loadtest.clone(SOURCE, run_id="R1", index=7, now=_dt(0))
        assert item["applicantId"] == {"S": "APP-LOADTEST-R1-007"}
        assert item["state"] == {"S": "RECEIVED"} and item["loadTest"] == {"S": "R1"}
        for attribute in loadtest.COPIED:
            assert item[attribute] == SOURCE[attribute]
        # Nothing decided travels with it: the copy is decided from scratch.
        assert "phase1" not in item and "factScreening" not in item
        assert message == {"applicantId": "APP-LOADTEST-R1-007",
                           "eventId": item["lastEventId"]["S"]}

    def test_waves_cover_every_copy_once(self):
        assert [list(w) for w in loadtest.waves(7, 3)] == [[0, 1, 2], [3, 4, 5], [6]]
