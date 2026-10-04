"""What an applicant may be told — `api/notifications.py`, the tipping-off guard."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import account_stub
import notifications
from notifications import (
    APPLICANT_SAFE_REASON_CODES,
    CONTRADICTS_ACCOUNT,
    NO_MESSAGE,
    TEMPLATES,
    TIPPING_OFF,
    UNVERIFIABLE_DETAIL,
    applicant_outcome,
    check,
    guard,
    message_text,
    tips_off,
)

#: Replies the deployed Notify agent (Nova Micro, temperature 0) wrote for real cases, as stored.
#: It answers in the fields its prompt asks for, so the inbox showed the object itself.
#: Given six restrictions, it listed three.
REAL_PROVISIONAL_PARTIAL_LIST = (
    '{\n  "subject": "Your Account Status Update",\n  "body": "Your account is now open and '
    'usable. Here are the current limits:\\n\\n1. Cash withdrawals are not available yet.\\n2. '
    'Transfers out of the account are not available yet.\\n3. International transfers are not '
    'available yet.\\n\\nThe limits are temporary while standard checks complete.",\n  '
    '"limitsExplained": "Your account is open with the following temporary limits:\\n\\n- Cash '
    'withdrawals are not available yet.\\n- Transfers out of the account are not available '
    'yet.\\n- International transfers are not available yet."\n}')
#: Fully open, and said so; the stale ceiling is only in `limitsExplained`.
REAL_OUTCOME_CLEAN = (
    '{\n  "subject": "Your Account Status Update",\n  "body": "Your account is now fully open and '
    'usable. There are no current limits on your account. Standard checks have been '
    'completed.",\n  "limitsExplained": "Your account is fully open with no restrictions. You can '
    'make international transfers, upgrade products, and send third-party payments. The deposit '
    'ceiling is $2,000 USD."\n}')
#: Fully open (no restrictions), told its old ceiling still applies.
REAL_OUTCOME_STALE_CEILING = (
    '{\n  "subject": "Your Account Status Update",\n  "body": "Your account is now open and '
    'usable. Here are the details:\\n\\n1. Your account is open and usable now.\\n2. Your account '
    'has a deposit ceiling of $2,000 USD.\\n3. These limits are temporary while standard checks '
    'complete.",\n  "limitsExplained": "Your account is open and usable now. Your deposit limit is '
    '$2,000 USD. These limits are temporary while standard checks complete."\n}')
#: Fully open, told a transfer limit it no longer has.
REAL_OUTCOME_INVENTED_LIMIT = (
    '{\n  "subject": "Your Account Update",\n  "body": "Your account is now open and usable. Here '
    'are the limits that apply:\\n\\n1. International transfers are blocked.\\n2. You can deposit '
    'up to $2,000 USD.\\n3. These limits are temporary while standard checks complete.",\n  '
    '"limitsExplained": "Your account is open and usable now. International transfers are '
    'blocked and you can deposit up to $2,000 USD. These limits are temporary while standard '
    'checks complete."\n}')


class TestFixedWordingIsItselfSafe:
    """The guard's fallback, and every other fixed string an applicant reads, must pass the guard.
    A template that tripped it would be replaced by... itself, and nobody would notice it says
    "screening"."""

    @pytest.mark.parametrize("state", sorted(TEMPLATES))
    def test_every_template_passes_unchanged(self, state):
        assert guard(TEMPLATES[state], account_state=state) == (TEMPLATES[state], False)

    @pytest.mark.parametrize("code", sorted(APPLICANT_SAFE_REASON_CODES))
    def test_every_reason_code_shown_to_an_applicant_passes(self, code):
        assert not notifications._FORBIDDEN.search(code.replace("_", " "))

    @pytest.mark.parametrize("restriction", list(account_stub._DESCRIPTIONS))
    def test_the_account_platforms_restriction_wording_passes(self, restriction):
        """The applicant's account view shows these descriptions verbatim. Their figures are the
        platform's own, so only the tipping-off check applies."""
        text = account_stub._DESCRIPTIONS[restriction].format(ceiling="$2,000")
        assert not tips_off(text)

    def test_there_is_a_template_for_every_account_state(self):
        assert set(TEMPLATES) == {s.value for s in account_stub.AccountState}


class TestEveryRefusalThePlatformGivesIsSafe:
    """The applicant's account view shows a refused transaction's `reason` verbatim."""

    def refusals(self):
        plat = account_stub.AccountPlatform(account_stub.MemoryStore())
        reasons = set()
        plat.open_provisional("APP-t")
        plat.restrict("APP-t", "TIGHTENED", hold="EVT-1")
        for kind in account_stub.TxnType:
            reasons.add(plat.transact("APP-t", kind, 10)["reason"])
        plat.open_provisional("APP-c")
        reasons.add(plat.transact("APP-c", "DEPOSIT", 1_000_000)["reason"])
        plat.open_provisional("APP-f")
        plat.freeze("APP-f", outcome="FREEZE", reason="test")
        reasons.add(plat.transact("APP-f", "DEPOSIT", 1)["reason"])
        plat.open_provisional("APP-s")
        plat.lift("APP-s", "STANDARD", outcome="FULL_APPROVE_STANDARD")
        reasons.add(plat.transact("APP-s", "CASH_WITHDRAWAL", 5)["reason"])
        return reasons

    def test_each_refusal_reason_passes_the_guard(self):
        reasons = self.refusals()
        assert len(reasons) >= 6, reasons
        for reason in reasons:
            assert not tips_off(reason), reason


class TestTheApplicantsOwnScreensCannotTipOff:
    """The applicant app's post-submission wording lives in one file so this can hold it to the
    same guard as the agents' messages. A screen that said "our compliance team is reviewing your
    application" would tip off as surely as a model that said it."""

    COPY = (Path(__file__).resolve().parents[2]
            / "frontend" / "apps" / "console" / "src" / "applicant" / "copy.ts")

    def strings(self):
        text = self.COPY.read_text(encoding="utf-8")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        # Join `"a " + "b"` first, so a word split across a line break is still one word.
        text = re.sub(r'"\s*\+\s*"', "", text)
        return [s for s in re.findall(r'"((?:[^"\\]|\\.)*)"', text) if not s.startswith("@")]

    def test_every_sentence_passes_the_guard(self):
        strings = self.strings()
        assert len(strings) > 40, "copy.ts no longer parses as expected"
        assert [s for s in strings if notifications._FORBIDDEN.search(s)] == []

    def test_the_account_wording_agrees_with_the_guards_templates(self):
        """Same state, same sentence, whichever surface the applicant reads it on."""
        text = " ".join(self.strings())
        for phrase in ("You don't need to do anything right now, and we'll be in touch.",
                       "The earlier limits no longer apply.",
                       "A limit on total deposits still applies"):
            assert phrase in text
            assert any(phrase in template for template in TEMPLATES.values())


class TestTheGuard:
    @pytest.mark.parametrize("text", [
        "Your account is limited while sanctions screening completes.",
        "We are running a SCREENING check on your details.",
        "Your risk score is being reviewed.",
        "A risk-based review applies to your account.",
        "Our fraud team will contact you.",
        "You matched a watch list entry.",
        "You matched a watchlist entry.",
        "Because you are a PEP, transfers are limited.",
        "Politically exposed persons need extra checks.",
        "We found adverse media about you.",
        "This is an AML requirement.",
        "OFAC rules apply.",
        "Our compliance team is reviewing your account.",
        "Your account is under investigation.",
        "Suspected money laundering.",
        "You may be a designated person.",
        "An alert was raised on your account.",
    ])
    def test_a_message_that_says_why_is_replaced_whole(self, text):
        shown, replaced = guard(text, account_state="OPEN_RESTRICTED")
        assert replaced is True
        assert shown == TEMPLATES["OPEN_RESTRICTED"]

    def test_the_replacement_is_chosen_by_account_state(self):
        assert guard("frozen for sanctions", account_state="FROZEN")[0] == TEMPLATES["FROZEN"]
        assert guard("risk", account_state="OPEN_STANDARD")[0] == TEMPLATES["OPEN_STANDARD"]

    def test_an_unknown_state_gets_the_most_cautious_wording(self):
        assert guard("risk", account_state="UNKNOWN")[0] == TEMPLATES["OPEN_RESTRICTED"]
        assert guard("risk", account_state=None)[0] == TEMPLATES["OPEN_RESTRICTED"]

    @pytest.mark.parametrize("text, state", [
        ("Your account is open. Transfers out are not available yet.", "OPEN_RESTRICTED"),
        ("Welcome aboard, Taylor. Your debit card is on its way.", "OPEN_STANDARD"),
        ("Add some pepper to your day: your account is fully open.", "OPEN_STANDARD"),
    ])
    def test_an_ordinary_message_passes_as_written(self, text, state):
        assert guard(text, account_state=state) == (text, False)

    def test_empty_text_is_replaced_not_shown_blank(self):
        assert guard("", account_state="FROZEN") == (TEMPLATES["FROZEN"], True)
        assert guard(None, account_state="FROZEN") == (TEMPLATES["FROZEN"], True)
        assert guard(" \x00 ", account_state="FROZEN") == (TEMPLATES["FROZEN"], True)

    def test_control_characters_are_removed_and_length_is_capped(self):
        shown, replaced = guard("Hello\x07 there\x1b" + "x" * 5000, account_state="OPEN_STANDARD")
        assert replaced is False
        assert shown.startswith("Hello there")
        assert "\x07" not in shown and "\x1b" not in shown
        assert len(shown) <= notifications.MAX_LENGTH

    def test_saying_why_is_the_reason_given_even_when_the_message_is_also_wrong(self):
        assert check("Sanctions screening limits your deposits to $2,000.",
                     account_state="OPEN_STANDARD") == (TEMPLATES["OPEN_STANDARD"], TIPPING_OFF)


class TestTheAgentsReply:
    """The Notify agent answers in the fields its prompt asks for. The message is the `body`."""

    BODY = "Your account is open. We'll let you know when that changes."

    def test_a_reply_in_fields_gives_its_body(self):
        reply = '{"subject": "Update", "body": "%s", "limitsExplained": "x"}' % self.BODY
        assert message_text(reply) == self.BODY

    def test_a_fenced_reply_gives_its_body(self):
        assert message_text('```json\n{"body": "%s"}\n```' % self.BODY) == self.BODY

    def test_a_reply_after_a_line_of_preamble_gives_its_body(self):
        reply = 'Here is the message:\n{"subject": "Update", "body": "%s"}\nThanks.' % self.BODY
        assert message_text(reply) == self.BODY

    def test_the_body_keeps_its_line_breaks(self):
        assert message_text('{"body": "One.\\n\\nTwo."}') == "One.\n\nTwo."

    def test_a_message_field_is_read_when_there_is_no_body(self):
        assert message_text('{"message": "%s"}' % self.BODY) == self.BODY

    @pytest.mark.parametrize("reply", [
        '{"subject": "Update", "body": "Your account is',
        '{"subject": "Update", "body": "Your account is open.",}',
        '{"subject": "Update"}',
        '{"body": ["Your account is open."]}',
    ])
    def test_a_reply_that_is_structure_but_has_no_message_gives_none(self, reply):
        assert message_text(reply) == ""
        assert check(reply, account_state="OPEN_STANDARD") == (TEMPLATES["OPEN_STANDARD"],
                                                               NO_MESSAGE)

    def test_plain_text_is_the_message(self):
        assert message_text("  %s  " % self.BODY) == self.BODY
        assert message_text("```\n%s\n```" % self.BODY) == self.BODY

    def test_a_brace_inside_a_sentence_is_part_of_the_sentence(self):
        text = "Your account is open {see the app} today."
        assert message_text(text) == text


class TestTheMessageIsTrue:
    """A message may say what state the account is in. It may not quote figures, list limits,
    or say something the state rules out: none of that can be checked here, and a customer told a
    wrong limit has been told something false by their bank."""

    def test_the_real_list_of_three_limits_out_of_six_is_replaced(self):
        assert check(REAL_PROVISIONAL_PARTIAL_LIST, account_state="OPEN_RESTRICTED") == (
            TEMPLATES["OPEN_RESTRICTED"], UNVERIFIABLE_DETAIL)

    def test_the_real_fully_open_message_is_shown_as_written_without_its_fields(self):
        assert check(REAL_OUTCOME_CLEAN, account_state="OPEN_STANDARD") == (
            "Your account is now fully open and usable. There are no current limits on your "
            "account. Standard checks have been completed.", None)

    @pytest.mark.parametrize("reply", [REAL_OUTCOME_STALE_CEILING, REAL_OUTCOME_INVENTED_LIMIT])
    def test_a_real_fully_open_account_told_it_has_limits_is_replaced(self, reply):
        assert check(reply, account_state="OPEN_STANDARD") == (TEMPLATES["OPEN_STANDARD"],
                                                               CONTRADICTS_ACCOUNT)

    @pytest.mark.parametrize("text", [
        "You can deposit up to $2,000 for now.",
        "You can deposit up to USD 2000 for now.",
        "You can deposit up to 2000 USD for now.",
        "You can deposit up to 2,000 dollars for now.",
        "Deposits are capped at 2,000 for now.",
        "You can pay in up to £500 for now.",
    ])
    def test_a_figure_is_replaced(self, text):
        assert check(text, account_state="OPEN_RESTRICTED") == (TEMPLATES["OPEN_RESTRICTED"],
                                                                UNVERIFIABLE_DETAIL)

    @pytest.mark.parametrize("text", [
        "Your account is open. For now:\n- transfers out are not available yet",
        "Your account is open. For now:\n1. transfers out are not available yet",
        "Your account is open. For now:\n2) transfers out are not available yet",
        "Your account is open. For now:\n\u2022 transfers out are not available yet",
    ])
    def test_a_list_of_limits_is_replaced(self, text):
        assert check(text, account_state="OPEN_RESTRICTED") == (TEMPLATES["OPEN_RESTRICTED"],
                                                                UNVERIFIABLE_DETAIL)

    @pytest.mark.parametrize("state, text", [
        ("OPEN_RESTRICTED", "Your account is fully open."),
        ("OPEN_RESTRICTED", "There are no limits on your account."),
        ("OPEN_RESTRICTED", "Your limits have been lifted."),
        ("OPEN_ENHANCED", "Your account is fully open. The earlier limits no longer apply."),
        ("OPEN_ENHANCED", "There are no restrictions on your account."),
        ("OPEN_STANDARD", "Your account is open. Transfers out are not available yet."),
        ("OPEN_STANDARD", "Your account is open, but some features are limited for now."),
        ("OPEN_STANDARD", "Your account is open. A limit on deposits still applies."),
        ("OPEN_STANDARD", "Your account is open, but you cannot send money abroad yet."),
        ("OPEN_STANDARD", "International transfers are blocked for now."),
        ("FROZEN", "Your account is fully open."),
        ("FROZEN", "You can still make payments while we finish."),
        ("FROZEN", "Transfers are available as usual."),
        (None, "There are no restrictions on your account."),
    ])
    def test_a_claim_the_account_state_rules_out_is_replaced(self, state, text):
        assert check(text, account_state=state) == (
            TEMPLATES.get(state or "", TEMPLATES["OPEN_RESTRICTED"]), CONTRADICTS_ACCOUNT)

    @pytest.mark.parametrize("state, text", [
        ("OPEN_STANDARD", "Your account is fully open. Transfers are no longer blocked."),
        ("OPEN_STANDARD", "Good news: your account isn't restricted any more."),
        ("OPEN_STANDARD", "Your account is fully open, and deposits are not capped."),
        ("OPEN_ENHANCED", "Payments and transfers are open now. Your account page shows the rest."),
        ("OPEN_RESTRICTED", "Some features will open once our standard checks finish."),
        ("FROZEN", "Your account is not available right now. We'll be in touch."),
        ("FROZEN", "You can't use your account for now, and you don't need to do anything."),
        ("FROZEN", "Your account is temporarily unavailable."),
    ])
    def test_a_true_statement_of_the_state_passes(self, state, text):
        assert check(text, account_state=state) == (text, None)

    def test_an_accuracy_replacement_counts_as_replaced(self):
        assert guard("Deposits are limited to $2,000.", account_state="OPEN_RESTRICTED") == (
            TEMPLATES["OPEN_RESTRICTED"], True)

    def test_windows_line_breaks_are_normalised(self):
        assert check("Your account is open.\r\n\r\nWelcome aboard.",
                     account_state="OPEN_RESTRICTED") == (
            "Your account is open.\n\nWelcome aboard.", None)


class TestProjection:
    def test_the_applicant_sees_only_kind_text_and_time(self):
        notes = [{"kind": "PROVISIONAL", "text": "Open while screening runs.",
                  "accountState": "OPEN_RESTRICTED", "at": "2026-12-02T17:00:00Z",
                  "model": "x", "raw": "y"},
                 "not a note", None]
        assert notifications.project(notes) == [{
            "kind": "PROVISIONAL", "text": TEMPLATES["OPEN_RESTRICTED"],
            "at": "2026-12-02T17:00:00Z"}]

    def test_the_reviewer_sees_both_sides_of_the_guard(self):
        view = notifications.reviewer_view([{"kind": "FULL", "text": "Your risk is low.",
                                             "accountState": "OPEN_STANDARD", "at": "t"}])
        assert view == [{"kind": "FULL", "written": "Your risk is low.",
                         "shown": TEMPLATES["OPEN_STANDARD"], "replacedByGuard": True,
                         "replacedBecause": TIPPING_OFF,
                         "accountState": "OPEN_STANDARD", "at": "t"}]

    def test_the_reviewer_reads_the_message_the_agent_wrote_not_its_fields(self):
        view = notifications.reviewer_view([{"kind": "ACCOUNT_UPDATED",
                                             "text": REAL_OUTCOME_STALE_CEILING,
                                             "accountState": "OPEN_STANDARD", "at": "t"}])
        assert view[0]["written"].startswith("Your account is now open and usable. Here are")
        assert "subject" not in view[0]["written"]
        assert view[0]["replacedBecause"] == CONTRADICTS_ACCOUNT

    def test_a_reply_with_no_message_shows_the_reviewer_what_came_back(self):
        view = notifications.reviewer_view([{"kind": "ACCOUNT_UPDATED", "text": '{"subject": "Hi"',
                                             "accountState": "OPEN_STANDARD", "at": "t"}])
        assert view[0]["written"] == '{"subject": "Hi"'
        assert view[0]["shown"] == TEMPLATES["OPEN_STANDARD"]
        assert view[0]["replacedBecause"] == NO_MESSAGE

    def test_the_applicant_never_sees_the_fields(self):
        notes = [{"kind": "ACCOUNT_UPDATED", "text": REAL_OUTCOME_CLEAN,
                  "accountState": "OPEN_STANDARD", "at": "t"}]
        assert notifications.project(notes) == [{
            "kind": "ACCOUNT_UPDATED",
            "text": ("Your account is now fully open and usable. There are no current limits on "
                     "your account. Standard checks have been completed."),
            "at": "t"}]

    def test_nothing_stored_is_nothing_shown(self):
        assert notifications.project(None) == []
        assert notifications.reviewer_view([]) == []


class TestApplicantOutcome:
    @pytest.mark.parametrize("outcome, status", [
        ("PROVISIONAL_APPROVE_RESTRICTED", "OPEN_WITH_LIMITS"),
        ("FULL_APPROVE_STANDARD", "OPEN"),
        ("FULL_APPROVE_ENHANCED_MONITORING", "OPEN"),
        ("STEP_UP", "MORE_INFORMATION_NEEDED"),
        ("MANUAL_REVIEW", "IN_REVIEW"),
        ("REJECT", "DECLINED"),
        ("RISK_INCREASED_REVIEW", "IN_PROGRESS"),
        ("", "IN_PROGRESS"),
    ])
    def test_the_applicant_learns_the_status_never_the_rule(self, outcome, status):
        record = {"outcome": outcome, "rule": "O1.screening_confirmed", "tier": "HIGH",
                  "reasonCode": "SCREENING_CONFIRMED_MATCH", "explanation": "SYN-0001"}
        assert applicant_outcome(record) == {"status": status}

    def test_a_step_up_names_what_to_fix(self):
        summary = applicant_outcome({"outcome": "STEP_UP", "reasonCode": "FACE_IMAGE_UNREADABLE",
                                     "requiredItem": "A clear photo of the photo page"})
        assert summary == {"status": "MORE_INFORMATION_NEEDED",
                           "reasonCode": "FACE_IMAGE_UNREADABLE",
                           "requiredItem": "A clear photo of the photo page"}

    def test_a_step_up_reason_outside_the_allowlist_is_withheld(self):
        summary = applicant_outcome({"outcome": "STEP_UP", "reasonCode": "SCREENING_PENDING",
                                     "requiredItem": "A current, unexpired identity document"})
        assert "reasonCode" not in summary
        assert summary["requiredItem"] == "A current, unexpired identity document"

    def test_a_required_item_that_would_say_why_is_withheld(self):
        summary = applicant_outcome({"outcome": "STEP_UP",
                                     "requiredItem": "A replacement for: screening"})
        assert summary == {"status": "MORE_INFORMATION_NEEDED"}

    def test_no_record_is_an_empty_summary(self):
        assert applicant_outcome(None) == {}
        assert applicant_outcome("MANUAL_REVIEW") == {}
