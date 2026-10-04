"""What an applicant may be told — the tipping-off guard. Pure: no AWS, no clock.

The Notify agent writes each message from the account's status and limits alone; it never sees a
screening result or the reason for a decision, so it cannot reveal one it was never given. This
module is the second line, and the deterministic one: before any text reaches an applicant it is
checked for language that would tell them why their account is limited — a sanctions or fraud
check, a watchlist, a risk score — and replaced with fixed, neutral wording if it contains any.

Why fixed wording rather than redaction. A message with a phrase cut out still reads as a message
with something cut out. Telling a customer they are under a sanctions or money-laundering review
can itself be an offence (tipping off), and the safest text is one written by a person, reviewed
once, and chosen by account state.

The same applies to decision outcomes: an applicant learns that an application needs more
information, is being reviewed, was declined or is open — never which rule decided it. Only a
step-up names what to fix, because the applicant cannot fix what they are not told about, and no
step-up reason is about screening.

The guard also holds a message to what the bank can vouch for. A model given an account's limits
will list half of them, quote a ceiling that no longer applies, or call a restricted account fully
open. Nothing here can check a paraphrase against the platform's list, so a message may say what
state the account is in but may not quote figures or list limits (the account page shows the
platform's own list), and a message the state contradicts is replaced the same way.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

MAX_LENGTH = 1500

#: Words that would tell an applicant why. Matched on word starts, case-insensitively, so
#: "screening", "Sanctioned" and "risk-based" are all caught. False positives only cost a fixed
#: template in place of a model-written one.
_FORBIDDEN = re.compile(
    r"\b(?:sanction|fraud|screen|watch\s*list|risk|compliance|pep\b|politically\s+exposed|"
    r"adverse\s+media|suspicious|investigat|money\s+launder|launder|aml\b|ofac|terror|"
    r"blocked\s+person|designat|criminal|police|law\s+enforcement|intelligence|alert)",
    re.IGNORECASE)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Written by a person, reviewed once, chosen by account state. They say what the customer can
#: do and when they will hear more, and nothing about why.
TEMPLATES: Mapping[str, str] = {
    "OPEN_RESTRICTED": (
        "Your account is open. Some features are limited for now while our standard checks "
        "finish, and your account page shows exactly what you can do today. We'll let you know "
        "as soon as the limits change."),
    "OPEN_ENHANCED": (
        "Your account is open for payments and transfers. A limit on total deposits still "
        "applies; your account page shows it."),
    "OPEN_STANDARD": (
        "Your account is fully open. The earlier limits no longer apply."),
    "FROZEN": (
        "Your account is temporarily unavailable. You don't need to do anything right now, and "
        "we'll be in touch."),
}
_DEFAULT_TEMPLATE = TEMPLATES["OPEN_RESTRICTED"]

#: Step-up reasons are about what the applicant sent — a document, a selfie — so they can be
#: shown. Anything else is withheld, whatever the outcome.
APPLICANT_SAFE_REASON_CODES = frozenset({
    "LIVENESS_REQUIRED", "LIVENESS_FAILED", "DOCUMENTS_REQUIRED", "FACE_IMAGE_UNREADABLE",
    "NO_FACE_ON_DOCUMENT", "DOCUMENT_UNUSABLE", "DOCUMENT_NOT_FOUND", "DOCUMENT_TOO_LARGE",
    "UNSUPPORTED_FORMAT", "NO_DOCUMENT_ON_CASE", "DOCUMENT_UNREADABLE",
})

#: Outcome -> what the applicant is told. A decision's rule, tier and reason stay internal.
APPLICANT_OUTCOMES: Mapping[str, str] = {
    "PROVISIONAL_APPROVE_RESTRICTED": "OPEN_WITH_LIMITS",
    "FULL_APPROVE_STANDARD": "OPEN",
    "FULL_APPROVE_ENHANCED_MONITORING": "OPEN",
    "STEP_UP": "MORE_INFORMATION_NEEDED",
    "MANUAL_REVIEW": "IN_REVIEW",
    "REJECT": "DECLINED",
}


# ------------------------------------------------------------------- what the model wrote

_FENCE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.DOTALL)
#: An object that starts a line: the whole reply, or the reply after a line of preamble. A brace
#: inside a sentence is part of the sentence.
_OBJECT_START = re.compile(r"(?m)^[ \t]*\{")


def message_text(reply: Any) -> str:
    """The message in the Notify agent's reply, which is not always the reply itself.

    Its prompt asks for fields (`subject`, `body`, `limitsExplained`), so the reply is often a JSON
    object, sometimes fenced, sometimes after a line of preamble. The customer's message is the
    `body`: the subject is a heading this inbox does not show, and `limitsExplained` is the model's
    working, not copy. A reply that looks like structure but does not parse gives no message, so
    the guard falls back to fixed wording rather than show a customer half a JSON object.
    """
    text = str(reply or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    start = _OBJECT_START.search(text)
    if not start:
        return text
    # To the last closing brace, so a line of sign-off after the object is left out. None at all
    # is a reply cut off mid-object, which parses as nothing.
    candidate = text[start.end() - 1:]
    try:
        data = json.loads(candidate[:candidate.rfind("}") + 1])
    except ValueError:  # json.JSONDecodeError is one
        return ""
    for field in ("body", "message", "text"):
        if isinstance(data.get(field), str):
            return data[field]
    return ""


# ------------------------------------------------------------- what the account can bear

#: A figure the model wrote: "$2,000", "USD 2000", "2,000 dollars", "2,000". The account page
#: states the platform's own amounts; one a model wrote cannot be checked here, and a wrong amount
#: told to a customer is worse than none.
_AMOUNT = re.compile(
    r"[$€£]\s?\d|\b(?:usd|eur|gbp)\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|dollars?|euros?)\b"
    r"|\b\d{1,3}(?:,\d{3})+\b",
    re.IGNORECASE)
#: A list of limits. Whether it names every limit cannot be checked here, and "here are your
#: limits" with half of them left out tells the customer something false.
_LIST = re.compile(r"(?m)^\s*(?:[-*\u2022]|\d+[.)])\s+\S")
#: Something said to be unavailable: a limit in force, whatever the feature.
_NOT_AVAILABLE = re.compile(
    r"\b(?:not|no\s+longer|isn[’']t|aren[’']t)\s+(?:yet\s+)?available\b|\bunavailable\b",
    re.IGNORECASE)
#: A limit said to be gone. "No longer blocked" is not "blocked".
_LIMIT_LIFTED = re.compile(
    r"\b(?:not|no\s+longer|isn[’']t|aren[’']t)\s+"
    r"(?:limited|capped|blocked|restricted|paused|on\s+hold)\b",
    re.IGNORECASE)
#: A limit in force.
_LIMIT_WORD = re.compile(
    r"\b(?:limited|limit\s+of|ceiling|capped|blocked|restricted|paused|on\s+hold|"
    r"still\s+appl(?:y|ies)|cannot|can[’']t)\b",
    re.IGNORECASE)
#: The whole account said to be unlimited.
_UNLIMITED = re.compile(
    r"\b(?:fully\s+open|no\s+(?:current\s+|more\s+)?(?:limits?|restrictions?)|"
    r"(?:limits?|restrictions?)\s+(?:no\s+longer\s+appl|ha(?:ve|s)\s+been\s+(?:lifted|removed)))",
    re.IGNORECASE)
#: Something the customer can do, said of an account on hold.
_CAN_DO = re.compile(r"\b(?:you\s+can(?![’']t)|open\s+for|available)\b", re.IGNORECASE)

#: Why a message was replaced, for the reviewer.
TIPPING_OFF = "TIPPING_OFF"
UNVERIFIABLE_DETAIL = "UNVERIFIABLE_DETAIL"
CONTRADICTS_ACCOUNT = "CONTRADICTS_ACCOUNT"
NO_MESSAGE = "NO_MESSAGE"


def tips_off(text: Any) -> bool:
    """The text would tell an applicant why: the tipping-off check on its own.

    For wording the platform writes and shows verbatim (restriction descriptions, refusal
    reasons). Their figures are the platform's own, so only `check` holds a message to accuracy.
    """
    return bool(_FORBIDDEN.search(str(text or "")))


def _contradiction(text: str, account_state: str) -> bool:
    """The message says something about the account that its state rules out."""
    if account_state == "OPEN_STANDARD":
        return bool(_NOT_AVAILABLE.search(text)
                    or _LIMIT_WORD.search(_LIMIT_LIFTED.sub(" ", text)))
    if account_state == "FROZEN":
        return bool(_UNLIMITED.search(text) or _CAN_DO.search(_NOT_AVAILABLE.sub(" ", text)))
    # Restricted, enhanced, or a state the record did not name: still limited.
    return bool(_UNLIMITED.search(text))


def check(text: Any, *, account_state: str | None) -> tuple[str, str | None]:
    """The text an applicant may see, and why it was replaced (None when it was not).

    Two lines of defence, both deterministic. The tipping-off check: nothing that says why. The
    accuracy check: nothing the bank cannot vouch for from the account's state alone, which means
    no claim the state contradicts (a restricted account called fully open, an open one called
    limited), no figures and no lists of limits. Either way the replacement is the fixed wording
    for the state, and the account page shows the platform's own list of what is limited.
    """
    state = str(account_state or "")
    template = TEMPLATES.get(state, _DEFAULT_TEMPLATE)
    cleaned = _CONTROL.sub("", message_text(text).replace("\r\n", "\n").replace("\r", "\n"))
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)[:MAX_LENGTH].strip()
    if not cleaned:
        return template, NO_MESSAGE
    if _FORBIDDEN.search(cleaned):
        return template, TIPPING_OFF
    if cleaned == template:
        return cleaned, None
    # A false claim first: it is the stronger finding for the reviewer who reads the reason.
    if _contradiction(cleaned, state):
        return template, CONTRADICTS_ACCOUNT
    if _AMOUNT.search(cleaned) or _LIST.search(cleaned):
        return template, UNVERIFIABLE_DETAIL
    return cleaned, None


def guard(text: Any, *, account_state: str | None) -> tuple[str, bool]:
    """The text an applicant may see, and whether it had to be replaced."""
    shown, reason = check(text, account_state=account_state)
    return shown, reason is not None


def project(notifications: Iterable[Any]) -> list[dict[str, Any]]:
    """Every stored message, guarded, oldest first. Nothing else from the record is passed on."""
    out = []
    for note in notifications or ():
        if not isinstance(note, Mapping):
            continue
        text, _ = guard(note.get("text"), account_state=note.get("accountState"))
        out.append({"kind": str(note.get("kind") or ""), "text": text,
                    "at": str(note.get("at") or "")})
    return out


def reviewer_view(notifications: Iterable[Any]) -> list[dict[str, Any]]:
    """For the console: what the model wrote, what the applicant saw, and why the guard stepped
    in if it did. A reviewer seeing the guard fire is how a prompt regression gets noticed."""
    out = []
    for note in notifications or ():
        if not isinstance(note, Mapping):
            continue
        shown, reason = check(note.get("text"), account_state=note.get("accountState"))
        written = message_text(note.get("text")) or str(note.get("text") or "")
        out.append({"kind": str(note.get("kind") or ""), "written": written,
                    "shown": shown, "replacedByGuard": reason is not None,
                    "replacedBecause": reason,
                    "accountState": note.get("accountState"), "at": str(note.get("at") or "")})
    return out


def applicant_outcome(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """The applicant-facing summary of a phase decision record."""
    if not isinstance(record, Mapping):
        return {}
    outcome = str(record.get("outcome") or "")
    summary: dict[str, Any] = {"status": APPLICANT_OUTCOMES.get(outcome, "IN_PROGRESS")}
    if outcome == "STEP_UP":
        reason = str(record.get("reasonCode") or "")
        if reason in APPLICANT_SAFE_REASON_CODES:
            summary["reasonCode"] = reason
        # Written by the rules, not a model, and checked anyway: what to fix is shown only if
        # saying it does not also say why.
        item = _CONTROL.sub("", str(record.get("requiredItem") or "")).strip()[:300]
        if item and not _FORBIDDEN.search(item):
            summary["requiredItem"] = item
    return summary
