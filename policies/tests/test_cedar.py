"""Cedar policy text generation — task 8.1-8.4, R14.2-R14.3, R15.1-R15.4, F-16, F-35.

**One `AWS::BedrockAgentCore::Policy` resource holds exactly one Cedar statement** — confirmed
live against the real `CreatePolicy` API: a `Statement` containing several concatenated
`permit(...)` blocks was rejected with `unexpected token 'permit'`. So every generator function
returns a list of individually-deployable `Statement` objects rather than one multi-statement
document, and these tests assert on that list, not on a single blob of text.
"""

from __future__ import annotations

import pytest

from cedar import (
    Statement,
    _threshold,
    all_statements,
    authorization_statements,
    content_filter_statements,
    pii_suppression_statements,
    prompt_injection_statements,
)

GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/kycgateway-us"
HARNESS_ROLES = {
    "documents": "arn:aws:iam::123456789012:role/kyc90-harness-documents",
    "identity": "arn:aws:iam::123456789012:role/kyc90-harness-identity",
    "fraud": "arn:aws:iam::123456789012:role/kyc90-harness-fraud",
    "prescreen": "arn:aws:iam::123456789012:role/kyc90-harness-prescreen",
    "compliance": "arn:aws:iam::123456789012:role/kyc90-harness-compliance",
    "notify": "arn:aws:iam::123456789012:role/kyc90-harness-notify",
    "briefing": "arn:aws:iam::123456789012:role/kyc90-harness-briefing",
}


#: The three screening tools, the only Gateway actions that declare `input.name`.
SCREENING_SCOPE = ('action in [AgentCore::Action::"AdverseMedia___adverse_media", '
                   'AgentCore::Action::"PepList___pep_lookup", '
                   'AgentCore::Action::"SanctionsScreen___sanctions_screen"]')


def _texts(statements: list[Statement]) -> list[str]:
    return [s.cedar for s in statements]


class TestOneStatementPerResource:
    def test_every_statement_contains_exactly_one_top_level_effect(self):
        """The bug this whole shape exists to prevent: a `Statement` with two `permit(...)`
        blocks concatenated fails at `CreatePolicy` with `unexpected token 'permit'`, not at
        generation time. Each returned statement must be independently valid on its own."""
        for s in authorization_statements(gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES):
            assert s.cedar.count("permit(") == 1
            assert s.cedar.strip().endswith(");")

    def test_statement_names_are_stable_and_unique(self):
        """Names are derived from content (agent + tool), not an incrementing index, so
        CloudFormation logical IDs do not silently reassign identity when an agent's tool list
        changes order."""
        statements = authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)
        names = [s.name for s in statements]
        assert len(names) == len(set(names)), "duplicate statement names"

    def test_generating_twice_produces_identical_names(self):
        first = [s.name for s in authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)]
        second = [s.name for s in authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)]
        assert first == second


class TestAuthorizationStatements:
    def test_every_agent_tool_pair_gets_an_explicit_permit(self):
        """R14.2 — explicit permit per agent per tool, not a blanket grant."""
        texts = _texts(authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES))
        joined = "\n".join(texts)
        assert 'action == AgentCore::Action::"DocumentStore___s3_get_document"' in joined
        assert 'action == AgentCore::Action::"FaceCompare___rekognition_compare_faces"' in joined
        assert 'action == AgentCore::Action::"VelocityStore___velocity_check"' in joined

    def test_the_action_name_has_exactly_three_underscores(self):
        """F-35 — the single easiest thing in this build to get wrong."""
        joined = "\n".join(_texts(authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)))
        assert "DocumentStore___s3_get_document" in joined
        assert "DocumentStore____s3_get_document" not in joined  # four underscores: wrong
        assert "DocumentStores3_get_document" not in joined       # zero: wrong

    def test_the_principal_is_the_assumed_role_arn_not_the_role_arn(self):
        """The Cedar entity AgentCore resolves an IAM-authenticated caller to is the
        `sts::...:assumed-role/...` form (policy-conditions.html), not the `iam::role/...` ARN a
        CDK role object reports — using the wrong form means every permit silently never
        matches."""
        joined = "\n".join(_texts(authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)))
        assert ('AgentCore::IamEntity::"arn:aws:sts::123456789012:assumed-role/'
                'kyc90-harness-documents"') in joined
        assert "iam::123456789012:role/kyc90-harness-documents" not in joined

    def test_the_resource_is_the_exact_gateway_arn(self):
        """Cedar forbids wildcard resources (F-17) — this must be the concrete ARN, always."""
        statements = authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)
        for s in statements:
            assert f'resource == AgentCore::Gateway::"{GATEWAY_ARN}"' in s.cedar
            assert "*" not in s.cedar

    def test_briefing_and_notify_have_no_tools_so_get_no_statement(self):
        """Briefing and Notify use no tools; they must not appear as a principal in any permit
        statement, since an agent with no tools needs no authorisation surface."""
        joined = "\n".join(_texts(authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)))
        assert "kyc90-harness-briefing" not in joined
        assert "kyc90-harness-notify" not in joined

    def test_an_agent_with_multiple_tools_gets_one_statement_each(self):
        """Compliance Risk has two tools (pep_lookup, adverse_media) — one permit statement per
        tool, not one permit trying to cover both."""
        joined = "\n".join(_texts(authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)))
        for action in ("PepList___pep_lookup", "AdverseMedia___adverse_media"):
            assert f'action == AgentCore::Action::"{action}"' in joined

    def test_screening_is_permitted_to_pre_screen_only(self):
        """One screening tool, one agent allowed to call it. Compliance no longer screens
        sanctions in Phase 2 — the full screen runs before the account opens."""
        statements = authorization_statements(
            gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)
        screening = [s for s in statements if "SanctionsScreen___sanctions_screen" in s.cedar]
        assert len(screening) == 1
        assert "kyc90-harness-prescreen" in screening[0].cedar

    def test_no_permit_statements_raises(self):
        """A generator that silently produced zero permits would leave every agent unable to
        call any tool — indistinguishable from a working Gateway until the first live call."""
        with pytest.raises(ValueError):
            authorization_statements(
                gateway_arn=GATEWAY_ARN, harness_roles={"briefing": HARNESS_ROLES["briefing"]})


class TestPromptInjectionStatements:
    def test_is_a_single_forbid_statement_on_prompt_attack(self):
        """R15.1."""
        statements = prompt_injection_statements(gateway_arn=GATEWAY_ARN)
        assert len(statements) == 1
        text = statements[0].cedar
        assert text.startswith("forbid(")
        assert 'BedrockGuardrails::PromptAttack(["PROMPT_INJECTION"]' in text
        assert 'greaterThan(decimal("0.4"))' in text

    def test_the_datapath_has_a_field_segment_not_a_bare_context_object(self):
        """Confirmed live: a bare `context.input` (no field) is rejected with "Dogwood data
        path missing field segment". `context.input.name` is the field every screening tool
        this policy protects carries applicant-supplied free text through."""
        text = prompt_injection_statements(gateway_arn=GATEWAY_ARN)[0].cedar
        assert "context.input.name" in text
        assert "[context.input]" not in text

    def test_is_scoped_to_exactly_the_actions_that_declare_the_field(self):
        """Confirmed live: an unscoped `action` is rejected, because it covers the `CallTool`
        parent, which declares no `input.name`."""
        text = prompt_injection_statements(gateway_arn=GATEWAY_ARN)[0].cedar
        assert SCREENING_SCOPE in text
        assert "  action,\n" not in text

    def test_uses_a_guardrails_when_block_only(self):
        """F-16 — `when guardrails`, never a plain `when`, and never mixed with a `permit` in
        the same statement."""
        text = prompt_injection_statements(gateway_arn=GATEWAY_ARN)[0].cedar
        assert "when guardrails {" in text
        assert "permit(" not in text

    def test_rejects_an_invalid_threshold(self):
        """F-16/R15.4 — only {0, 0.2, 0.4, 0.6, 0.8, 1.0} are real scores the service returns."""
        with pytest.raises(ValueError):
            prompt_injection_statements(gateway_arn=GATEWAY_ARN, threshold="0.45")


class TestPiiSuppressionStatements:
    #: An action that would declare an output schema. None does today, so the statement text is
    #: tested against an explicit scope.
    ACTIONS = ["DocumentStore___s3_get_document"]

    def test_nothing_while_no_gateway_tool_declares_an_output_schema(self):
        """The service rejects an output field the scoped actions do not declare, and no tool
        declares one, so there is no statement to create and nothing it could scan."""
        assert pii_suppression_statements(gateway_arn=GATEWAY_ARN) == []

    def test_covers_ssn_and_credit_card_by_default(self):
        """R15.2 — one statement per category."""
        statements = pii_suppression_statements(gateway_arn=GATEWAY_ARN, actions=self.ACTIONS)
        assert len(statements) == 2
        joined = "\n".join(_texts(statements))
        assert 'BedrockGuardrails::SensitiveInformation(["US_SOCIAL_SECURITY_NUMBER"]' in joined
        assert 'BedrockGuardrails::SensitiveInformation(["CREDIT_DEBIT_CARD_NUMBER"]' in joined
        assert 'action == AgentCore::Action::"DocumentStore___s3_get_document"' in joined

    def test_uses_suppress_output_not_forbid(self):
        """suppressOutput blocks the model's response after the fact; a `forbid` here would
        block the whole tool call rather than only the sensitive value in its result."""
        for s in pii_suppression_statements(gateway_arn=GATEWAY_ARN, actions=self.ACTIONS):
            assert s.cedar.startswith("suppressOutput(")
            assert "forbid(" not in s.cedar

    def test_uses_max_confidence_score_not_per_category_indexing(self):
        """Confirmed live: `SensitiveInformation` rejects the `["CATEGORY"].confidenceScore`
        indexing form the docs show for ContentFilter/PromptAttack — "per-category indexing is
        not supported for SensitiveInformation" — and requires an aggregation call
        (`.maxConfidenceScore()`) chained directly off the guardrail call instead."""
        for s in pii_suppression_statements(gateway_arn=GATEWAY_ARN, actions=self.ACTIONS):
            assert ".maxConfidenceScore()" in s.cedar
            assert ".confidenceScore" not in s.cedar


class TestContentFilterStatements:
    def test_covers_violence_and_hate_at_the_spec_threshold(self):
        """R15.3 — one statement per category."""
        statements = content_filter_statements(gateway_arn=GATEWAY_ARN)
        assert len(statements) == 2
        joined = "\n".join(_texts(statements))
        assert 'BedrockGuardrails::ContentFilter(["VIOLENCE"]' in joined
        assert 'BedrockGuardrails::ContentFilter(["HATE"]' in joined
        assert 'greaterThan(decimal("0.2"))' in joined

    def test_the_datapath_has_a_field_segment(self):
        for s in content_filter_statements(gateway_arn=GATEWAY_ARN):
            assert "context.input.name" in s.cedar
            assert SCREENING_SCOPE in s.cedar


class TestAllStatements:
    def test_combines_every_kind_for_one_gateway(self):
        """No pii_suppression while no Gateway tool declares an output schema (see above)."""
        statements = all_statements(gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)
        kinds = {s.kind for s in statements}
        assert kinds == {"authorization", "prompt_injection", "content_filter"}
        assert len(statements) == 9

    def test_every_name_across_all_kinds_is_unique(self):
        """These become CloudFormation logical IDs within one jurisdiction's construct scope —
        a collision between, say, an authorisation statement and a content-filter statement
        would silently drop one resource."""
        statements = all_statements(gateway_arn=GATEWAY_ARN, harness_roles=HARNESS_ROLES)
        names = [s.name for s in statements]
        assert len(names) == len(set(names))


class TestThreshold:
    @pytest.mark.parametrize("value", ["0", "0.2", "0.4", "0.6", "0.8", "1.0", "1"])
    def test_accepts_every_valid_score(self, value):
        _threshold(value)  # must not raise

    @pytest.mark.parametrize("value", ["0.1", "0.45", "0.99", "2", "-0.2"])
    def test_rejects_everything_else(self, value):
        with pytest.raises(ValueError):
            _threshold(value)
