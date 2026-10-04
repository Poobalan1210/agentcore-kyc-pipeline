"""Who is calling — `api/auth.py`. Every route's access decision goes through these four functions."""

from __future__ import annotations

from typing import Any

import pytest

import auth

SUB = "8f14e45f-ceea-467f-a0e6-1f2b3c4d5e6f"


def http_api(claims: dict[str, Any]) -> dict[str, Any]:
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def rest_api(claims: dict[str, Any]) -> dict[str, Any]:
    return {"requestContext": {"authorizer": {"claims": claims}}}


class TestClaims:
    def test_http_api_and_rest_api_shapes_are_both_read(self):
        assert auth.claims(http_api({"sub": SUB}))["sub"] == SUB
        assert auth.claims(rest_api({"sub": SUB}))["sub"] == SUB

    @pytest.mark.parametrize("event", [
        {}, {"requestContext": None}, {"requestContext": {"authorizer": "x"}},
        {"requestContext": {"authorizer": {"jwt": {"claims": "x"}}}},
        {"requestContext": {"authorizer": {"claims": ["sub"]}}},
    ])
    def test_anything_else_is_no_claims_at_all(self, event):
        assert auth.claims(event) == {}
        assert auth.caller_sub(event) == ""
        assert auth.groups(event) == frozenset()

    def test_identity_comes_from_the_claims_never_from_the_request(self):
        event = {**http_api({"sub": SUB}), "body": '{"sub": "someone-else"}',
                 "pathParameters": {"sub": "someone-else"}}
        assert auth.caller_sub(event) == SUB


class TestGroups:
    @pytest.mark.parametrize("raw, expected", [
        (["reviewers"], {"reviewers"}),
        (["applicants", "reviewers"], {"applicants", "reviewers"}),
        # How the HTTP API's JWT authorizer renders an array claim.
        ("[reviewers]", {"reviewers"}),
        ("[applicants reviewers]", {"applicants", "reviewers"}),
        ("reviewers,presenters", {"reviewers", "presenters"}),
        ('["reviewers", "presenters"]', {"reviewers", "presenters"}),
        ("", set()),
        ("[]", set()),
        (None, set()),
    ])
    def test_the_claim_is_parsed_into_whole_names(self, raw, expected):
        assert auth.groups(http_api({"cognito:groups": raw})) == frozenset(expected)

    @pytest.mark.parametrize("raw", [
        "[notreviewers]", "[reviewers-test]", "[reviewersx]", "[xreviewers applicants]",
        ["notreviewers"], ["Reviewers"], "reviewer",
    ])
    def test_a_lookalike_is_not_a_member(self, raw):
        """The old check was `"reviewers" in str(groups)`: true for every one of these."""
        assert not auth.is_member(http_api({"cognito:groups": raw}), auth.REVIEWERS)

    def test_membership_is_per_group(self):
        event = http_api({"cognito:groups": "[presenters]"})
        assert auth.is_member(event, auth.PRESENTERS)
        assert not auth.is_member(event, auth.REVIEWERS)


class TestOwnership:
    @pytest.mark.parametrize("applicant_id", [f"APP-{SUB}", f"APP-{SUB}-run-1",
                                              f"APP-{SUB}-1764694800"])
    def test_an_applicant_owns_ids_built_from_their_own_sub(self, applicant_id):
        assert auth.owns(http_api({"sub": SUB}), applicant_id)

    @pytest.mark.parametrize("applicant_id", [
        f"APP-{SUB}x",          # a longer sub that starts with this one
        f"APP-{SUB[:-1]}",      # a shorter one
        "APP-another-user",
        f"{SUB}",               # no prefix
        f"app-{SUB}",           # wrong case
        "",
    ])
    def test_nothing_else_is_owned(self, applicant_id):
        assert not auth.owns(http_api({"sub": SUB}), applicant_id)

    def test_no_sub_owns_nothing(self):
        assert not auth.owns(http_api({}), "APP-")
        assert not auth.owns(http_api({"sub": "  "}), "APP-  ")

    def test_reviewers_and_presenters_may_read_any_case_and_nobody_else_may(self):
        other = "APP-another-user"
        assert auth.may_read(http_api({"sub": SUB, "cognito:groups": "[reviewers]"}), other)
        assert auth.may_read(http_api({"sub": SUB, "cognito:groups": "[presenters]"}), other)
        assert not auth.may_read(http_api({"sub": SUB, "cognito:groups": "[applicants]"}), other)
        assert not auth.may_read(http_api({"sub": SUB, "cognito:groups": "[notreviewers]"}),
                                 other)
        assert auth.may_read(http_api({"sub": SUB}), f"APP-{SUB}")
