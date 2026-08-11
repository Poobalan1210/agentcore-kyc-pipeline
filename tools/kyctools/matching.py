"""Name matching and hit classification.

This is a pure function and it is deliberately not a model. R27.5 requires every hit to be
classified CONFIRMED / PROBABLE / POSSIBLE, and R27.7 forbids treating a name collision as a
match. Both are properties that must hold identically on every run, so they belong in code.

The classification rule that carries the most weight:

    **A name match alone is never CONFIRMED.**

Confirmation requires a corroborating attribute — date of birth or country. This is what
separates SYN-0001 "Dmitri Volkov, 1968-04-12, RU" from decoy SYN-0207 "Dmitri Volkov,
1994-10-05, UA": identical names, 26 years apart. A matcher that confirms on name alone would
reject a real applicant and would be indefensible in front of a regulator.

The consequence is intentional: this matcher produces a lot of PROBABLE and POSSIBLE, and both
route to human review (R27.5). That is the correct behaviour when 90-95% of hits are not true
matches (B-08), and it is why the review queue is sized as the busiest surface in the system.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract import HitClassification
from .data.sanctions import Entry, normalise, tokens


@dataclass(frozen=True)
class Candidate:
    """An applicant's screenable identity attributes."""

    name: str
    dob: str | None = None
    country: str | None = None
    #: R27.4: both names supplied where known.
    alternate_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hit:
    entry_id: str
    matched_name: str
    classification: HitClassification
    #: 0..1. Reported for transparency; the classification is what the pipeline branches on.
    score: float
    #: Why this classification was reached, in words a reviewer can act on (R18.1).
    reason: str
    programme: str
    dob_match: bool
    country_match: bool
    name_exact: bool
    name_reordered: bool


def _name_variants(c: Candidate) -> list[str]:
    return [c.name, *c.alternate_names]


def _entry_names(e: Entry) -> list[str]:
    return [e.name, *e.aliases]


def exact_match(candidate: Candidate, entries: tuple[Entry, ...]) -> list[Hit]:
    """Phase 1 pre-screen. Exact, normalised name equality only (R11.2).

    Intentionally narrow. Phase 1 must be fast and must fail closed, so it looks for the
    unambiguous case and leaves everything else to Phase 2. Diacritics and punctuation are
    normalised away because "Jose" and "José" are the same string to a human and differing on
    them would be a bug, not strictness.
    """
    hits: list[Hit] = []
    cand_norms = {normalise(n) for n in _name_variants(candidate)}
    for e in entries:
        for entry_name in _entry_names(e):
            if normalise(entry_name) not in cand_norms:
                continue
            dob_match = bool(candidate.dob and e.dob and candidate.dob == e.dob)
            country_match = bool(candidate.country and e.country
                                 and candidate.country == e.country)
            if dob_match:
                cls, score = HitClassification.CONFIRMED, 1.0
                reason = ("exact name match with identical date of birth — two independent "
                          "attributes agree")
            elif country_match:
                cls, score = HitClassification.PROBABLE, 0.7
                reason = ("exact name match with matching country but no date of birth "
                          "corroboration")
            else:
                # The SYN-0207 case. Name alone is not enough, ever.
                cls, score = HitClassification.POSSIBLE, 0.5
                reason = ("exact name match with no corroborating attribute; a name alone "
                          "cannot confirm identity")
            hits.append(Hit(e.entry_id, entry_name, cls, score, reason, e.programme,
                            dob_match, country_match, True, False))
            break
    return hits


def fuzzy_match(candidate: Candidate, entries: tuple[Entry, ...],
                min_token_overlap: float = 0.6) -> list[Hit]:
    """Phase 2 screening. Transliteration, diacritics, name order, married/maiden.

    Token-set overlap rather than edit distance, because the real variation in these names is
    whole-token substitution (Mohammed/Muhammad) and reordering (Zhang Wei/Wei Zhang), not
    character typos. Edit distance handles typos well and both of those badly.
    """
    hits: list[Hit] = []
    for e in entries:
        best: tuple[float, str, bool, bool] | None = None
        for cand_name in _name_variants(candidate):
            ct = tokens(cand_name)
            if not ct:
                continue
            for entry_name in _entry_names(e):
                et = tokens(entry_name)
                if not et:
                    continue
                overlap = len(ct & et) / max(len(ct), len(et))
                if overlap < min_token_overlap:
                    continue
                exact = normalise(cand_name) == normalise(entry_name)
                reordered = (not exact) and ct == et
                if best is None or overlap > best[0]:
                    best = (overlap, entry_name, exact, reordered)
        if best is None:
            continue

        overlap, matched_name, exact, reordered = best
        dob_match = bool(candidate.dob and e.dob and candidate.dob == e.dob)
        country_match = bool(candidate.country and e.country
                             and candidate.country == e.country)

        # Classification ladder. Note that no branch reaches CONFIRMED without a DOB match.
        if dob_match and (exact or reordered or overlap >= 0.99):
            cls = HitClassification.CONFIRMED
            reason = ("name variant matched with identical date of birth; transliteration or "
                      "ordering differences do not weaken a two-attribute agreement")
        elif dob_match:
            cls = HitClassification.PROBABLE
            reason = ("date of birth matches and names overlap %.0f%%, but the names are not "
                      "equivalent" % (overlap * 100))
        elif country_match and overlap >= 0.8:
            cls = HitClassification.PROBABLE
            reason = ("strong name overlap and matching country, no date of birth "
                      "corroboration")
        elif e.dob is None or candidate.dob is None:
            cls = HitClassification.POSSIBLE
            reason = ("names overlap but at least one record has no date of birth, so the "
                      "match cannot be corroborated or excluded")
        else:
            # Both DOBs known and different. This is an exclusion, not a weak match.
            cls = HitClassification.NO_MATCH
            reason = ("names overlap %.0f%% but dates of birth differ (%s vs %s), which "
                      "excludes the match" % (overlap * 100, candidate.dob, e.dob))

        score = round(min(1.0, overlap * (1.0 if dob_match else 0.7)), 3)
        hits.append(Hit(e.entry_id, matched_name, cls, score, reason, e.programme,
                        dob_match, country_match, exact, reordered))
    return hits


def disambiguate_media(candidate: Candidate, subject_name: str,
                       subject_dob: str | None,
                       subject_country: str | None) -> tuple[bool, str]:
    """R27.7. Returns (is_match, reason).

    An article is only attributed to the applicant when something beyond the name agrees. With
    no corroborating attribute the honest answer is "cannot attribute", which is not the same
    as "not the applicant" — the reason string says so, because a reviewer needs to know which
    of the two it is.
    """
    if not subject_name:
        return False, "article has no named subject"
    ct = tokens(candidate.name)
    st = tokens(subject_name)
    if not ct or not st:
        return False, "insufficient name tokens to compare"
    overlap = len(ct & st) / max(len(ct), len(st))
    if overlap < 0.6:
        return False, "subject name does not match the applicant"

    if candidate.dob and subject_dob:
        if candidate.dob == subject_dob:
            return True, "name and date of birth both match"
        return False, ("name matches but dates of birth differ (%s vs %s) — a different "
                       "person of the same name" % (candidate.dob, subject_dob))
    if candidate.country and subject_country and candidate.country == subject_country:
        return True, "name and country match; no date of birth available on the article"
    return False, ("name matches but the article carries no corroborating attribute, so it "
                   "cannot be attributed to this applicant")


def worst(hits: list[Hit]) -> HitClassification:
    """The strongest classification present. Used to decide routing, never to average."""
    order = [HitClassification.CONFIRMED, HitClassification.PROBABLE,
             HitClassification.POSSIBLE, HitClassification.NO_MATCH]
    for c in order:
        if any(h.classification is c for h in hits):
            return c
    return HitClassification.NO_MATCH
