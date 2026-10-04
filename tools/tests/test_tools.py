"""Tool layer tests — task 2.8, reworked for "tools read the record, tools record the facts".

The tests that carry the most weight are not the happy paths; they are:

  * fail-closed: an unreachable source returns UNAVAILABLE, never "no match", and the case record
    says the check did not run (R25.7, AC-12)
  * the tool screens the applicant on the record, not whoever the agent names
  * no confirmation on a name collision, and a date-of-birth conflict excludes (R27.7)
  * every innocent namesake is cleared without a human (B-08)
  * the screening finds all five exact seeds with zero misses (R25.5)
  * a list update is picked up on the next call, and an unreadable one fails closed
  * nothing sensitive reaches a log (task 2.1)
"""

from __future__ import annotations

import json
import logging

import pytest

from kyctools.contract import HitClassification, Status
from kyctools.data.sanctions import (
    ALL_ENTRIES,
    COLLISION_SEEDS,
    EXACT_MATCH_SEEDS,
    FUZZY_SEEDS,
    MEDIA_ARTICLES,
    NAMESAKE_APPLICANTS,
    normalise,
    strip_diacritics,
    tokens,
)
from kyctools.matching import (
    Candidate,
    disambiguate_media,
    exact_match,
    fuzzy_match,
    names_overlap,
    screen,
    worst,
)
from kyctools.redact import SENSITIVE_KEYS, scrub
from kyctools.tools_impl import PHASE1_TOOLS, PHASE2_TOOLS, SCREENING_TOOLS, TOOLS

import handler as h


def call(tool: str, args: dict) -> dict:
    return h.handler({"toolName": tool, "arguments": args})


def split_name(full: str) -> tuple[str, str]:
    parts = full.split()
    return " ".join(parts[:-1]), parts[-1]


def seed_identity(aws, applicant_id: str, name: str, dob: str | None, country: str | None,
                  **extra):
    given, surname = split_name(name)
    aws.seed(applicant_id, given=given, surname=surname, dob=dob or "",
             nationality=country or "", **extra)


def screen_applicant(aws, name, dob=None, country=None, *, applicant_id="APP-S", **extra):
    seed_identity(aws, applicant_id, name, dob, country, **extra)
    return call("sanctions_screen", {"applicantId": applicant_id, "name": name})


# ==================================================================== fail closed
class TestFailClosed:
    """R25.7 / AC-12. The single most important property in the tool layer."""

    @pytest.mark.parametrize("tool", SCREENING_TOOLS)
    def test_unreachable_source_returns_unavailable_not_no_match(self, tool, aws, monkeypatch):
        seed_identity(aws, "APP-F", "Dmitri Volkov", "1968-04-12", "RU")
        monkeypatch.setenv("FORCE_UNAVAILABLE", tool)

        result = call(tool, {"applicantId": "APP-F", "name": "Dmitri Volkov"})
        assert result["status"] == Status.UNAVAILABLE.value, result
        # The failure mode this guards against: a caller reading `matched: False` and
        # concluding the applicant is clean when nothing was actually checked.
        assert "matched" not in result.get("data", {})
        assert result.get("reason_code")

    @pytest.mark.parametrize("tool,attribute", [
        ("sanctions_screen", "factScreening"),
        ("pep_lookup", "factPep"),
        ("adverse_media", "factAdverseMedia"),
    ])
    def test_the_case_record_says_the_check_did_not_run(self, tool, attribute, aws,
                                                        monkeypatch):
        """The rules read the record, not the agent. An agent that reads UNAVAILABLE as "no match"
        — measured in this build — changes nothing, because the record says `performed: false`."""
        seed_identity(aws, "APP-F", "Dmitri Volkov", "1968-04-12", "RU")
        monkeypatch.setenv("FORCE_UNAVAILABLE", tool)
        call(tool, {"applicantId": "APP-F", "name": "Dmitri Volkov"})
        fact = aws.ddb.fact("APP-F", attribute)
        assert fact["performed"] is False
        assert fact["status"] == "UNAVAILABLE"
        assert fact["reasonCode"] == "FORCED_UNAVAILABLE"

    def test_handler_never_raises(self, aws):
        """A Lambda error is ambiguous at the agent boundary; UNAVAILABLE is not."""
        for bad in ({}, {"toolName": "nope"}, {"toolName": "sanctions_screen"},
                    {"body": "not json"}, {"toolName": "pep_lookup", "arguments": {}}):
            out = h.handler(bad)
            assert out["status"] in {s.value for s in Status}
            assert "tool" in out

    def test_missing_applicant_is_invalid_not_clean(self, aws):
        out = call("sanctions_screen", {"name": "Dmitri Volkov"})
        assert out["status"] == Status.INVALID.value
        assert out["reason_code"] == "MISSING_APPLICANT"
        assert "matched" not in out.get("data", {})

    def test_an_unknown_case_is_invalid_and_creates_nothing(self, aws):
        out = call("sanctions_screen", {"applicantId": "APP-NOBODY", "name": "x"})
        assert out["status"] == Status.INVALID.value
        assert out["reason_code"] == "CASE_NOT_FOUND"
        assert ("APP-NOBODY", "CASE") not in aws.ddb.items

    def test_a_case_store_outage_is_unavailable(self, aws):
        seed_identity(aws, "APP-F", "Farida Nasser", "1975-11-03", "SY")
        aws.ddb.fail_reads = "ProvisionedThroughputExceededException"
        out = call("sanctions_screen", {"applicantId": "APP-F", "name": "Farida Nasser"})
        assert out["status"] == Status.UNAVAILABLE.value
        assert out["reason_code"] == "CASE_STORE_UNAVAILABLE"

    def test_a_fact_that_cannot_be_recorded_is_unavailable(self, aws):
        """A result the rules cannot read is a check that did not happen."""
        seed_identity(aws, "APP-F", "Farida Nasser", "1975-11-03", "SY")
        aws.ddb.fail_updates = "InternalServerError"
        out = call("sanctions_screen", {"applicantId": "APP-F", "name": "Farida Nasser"})
        assert out["status"] == Status.UNAVAILABLE.value
        assert out["reason_code"] == "FACT_NOT_RECORDED"

    def test_no_name_on_the_record_is_not_a_clean_result(self, aws):
        aws.seed("APP-N", given="", surname="")
        out = call("sanctions_screen", {"applicantId": "APP-N", "name": ""})
        assert out["status"] == Status.INVALID.value
        fact = aws.ddb.fact("APP-N", "factScreening")
        assert fact["performed"] is False
        assert fact["reasonCode"] == "NO_NAME_ON_RECORD"


# ==================================================================== the record, not the agent
class TestTheRecordNotTheAgent:
    def test_the_tool_screens_the_name_on_the_record_whatever_the_agent_passes(self, aws):
        """A prompt-injected agent that asks to screen "John Doe" gets the applicant screened."""
        seed_identity(aws, "APP-I", "Farida Nasser", "1975-11-03", "SY")
        out = call("sanctions_screen", {"applicantId": "APP-I", "name": "John Doe"})
        assert out["data"]["strongest"] == "CONFIRMED"
        fact = aws.ddb.fact("APP-I", "factScreening")
        assert fact["strongest"] == "CONFIRMED"
        assert fact["agentSuppliedName"] == "John Doe"
        assert fact["agentNameMatchesRecord"] is False
        assert fact["screened"]["names"] == ["Farida Nasser"]

    def test_the_agents_view_matching_the_record_is_recorded_too(self, aws):
        seed_identity(aws, "APP-I", "Aurora Whitfield Penrose", "1993-05-05", "GB")
        call("sanctions_screen", {"applicantId": "APP-I", "name": "Aurora Whitfield Penrose"})
        assert aws.ddb.fact("APP-I", "factScreening")["agentNameMatchesRecord"] is True

    def test_the_fact_is_written_as_a_flat_attribute_conditioned_on_the_case(self, aws):
        seed_identity(aws, "APP-I", "Aurora Whitfield Penrose", "1993-05-05", "GB")
        call("sanctions_screen", {"applicantId": "APP-I", "name": "x"})
        update = aws.ddb.updates[-1]
        assert update["UpdateExpression"] == "SET #f = :f, updatedAt = :t"
        assert update["ExpressionAttributeNames"] == {"#f": "factScreening"}
        assert update["ConditionExpression"] == "attribute_exists(applicantId)"

    def test_every_fact_carries_the_time_it_was_recorded(self, aws):
        """The decision engine rejects facts older than the phase that reads them."""
        seed_identity(aws, "APP-I", "Aurora Whitfield Penrose", "1993-05-05", "GB")
        call("sanctions_screen", {"applicantId": "APP-I", "name": "x"})
        assert aws.ddb.fact("APP-I", "factScreening")["recordedAt"].endswith("Z")

    def test_a_non_iso_date_of_birth_is_treated_as_unknown(self, aws):
        """Two known dates that differ exclude a match, so a US-format date compared as a string
        would exclude a true match. Unknown can only send an alert to a human."""
        seed_identity(aws, "APP-D", "Farida Nasser", "11/03/1975", "SY")
        call("sanctions_screen", {"applicantId": "APP-D", "name": "Farida Nasser"})
        fact = aws.ddb.fact("APP-D", "factScreening")
        assert fact["screened"]["dob"] is None
        assert fact["strongest"] in ("PROBABLE", "POSSIBLE")
        assert not fact["excluded"]


# ==================================================================== screening outcomes
class TestScreening:
    def test_all_five_exact_seeds_found_with_zero_misses(self, aws):
        """R25.5: all 5 exact matches, zero misses."""
        for entry in EXACT_MATCH_SEEDS:
            out = screen_applicant(aws, entry.name, entry.dob, entry.country,
                                   applicant_id=entry.entry_id)
            assert out["status"] == Status.OK.value
            confirmed = [hit for hit in out["data"]["hits"]
                         if hit["entryId"] == entry.entry_id
                         and hit["classification"] == HitClassification.CONFIRMED.value]
            assert confirmed, f"{entry.entry_id} should CONFIRM on name+dob"

    def test_clean_applicant_produces_no_alert(self, aws):
        out = screen_applicant(aws, "Aurora Whitfield Penrose", "1993-05-05", "GB")
        assert out["data"]["matched"] is False
        assert out["data"]["hitCount"] == 0
        assert out["data"]["strongest"] == "NO_MATCH"

    @pytest.mark.parametrize("namesake", NAMESAKE_APPLICANTS, ids=lambda n: n.fixture_id)
    def test_every_innocent_namesake_is_cleared_without_a_human(self, aws, namesake):
        """B-08. These are the 90-95%. Each is cleared by the matcher itself, and where the names
        do overlap the clearance is an explicit, reported exclusion rather than silence."""
        out = screen_applicant(aws, namesake.name, namesake.dob, namesake.country,
                               applicant_id=namesake.fixture_id)
        assert out["data"]["strongest"] == "NO_MATCH", (namesake.fixture_id, out["data"])
        excluded = {e["entryId"] for e in out["data"]["excluded"]}
        if namesake.expect_exclusion:
            assert namesake.resembles in excluded, (namesake.fixture_id, out["data"])
            reason = next(e["reason"] for e in out["data"]["excluded"]
                          if e["entryId"] == namesake.resembles)
            assert "differ" in reason

    def test_the_act_two_namesake_is_excluded_with_both_dates_in_the_reason(self, aws):
        out = screen_applicant(aws, "Dmitri Volkov", "1994-10-05", "UA")
        excluded = {e["entryId"]: e["reason"] for e in out["data"]["excluded"]}
        assert "1994-10-05" in excluded["SYN-0001"] and "1968-04-12" in excluded["SYN-0001"]

    def test_a_listing_without_a_date_of_birth_goes_to_a_human(self, aws):
        """R27.5. Presenter B's persona. No date of birth on the listing means the overlap can be
        neither corroborated nor excluded — POSSIBLE, which routes to review."""
        out = screen_applicant(aws, "Avery Quinn Hale", "1990-11-06", "USA")
        hits = {hit["entryId"]: hit for hit in out["data"]["hits"]}
        assert hits["SYN-0106"]["classification"] == "POSSIBLE"
        assert out["data"]["strongest"] == "POSSIBLE"

    def test_diacritics_are_normalised(self, aws):
        """R27.3. José and Jose are the same string to a human."""
        a = screen_applicant(aws, "José María Fernández", "1971-07-08", applicant_id="A")
        b = screen_applicant(aws, "Jose Maria Fernandez", "1971-07-08", applicant_id="B")
        assert a["data"]["strongest"] == b["data"]["strongest"] == "CONFIRMED"

    @pytest.mark.parametrize("variant", [
        "Mohammed Al-Rashid", "Mohamed Alrashid", "Muhammed Al Rasheed", "Muhammad Al Rashid",
    ])
    def test_transliteration_variants_alert(self, aws, variant):
        """R27.1 — now in Phase 1, because the account is open by the time Phase 2 runs."""
        out = screen_applicant(aws, variant, "1979-03-11", "SY")
        ids = {hit["entryId"] for hit in out["data"]["hits"]}
        assert "SYN-0101" in ids, (variant, out["data"])

    def test_reversed_name_order_confirms_with_the_same_date_of_birth(self, aws):
        """R27.2. Zhang Wei / Wei Zhang is one name written in two orders."""
        out = screen_applicant(aws, "Wei Zhang", "1985-02-14", "CN")
        hits = {hit["entryId"]: hit for hit in out["data"]["hits"]}
        assert hits["SYN-0103"]["classification"] == "CONFIRMED"

    def test_married_and_maiden_names_are_both_screened(self, aws):
        """R27.4."""
        seed_identity(aws, "APP-M", "Katarzyna Nowak", "1988-12-01", "POL")
        item = aws.ddb.items[("APP-M", "CASE")]
        item["declaredIdentity"]["M"]["otherNames"] = {"L": [{"S": "Katarzyna Kowalski"}]}
        out = call("sanctions_screen", {"applicantId": "APP-M", "name": "Katarzyna Nowak"})
        ids = {hit["entryId"] for hit in out["data"]["hits"]}
        assert "SYN-0104" in ids
        assert aws.ddb.fact("APP-M", "factScreening")["screened"]["names"] == [
            "Katarzyna Nowak", "Katarzyna Kowalski"]

    def test_hits_carry_provenance(self, aws):
        """R18.1. Every result must name its list and version."""
        out = screen_applicant(aws, "Dmitri Volkov", "1968-04-12", "RU")
        assert out["source"]
        assert out["list_version"]
        assert out["retrieved_at"]
        fact = aws.ddb.fact("APP-S", "factScreening")
        assert fact["listVersion"] == out["list_version"]
        assert fact["syntheticData"] is True


# ==================================================================== list updates
class TestListUpdates:
    OVERLAY = "/kyc90/screening/list-overlay"

    def overlay(self, aws, monkeypatch, document):
        monkeypatch.setenv("LIST_OVERLAY_PARAMETER", self.OVERLAY)
        aws.ssm.parameters[self.OVERLAY] = json.dumps(document)

    def test_a_published_designation_is_screened_on_the_next_call(self, aws, monkeypatch):
        seed_identity(aws, "APP-L", "Nadia Rose Korval", "1987-02-11", "USA")
        first = call("sanctions_screen", {"applicantId": "APP-L", "name": "Nadia Rose Korval"})
        assert first["data"]["strongest"] == "NO_MATCH"

        self.overlay(aws, monkeypatch, {"version": "2026-12-02.1", "entries": [
            {"entryId": "SYN-0901", "name": "Nadia Rose Korval", "dob": "1987-02-11",
             "country": "US", "programme": "SYN-UPDATE"}]})
        second = call("sanctions_screen", {"applicantId": "APP-L", "name": "Nadia Rose Korval"})
        assert second["data"]["strongest"] == "CONFIRMED"
        assert second["list_version"].endswith("+2026-12-02.1")

    def test_an_unreadable_list_update_fails_closed(self, aws, monkeypatch):
        """Screening against a list that could not be loaded is screening against a stale list."""
        seed_identity(aws, "APP-L", "Nadia Rose Korval", "1987-02-11", "USA")
        monkeypatch.setenv("LIST_OVERLAY_PARAMETER", self.OVERLAY)
        aws.ssm.fail = "ThrottlingException"
        out = call("sanctions_screen", {"applicantId": "APP-L", "name": "Nadia Rose Korval"})
        assert out["status"] == Status.UNAVAILABLE.value
        assert out["reason_code"] == "LIST_UNAVAILABLE"
        assert aws.ddb.fact("APP-L", "factScreening")["performed"] is False

    def test_a_malformed_list_update_fails_closed(self, aws, monkeypatch):
        seed_identity(aws, "APP-L", "Nadia Rose Korval", "1987-02-11", "USA")
        monkeypatch.setenv("LIST_OVERLAY_PARAMETER", self.OVERLAY)
        aws.ssm.parameters[self.OVERLAY] = "{not json"
        out = call("sanctions_screen", {"applicantId": "APP-L", "name": "x"})
        assert out["status"] == Status.UNAVAILABLE.value

    def test_an_update_that_reuses_a_listed_id_fails_closed(self, aws, monkeypatch):
        """Reusing an id would silently shadow an existing designation."""
        seed_identity(aws, "APP-L", "Nadia Rose Korval", "1987-02-11", "USA")
        self.overlay(aws, monkeypatch, {"version": "v2", "entries": [
            {"entryId": "SYN-0001", "name": "Someone Else", "dob": "1970-01-01"}]})
        out = call("sanctions_screen", {"applicantId": "APP-L", "name": "x"})
        assert out["status"] == Status.UNAVAILABLE.value
        assert out["reason_code"] == "LIST_UNAVAILABLE"

    def test_an_empty_update_is_the_base_list(self, aws, monkeypatch):
        seed_identity(aws, "APP-L", "Nadia Rose Korval", "1987-02-11", "USA")
        self.overlay(aws, monkeypatch, {"version": "v0", "entries": []})
        out = call("sanctions_screen", {"applicantId": "APP-L", "name": "x"})
        assert out["status"] == Status.OK.value
        assert "+" not in out["list_version"]


# ==================================================================== runtime fault flags
class TestRuntimeFaultFlags:
    """The flags live in Parameter Store because the Gateway calls the published alias: an
    environment variable edited on the function never reaches it."""

    FAULTS = "/kyc90/demo/faults"

    def test_a_flag_flipped_between_calls_takes_effect_without_a_redeploy(self, aws, monkeypatch):
        seed_identity(aws, "APP-R", "Aurora Whitfield Penrose", "1993-05-05", "GB")
        monkeypatch.setenv("DEMO_FAULTS_PARAMETER", self.FAULTS)
        aws.ssm.parameters[self.FAULTS] = json.dumps({"unavailableTools": []})
        assert call("sanctions_screen", {"applicantId": "APP-R", "name": "x"})["status"] == "OK"

        aws.ssm.parameters[self.FAULTS] = json.dumps({"unavailableTools": ["sanctions_screen"]})
        out = call("sanctions_screen", {"applicantId": "APP-R", "name": "x"})
        assert out["status"] == Status.UNAVAILABLE.value
        assert out["reason_code"] == "FORCED_UNAVAILABLE"

    def test_unreadable_flags_mean_no_faults(self, aws, monkeypatch):
        """A switch that exists to break the demo must never break production."""
        seed_identity(aws, "APP-R", "Aurora Whitfield Penrose", "1993-05-05", "GB")
        monkeypatch.setenv("DEMO_FAULTS_PARAMETER", self.FAULTS)
        aws.ssm.fail = "AccessDeniedException"
        assert call("sanctions_screen", {"applicantId": "APP-R", "name": "x"})["status"] == "OK"

    def test_flags_are_cached_briefly(self, aws, monkeypatch):
        from kyctools import controls

        monkeypatch.setenv("CONTROL_TTL_SECONDS", "60")
        monkeypatch.setenv("DEMO_FAULTS_PARAMETER", self.FAULTS)
        aws.ssm.parameters[self.FAULTS] = "{}"
        controls.reset_cache()
        controls.unavailable_tools()
        controls.unavailable_tools()
        assert aws.ssm.reads == 1


# ==================================================================== matcher (pure)
class TestMatcher:
    def test_an_exact_name_with_conflicting_dates_is_excluded(self):
        hits = exact_match(Candidate(name="Dmitri Volkov", dob="1994-10-05", country="UA"),
                           ALL_ENTRIES)
        (hit,) = [h for h in hits if h.entry_id == "SYN-0001"]
        assert hit.classification is HitClassification.NO_MATCH

    def test_a_shared_country_never_outweighs_a_date_conflict(self):
        """The earlier ladder checked country before the date conflict, so a strong overlap in the
        same country was PROBABLE even with two known, different dates of birth."""
        hits = fuzzy_match(Candidate(name="Ivan Petrovich Sokolov", dob="1975-01-01",
                                     country="BY"), ALL_ENTRIES)
        (hit,) = [h for h in hits if h.entry_id == "SYN-0004"]
        assert hit.classification is HitClassification.NO_MATCH

    def test_name_alone_never_confirms(self):
        for hit in fuzzy_match(Candidate(name="Dmitri Volkov"), ALL_ENTRIES):
            assert hit.classification is not HitClassification.CONFIRMED

    def test_screen_output_is_sorted_and_split(self):
        alerts, exclusions = screen(
            Candidate(name="Dmitri Volkov", dob="1994-10-05", country="UA"), ALL_ENTRIES)
        assert all(not h.excluded for h in alerts)
        assert all(h.excluded for h in exclusions)
        assert [h.entry_id for h in exclusions] == sorted(h.entry_id for h in exclusions)

    def test_worst_classification_picks_the_strongest(self):
        cand = Candidate(name="Dmitri Volkov", dob="1968-04-12", country="RU")
        hits = exact_match(cand, ALL_ENTRIES)
        assert worst(hits) is HitClassification.CONFIRMED
        assert worst([]) is HitClassification.NO_MATCH

    def test_names_overlap(self):
        assert names_overlap("Kwame Asante-Boateng", "Kwame Asante Boateng")
        assert not names_overlap("Kwame Asante-Boateng", "Elena Marchetti")


# ==================================================================== PEP
class TestPep:
    def pep(self, aws, name, dob, country="NGA", applicant_id="APP-P"):
        seed_identity(aws, applicant_id, name, dob, country)
        return call("pep_lookup", {"applicantId": applicant_id, "name": name})

    def test_pep_self(self, aws):
        assert self.pep(aws, "Olusegun Adeyemi", "1962-03-08")["data"]["pepStatus"] == "SELF"

    @pytest.mark.parametrize("name,dob,rel", [
        ("Adaeze Adeyemi", "1990-07-22", "SPOUSE"),
        ("Chidi Adeyemi", "2001-01-15", "CHILD"),
        ("Bernard Okafor", "1970-11-30", "BUSINESS_PARTNER"),
    ])
    def test_pep_by_association_records_relationship(self, aws, name, dob, rel):
        """R27.6. The relationship type must be recorded, not just the fact of association."""
        out = self.pep(aws, name, dob)
        assert out["data"]["pepStatus"] == "ASSOCIATE"
        assert any(hit["relationship"] == rel for hit in out["data"]["hits"]), out["data"]
        assert aws.ddb.fact("APP-P", "factPep")["pepStatus"] == "ASSOCIATE"

    def test_an_exact_name_with_a_different_date_of_birth_is_not_a_pep(self, aws):
        """The earlier version let an exact name set pepStatus even when the dates conflicted."""
        out = self.pep(aws, "Elena Marchetti", "1985-01-01", "ITA")
        assert out["data"]["pepStatus"] == "NONE"
        assert {e["entryId"] for e in out["data"]["excluded"]} == {"PEP-0005"}

    def test_unrelated_person_is_not_pep(self, aws):
        assert self.pep(aws, "Aurora Whitfield Penrose", "1993-05-05",
                        "GBR")["data"]["pepStatus"] == "NONE"


# ==================================================================== adverse media
class TestAdverseMedia:
    def media(self, aws, name, dob, country, applicant_id="APP-M"):
        seed_identity(aws, applicant_id, name, dob, country)
        return call("adverse_media", {"applicantId": applicant_id, "name": name})

    def test_same_name_different_dob_is_a_collision_not_a_match(self, aws):
        """R27.7. The footballer is not the businessman."""
        out = self.media(aws, "Kwame Asante-Boateng", "1982-06-25", "GHA")
        attributed = {a["articleId"] for a in out["data"]["attributed"]}
        rejected = {r["articleId"] for r in out["data"]["rejectedCollisions"]}
        assert "MED-0001" in attributed
        assert "MED-0002" in rejected

    def test_article_without_corroborating_attribute_is_never_attributed(self, aws):
        """MED-0004 names Dmitri Volkov with no DOB and no country."""
        out = self.media(aws, "Dmitri Volkov", "1968-04-12", "RUS")
        attributed = {a["articleId"] for a in out["data"]["attributed"]}
        assert "MED-0003" in attributed
        assert "MED-0004" not in attributed

    def test_unrelated_articles_are_not_reported_as_collisions(self, aws):
        """The earlier version put every non-matching headline on every applicant's record."""
        out = self.media(aws, "Aurora Whitfield Penrose", "1993-05-05", "GBR")
        assert out["data"]["attributed"] == []
        assert out["data"]["rejectedCollisions"] == []
        assert aws.ddb.fact("APP-M", "factAdverseMedia")["maxSeverity"] == "NONE"

    def test_ground_truth_matches_the_disambiguator(self):
        """Every article carries is_same_person as ground truth. Checked in both directions now:
        the old version of this test never read the flag at all, so it checked nothing."""
        for art in MEDIA_ARTICLES:
            if not art.subject_name:
                continue
            # The person each article is actually about, where the dataset says who that is.
            subject = {"MED-0001": ("Kwame Asante-Boateng", "1982-06-25", "GH"),
                       "MED-0002": ("Kwame Asante-Boateng", "1982-06-25", "GH"),
                       "MED-0003": ("Dmitri Volkov", "1968-04-12", "RU"),
                       "MED-0004": ("Dmitri Volkov", "1968-04-12", "RU"),
                       "MED-0005": ("Elena Marchetti", "1968-09-12", "IT")}[art.article_id]
            cand = Candidate(name=subject[0], dob=subject[1], country=subject[2])
            is_match, reason = disambiguate_media(
                cand, art.subject_name, art.subject_dob, art.subject_country)
            assert is_match is art.is_same_person, (art.article_id, reason)


# ==================================================================== dataset integrity
class TestDataset:
    def test_the_list_holds_designations_only(self):
        assert len(EXACT_MATCH_SEEDS) == 5
        assert len(FUZZY_SEEDS) == 5
        assert len(COLLISION_SEEDS) == 2
        assert ALL_ENTRIES == EXACT_MATCH_SEEDS + FUZZY_SEEDS + COLLISION_SEEDS
        # The decoy flag is gone: no screening list carries entries that are secretly not
        # designations, and the matcher never read it.
        assert not any(hasattr(e, "is_true_designation") for e in ALL_ENTRIES)

    def test_namesakes_outnumber_nothing_but_are_all_applicants(self):
        """B-08 is modelled from the applicant side now: seven innocent namesakes."""
        assert len(NAMESAKE_APPLICANTS) == 7
        assert all(n.dob for n in NAMESAKE_APPLICANTS)

    def test_the_presenter_b_listing_has_no_date_of_birth(self):
        (entry,) = [e for e in ALL_ENTRIES if e.entry_id == "SYN-0106"]
        assert entry.dob is None

    def test_entry_ids_unique(self):
        ids = [e.entry_id for e in ALL_ENTRIES]
        assert len(ids) == len(set(ids))

    def test_every_result_is_labelled_synthetic(self, aws):
        """R26.5 / D-03. The label travels with the data, not just on a slide."""
        seed_identity(aws, "APP-Y", "Dmitri Volkov", "1968-04-12", "RUS")
        for tool in SCREENING_TOOLS:
            out = call(tool, {"applicantId": "APP-Y", "name": "Dmitri Volkov"})
            assert out["data"].get("syntheticData") is True, tool

    def test_media_severity_values_are_valid(self):
        """__post_init__ enforces this; assert it actually fires."""
        from kyctools.data.sanctions import MediaArticle
        with pytest.raises(ValueError):
            MediaArticle(article_id="X", headline="h", subject_name="n",
                         subject_dob=None, subject_country=None,
                         severity="CRITICAL", is_same_person=False)
        with pytest.raises(TypeError):
            MediaArticle(article_id="X", headline="h", subject_name="n",
                         subject_dob=None, subject_country=None,
                         severity="LOW", is_same_person="yes")


# ==================================================================== velocity
class TestVelocity:
    def test_no_identifiers_is_not_established_not_zero(self, aws):
        """"We did not look" and "we looked and saw nothing" are different facts."""
        aws.seed("APP-V")
        out = call("velocity_check", {"applicantId": "APP-V"})
        assert out["status"] == Status.OK.value
        assert out["data"]["performed"] is False
        fact = aws.ddb.fact("APP-V", "factVelocity")
        assert fact["status"] == "NOT_ESTABLISHED"
        assert "deviceApplications" not in fact

    def test_counts_only_applications_inside_the_window(self, aws, monkeypatch):
        import time as _time

        monkeypatch.setenv("VELOCITY_TABLE", "kyc90-velocity")
        aws.seed("APP-V", intake={"deviceHash": "d1", "ipHash": "i1"})
        now = int(_time.time())
        aws.ddb.velocity["device#d1"] = [now + 3600] * 5 + [now - 10]
        aws.ddb.velocity["ip#i1"] = [now + 3600] * 2
        out = call("velocity_check", {"applicantId": "APP-V"})
        assert out["data"]["deviceApplications"] == 5
        assert out["data"]["signals"]["criticalDeviceReuse"] is True
        assert aws.ddb.queries[0]["FilterExpression"] == "expiresAt > :now"

    def test_a_velocity_store_outage_is_unavailable(self, aws, monkeypatch):
        monkeypatch.setenv("VELOCITY_TABLE", "kyc90-velocity")
        aws.seed("APP-V", intake={"deviceHash": "d1"})
        aws.ddb.fail_queries = "InternalServerError"
        out = call("velocity_check", {"applicantId": "APP-V"})
        assert out["status"] == Status.UNAVAILABLE.value
        assert aws.ddb.fact("APP-V", "factVelocity")["status"] == "UNAVAILABLE"


# ==================================================================== normalisation
class TestNormalisation:
    def test_strip_diacritics(self):
        assert strip_diacritics("José María Fernández") == "Jose Maria Fernandez"

    def test_normalise_collapses_punctuation(self):
        assert normalise("Al-Rashid,  Mohammed") == "al rashid mohammed"

    def test_particles_dropped_from_tokens(self):
        assert tokens("Abdul Rahman bin Yusuf") == frozenset(
            {"abdul", "rahman", "yusuf"})

    def test_token_sets_equal_for_reordered_names(self):
        assert tokens("Zhang Wei") == tokens("Wei Zhang")


# ==================================================================== redaction
class TestRedaction:
    def test_sensitive_values_are_fingerprinted_not_logged(self):
        out = scrub({"name": "Dmitri Volkov", "dob": "1968-04-12",
                     "status": "OK", "jurisdiction": "us"})
        assert out["name"].startswith("fp:")
        assert out["dob"].startswith("fp:")
        assert "Volkov" not in json.dumps(out)
        assert "1968-04-12" not in json.dumps(out)
        # Safe fields survive intact, otherwise the logs are useless.
        assert out["status"] == "OK"
        assert out["jurisdiction"] == "us"

    def test_nested_pii_is_scrubbed(self):
        out = scrub({"applicantId": "A1",
                     "cip": {"name": "Farida Nasser", "address": "12 Somewhere St"}})
        blob = json.dumps(out)
        assert "Farida" not in blob
        assert "Somewhere" not in blob
        assert "A1" in blob   # applicantId is a safe correlator

    def test_unknown_string_fields_are_shape_only(self):
        out = scrub({"mysteryField": "possibly-a-passport-number"})
        assert "possibly" not in json.dumps(out)
        assert out["mysteryField"].startswith("<str len=")

    def test_no_pii_in_handler_logs(self, aws, caplog):
        seed_identity(aws, "APP-L", "Farida Nasser", "1975-11-03", "SYR")
        with caplog.at_level(logging.INFO):
            call("sanctions_screen", {"applicantId": "APP-L", "name": "Farida Nasser"})
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "Farida" not in blob
        assert "1975-11-03" not in blob

    def test_every_declared_sensitive_key_is_actually_scrubbed(self):
        probe = {k: "SENTINEL-VALUE" for k in SENSITIVE_KEYS}
        blob = json.dumps(scrub(probe))
        assert "SENTINEL-VALUE" not in blob


# ==================================================================== structure
class TestStructure:
    def test_six_evidence_tools_four_phase1_two_phase2(self):
        """Six evidence tools: the exact and variant screening tools became one, because the
        variant matcher is plain code and belongs in Phase 1 before the account opens."""
        from kyctools.tools_impl import CONTRACT_TOOLS, EVIDENCE_TOOLS

        assert len(EVIDENCE_TOOLS) == 6
        assert len(CONTRACT_TOOLS) == 7
        assert len(TOOLS) == 13
        assert set(PHASE1_TOOLS) | set(PHASE2_TOOLS) == set(TOOLS)
        phase1_evidence = [t for t in PHASE1_TOOLS if t not in CONTRACT_TOOLS]
        phase2_evidence = [t for t in PHASE2_TOOLS if t not in CONTRACT_TOOLS]
        assert sorted(phase1_evidence) == ["rekognition_compare_faces", "s3_get_document",
                                           "sanctions_screen", "velocity_check"]
        assert sorted(phase2_evidence) == ["adverse_media", "pep_lookup"]

    def test_every_evidence_tool_records_one_fact(self):
        from kyctools.case_record import FACT_ATTRIBUTES
        from kyctools.tools_impl import EVIDENCE_TOOLS

        assert len(set(FACT_ATTRIBUTES.values())) == len(EVIDENCE_TOOLS)

    def test_there_is_no_liveness_tool(self):
        """R33.5 / F-40. Liveness is fetched by the Public API inside the 180-second window and
        persisted; a tool would race the expiry and lose the evidence permanently."""
        assert not any("liveness" in name for name in TOOLS)

    def test_unknown_tool_is_reported_as_invalid(self):
        out = call("sanctions_exactish_match", {"name": "x"})
        assert out["status"] == Status.INVALID.value
        assert out["reason_code"] == "UNKNOWN_TOOL"

    def test_latency_is_always_reported(self, aws):
        seed_identity(aws, "APP-T", "Elena Marchetti", "1968-09-12", "ITA")
        out = call("pep_lookup", {"applicantId": "APP-T", "name": "Elena Marchetti"})
        assert isinstance(out["latency_ms"], float)
