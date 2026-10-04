"""What each demo persona must do on stage, phase by phase.

The personas are `demo-assets/gen_documents.py`'s, so the evals run the same identities the
documents print. The expectations are the run-of-show (`docs/RUNBOOK.md`): if one of these fails,
the act it belongs to would fail on stage in the same way.
"""

from __future__ import annotations

from dataclasses import dataclass

from eval_offline import REPO  # noqa: F401  (puts demo-assets on the path)
from gen_documents import DEMO_IDENTITIES, DemoIdentity


@dataclass(frozen=True)
class Expected:
    """Outcomes per phase. `review` is what the reviewer does when Phase 1 refers the case."""

    phase1: str
    phase2: str | None = None
    #: The re-assessment after Act 3's list update. None: the persona has no open account then.
    act3: str | None = None
    review: str | None = None
    #: The screening alert ids the reviewer's approval clears.
    cleared: tuple[str, ...] = ()


EXPECTED: dict[str, Expected] = {
    # Act 1: the clean path.
    "DEMO-US-01": Expected(phase1="PROVISIONAL_APPROVE_RESTRICTED",
                           phase2="FULL_APPROVE_STANDARD", act3="RISK_UNCHANGED"),
    # Act 2: a POSSIBLE match goes to a person; approving clears it, so neither Phase 2 nor the
    # list update raises it again.
    "DEMO-US-02": Expected(phase1="MANUAL_REVIEW", review="APPROVE", cleared=("SYN-0106",),
                           phase2="FULL_APPROVE_STANDARD", act3="RISK_UNCHANGED"),
    # Act 3: clean at onboarding, then the list update names her.
    "DEMO-US-03": Expected(phase1="PROVISIONAL_APPROVE_RESTRICTED",
                           phase2="FULL_APPROVE_STANDARD", act3="FREEZE"),
    # Act 2: the namesake is excluded on date of birth and approved without a person.
    "DEMO-US-04": Expected(phase1="PROVISIONAL_APPROVE_RESTRICTED",
                           phase2="FULL_APPROVE_STANDARD", act3="RISK_UNCHANGED"),
    # Outside the US every case goes to a reviewer for the jurisdiction alone.
    "DEMO-UK-01": Expected(phase1="JURISDICTION_NOT_DECIDED"),
    "DEMO-AU-01": Expected(phase1="JURISDICTION_NOT_DECIDED"),
    "DEMO-JP-01": Expected(phase1="JURISDICTION_NOT_DECIDED"),
}

PERSONAS: dict[str, DemoIdentity] = {i.persona_id: i for i in DEMO_IDENTITIES}

#: The personas whose screening the rules decide automatically — the US ones.
DECIDED = tuple(pid for pid, exp in EXPECTED.items() if exp.phase1 != "JURISDICTION_NOT_DECIDED")
