"""Name matching and hit classification.

This is a pure function and it is deliberately not a model. R27.5 requires every hit to be
classified CONFIRMED / PROBABLE / POSSIBLE, and R27.7 forbids treating a name collision as a
match. Both are properties that must hold identically on every run, so they belong in code.

The two classification rules that carry the most weight:

    **A name match alone is never CONFIRMED.** Confirmation requires a matching date of birth.
    Country corroborates a PROBABLE; it never confirms.

    **Two known dates of birth that differ are an exclusion, not a weak match.** This is what
    separates SYN-0001 "Dmitri Volkov, 1968-04-12, RU" from namesake applicant NS-0207 "Dmitri
    Volkov, 1994-10-05, UA": identical names, 26 years apart. Reporting that as a POSSIBLE would
    send an innocent person to a human queue for a match no human could uphold.

Everything else — an overlapping name where either record has no date of birth — is POSSIBLE or
PROBABLE, and both route to human review (R27.5). That is the correct behaviour when 90-95% of
alerts are not true matches (B-08), and it is why the review queue is sized as the busiest
surface in the system.

Exclusions are returned, not dropped. A reviewer seeing "we looked at this and ruled it out, here
is why" is being given evidence; silence is not.
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

    @property
    def excluded(self) -> bool:
        return self.classification is HitClassification.NO_MATCH

    def to_dict(self) -> dict[str, object]:
        return {
            "entryId": self.entry_id,
            "matchedName": self.matched_name,
            "classification": self.classification.value,
            "score": self.score,
            "programme": self.programme,
            "reason": self.reason,
            "dobMatch": self.dob_match,
            "countryMatch": self.country_match,
            "nameExact": self.name_exact,
            "nameReordered": self.name_reordered,
        }


def _name_variants(c: Candidate) -> list[str]:
    return [c.name, *c.alternate_names]


def _entry_names(e: Entry) -> list[str]:
    return [e.name, *e.aliases]


def _dob_conflict(candidate: Candidate, entry: Entry) -> bool:
    """Both dates of birth are known and they differ."""
    return bool(candidate.dob and entry.dob and candidate.dob != entry.dob)


def exact_match(candidate: Candidate, entries: tuple[Entry, ...]) -> list[Hit]:
    """Exact, normalised name equality (R11.2), classified by the corroborating attributes.

    Diacritics and punctuation are normalised away because "Jose" and "José" are the same string
    to a human and differing on them would be a bug, not strictness.
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
            elif _dob_conflict(candidate, e):
                cls, score = HitClassification.NO_MATCH, 0.0
                reason = ("exact name match but the dates of birth differ (%s vs %s), which "
                          "excludes the match: a different person with the same name"
                          % (candidate.dob, e.dob))
            elif country_match:
                cls, score = HitClassification.PROBABLE, 0.7
                reason = ("exact name match with matching country; no date of birth on one "
                          "record, so it cannot be confirmed or excluded")
            else:
                cls, score = HitClassification.POSSIBLE, 0.5
                reason = ("exact name match with no corroborating attribute; a name alone "
                          "cannot confirm identity")
            hits.append(Hit(e.entry_id, entry_name, cls, score, reason, e.programme,
                            dob_match, country_match, True, False))
            break
    return hits


def fuzzy_match(candidate: Candidate, entries: tuple[Entry, ...],
                min_token_overlap: float = 0.6) -> list[Hit]:
    """Variant screening. Transliteration, diacritics, name order, married/maiden.

    Token-set overlap rather than edit distance, because the real variation in these names is
    whole-token substitution (Mohammed/Muhammad) and reordering (Zhang Wei/Wei Zhang), not
    character typos. Edit distance handles typos well and both of those badly.

    Subsumes `exact_match`: an exact name has overlap 1.0 and reaches the same classification.
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

        # Classification ladder. No branch reaches CONFIRMED without a DOB match, and a DOB
        # conflict is checked before country so that a shared country can never outweigh it.
        if dob_match and (exact or reordered or overlap >= 0.99):
            cls = HitClassification.CONFIRMED
            reason = ("name variant matched with identical date of birth; transliteration or "
                      "ordering differences do not weaken a two-attribute agreement")
        elif dob_match:
            cls = HitClassification.PROBABLE
            reason = ("date of birth matches and names overlap %.0f%%, but the names are not "
                      "equivalent" % (overlap * 100))
        elif _dob_conflict(candidate, e):
            cls = HitClassification.NO_MATCH
            reason = ("names overlap %.0f%% but the dates of birth differ (%s vs %s), which "
                      "excludes the match" % (overlap * 100, candidate.dob, e.dob))
        elif country_match and overlap >= 0.8:
            cls = HitClassification.PROBABLE
            reason = ("strong name overlap and matching country; no date of birth on one "
                      "record, so it cannot be confirmed or excluded")
        else:
            cls = HitClassification.POSSIBLE
            reason = ("names overlap %.0f%% but at least one record has no date of birth, so "
                      "the match can be neither corroborated nor excluded" % (overlap * 100))

        score = 0.0 if cls is HitClassification.NO_MATCH else round(
            min(1.0, overlap * (1.0 if dob_match else 0.7)), 3)
        hits.append(Hit(e.entry_id, matched_name, cls, score, reason, e.programme,
                        dob_match, country_match, exact, reordered))
    return hits


def screen(candidate: Candidate, entries: tuple[Entry, ...]) -> tuple[list[Hit], list[Hit]]:
    """The one screening entry point: `(alerts, exclusions)`, each sorted by entry id.

    Sorted so that the same identity against the same list always produces byte-identical output,
    which is what lets a recorded screening fact be compared across runs (R3.5).
    """
    results = fuzzy_match(candidate, entries)
    alerts = sorted((h for h in results if not h.excluded), key=lambda h: h.entry_id)
    exclusions = sorted((h for h in results if h.excluded), key=lambda h: h.entry_id)
    return alerts, exclusions


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


def names_overlap(a: str, b: str, threshold: float = 0.6) -> bool:
    """Whether two names overlap enough to be worth comparing at all."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / max(len(ta), len(tb)) >= threshold


def worst(hits: list[Hit]) -> HitClassification:
    """The strongest classification present. Used to decide routing, never to average."""
    order = [HitClassification.CONFIRMED, HitClassification.PROBABLE,
             HitClassification.POSSIBLE, HitClassification.NO_MATCH]
    for c in order:
        if any(h.classification is c for h in hits):
            return c
    return HitClassification.NO_MATCH
