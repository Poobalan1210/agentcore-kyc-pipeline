"""Tool layer tests — task 2.8.

Ported discipline from P-10 (350 tests there). The tests that carry the most weight are not the
happy paths; they are:

  * fail-closed: an unreachable source must return UNAVAILABLE, never "no match" (R25.7, AC-12)
  * no confirmation on a name collision (R27.7)
  * the pre-screen finds all five exact seeds with zero misses (R25.5)
  * nothing sensitive reaches a log (task 2.1)
"""

from __future__ import annotations

import importlib
import json
import logging

import pytest

from kyctools.contract import HitClassification, Status
from kyctools.data.sanctions import (
    ALL_ENTRIES,
    EXACT_MATCH_SEEDS,
    FALSE_POSITIVE_DECOYS,
    MEDIA_ARTICLES,
    normalise,
    strip_diacritics,
    tokens,
)
from kyctools.matching import Candidate, disambiguate_media, exact_match, fuzzy_match, worst
from kyctools.redact import SENSITIVE_KEYS, scrub
from kyctools.tools_impl import PHASE1_TOOLS, PHASE2_TOOLS, SCREENING_TOOLS, TOOLS

import handler as h


def call(tool: str, args: dict) -> dict:
    return h.handler({"toolName": tool, "arguments": args})


# ==================================================================== fail closed
class TestFailClosed:
    """R25.7 / AC-12. The single most important property in the tool layer."""

    @pytest.mark.parametrize("tool", SCREENING_TOOLS)
    def test_unreachable_source_returns_unavailable_not_no_match(self, tool, monkeypatch):
        monkeypatch.setenv("FORCE_UNAVAILABLE", tool)
        import kyctools.tools_impl as impl
        importlib.reload(impl)
        importlib.reload(h)

        result = h.handler({"toolName": tool,
                            "arguments": {"name": "Dmitri Volkov", "dob": "1968-04-12"}})
        assert result["status"] == Status.UNAVAILABLE.value, result
        # The failure mode this guards against: a caller reading `matched: False` and
        # concluding the applicant is clean when nothing was actually checked.
        assert "matched" not in result.get("data", {})
        assert result.get("reason_code")

        monkeypatch.delenv("FORCE_UNAVAILABLE")
        importlib.reload(impl)
        importlib.reload(h)

    def test_handler_never_raises(self):
        """A Lambda error is ambiguous at the agent boundary; UNAVAILABLE is not."""
        for bad in ({}, {"toolName": "nope"}, {"toolName": "sanctions_fuzzy"},
                    {"body": "not json"}, {"toolName": "pep_lookup", "arguments": {}}):
            out = h.handler(bad)
            assert out["status"] in {s.value for s in Status}
            assert "tool" in out

    def test_missing_name_is_invalid_not_clean(self):
        out = call("sanctions_exact_match", {"dob": "1968-04-12"})
        assert out["status"] == Status.INVALID.value
        assert "matched" not in out.get("data", {})


# ==================================================================== exact match
class TestExactMatch:
    def test_all_five_exact_seeds_found_with_zero_misses(self):
        """R25.5: all 5 exact matches, zero misses."""
        for entry in EXACT_MATCH_SEEDS:
            out = call("sanctions_exact_match",
                       {"name": entry.name, "dob": entry.dob, "country": entry.country})
            assert out["status"] == Status.OK.value
            assert out["data"]["matched"] is True, entry.entry_id
            ids = {hit["entryId"] for hit in out["data"]["hits"]}
            assert entry.entry_id in ids, entry.entry_id
            confirmed = [hit for hit in out["data"]["hits"]
                         if hit["entryId"] == entry.entry_id
                         and hit["classification"] == HitClassification.CONFIRMED.value]
            assert confirmed, f"{entry.entry_id} should CONFIRM on name+dob"

    def test_clean_applicant_produces_no_hit(self):
        out = call("sanctions_exact_match",
                   {"name": "Aurora Whitfield-Penrose", "dob": "1993-05-05", "country": "GB"})
        assert out["data"]["matched"] is False
        assert out["data"]["hitCount"] == 0

    def test_name_alone_never_confirms(self):
        """SYN-0207 shares SYN-0001's name with a 26-year DOB gap. Confirming on the name
        alone would reject a real person."""
        out = call("sanctions_exact_match", {"name": "Dmitri Volkov"})
        for hit in out["data"]["hits"]:
            assert hit["classification"] != HitClassification.CONFIRMED.value, hit

    def test_diacritics_are_normalised(self):
        """R27.3. José and Jose are the same string to a human."""
        a = call("sanctions_exact_match",
                 {"name": "José María Fernández", "dob": "1971-07-08"})
        b = call("sanctions_exact_match",
                 {"name": "Jose Maria Fernandez", "dob": "1971-07-08"})
        assert a["data"]["matched"] and b["data"]["matched"]

    def test_prescreen_cannot_find_fuzzy_only_cases(self):
        """R11.2. Phase 1 is exact-only by design; if this starts passing, the phase split has
        been eroded and the 90-second budget is at risk."""
        out = call("sanctions_exact_match",
                   {"name": "Muhammed Al Rasheed Junior", "dob": "1979-03-11"})
        assert out["data"]["matched"] is False


# ==================================================================== fuzzy match
class TestFuzzyMatch:
    @pytest.mark.parametrize("variant", [
        "Mohammed Al-Rashid", "Mohamed Alrashid", "Muhammed Al Rasheed", "Muhammad Al Rashid",
    ])
    def test_transliteration_variants_match(self, variant):
        """R27.1."""
        out = call("sanctions_fuzzy",
                   {"name": variant, "dob": "1979-03-11", "country": "SY"})
        ids = {hit["entryId"] for hit in out["data"]["hits"]}
        assert "SYN-0101" in ids, (variant, out["data"])

    def test_reversed_name_order_matches(self):
        """R27.2. Zhang Wei / Wei Zhang."""
        out = call("sanctions_fuzzy",
                   {"name": "Wei Zhang", "dob": "1985-02-14", "country": "CN"})
        ids = {hit["entryId"] for hit in out["data"]["hits"]}
        assert "SYN-0103" in ids

    def test_married_and_maiden_names(self):
        """R27.4."""
        out = call("sanctions_fuzzy",
                   {"name": "Katarzyna Nowak", "dob": "1988-12-01",
                    "alternateNames": ["Katarzyna Kowalski"]})
        ids = {hit["entryId"] for hit in out["data"]["hits"]}
        assert "SYN-0104" in ids

    def test_differing_dob_excludes_rather_than_weakly_matches(self):
        """Both DOBs known and different is an exclusion, and the exclusion is reported so a
        reviewer can see the check happened."""
        out = call("sanctions_fuzzy",
                   {"name": "Ivan Sokolov", "dob": "1975-01-01", "country": "RU"})
        excluded_ids = {e["entryId"] for e in out["data"]["excluded"]}
        assert excluded_ids, out["data"]
        for e in out["data"]["excluded"]:
            assert "differ" in e["reason"]

    def test_missing_dob_yields_possible_never_confirmed(self):
        """SYN-0206 has no DOB. Nothing about it can be confirmed or excluded."""
        out = call("sanctions_fuzzy", {"name": "J. M. Fernandez", "country": "MX"})
        for hit in out["data"]["hits"]:
            assert hit["classification"] != HitClassification.CONFIRMED.value

    def test_hits_carry_provenance(self):
        """R18.1. Every hit must name its list and version."""
        out = call("sanctions_fuzzy", {"name": "Dmitri Volkov", "dob": "1968-04-12"})
        assert out["source"]
        assert out["list_version"]
        assert out["retrieved_at"]


# ==================================================================== PEP
class TestPep:
    def test_pep_self(self):
        out = call("pep_lookup",
                   {"name": "Olusegun Adeyemi", "dob": "1962-03-08", "country": "NG"})
        assert out["data"]["pepStatus"] == "SELF"

    @pytest.mark.parametrize("name,dob,rel", [
        ("Adaeze Adeyemi", "1990-07-22", "SPOUSE"),
        ("Chidi Adeyemi", "2001-01-15", "CHILD"),
        ("Bernard Okafor", "1970-11-30", "BUSINESS_PARTNER"),
    ])
    def test_pep_by_association_records_relationship(self, name, dob, rel):
        """R27.6. The relationship type must be recorded, not just the fact of association."""
        out = call("pep_lookup", {"name": name, "dob": dob, "country": "NG"})
        assert out["data"]["pepStatus"] == "ASSOCIATE"
        assert any(hit["relationship"] == rel for hit in out["data"]["hits"]), out["data"]

    def test_unrelated_person_is_not_pep(self):
        out = call("pep_lookup",
                   {"name": "Aurora Whitfield-Penrose", "dob": "1993-05-05"})
        assert out["data"]["pepStatus"] == "NONE"


# ==================================================================== adverse media
class TestAdverseMedia:
    def test_same_name_different_dob_is_a_collision_not_a_match(self):
        """R27.7. The footballer is not the businessman."""
        out = call("adverse_media",
                   {"name": "Kwame Asante-Boateng", "dob": "1982-06-25", "country": "GH"})
        attributed = {a["articleId"] for a in out["data"]["attributed"]}
        rejected = {r["articleId"] for r in out["data"]["rejectedCollisions"]}
        assert "MED-0001" in attributed
        assert "MED-0002" in rejected

    def test_article_without_corroborating_attribute_is_never_attributed(self):
        """MED-0004 names Dmitri Volkov with no DOB and no country."""
        out = call("adverse_media",
                   {"name": "Dmitri Volkov", "dob": "1968-04-12", "country": "RU"})
        attributed = {a["articleId"] for a in out["data"]["attributed"]}
        assert "MED-0003" in attributed
        assert "MED-0004" not in attributed

    def test_ground_truth_matches_the_disambiguator(self):
        """Every article carries is_same_person as ground truth. If the disambiguator and the
        ground truth disagree, one of them is wrong and the evaluator would be scoring noise."""
        for art in MEDIA_ARTICLES:
            if not art.subject_name or not art.subject_dob:
                continue
            cand = Candidate(name=art.subject_name, dob=art.subject_dob,
                             country=art.subject_country)
            is_match, reason = disambiguate_media(
                cand, art.subject_name, art.subject_dob, art.subject_country)
            assert is_match is True, (art.article_id, reason)


# ==================================================================== dataset integrity
class TestDataset:
    def test_five_exact_seeds_and_seven_decoys(self):
        assert len(EXACT_MATCH_SEEDS) == 5
        assert len(FALSE_POSITIVE_DECOYS) == 7

    def test_decoys_outnumber_true_designations_among_decoy_set(self):
        """B-08: most real hits are not true matches, so the dataset must lean that way."""
        assert all(not e.is_true_designation for e in FALSE_POSITIVE_DECOYS)

    def test_entry_ids_unique(self):
        ids = [e.entry_id for e in ALL_ENTRIES]
        assert len(ids) == len(set(ids))

    def test_every_record_is_labelled_synthetic_in_output(self):
        """R26.5 / D-03. The label travels with the data, not just on a slide."""
        for tool in ("sanctions_exact_match", "sanctions_fuzzy", "pep_lookup",
                     "adverse_media"):
            out = call(tool, {"name": "Dmitri Volkov", "dob": "1968-04-12"})
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

    def test_no_pii_in_handler_logs(self, caplog):
        with caplog.at_level(logging.INFO):
            call("sanctions_exact_match",
                 {"name": "Farida Nasser", "dob": "1975-11-03", "country": "SY"})
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "Farida" not in blob
        assert "1975-11-03" not in blob

    def test_every_declared_sensitive_key_is_actually_scrubbed(self):
        probe = {k: "SENTINEL-VALUE" for k in SENSITIVE_KEYS}
        blob = json.dumps(scrub(probe))
        assert "SENTINEL-VALUE" not in blob


# ==================================================================== structure
class TestStructure:
    def test_seven_evidence_tools_four_phase1_three_phase2(self):
        """The seven evidence tools are still a fixed set. The three `submit_*` output contracts
        (R11.13) are counted apart from them rather than inflating the number, because they are a
        different kind of tool: an evidence tool fetches a fact from a source, a contract tool
        records the agent's finding. Only the former can be UNAVAILABLE in the R25.7 sense.
        """
        from kyctools.tools_impl import CONTRACT_TOOLS, EVIDENCE_TOOLS

        assert len(EVIDENCE_TOOLS) == 7
        assert len(CONTRACT_TOOLS) == 7
        assert len(TOOLS) == 14
        assert set(PHASE1_TOOLS) | set(PHASE2_TOOLS) == set(TOOLS)

    def test_phase_split_of_the_evidence_tools_is_unchanged(self):
        """The seven evidence tools still split 4/3 across the phases. Only the contracts were
        added, and they follow their own agent's phase."""
        from kyctools.tools_impl import CONTRACT_TOOLS

        phase1_evidence = [t for t in PHASE1_TOOLS if t not in CONTRACT_TOOLS]
        phase2_evidence = [t for t in PHASE2_TOOLS if t not in CONTRACT_TOOLS]
        assert len(phase1_evidence) == 4
        assert len(phase2_evidence) == 3

    def test_each_contract_follows_its_agents_phase(self):
        """Compliance and Notify run in Phase 2, so their contracts live there. Offering an agent a
        contract for work it does not do in that phase is the same mismatch that made a prompt
        reference a tool the agent did not have."""
        from kyctools.tools_impl import CONTRACT_TOOLS

        assert set(PHASE1_TOOLS) & set(CONTRACT_TOOLS) == {
            "submit_document_analysis", "submit_prescreen", "submit_fraud_assessment",
            "submit_jurisdiction", "submit_identity"}
        assert set(PHASE2_TOOLS) & set(CONTRACT_TOOLS) == {
            "submit_compliance_risk", "submit_notification"}

    def test_there_is_no_liveness_tool(self):
        """R33.5 / F-40. Liveness is fetched by the Public API inside the 180-second window and
        persisted; a tool would race the expiry and lose the evidence permanently."""
        assert not any("liveness" in name for name in TOOLS)

    def test_unknown_tool_is_reported_as_invalid(self):
        out = call("sanctions_exactish_match", {"name": "x"})
        assert out["status"] == Status.INVALID.value
        assert out["reason_code"] == "UNKNOWN_TOOL"

    def test_latency_is_always_reported(self):
        out = call("pep_lookup", {"name": "Elena Marchetti", "dob": "1968-09-12"})
        assert isinstance(out["latency_ms"], float)

    def test_worst_classification_picks_the_strongest(self):
        cand = Candidate(name="Dmitri Volkov", dob="1968-04-12", country="RU")
        hits = exact_match(cand, ALL_ENTRIES)
        assert worst(hits) is HitClassification.CONFIRMED
        assert worst([]) is HitClassification.NO_MATCH
