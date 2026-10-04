"""Synthetic screening datasets — task 2.4.

Entirely fabricated. D-03 chose labelled synthetic data over a licensed provider for two
reasons: licensing, and the fact that a live external call during a conference demo is a
failure waiting for an audience. R26.5 requires the synthetic label to be visible on screen
whenever this data is shown.

The dataset is shaped by B-08: **90-95% of real sanctions hits are not true matches.** A
dataset containing only true positives would make the pipeline look good and would never
exercise the review queue, the component most likely to break under real volume. So the false
positives here are deliberate and they outnumber the true ones.

Every record is declared with keyword arguments and every dataclass is `kw_only`. That is not
style. The first version of this file used positional arguments and one record silently landed
`"LOW"` in `subject_country`, `False` in `severity` and the note in `is_same_person` — a
mis-ordered call that type checking cannot catch because the fields are all strings. It would
have corrupted the evaluator's ground truth while every test still passed. `kw_only` makes that
class of defect unrepresentable.

Names are invented. Any resemblance to a real person is unintended, and none of these entries
corresponds to an actual designated individual.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

#: Bumped when the base list changed shape: the decoys left the list (they are applicants now, see
#: `NAMESAKE_APPLICANTS`) and SYN-0006 and SYN-0106 were added. Screening results carry this
#: version, so a result produced against the old list can never be mistaken for one produced
#: against this one.
LIST_VERSION = "SYNTHETIC-2026-10-01"
LIST_SOURCE = "synthetic-consolidated-list (NOT a real sanctions source)"


@dataclass(frozen=True, kw_only=True)
class Entry:
    entry_id: str
    name: str
    dob: str | None
    country: str
    programme: str
    #: Alternate spellings the real lists carry as a.k.a. values.
    aliases: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


# --------------------------------------------------------------------- exact-match seeds
# R25.5 requires the pre-screen to find all five exact matches with zero misses. Phase 1 now runs
# the full deterministic matcher (exact and variant), so these are no longer the only entries it
# can match; they remain the ones it must never miss.
EXACT_MATCH_SEEDS: tuple[Entry, ...] = (
    Entry(entry_id="SYN-0001", name="Dmitri Volkov", dob="1968-04-12", country="RU",
          programme="SYN-RUSSIA", note="exact-match seed 1 of 5"),
    Entry(entry_id="SYN-0002", name="Farida Nasser", dob="1975-11-03", country="SY",
          programme="SYN-SYRIA", note="exact-match seed 2 of 5"),
    Entry(entry_id="SYN-0003", name="Kwame Asante-Boateng", dob="1982-06-25", country="GH",
          programme="SYN-CORRUPTION", note="exact-match seed 3 of 5"),
    Entry(entry_id="SYN-0004", name="Ivan Petrovich Sokolov", dob="1959-01-30", country="BY",
          programme="SYN-BELARUS", note="exact-match seed 4 of 5"),
    Entry(entry_id="SYN-0005", name="Mei Ling Chen", dob="1990-09-17", country="KP",
          programme="SYN-DPRK", note="exact-match seed 5 of 5"),
)

# --------------------------------------------------------------------- fuzzy / variant seeds
# R27.1-R27.4: transliteration, name order, diacritics, married/maiden.
FUZZY_SEEDS: tuple[Entry, ...] = (
    Entry(entry_id="SYN-0101", name="Muhammad Al Rashid", dob="1979-03-11", country="SY",
          programme="SYN-SYRIA",
          aliases=("Mohammed Al-Rashid", "Mohamed Alrashid", "Muhammed Al Rasheed"),
          note="R27.1 transliteration: Mohammed / Muhammad / Mohamed"),
    Entry(entry_id="SYN-0102", name="José María Fernández", dob="1971-07-08", country="VE",
          programme="SYN-VENEZUELA",
          aliases=("Jose Maria Fernandez", "Jose Maria Fernandes"),
          note="R27.3 diacritics: José / Jose"),
    Entry(entry_id="SYN-0103", name="Zhang Wei", dob="1985-02-14", country="CN",
          programme="SYN-PROLIFERATION", aliases=("Wei Zhang",),
          note="R27.2 surname-first name order"),
    Entry(entry_id="SYN-0104", name="Katarzyna Nowak-Kowalski", dob="1988-12-01",
          country="PL", programme="SYN-CORRUPTION",
          aliases=("Katarzyna Nowak", "Katarzyna Kowalski"),
          note="R27.4 married and maiden names both supplied"),
    Entry(entry_id="SYN-0105", name="Abdul Rahman bin Yusuf", dob="1963-05-19", country="MY",
          programme="SYN-TERRORISM",
          aliases=("Abdulrahman Yusuf", "Abd al-Rahman Yusof"),
          note="R27.1 patronymic and particle variants"),
)

# --------------------------------------------------------------------- collision seeds
# Listings that exist to be collided with. Neither is a decoy: both are ordinary designations. What
# makes them interesting is who they collide with — see `NAMESAKE_APPLICANTS` below.
COLLISION_SEEDS: tuple[Entry, ...] = (
    Entry(entry_id="SYN-0006", name="David Smith", dob="1958-03-19", country="GB",
          programme="SYN-CORRUPTION",
          note="an extremely common name on the list, so most applicants who share it are "
               "someone else; only the date of birth tells them apart"),
    # R27.5. Real listings often lack a date of birth. With none, an overlapping name can be neither
    # confirmed nor excluded, and the only defensible answer is a human. Presenter B's demo persona,
    # Avery Quinn Hale, collides with this listing by design.
    Entry(entry_id="SYN-0106", name="Avery Hale", dob=None, country="US",
          programme="SYN-CYBER", aliases=("A. Q. Hale",),
          note="no date of birth on the listing: a name overlap cannot be corroborated or "
               "excluded, so it routes to review"),
)

#: The list the screening tool actually consults. Every entry is a designation. An earlier version
#: also carried "decoys" here — entries flagged `is_true_designation=False` — but no screening list
#: contains entries that are secretly not designations, and the matcher (rightly) never read the
#: flag, so a decoy could CONFIRM. The collisions they were meant to exercise are now modelled from
#: the correct side: as innocent applicants, below.
ALL_ENTRIES: tuple[Entry, ...] = EXACT_MATCH_SEEDS + FUZZY_SEEDS + COLLISION_SEEDS


# --------------------------------------------------------------------- namesake applicants
@dataclass(frozen=True, kw_only=True)
class NamesakeApplicant:
    """An innocent applicant whose name resembles a designation. B-08: 90-95% of real screening
    alerts are this person. Every one must be cleared by the matcher without a human, either
    because the names do not overlap enough to alert or because both dates of birth are known and
    differ, which is an exclusion rather than a weak match."""

    fixture_id: str
    name: str
    dob: str
    country: str
    #: The listing the name resembles, or None when it resembles nothing on the list.
    resembles: str | None
    #: True when the clearance should be an explicit, reported exclusion rather than silence.
    expect_exclusion: bool
    note: str = ""


NAMESAKE_APPLICANTS: tuple[NamesakeApplicant, ...] = (
    NamesakeApplicant(fixture_id="NS-0201", name="David Smith", dob="1980-01-01", country="GB",
                      resembles="SYN-0006", expect_exclusion=True,
                      note="same common name as the listing, 22 years apart"),
    NamesakeApplicant(fixture_id="NS-0202", name="Maria Garcia", dob="1977-04-04", country="ES",
                      resembles=None, expect_exclusion=False,
                      note="common name, nothing on the list"),
    NamesakeApplicant(fixture_id="NS-0203", name="Wei Zhang", dob="1992-06-30", country="CN",
                      resembles="SYN-0103", expect_exclusion=True,
                      note="the listed name in the other order is the same name; only the date "
                           "of birth separates them"),
    NamesakeApplicant(fixture_id="NS-0204", name="Mohammed Ali", dob="1979-03-11", country="EG",
                      resembles="SYN-0101", expect_exclusion=False,
                      note="shares a first name and the date of birth with SYN-0101, but not "
                           "enough of the name to alert"),
    NamesakeApplicant(fixture_id="NS-0205", name="Ivan Sokolov", dob="1991-08-22", country="RU",
                      resembles="SYN-0004", expect_exclusion=True,
                      note="name is a subset of SYN-0004's, dates of birth differ"),
    NamesakeApplicant(fixture_id="NS-0206", name="J. M. Fernandez", dob="1983-04-09", country="MX",
                      resembles="SYN-0102", expect_exclusion=False,
                      note="initials do not overlap the listed given names"),
    NamesakeApplicant(fixture_id="NS-0207", name="Dmitri Volkov", dob="1994-10-05", country="UA",
                      resembles="SYN-0001", expect_exclusion=True,
                      note="exact name match to SYN-0001 with a 26-year gap in dates of birth; the "
                           "Act 2 namesake"),
)


# --------------------------------------------------------------------- PEP data
@dataclass(frozen=True, kw_only=True)
class PepEntry:
    entry_id: str
    name: str
    dob: str | None
    country: str
    position: str
    #: SELF for the PEP, or the relationship for an associate (R27.6).
    relationship: str = "SELF"
    related_to: str | None = None


PEP_ENTRIES: tuple[PepEntry, ...] = (
    PepEntry(entry_id="PEP-0001", name="Olusegun Adeyemi", dob="1962-03-08", country="NG",
             position="Synthetic Minister of Finance"),
    PepEntry(entry_id="PEP-0002", name="Adaeze Adeyemi", dob="1990-07-22", country="NG",
             position="Synthetic Minister of Finance",
             relationship="SPOUSE", related_to="PEP-0001"),
    PepEntry(entry_id="PEP-0003", name="Chidi Adeyemi", dob="2001-01-15", country="NG",
             position="Synthetic Minister of Finance",
             relationship="CHILD", related_to="PEP-0001"),
    PepEntry(entry_id="PEP-0004", name="Bernard Okafor", dob="1970-11-30", country="NG",
             position="Synthetic Minister of Finance",
             relationship="BUSINESS_PARTNER", related_to="PEP-0001"),
    PepEntry(entry_id="PEP-0005", name="Elena Marchetti", dob="1968-09-12", country="IT",
             position="Synthetic Regional Governor"),
)


# --------------------------------------------------------------------- adverse media
@dataclass(frozen=True, kw_only=True)
class MediaArticle:
    article_id: str
    headline: str
    subject_name: str | None
    #: Extra attributes a disambiguator can use. R27.7 forbids treating a name collision as a
    #: match, so an article with no corroborating attribute must not confirm anything.
    subject_dob: str | None
    subject_country: str | None
    severity: str          # LOW | MEDIUM | HIGH
    is_same_person: bool   # ground truth for the evaluators
    note: str = ""

    def __post_init__(self) -> None:
        # The ground truth these records provide is what the evaluators score against, so a
        # malformed record must fail loudly at import rather than quietly skew a result.
        if self.severity not in ("LOW", "MEDIUM", "HIGH"):
            raise ValueError("%s: severity must be LOW/MEDIUM/HIGH, got %r"
                             % (self.article_id, self.severity))
        if not isinstance(self.is_same_person, bool):
            raise TypeError("%s: is_same_person must be bool, got %r"
                            % (self.article_id, self.is_same_person))


MEDIA_ARTICLES: tuple[MediaArticle, ...] = (
    MediaArticle(article_id="MED-0001",
                 headline="Businessman charged in synthetic procurement inquiry",
                 subject_name="Kwame Asante-Boateng", subject_dob="1982-06-25",
                 subject_country="GH", severity="HIGH", is_same_person=True),
    MediaArticle(article_id="MED-0002",
                 headline="Local footballer of the same name signs contract",
                 subject_name="Kwame Asante-Boateng", subject_dob="1999-02-02",
                 subject_country="GH", severity="LOW", is_same_person=False,
                 note="R27.7: same name, different DOB — a collision, not a match"),
    MediaArticle(article_id="MED-0003",
                 headline="Fraud investigation names synthetic shipping firm",
                 subject_name="Dmitri Volkov", subject_dob="1968-04-12",
                 subject_country="RU", severity="HIGH", is_same_person=True),
    MediaArticle(article_id="MED-0004",
                 headline="Novelist Dmitri Volkov wins synthetic literary prize",
                 subject_name="Dmitri Volkov", subject_dob=None, subject_country=None,
                 severity="LOW", is_same_person=False,
                 note="R27.7: name matches but the article carries no corroborating "
                      "attribute — must not be attributed"),
    MediaArticle(article_id="MED-0005",
                 headline="Regulator fines synthetic bank over controls",
                 subject_name="Elena Marchetti", subject_dob="1968-09-12",
                 subject_country="IT", severity="MEDIUM", is_same_person=True),
)


# --------------------------------------------------------------------- normalisation
def strip_diacritics(value: str) -> str:
    """José -> Jose (R27.3)."""
    return "".join(c for c in unicodedata.normalize("NFKD", value)
                   if not unicodedata.combining(c))


def normalise(value: str) -> str:
    """Lower, de-diacritic, collapse punctuation and whitespace.

    Deliberately does NOT reorder tokens. Name order is handled explicitly by the matcher so
    that a reordered match can be reported as a weaker signal than an in-order one.
    """
    v = strip_diacritics(value).lower()
    for ch in "-_.,'`’":
        v = v.replace(ch, " ")
    return " ".join(v.split())


def tokens(value: str) -> frozenset[str]:
    #: Particles carry no identifying weight and differ by transliteration convention.
    particles = {"bin", "binti", "bint", "al", "el", "ibn", "abu", "van", "von",
                 "de", "da", "del", "della", "di", "du", "la", "le", "of"}
    return frozenset(t for t in normalise(value).split() if t not in particles)
