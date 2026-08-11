"""Tiering and outcome tests — task 6.1.

Covers design §8.1, §8.2, §8.3 and the determinism obligation in R4.10 / R25.8.
"""

from __future__ import annotations

import itertools

import pytest
from models import (
    MatchStrength,
    PepStatus,
    Phase1Outcome,
    Phase2Outcome,
    RiskTier,
    ScreeningHit,
    Severity,
    VelocitySignals,
)
from outcomes import select_phase1_outcome, select_phase2_outcome
from tiering import full_tier, preliminary_tier, tier_action

from engine_builders import CRITICAL_VELOCITY, full, prelim


# --- design §8.1 preliminary tier ---------------------------------------------------------


class TestPreliminaryTier:
    def test_clean_case_is_low(self):
        d = preliminary_tier(prelim())
        assert d.tier is RiskTier.LOW
        assert d.rule == "P1.default"

    def test_pre_screen_match_prohibits(self):
        d = preliminary_tier(prelim(pre_screen_matched=True))
        assert d.tier is RiskTier.PROHIBITED
        assert d.rule == "P1.pre_screen_matched"

    def test_pre_screen_match_beats_every_other_signal(self):
        """R4.3 — the prohibition must not be maskable by a lower-priority rule."""
        d = preliminary_tier(prelim(
            pre_screen_matched=True,
            document_confidence=0.1,
            velocity_signals=CRITICAL_VELOCITY,
            jurisdiction_risk=RiskTier.HIGH,
        ))
        assert d.tier is RiskTier.PROHIBITED

    def test_weak_document_confidence_is_high(self):
        d = preliminary_tier(prelim(document_confidence=0.79))
        assert d.tier is RiskTier.HIGH
        assert d.rule == "P1.document_confidence_below_threshold"

    def test_threshold_is_exclusive_at_the_boundary(self):
        """0.8 is not below 0.8. A boundary that drifts changes who gets restricted."""
        assert preliminary_tier(prelim(document_confidence=0.8)).tier is RiskTier.LOW
        assert preliminary_tier(prelim(document_confidence=0.7999)).tier is RiskTier.HIGH

    def test_critical_velocity_is_high(self):
        d = preliminary_tier(prelim(velocity_signals=CRITICAL_VELOCITY))
        assert d.tier is RiskTier.HIGH
        assert d.rule == "P1.velocity_critical"

    def test_high_risk_jurisdiction_is_medium(self):
        d = preliminary_tier(prelim(jurisdiction_risk=RiskTier.HIGH))
        assert d.tier is RiskTier.MEDIUM

    def test_records_every_input_and_the_rule(self):
        """R4.9 — a decision that cannot be explained later is not auditable."""
        d = preliminary_tier(prelim(document_confidence=0.5))
        assert d.rule
        assert set(d.inputs) == {
            "jurisdiction_risk", "pre_screen_matched", "document_confidence",
            "velocity_critical", "velocity_detail", "product_applied",
        }
        assert d.thresholds["document_confidence_threshold"] == 0.8
        assert d.config_version


# --- design §8.2 full tier ----------------------------------------------------------------


class TestFullTier:
    def test_clean_case_is_low(self):
        assert full_tier(full()).tier is RiskTier.LOW

    def test_confirmed_hit_prohibits(self):
        d = full_tier(full(screening_hits=(
            ScreeningHit(list_id="OFAC-SDN", strength=MatchStrength.CONFIRMED),)))
        assert d.tier is RiskTier.PROHIBITED

    def test_pep_self_is_high(self):
        assert full_tier(full(pep_status=PepStatus.SELF)).tier is RiskTier.HIGH

    def test_pep_associate_is_high(self):
        """An associate is HIGH, same as SELF. Diluting this would defeat the EDD trigger."""
        assert full_tier(full(pep_status=PepStatus.ASSOCIATE)).tier is RiskTier.HIGH

    @pytest.mark.parametrize("sev", [Severity.HIGH, Severity.CRITICAL])
    def test_adverse_media_at_or_above_high_is_high(self, sev):
        assert full_tier(full(adverse_media_severity=sev)).tier is RiskTier.HIGH

    @pytest.mark.parametrize("sev", [Severity.NONE, Severity.LOW, Severity.MEDIUM])
    def test_adverse_media_below_high_does_not_escalate(self, sev):
        assert full_tier(full(adverse_media_severity=sev)).tier is RiskTier.LOW

    @pytest.mark.parametrize("strength", [MatchStrength.PROBABLE, MatchStrength.POSSIBLE])
    def test_unresolved_hit_is_high(self, strength):
        """R27.5 — an unresolved hit is a human's call, never an automated pass."""
        d = full_tier(full(screening_hits=(
            ScreeningHit(list_id="EU-CFSP", strength=strength),)))
        assert d.tier is RiskTier.HIGH
        assert d.rule == "P2.screening_hit_unresolved"

    def test_no_match_hit_does_not_escalate(self):
        d = full_tier(full(screening_hits=(
            ScreeningHit(list_id="EU-CFSP", strength=MatchStrength.NO_MATCH),)))
        assert d.tier is RiskTier.LOW

    def test_weak_document_confidence_is_medium_not_high(self):
        """The deliberate asymmetry with §8.1: by Phase 2 stronger evidence outranks it."""
        d = full_tier(full(document_confidence=0.5))
        assert d.tier is RiskTier.MEDIUM

    def test_critical_velocity_is_medium_not_high(self):
        assert full_tier(full(velocity_signals=CRITICAL_VELOCITY)).tier is RiskTier.MEDIUM

    def test_every_declared_input_is_read_by_some_rule(self):
        """design §8.2: "a declared-but-unread input is a defect, because it implies a control
        that does not exist." Each input must be able to move the tier on its own."""
        baseline = full_tier(full()).tier
        movers = {
            "pep_status": full(pep_status=PepStatus.SELF),
            "adverse_media_severity": full(adverse_media_severity=Severity.HIGH),
            "document_confidence": full(document_confidence=0.1),
            "velocity_signals": full(velocity_signals=CRITICAL_VELOCITY),
            "jurisdiction_risk": full(jurisdiction_risk=RiskTier.HIGH),
            "screening_hits": full(screening_hits=(
                ScreeningHit(list_id="X", strength=MatchStrength.CONFIRMED),)),
        }
        for name, inputs in movers.items():
            assert full_tier(inputs).tier is not baseline, f"{name} is declared but unread"


# --- R4.7 tier actions --------------------------------------------------------------------


class TestTierActions:
    @pytest.mark.parametrize("tier,interval", [
        (RiskTier.LOW, "annual"),
        (RiskTier.MEDIUM, "6_months"),
        (RiskTier.HIGH, "quarterly"),
        (RiskTier.PROHIBITED, None),
    ])
    def test_fixed_action_and_interval(self, tier, interval):
        assert tier_action(tier)["re_kyc_interval"] == interval

    def test_every_tier_is_mapped(self):
        for tier in RiskTier:
            assert tier_action(tier)["action"]


# --- design §8.3 outcome selection --------------------------------------------------------


class TestPhase1Outcomes:
    def test_clean_case_approves_restricted(self):
        d = select_phase1_outcome(tier=preliminary_tier(prelim()))
        assert d.outcome is Phase1Outcome.PROVISIONAL_APPROVE_RESTRICTED

    def test_prohibited_rejects_with_reason_and_notifies_compliance(self):
        """R3.7 needs a machine reason code and a human explanation; R4.3 needs the rejection."""
        d = select_phase1_outcome(tier=preliminary_tier(prelim(pre_screen_matched=True)))
        assert d.outcome is Phase1Outcome.REJECT
        assert d.reason_code == "SCREENING_CONFIRMED_MATCH"
        assert d.explanation
        assert d.notify_compliance is True

    def test_rejection_explanation_does_not_blame_the_applicant(self):
        """Design §8.4: a confirmed hit is not "an automated rejection of the person"."""
        d = select_phase1_outcome(tier=preliminary_tier(prelim(pre_screen_matched=True)))
        assert "not an assessment of the applicant" in d.explanation

    def test_high_tier_goes_to_manual_review(self):
        d = select_phase1_outcome(tier=preliminary_tier(prelim(document_confidence=0.2)))
        assert d.outcome is Phase1Outcome.MANUAL_REVIEW

    def test_never_emits_a_full_approve(self):
        """R3.3 — structurally impossible, not merely avoided."""
        for inputs in [prelim(), prelim(pre_screen_matched=True),
                       prelim(document_confidence=0.1), prelim(jurisdiction_risk=RiskTier.HIGH)]:
            d = select_phase1_outcome(tier=preliminary_tier(inputs))
            assert isinstance(d.outcome, Phase1Outcome)
            assert "FULL_APPROVE" not in d.outcome.value


class TestPhase2Outcomes:
    def test_low_tier_approves_standard(self):
        d = select_phase2_outcome(tier=full_tier(full()))
        assert d.outcome is Phase2Outcome.FULL_APPROVE_STANDARD

    def test_medium_tier_approves_with_enhanced_monitoring(self):
        d = select_phase2_outcome(tier=full_tier(full(jurisdiction_risk=RiskTier.HIGH)))
        assert d.outcome is Phase2Outcome.FULL_APPROVE_ENHANCED_MONITORING

    def test_high_tier_goes_to_manual_review_regardless(self):
        """R4.8 is unconditional — "regardless of other results"."""
        d = select_phase2_outcome(tier=full_tier(full(pep_status=PepStatus.SELF)))
        assert d.outcome is Phase2Outcome.MANUAL_REVIEW

    def test_confirmed_hit_rejects(self):
        d = select_phase2_outcome(tier=full_tier(full(screening_hits=(
            ScreeningHit(list_id="OFAC-SDN", strength=MatchStrength.CONFIRMED),))))
        assert d.outcome is Phase2Outcome.REJECT
        assert d.notify_compliance is True


# --- R4.10 / R25.8 determinism ------------------------------------------------------------


class TestDeterminism:
    def test_1000_identical_inputs_give_1000_identical_preliminary_tiers(self):
        inputs = prelim(document_confidence=0.81, jurisdiction_risk=RiskTier.HIGH)
        results = {(preliminary_tier(inputs).tier, preliminary_tier(inputs).rule)
                   for _ in range(1000)}
        assert len(results) == 1

    def test_1000_identical_inputs_give_1000_identical_full_tiers(self):
        inputs = full(pep_status=PepStatus.ASSOCIATE, adverse_media_severity=Severity.MEDIUM)
        results = {(full_tier(inputs).tier, full_tier(inputs).rule) for _ in range(1000)}
        assert len(results) == 1

    def test_1000_identical_inputs_give_1000_identical_outcomes(self):
        """AC-13 / R3.5 — the whole pipeline, not just the tier."""
        inputs = prelim(velocity_signals=CRITICAL_VELOCITY)
        seen = set()
        for _ in range(1000):
            d = select_phase1_outcome(tier=preliminary_tier(inputs))
            seen.add((d.outcome, d.rule, d.reason_code))
        assert len(seen) == 1

    def test_tiering_is_total_over_the_input_space(self):
        """Ported from P-10's totality property: every combination returns exactly one tier."""
        count = 0
        for jr, pep, sev, conf, crit in itertools.product(
            list(RiskTier), list(PepStatus), list(Severity),
            [0.0, 0.5, 0.79, 0.8, 1.0], [True, False],
        ):
            d = full_tier(full(
                jurisdiction_risk=jr, pep_status=pep, adverse_media_severity=sev,
                document_confidence=conf, velocity_signals=VelocitySignals(critical=crit),
            ))
            assert isinstance(d.tier, RiskTier)
            assert d.rule
            count += 1
        assert count == 4 * 3 * 5 * 5 * 2

class TestPurity:
    """design §8: "Pure functions. No model, no network, no I/O."

    Checked by parsing the source rather than trusting the docstring. This is the property the
    whole determinism argument rests on — if a rule could read a clock, a feature flag or a
    network response, then "1,000 identical inputs give 1,000 identical outputs" would be a
    statement about one lucky run rather than about the code.
    """

    FORBIDDEN = {
        "boto3", "botocore", "requests", "urllib", "urllib3", "http", "socket",
        "random", "time", "datetime", "os", "pathlib", "subprocess", "open",
    }

    def _rule_modules(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        return [p for p in root.glob("*.py") if p.name != "decision_handler.py"]

    def test_no_forbidden_imports_in_the_rule_layer(self):
        import ast
        offenders = []
        for path in self._rule_modules():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[0] in self.FORBIDDEN:
                            offenders.append((path.name, a.name))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".")[0] in self.FORBIDDEN:
                        offenders.append((path.name, node.module))
        assert not offenders, f"impure imports in the rule layer: {offenders}"

    def test_rule_layer_is_not_empty(self):
        """Guards the guard: a glob that matched nothing would pass the test above vacuously."""
        assert len(self._rule_modules()) >= 4
