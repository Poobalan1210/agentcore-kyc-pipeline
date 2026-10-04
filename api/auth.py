"""Who is calling, from the Cognito claims the HTTP API's JWT authorizer verified.

One module so every Lambda reads identity the same way. Three rules:

* **Identity comes from the verified claims, never from the request.** A body or path value can
  name an applicant; only the claims can say who the caller is.
* **Group membership is an exact match.** The HTTP API renders an array claim as the string
  `"[reviewers applicants]"`, and the old check was `"reviewers" in str(groups)` — true for a
  group named `notreviewers` or `reviewers-test`, which anyone able to create a group could
  arrange. The claim is parsed into names and compared whole.
* **An applicant owns the case ids under their own `sub`.** `applicants.applicant_id_from` builds
  them as `APP-{sub}` or `APP-{sub}-{applicationId}`. A Cognito `sub` is a fixed-length UUID, so a
  prefix check including the separator cannot match another user's id.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

REVIEWERS = "reviewers"
PRESENTERS = "presenters"

_GROUP_SEPARATORS = re.compile(r"[\s,]+")


def claims(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """The verified claims: HTTP API (`authorizer.jwt.claims`) or REST API (`authorizer.claims`)."""
    context = event.get("requestContext")
    if not isinstance(context, Mapping):
        return {}
    authorizer = context.get("authorizer")
    if not isinstance(authorizer, Mapping):
        return {}
    jwt = authorizer.get("jwt")
    if isinstance(jwt, Mapping) and isinstance(jwt.get("claims"), Mapping):
        return jwt["claims"]
    direct = authorizer.get("claims")
    return direct if isinstance(direct, Mapping) else {}


def caller_sub(event: Mapping[str, Any]) -> str:
    return str(claims(event).get("sub") or "").strip()


def groups(event: Mapping[str, Any]) -> frozenset[str]:
    """Every group the caller is in, parsed whichever way the claim was rendered."""
    raw = claims(event).get("cognito:groups")
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        names = (str(g) for g in raw)
    else:
        names = _GROUP_SEPARATORS.split(str(raw).strip().strip("[]"))
    return frozenset(n.strip().strip("\"'") for n in names if n.strip().strip("\"'"))


def is_member(event: Mapping[str, Any], group: str) -> bool:
    return group in groups(event)


def owns(event: Mapping[str, Any], applicant_id: str) -> bool:
    """Whether this applicant id was derived from the caller's own `sub`."""
    sub = caller_sub(event)
    if not sub or not applicant_id:
        return False
    own = f"APP-{sub}"
    return applicant_id == own or applicant_id.startswith(own + "-")


def may_read(event: Mapping[str, Any], applicant_id: str) -> bool:
    """The applicant themself, a reviewer, or a presenter running the demo."""
    return owns(event, applicant_id) or bool(groups(event) & {REVIEWERS, PRESENTERS})
