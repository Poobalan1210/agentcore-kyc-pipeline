"""Presenter controls — `api/demo.py`. Act 3 is published from here, so these pin the two things
that would break it on stage: an update the screening tool cannot read, and events Phase 3 would
act on differently from what the act claims (a CRITICAL event freezes without screening)."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import pytest

import demo
from kyctools import controls
from kyctools.data.sanctions import ALL_ENTRIES

FAULTS = "/kyc90/demo/faults"
OVERLAY = "/kyc90/screening/list-overlay"


class FakeSsm:
    def __init__(self, values=None, broken=False):
        self.values = dict(values or {})
        self.broken = broken
        self.puts: list[dict[str, Any]] = []

    def get_parameter(self, Name):
        if self.broken:
            raise RuntimeError("ThrottlingException")
        if Name not in self.values:
            raise KeyError(Name)
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, **kwargs):
        self.puts.append(kwargs)
        self.values[kwargs["Name"]] = kwargs["Value"]


class FakeDynamo:
    """The `state` GSI. Items per state; `page_size` pages them like DynamoDB."""

    def __init__(self, by_state=None, page_size=None):
        self.by_state = by_state or {}
        self.page_size = page_size
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        items = self.by_state.get(kwargs["ExpressionAttributeValues"][":state"]["S"], [])
        if not self.page_size:
            return {"Items": items}
        start = int((kwargs.get("ExclusiveStartKey") or {}).get("i", 0))
        resp: dict[str, Any] = {"Items": items[start:start + self.page_size]}
        if start + self.page_size < len(items):
            resp["LastEvaluatedKey"] = {"i": start + self.page_size}
        return resp


class FakeEvents:
    def __init__(self, fail_every=0):
        self.batches: list[list[dict[str, Any]]] = []
        self.fail_every = fail_every

    def put_events(self, Entries):
        self.batches.append(Entries)
        return {"FailedEntryCount": self.fail_every}


class FakeBoto3:
    def __init__(self, **clients):
        self.clients = clients

    def client(self, name, **_):
        return self.clients[name]


def account_item(applicant: str, *, opened: bool = True) -> dict[str, Any]:
    item = {"applicantId": {"S": applicant}, "state": {"S": "MONITORING"}}
    if opened:
        item["provisionalDecidedAt"] = {"S": "2026-12-02T16:59:00Z"}
    return item


@pytest.fixture
def aws(monkeypatch):
    ssm = FakeSsm({FAULTS: "{}", OVERLAY: json.dumps({"entries": []})})
    ddb = FakeDynamo({"MONITORING": [account_item("APP-a"), account_item("APP-b")],
                      "PROVISIONAL": [account_item("APP-c")],
                      "AWAITING_REVIEW": [account_item("APP-phase1-review", opened=False)]})
    events = FakeEvents()
    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3(ssm=ssm, dynamodb=ddb, events=events))
    monkeypatch.setenv("DEMO_FAULTS_PARAMETER", FAULTS)
    monkeypatch.setenv("LIST_OVERLAY_PARAMETER", OVERLAY)
    monkeypatch.setenv("LIFECYCLE_BUS_NAME", "kyc90-lifecycle")
    monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
    monkeypatch.setenv("LIST_UPDATE_SETTLE_SECONDS", "0")
    return {"ssm": ssm, "ddb": ddb, "events": events}


def presenter(route: str, body: Any = None, groups: Any = "[presenters]") -> dict[str, Any]:
    ev: dict[str, Any] = {"requestContext": {"routeKey": route, "authorizer": {"jwt": {"claims": {
        "sub": "presenter-1", "cognito:groups": groups}}}}}
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def call(ev):
    resp = demo.lambda_handler(ev)
    return resp["statusCode"], json.loads(resp["body"])


NADIA = {"entryId": "SYN-0901", "name": "Nadia Rose Korval", "dob": "1987-02-11",
         "country": "us"}


class TestOnlyPresenters:
    @pytest.mark.parametrize("groups", ["[reviewers]", "[applicants]", "[notpresenters]",
                                        "[presenters-test]", None])
    @pytest.mark.parametrize("route", ["GET /demo/controls", "PUT /demo/faults",
                                       "POST /demo/list-update", "POST /demo/reset"])
    def test_anyone_else_is_refused_before_anything_is_touched(self, aws, route, groups):
        status, _ = call(presenter(route, {"entries": [NADIA]}, groups=groups))
        assert status == 403
        assert aws["ssm"].puts == [] and aws["events"].batches == []

    def test_an_unknown_demo_route_is_a_404(self, aws):
        assert call(presenter("DELETE /demo/everything"))[0] == 404


class TestFaults:
    def test_faults_are_written_where_the_tools_and_state_machine_read_them(self, aws):
        status, body = call(presenter("PUT /demo/faults", {
            "unavailableTools": ["sanctions_screen", "sanctions_screen"],
            "agentTimeouts": {"fraud": 5}}))
        assert status == 200
        assert body["faults"] == {"unavailableTools": ["sanctions_screen"],
                                  "agentTimeouts": {"fraud": 5}}
        stored = json.loads(aws["ssm"].values[FAULTS])
        assert stored == body["faults"]
        # The reader the tools use sees the same thing.
        controls.reset_cache()
        try:
            assert "sanctions_screen" in controls.unavailable_tools()
        finally:
            controls.reset_cache()

    @pytest.mark.parametrize("faults", [
        {"unavailableTools": ["approve_everyone"]},
        {"unavailableTools": "sanctions_screen"},
        {"agentTimeouts": {"notify": 5}},
        {"agentTimeouts": {"fraud": 0}},
        {"agentTimeouts": {"fraud": 91}},
        {"agentTimeouts": {"fraud": True}},
        {"agentTimeouts": {"fraud": "5"}},
        {"agentTimeouts": ["fraud"]},
    ])
    def test_a_fault_outside_the_known_set_is_refused_and_nothing_is_written(self, aws, faults):
        status, body = call(presenter("PUT /demo/faults", faults))
        assert status == 400 and body["error"] == "INVALID_FAULTS"
        assert aws["ssm"].puts == []

    def test_the_faultable_tools_and_agents_are_exactly_the_real_ones(self):
        """A fault naming a tool that does not exist would be accepted and do nothing on stage."""
        import tool_schemas
        from kyc.config import PHASE1_EVIDENCE_AGENTS

        evidence_tools = (set(tool_schemas.ALL_SCHEMAS)
                          - set(tool_schemas.ALL_OUTPUT_CONTRACTS.values()))
        assert demo.TOOLS == evidence_tools
        assert demo.AGENTS == set(PHASE1_EVIDENCE_AGENTS)


class TestListUpdate:
    def test_the_reserved_range_can_never_shadow_a_base_listing(self):
        """The screening tool treats an update that reuses a base id as an unreadable list
        (`tools_impl.current_list`): on stage that is a review, not a freeze."""
        clashes = [e.entry_id for e in ALL_ENTRIES if re.fullmatch(r"SYN-09\d{2}", e.entry_id)]
        assert clashes == []

    def test_the_update_is_published_in_the_shape_the_screening_tool_reads(self, aws):
        status, body = call(presenter("POST /demo/list-update",
                                      {"version": "ACT3-1", "entries": [NADIA]}))
        assert status == 200
        version, entries = controls.parse_overlay(aws["ssm"].values[OVERLAY])
        assert version == "ACT3-1"
        assert [(e.entry_id, e.name, e.dob, e.country) for e in entries] == [
            ("SYN-0901", "Nadia Rose Korval", "1987-02-11", "US")]
        assert body["entries"][0]["country"] == "US"

    def test_one_high_severity_event_per_open_account(self, aws):
        """HIGH, never CRITICAL: a CRITICAL event freezes regardless of what screening finds, and
        the point of the act is that the screening decides."""
        status, body = call(presenter("POST /demo/list-update",
                                      {"version": "ACT3-1", "entries": [NADIA]}))
        assert status == 200
        sent = [entry for batch in aws["events"].batches for entry in batch]
        details = [json.loads(e["Detail"]) for e in sent]
        assert sorted(d["customerId"] for d in details) == ["APP-a", "APP-b", "APP-c"]
        assert {e["DetailType"] for e in sent} == {"SanctionsListUpdate"}
        assert {e["Source"] for e in sent} == {"kyc90.screening"}
        assert {e["EventBusName"] for e in sent} == {"kyc90-lifecycle"}
        assert {d["severity"] for d in details} == {"HIGH"}
        assert {d["eventType"] for d in details} == {"SANCTIONS_LIST_UPDATE"}
        assert {d["eventId"] for d in details} == {"LIST-UPDATE-ACT3-1"}
        assert all(d["detail"] == {"listUpdate": "ACT3-1", "entryIds": ["SYN-0901"]}
                   for d in details)
        assert (body["accounts"], body["eventsSent"], body["eventsFailed"]) == (3, 3, 0)

    def test_the_event_passes_the_lifecycle_engines_own_validation(self, aws):
        """Phase 3 hands `detail` to the engine as `riskEvent` (`asl.LoadDayZero`). Every field
        the engine requires is present, and the history key carries this update's id."""
        import decision_handler

        call(presenter("POST /demo/list-update", {"version": "ACT3-1", "entries": [NADIA]}))
        detail = json.loads(aws["events"].batches[0][0]["Detail"])
        row = decision_handler._phase3(
            {"riskEvent": detail, "day0": {"caseId": "CASE-APP-a", "tier": "LOW"}},
            reading=None)
        assert row["historySortKey"].endswith("#LIST-UPDATE-ACT3-1")
        # No fresh evidence was given here, so the engine refuses to call it unchanged.
        assert row["outcome"] == "SYSTEM_ERROR"

    def test_a_case_without_an_account_is_not_sent_an_event(self, aws):
        call(presenter("POST /demo/list-update", {"version": "ACT3-1", "entries": [NADIA]}))
        sent = [json.loads(e["Detail"])["customerId"]
                for batch in aws["events"].batches for e in batch]
        assert "APP-phase1-review" not in sent

    def test_events_go_out_in_batches_of_ten(self, aws):
        aws["ddb"].by_state = {"MONITORING": [account_item(f"APP-{i:02d}") for i in range(23)]}
        aws["ddb"].page_size = 5
        status, body = call(presenter("POST /demo/list-update", {"entries": [NADIA]}))
        assert status == 200
        assert [len(b) for b in aws["events"].batches] == [10, 10, 3]
        assert body["eventsSent"] == 23

    def test_failed_entries_are_counted_not_hidden(self, aws):
        aws["events"].fail_every = 1
        body = call(presenter("POST /demo/list-update", {"entries": [NADIA]}))[1]
        assert (body["eventsSent"], body["eventsFailed"]) == (2, 1)

    def test_an_update_adds_to_what_is_already_published(self, aws):
        call(presenter("POST /demo/list-update", {"version": "V1", "entries": [NADIA]}))
        second = {"entryId": "SYN-0902", "name": "Ilya Petrov Sand"}
        body = call(presenter("POST /demo/list-update", {"version": "V2", "entries": [second]}))[1]
        assert [e["entryId"] for e in body["entries"]] == ["SYN-0901", "SYN-0902"]
        version, entries = controls.parse_overlay(aws["ssm"].values[OVERLAY])
        assert version == "V2" and len(entries) == 2

    def test_an_unreadable_published_list_is_left_alone(self, aws):
        """Reading "nothing" because Parameter Store blinked would publish the new listing alone
        and silently delist the earlier ones."""
        aws["ssm"].broken = True
        status, body = call(presenter("POST /demo/list-update", {"entries": [NADIA]}))
        assert (status, body["error"]) == (503, "LIST_OVERLAY_UNREADABLE")
        assert aws["ssm"].puts == [] and aws["events"].batches == []

    def test_a_default_version_is_generated_and_is_valid(self, aws):
        body = call(presenter("POST /demo/list-update", {"entries": [NADIA]}))[1]
        assert re.fullmatch(r"LIST-\d{8}T\d{6}Z", body["version"])

    @pytest.mark.parametrize("entries, fragment", [
        (None, "non-empty list"),
        ([], "non-empty list"),
        (["SYN-0901"], "not an object"),
        ([{**NADIA, "entryId": "SYN-0001"}], "update range"),
        ([{**NADIA, "entryId": "SYN-1901"}], "update range"),
        ([{**NADIA, "entryId": "syn-0901"}], "update range"),
        ([NADIA, NADIA], "duplicate"),
        ([{**NADIA, "name": ""}], "name is required"),
        ([{**NADIA, "name": "x" * 129}], "at most 128"),
        ([{**NADIA, "dob": "11/02/1987"}], "YYYY-MM-DD"),
        ([{**NADIA, "country": "USA"}], "two-letter"),
        ([{**NADIA, "aliases": "N. Korval"}], "aliases"),
        ([{**NADIA, "aliases": ["a", "b", "c", "d", "e", "f"]}], "aliases"),
    ])
    def test_a_malformed_update_is_refused_before_anything_is_published(self, aws, entries,
                                                                        fragment):
        status, body = call(presenter("POST /demo/list-update", {"entries": entries}))
        assert status == 400 and body["error"] == "INVALID_LIST_UPDATE"
        assert fragment in body["detail"]
        assert aws["ssm"].puts == [] and aws["events"].batches == []

    @pytest.mark.parametrize("version", ["has space", "semi;colon", "x" * 65, "a/b"])
    def test_a_version_the_list_version_could_not_carry_is_refused(self, aws, version):
        status, _ = call(presenter("POST /demo/list-update",
                                   {"version": version, "entries": [NADIA]}))
        assert status == 400
        assert aws["ssm"].puts == []


class TestCaseBoard:
    """The presenter's board. The decision time is the system's own measurement."""

    def _item(self, applicant, state, created, **extra):
        item = {"applicantId": {"S": applicant}, "caseId": {"S": f"CASE-{applicant}"},
                "state": {"S": state}, "createdAt": {"S": created},
                "declaredIdentity": {"M": {"givenNames": {"S": "AVERY QUINN"},
                                           "surname": {"S": "HALE"}}}}
        item.update({k: {"S": v} for k, v in extra.items()})
        return item

    def test_time_to_decision_is_measured_from_the_case_records_own_timestamps(self, aws):
        aws["ddb"].by_state = {"PROVISIONAL": [self._item(
            "APP-a", "PROVISIONAL", "2026-12-02T17:04:01.250Z",
            provisionalDecidedAt="2026-12-02T17:05:13.750Z")]}
        status, body = call(presenter("GET /demo/cases"))
        assert status == 200
        row, = body["cases"]
        assert row["secondsToProvisional"] == 72.5
        assert row["name"] == "AVERY QUINN HALE"
        assert row["submittedAt"] == "2026-12-02T17:04:01.250Z"

    def test_a_resubmission_is_timed_from_when_it_was_resubmitted(self, aws):
        aws["ddb"].by_state = {"PROVISIONAL": [self._item(
            "APP-a", "PROVISIONAL", "2026-12-02T16:00:00Z",
            resubmittedAt="2026-12-02T17:00:00Z",
            provisionalDecidedAt="2026-12-02T17:01:05Z")]}
        assert call(presenter("GET /demo/cases"))[1]["cases"][0]["secondsToProvisional"] == 65.0

    def test_undecided_cases_have_no_time_and_reviews_say_why(self, aws):
        aws["ddb"].by_state = {
            "IN_PROGRESS": [self._item("APP-b", "IN_PROGRESS", "2026-12-02T17:06:00Z")],
            "AWAITING_REVIEW": [self._item("APP-c", "AWAITING_REVIEW", "2026-12-02T17:05:00Z",
                                           reviewPhase="phase1",
                                           reviewReason="ENGINE_MANUAL_REVIEW")]}
        rows = call(presenter("GET /demo/cases"))[1]["cases"]
        assert [r["applicantId"] for r in rows] == ["APP-b", "APP-c"]
        assert rows[0]["secondsToProvisional"] is None
        assert "reviewReason" not in rows[0]
        assert (rows[1]["reviewPhase"], rows[1]["reviewReason"]) == ("phase1",
                                                                     "ENGINE_MANUAL_REVIEW")

    def test_newest_first_capped_across_every_state(self, aws):
        aws["ddb"].by_state = {state: [self._item(f"APP-{state}-{i}", state,
                                                  f"2026-12-02T17:{i:02d}:00Z")
                                       for i in range(5)]
                               for state in demo.BOARD_STATES}
        rows = call(presenter("GET /demo/cases"))[1]["cases"]
        assert len(rows) == demo.BOARD_LIMIT
        stamps = [r["submittedAt"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)
        queried = {c["ExpressionAttributeValues"][":state"]["S"] for c in aws["ddb"].calls}
        assert queried == set(demo.BOARD_STATES)
        assert all(c["ScanIndexForward"] is False for c in aws["ddb"].calls)

    def test_every_case_state_is_on_the_board(self):
        """A state missing here is a case that silently never appears on stage. The applicant
        projection already has to name every state, so the two lists must agree; the states the
        review handler and the state machines write are checked against it as well."""
        import re as _re
        from pathlib import Path

        import applicants
        import decisions

        assert set(demo.BOARD_STATES) == set(applicants._APPLICANT_STATES)
        reviewed = {state for targets in decisions.TARGET_STATES.values()
                    for state in targets.values()}
        asl = (Path(__file__).resolve().parents[2] / "infra" / "kyc" / "asl.py").read_text()
        machine = set(_re.findall(r'_transition\("([A-Z_]+)"', asl))
        machine |= set(_re.findall(r'to_state="([A-Z_]+)"', asl))
        assert machine >= {"AWAITING_REVIEW", "IN_PROGRESS", "PROVISIONAL", "MONITORING"}, (
            "the patterns no longer find the states asl.py writes")
        assert reviewed | machine <= set(demo.BOARD_STATES)


class TestControlsAndReset:
    def test_controls_show_what_is_set(self, aws):
        call(presenter("PUT /demo/faults", {"unavailableTools": ["pep_lookup"]}))
        body = call(presenter("GET /demo/controls"))[1]
        assert body["faults"]["unavailableTools"] == ["pep_lookup"]
        assert body["listOverlay"] == {"entries": []}

    def test_unreadable_controls_display_as_their_defaults(self, aws):
        aws["ssm"].broken = True
        assert call(presenter("GET /demo/controls"))[1] == {"faults": {},
                                                            "listOverlay": {"entries": []}}

    def test_reset_clears_both_controls_and_unfreezes_nothing(self, aws):
        call(presenter("POST /demo/list-update", {"entries": [NADIA]}))
        call(presenter("PUT /demo/faults", {"unavailableTools": ["pep_lookup"]}))
        events_before = len(aws["events"].batches)
        status, body = call(presenter("POST /demo/reset"))
        assert status == 200
        assert json.loads(aws["ssm"].values[FAULTS]) == {}
        assert controls.parse_overlay(aws["ssm"].values[OVERLAY]) == (None, ())
        assert len(aws["events"].batches) == events_before
        assert "frozen accounts stay frozen" in body["note"]
