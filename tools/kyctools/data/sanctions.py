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

LIST_VERSION = "SYNTHETIC-2026-08-01"
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
    #: True designation vs a decoy that exists to generate a false positive.
    is_true_designation: bool = True
    note: str = ""


# --------------------------------------------------------------------- exact-match seeds
# R25.5 requires the pre-screen to find all five exact matches with zero misses. These are the
# only entries the Phase 1 pre-screen can match, because it does exact matching only (R11.2).
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

# --------------------------------------------------------------------- deliberate decoys
# These are the point of the dataset. Each is designed to *look* like a hit and not be one, so
# the review queue is exercised and the false-positive rate in R25.4 is measurable rather than
# theoretical. B-08: this is the common case in production, not the edge case.
FALSE_POSITIVE_DECOYS: tuple[Entry, ...] = (
    Entry(entry_id="SYN-0201", name="David Smith", dob="1980-01-01", country="GB",
          programme="SYN-CORRUPTION", is_true_designation=False,
          note="extremely common name; collides with many clean applicants"),
    Entry(entry_id="SYN-0202", name="Maria Garcia", dob="1977-04-04", country="ES",
          programme="SYN-CORRUPTION", is_true_designation=False,
          note="extremely common name"),
    Entry(entry_id="SYN-0203", name="Wei Zhang", dob="1985-02-14", country="CN",
          programme="SYN-PROLIFERATION", is_true_designation=False,
          note="same DOB as SYN-0103 with reversed order — tests that name order and "
               "identity are not conflated"),
    Entry(entry_id="SYN-0204", name="Mohammed Ali", dob="1979-03-11", country="EG",
          programme="SYN-TERRORISM", is_true_designation=False,
          note="shares SYN-0101's DOB and a partial name; the classic false positive"),
    Entry(entry_id="SYN-0205", name="Ivan Sokolov", dob="1991-08-22", country="RU",
          programme="SYN-RUSSIA", is_true_designation=False,
          note="name substring of SYN-0004, different DOB — must not confirm"),
    Entry(entry_id="SYN-0206", name="J. M. Fernandez", dob=None, country="MX",
          programme="SYN-VENEZUELA", is_true_designation=False,
          note="initials only, no DOB — cannot be disambiguated, so must never CONFIRM"),
    Entry(entry_id="SYN-0207", name="Dmitri Volkov", dob="1994-10-05", country="UA",
          programme="SYN-RUSSIA", is_true_designation=False,
          note="exact name match to SYN-0001 with a 26-year DOB gap; name alone is "
               "insufficient evidence"),
)

ALL_ENTRIES: tuple[Entry, ...] = (
    EXACT_MATCH_SEEDS + FUZZY_SEEDS + FALSE_POSITIVE_DECOYS)


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
