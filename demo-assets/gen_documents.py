"""Synthetic demo identity documents — no external dependency.

Generates a realistic-looking identity document data page per persona as a valid single-page PDF,
by hand-writing PDF object syntax directly (no Pillow/reportlab). Two reasons for that rather
than pulling in an imaging library:

1. This machine's Python 3.14 has no prebuilt Pillow wheel and no local libjpeg headers to build
   one from source — reaching for an image library here would trade a short asset script for a
   real dependency the rest of the repo doesn't need.
2. `applicants.py` never reads the document bytes at all (confirmed: nothing downstream fetches
   the uploaded object — the case record only stores `type`, `key`, `bucket`, `issuingCountry` as
   metadata). So content richness has no effect on Phase 1's outcome; only the demo audience
   needs to see something document-shaped.

The passport layout follows a real ICAO 9303 TD3 data page: header band, portrait box, paired
English/French field labels, and a genuine two-line machine-readable zone with correctly computed
check digits (`mrz_check_digit`). That fidelity is deliberate — an audience reads "real system"
from the MRZ more than from any other single detail, and a wrong check digit is exactly the sort
of thing someone in a re:Invent audience notices.

**It remains unmistakably synthetic on purpose.** Every page keeps a "SPECIMEN — NOT A REAL
TRAVEL DOCUMENT" band and a synthetic-data footer. Making these indistinguishable from genuine
travel documents would be forgery, not a better demo; making them structurally faithful *and*
clearly marked gets the demo value with none of that. Matches
`tools/kyctools/data/sanctions.py`'s conventions: every value is fabricated.

**Each persona's screening outcome is chosen, and pinned by test.** Three of the US personas meet
the synthetic list on purpose — a name overlap with a listing that has no date of birth, an exact
namesake of a listed person born 26 years later, and a customer a later list update names — and
`tests/test_gen_documents.py` runs the real matcher over every persona to hold each one to its
scenario. Any other overlap would be an unintended hit on stage, and fails the same test.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- page geometry
PAGE_W, PAGE_H = 612, 792

#: The document "card" — a bordered region, like the laminated data page of a passport.
CARD_X, CARD_Y, CARD_W, CARD_H = 46, 300, 520, 430

#: Portrait box, left side of the card as on a real TD3 data page. `PHOTO_Y` is set so the top of
#: the frame sits just under the header band (band bottom is at CARD_Y + CARD_H - 46 = 684) —
#: a data page has no dead space between the band and the portrait.
PHOTO_X, PHOTO_Y, PHOTO_W, PHOTO_H = 70, 492, 140, 176

#: Field column, right of the portrait.
FIELD_X = 236

#: Machine-readable zone: two fixed-pitch lines at the foot of the card.
MRZ_Y = 330


@dataclass(frozen=True, kw_only=True)
class DemoIdentity:
    """One synthetic applicant persona, one document.

    `surname`/`given_names` are stored separately rather than split from a single display name:
    the MRZ requires them as distinct fields, and inferring the split from a full name is
    ambiguous for exactly the names (compound surnames, patronymics) this dataset deliberately
    includes.
    """

    persona_id: str
    surname: str
    given_names: str
    dob: str                 # ISO yyyy-mm-dd
    sex: str                 # M | F | X  (ICAO 9303 permits X)
    document_type: str       # PASSPORT | DRIVERS_LICENSE | NATIONAL_ID
    document_number: str     # 9 chars, MRZ-compatible
    issuing_country: str     # ISO 3166-1 alpha-2, as printed
    mrz_country: str         # ISO 3166-1 alpha-3, as used in the MRZ
    jurisdiction: str        # us | uk | au | jp — matches IntakeStep's jurisdiction codes
    nationality: str         # printed long form
    place_of_birth: str
    authority: str
    address: str
    issued: str              # ISO yyyy-mm-dd
    expiry: str              # ISO yyyy-mm-dd
    #: What this persona shows on stage, in one line. Mirrored in `demoPersonas.ts`, behind the
    #: console's New application menu, so the presenter picks a scenario rather than a name.
    scenario: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.given_names} {self.surname}"


#: Act 3's sanctions list update, as the presenter publishes it from the console's Demo controls
#: (`frontend/apps/console/src/controls/ControlsView.tsx` `ACT3_ENTRY`, held equal by test). It
#: names DEMO-US-03 exactly — name, date of birth and country — so the re-screen of her open
#: account is CONFIRMED.
ACT3_LIST_UPDATE: dict[str, str] = {
    "entryId": "SYN-0901",
    "name": "Nadia Rose Korval",
    "dob": "1987-02-11",
    "country": "US",
}

#: The rest of Act 3's update (`ControlsView.tsx` `ACT3_OTHER_ENTRIES`, held equal by test). A
#: real list update arrives as a batch, and the point of the act is that the re-screen finds the
#: one listing that names a customer among the ones that do not. These share no name token with
#: any persona and no persona's date of birth, so they leave every screening exactly as it was;
#: the tests run them through the real matcher to keep it that way.
ACT3_OTHER_LISTINGS: tuple[dict[str, str], ...] = (
    {"entryId": "SYN-0902", "name": "Orrin Castellane", "dob": "1975-09-14", "country": "MT"},
    {"entryId": "SYN-0903", "name": "Lucian Varga", "dob": "1979-06-02", "country": "RO"},
    {"entryId": "SYN-0904", "name": "Ignatius Moreau", "dob": "1968-12-01", "country": "FR"},
)

#: The whole of Act 3's update, in the order the console publishes it.
ACT3_BATCH: tuple[dict[str, str], ...] = (ACT3_LIST_UPDATE, *ACT3_OTHER_LISTINGS)


_OUTSIDE_US = ("Outside the US: goes to a reviewer, because this build decides only US cases "
               "automatically")

# One persona per jurisdiction IntakeStep.tsx offers, plus three more in the US — the one
# jurisdiction this build decides automatically (`DECIDED_JURISDICTIONS`), so the only place a
# screening scenario can play out end to end without a reviewer stepping in for the jurisdiction.
DEMO_IDENTITIES: tuple[DemoIdentity, ...] = (
    DemoIdentity(
        persona_id="DEMO-US-01", surname="REEVES", given_names="TAYLOR JORDAN",
        dob="1991-05-14", sex="X", document_type="PASSPORT", document_number="471029305",
        issuing_country="US", mrz_country="USA", jurisdiction="us",
        nationality="UNITED STATES OF AMERICA", place_of_birth="AUSTIN, TEXAS, U.S.A.",
        authority="UNITED STATES DEPARTMENT OF STATE (SPECIMEN)",
        address="482 Kestrel Way, Austin, TX 78701, USA",
        issued="2022-05-14", expiry="2032-05-13",
        scenario="Clean path: a provisional account in under 90 seconds, limits lifted later",
    ),
    DemoIdentity(
        persona_id="DEMO-US-02", surname="HALE", given_names="AVERY QUINN",
        dob="1990-03-22", sex="F", document_type="PASSPORT", document_number="538204716",
        issuing_country="US", mrz_country="USA", jurisdiction="us",
        nationality="UNITED STATES OF AMERICA", place_of_birth="DENVER, COLORADO, U.S.A.",
        authority="UNITED STATES DEPARTMENT OF STATE (SPECIMEN)",
        address="1150 Larkspur Avenue, Denver, CO 80203, USA",
        issued="2021-03-22", expiry="2031-03-21",
        scenario=("Possible match: SYN-0106 'Avery Hale' is listed with no date of birth, so a "
                  "person decides; approving clears the alert for good"),
    ),
    DemoIdentity(
        persona_id="DEMO-US-03", surname="KORVAL", given_names="NADIA ROSE",
        dob="1987-02-11", sex="F", document_type="PASSPORT", document_number="629417308",
        issuing_country="US", mrz_country="USA", jurisdiction="us",
        nationality="UNITED STATES OF AMERICA", place_of_birth="SEATTLE, WASHINGTON, U.S.A.",
        authority="UNITED STATES DEPARTMENT OF STATE (SPECIMEN)",
        address="77 Harbor View Drive, Seattle, WA 98101, USA",
        issued="2020-02-11", expiry="2030-02-10",
        scenario=("Act 3: clean at onboarding; the list update SYN-0901 names her, and the "
                  "re-screen of her open account freezes it"),
    ),
    DemoIdentity(
        persona_id="DEMO-US-04", surname="VOLKOV", given_names="DMITRI",
        dob="1994-10-05", sex="M", document_type="PASSPORT", document_number="746039215",
        issuing_country="US", mrz_country="USA", jurisdiction="us",
        nationality="UNITED STATES OF AMERICA", place_of_birth="CHICAGO, ILLINOIS, U.S.A.",
        authority="UNITED STATES DEPARTMENT OF STATE (SPECIMEN)",
        address="2209 Birchwood Lane, Chicago, IL 60614, USA",
        issued="2024-10-05", expiry="2034-10-04",
        scenario=("Namesake: the exact name of SYN-0001, born 26 years later; excluded on date "
                  "of birth, with the reason recorded, and approved"),
    ),
    DemoIdentity(
        persona_id="DEMO-UK-01", surname="OSEI-BRIGHT", given_names="ALISTAIR",
        dob="1988-11-02", sex="M", document_type="DRIVERS_LICENSE",
        document_number="582013746", issuing_country="GB", mrz_country="GBR",
        jurisdiction="uk", nationality="UNITED KINGDOM", place_of_birth="MANCHESTER, UK",
        authority="DRIVER AND VEHICLE LICENSING AGENCY (SPECIMEN)",
        address="14 Harrow Close, Manchester, M1 4BT, United Kingdom",
        issued="2020-11-02", expiry="2030-11-01",
        scenario=_OUTSIDE_US,
    ),
    DemoIdentity(
        persona_id="DEMO-AU-01", surname="WHITLAM", given_names="MORGAN KIRI",
        dob="1995-02-27", sex="F", document_type="NATIONAL_ID",
        document_number="693124857", issuing_country="AU", mrz_country="AUS",
        jurisdiction="au", nationality="AUSTRALIA", place_of_birth="FREMANTLE, WA, AUSTRALIA",
        authority="AUSTRALIAN GOVERNMENT (SPECIMEN)",
        address="27 Banksia Street, Fremantle, WA 6160, Australia",
        issued="2019-02-27", expiry="2029-02-26",
        scenario=_OUTSIDE_US,
    ),
    DemoIdentity(
        persona_id="DEMO-JP-01", surname="ICHIKAWA", given_names="HARUTO",
        dob="1993-08-19", sex="M", document_type="PASSPORT", document_number="704235968",
        issuing_country="JP", mrz_country="JPN", jurisdiction="jp", nationality="JAPAN",
        place_of_birth="TOKYO, JAPAN",
        authority="MINISTRY OF FOREIGN AFFAIRS (SPECIMEN)",
        address="3-2-1 Marunouchi, Chiyoda-ku, Tokyo 100-0005, Japan",
        issued="2023-08-19", expiry="2033-08-18",
        scenario=_OUTSIDE_US,
    ),
)

_DOCUMENT_TITLE = {
    "PASSPORT": ("PASSPORT", "PASSEPORT / PASAPORTE"),
    "DRIVERS_LICENSE": ("DRIVING LICENCE", "PERMIS DE CONDUIRE"),
    "NATIONAL_ID": ("IDENTITY CARD", "CARTE D'IDENTITE"),
}

_DOCUMENT_CODE = {"PASSPORT": "P", "DRIVERS_LICENSE": "D", "NATIONAL_ID": "I"}

#: The specimen banner names the document kind, because "NOT A REAL TRAVEL DOCUMENT" printed on a
#: driving licence reads as boilerplate someone forgot to change — and a warning that reads as
#: boilerplate is a warning people stop seeing.
_SPECIMEN_BANNER = {
    "PASSPORT": "SPECIMEN - NOT A REAL TRAVEL DOCUMENT",
    "DRIVERS_LICENSE": "SPECIMEN - NOT A REAL DRIVING LICENCE",
    "NATIONAL_ID": "SPECIMEN - NOT A REAL IDENTITY DOCUMENT",
}


# --------------------------------------------------------------------------- MRZ (ICAO 9303)
#: Character values for the check-digit weighting: digits are themselves, A-Z are 10-35, filler
#: is 0.
def _mrz_char_value(char: str) -> int:
    if char.isdigit():
        return int(char)
    if "A" <= char <= "Z":
        return ord(char) - ord("A") + 10
    if char == "<":
        return 0
    raise ValueError(f"character {char!r} is not valid in an MRZ field")


def mrz_check_digit(field: str) -> str:
    """ICAO 9303 check digit: weights cycle 7,3,1 across the field, sum mod 10.

    Implemented rather than hardcoded because every check digit in the MRZ depends on the field
    it guards — a hardcoded digit would silently become wrong the moment a persona's document
    number or date changed, and a wrong check digit is precisely the detail that makes a document
    look fake to anyone who knows the format.
    """
    weights = (7, 3, 1)
    total = sum(_mrz_char_value(c) * weights[i % 3] for i, c in enumerate(field))
    return str(total % 10)


def _mrz_date(iso: str) -> str:
    """yyyy-mm-dd -> YYMMDD, the only date form the MRZ carries."""
    parsed = date.fromisoformat(iso)
    return f"{parsed.year % 100:02d}{parsed.month:02d}{parsed.day:02d}"


def _mrz_name_field(surname: str, given_names: str, width: int = 39) -> str:
    """`SURNAME<<GIVEN<NAMES` padded with filler. Hyphens and apostrophes become filler, spaces
    become single filler characters — per 9303's transliteration rules, which is why
    `OSEI-BRIGHT` appears as `OSEI<BRIGHT` in the MRZ but keeps its hyphen in the printed name."""
    def norm(value: str) -> str:
        out = []
        for char in value.upper():
            if char.isalpha():
                out.append(char)
            elif char in " -'":
                out.append("<")
        return "".join(out)

    field = f"{norm(surname)}<<{norm(given_names)}"
    return field[:width].ljust(width, "<")


def build_mrz(identity: DemoIdentity) -> tuple[str, str]:
    """The two 44-character TD3 lines."""
    doc_code = _DOCUMENT_CODE[identity.document_type]
    line1 = f"{doc_code}<{identity.mrz_country}{_mrz_name_field(identity.surname, identity.given_names)}"
    assert len(line1) == 44, f"MRZ line 1 is {len(line1)} chars, must be 44"

    number = identity.document_number.upper().ljust(9, "<")
    number_cd = mrz_check_digit(number)
    dob = _mrz_date(identity.dob)
    dob_cd = mrz_check_digit(dob)
    expiry = _mrz_date(identity.expiry)
    expiry_cd = mrz_check_digit(expiry)
    # Optional personal-number field: left as filler, which is valid and means "not used".
    personal = "<" * 14
    personal_cd = mrz_check_digit(personal)

    composite_source = number + number_cd + dob + dob_cd + expiry + expiry_cd + personal + personal_cd
    composite_cd = mrz_check_digit(composite_source)

    line2 = (
        f"{number}{number_cd}{identity.mrz_country}{dob}{dob_cd}{identity.sex}"
        f"{expiry}{expiry_cd}{personal}{personal_cd}{composite_cd}"
    )
    assert len(line2) == 44, f"MRZ line 2 is {len(line2)} chars, must be 44"
    return line1, line2


# --------------------------------------------------------------------------- photo handling
@dataclass(frozen=True, kw_only=True)
class JpegInfo:
    """Just enough JPEG header data to embed the file without decoding it."""

    data: bytes
    width: int
    height: int
    components: int


def read_jpeg(path: Path) -> JpegInfo:
    """Parse width/height/components from the JPEG's SOF marker.

    PDF can carry JPEG bytes verbatim as an image XObject with `/Filter /DCTDecode` — the viewer
    does the JPEG decoding. That means embedding a photo needs no image library at all, only the
    pixel dimensions and colour-component count from the header, which is what this reads. This
    is why the photos folder insists on JPEG rather than PNG: PNG has no equivalent passthrough
    filter and would require actually decoding and recompressing the pixels.
    """
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"{path.name} is not a JPEG (missing SOI marker)")

    offset = 2
    while offset < len(data) - 1:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            offset += 2
            continue
        if offset + 4 > len(data):
            break
        segment_length = int.from_bytes(data[offset + 2:offset + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[offset + 5:offset + 7], "big")
            width = int.from_bytes(data[offset + 7:offset + 9], "big")
            components = data[offset + 9]
            if components not in (1, 3):
                # CMYK JPEGs (4 components) need /Decode inversion handling; refuse rather than
                # emit a document that renders inverted.
                raise ValueError(
                    f"{path.name} has {components} colour components; use a greyscale or RGB JPEG")
            return JpegInfo(data=data, width=width, height=height, components=components)
        offset += 2 + segment_length

    raise ValueError(f"{path.name}: no SOF marker found, cannot determine dimensions")


def find_photo(photos_dir: Path | None, identity: DemoIdentity) -> Path | None:
    """Persona-specific photo, else `default.jpg`, else any single JPEG in the folder.

    The last fallback exists because the realistic case is one photo dropped in with whatever
    name the camera gave it — requiring an exact filename would turn "add your photo" into a
    rename step for no benefit. A missing photo is not an error: the document is generated with
    an empty portrait frame.
    """
    if photos_dir is None or not photos_dir.is_dir():
        return None
    stem = identity.persona_id.lower()
    for candidate in (
        photos_dir / f"{stem}.jpg", photos_dir / f"{stem}.jpeg",
        photos_dir / "default.jpg", photos_dir / "default.jpeg",
    ):
        if candidate.is_file():
            return candidate
    loose = sorted(
        p for p in photos_dir.iterdir()
        if p.suffix.lower() in (".jpg", ".jpeg") and p.is_file()
    )
    return loose[0] if loose else None


def _photo_placement(photo: JpegInfo) -> tuple[float, float, float, float]:
    """Cover-fit the photo to the portrait box, centred, preserving aspect ratio.

    Cover rather than contain: a passport portrait fills its frame, and letterboxing inside the
    frame is an immediate tell. The overflow is clipped by the `W n` clip path in the content
    stream, so nothing spills outside the frame.
    """
    scale = max(PHOTO_W / photo.width, PHOTO_H / photo.height)
    draw_w = photo.width * scale
    draw_h = photo.height * scale
    return (PHOTO_X + (PHOTO_W - draw_w) / 2, PHOTO_Y + (PHOTO_H - draw_h) / 2, draw_w, draw_h)


# --------------------------------------------------------------------------- PDF content
def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _text(font: str, size: float, x: float, y: float, value: str) -> str:
    return f"BT /{font} {size} Tf 1 0 0 1 {x} {y} Tm ({_pdf_escape(value)}) Tj ET"


def _field(x: float, y: float, label: str, value: str, *, value_size: float = 10.5) -> list[str]:
    """One label/value pair in the small-caps-label style real data pages use."""
    return [
        f"0.35 0.35 0.4 rg {_text('Helvetica', 6.2, x, y + 11, label)}",
        f"0 0 0 rg {_text('Helvetica-Bold', value_size, x, y, value)}",
    ]


def _content_stream(identity: DemoIdentity, photo: JpegInfo | None = None) -> str:
    title, subtitle = _DOCUMENT_TITLE[identity.document_type]
    ops: list[str] = []

    # ---- specimen banner, above the card, so it is the first thing read
    ops.append("0.70 0.12 0.10 rg")
    ops.append(_text("Helvetica-Bold", 12, CARD_X, 762,
                     _SPECIMEN_BANNER[identity.document_type]))
    ops.append("0.35 0.35 0.4 rg")
    ops.append(_text(
        "Helvetica", 8, CARD_X, 748,
        "Synthetic document generated for the AWS re:Invent 2026 KYC-in-90-Seconds demo (D-03). "
        "All data fabricated."))

    # ---- card outline
    ops.append("q 1 w 0.55 0.58 0.62 RG 0.98 0.98 0.99 rg")
    ops.append(f"{CARD_X} {CARD_Y} {CARD_W} {CARD_H} re B")
    ops.append("Q")

    # ---- header band
    band_h = 46
    band_y = CARD_Y + CARD_H - band_h
    ops.append("q 0.13 0.20 0.35 rg")
    ops.append(f"{CARD_X} {band_y} {CARD_W} {band_h} re f")
    ops.append("Q")
    ops.append("1 1 1 rg")
    ops.append(_text("Helvetica-Bold", 15, CARD_X + 16, band_y + 25, title))
    ops.append(_text("Helvetica", 7.5, CARD_X + 16, band_y + 12, subtitle))
    # Width budgeted for the longest nationality in the dataset ("UNITED STATES OF AMERICA",
    # 24 chars) at 9.5pt Helvetica-Bold. Truncating it instead — as an earlier version did with a
    # [:22] slice — printed "UNITED STATES OF AMERI" on the page, which reads as a broken
    # renderer rather than a document.
    nationality_x = CARD_X + CARD_W - 220
    ops.append(_text("Helvetica-Bold", 9.5, nationality_x, band_y + 26, identity.nationality))
    ops.append(_text("Helvetica", 7.5, nationality_x, band_y + 12,
                     f"Type {_DOCUMENT_CODE[identity.document_type]}  "
                     f"Code {identity.mrz_country}"))

    # ---- portrait frame + photo
    ops.append("q 0.7 w 0.45 0.48 0.52 RG 0.93 0.93 0.95 rg")
    ops.append(f"{PHOTO_X} {PHOTO_Y} {PHOTO_W} {PHOTO_H} re B")
    ops.append("Q")
    if photo is not None:
        draw_x, draw_y, draw_w, draw_h = _photo_placement(photo)
        # Clip to the frame so a cover-fitted photo cannot bleed over the card.
        ops.append("q")
        ops.append(f"{PHOTO_X} {PHOTO_Y} {PHOTO_W} {PHOTO_H} re W n")
        ops.append(f"{draw_w} 0 0 {draw_h} {draw_x} {draw_y} cm")
        ops.append("/Photo Do")
        ops.append("Q")
    else:
        ops.append("0.55 0.55 0.6 rg")
        ops.append(_text("Helvetica", 8, PHOTO_X + 30, PHOTO_Y + PHOTO_H / 2, "NO PHOTO"))
        ops.append(_text("Helvetica", 6, PHOTO_X + 8, PHOTO_Y + PHOTO_H / 2 - 14,
                         "add a JPEG to demo-assets/photos/"))

    # Signature strip under the portrait, as on a real data page.
    ops.append("0.35 0.35 0.4 rg")
    ops.append(_text("Helvetica", 6.2, PHOTO_X, PHOTO_Y - 12, "HOLDER'S SIGNATURE / SIGNATURE"))
    ops.append("q 0.7 w 0.55 0.58 0.62 RG")
    ops.append(f"{PHOTO_X} {PHOTO_Y - 34} {PHOTO_W} 18 re S")
    ops.append("Q")
    ops.append("0.45 0.45 0.5 rg")
    ops.append(_text("Helvetica-Oblique", 9, PHOTO_X + 8, PHOTO_Y - 29, "(specimen)"))

    # ---- field column
    rows = [
        ("SURNAME / NOM", identity.surname),
        ("GIVEN NAMES / PRENOMS", identity.given_names),
        ("DOCUMENT NO. / N° DU DOCUMENT", identity.document_number),
        ("NATIONALITY / NATIONALITE", identity.nationality),
        ("DATE OF BIRTH / DATE DE NAISSANCE", identity.dob),
        ("SEX / SEXE", identity.sex),
        ("PLACE OF BIRTH / LIEU DE NAISSANCE", identity.place_of_birth),
    ]
    y = PHOTO_Y + PHOTO_H - 12
    for label, value in rows:
        ops.extend(_field(FIELD_X, y, label, value))
        y -= 25

    # ---- issue/expiry/authority row across the card, placed under the signature strip rather
    # than floated near the MRZ, so the card has no large dead band in the middle.
    ops.extend(_field(PHOTO_X, CARD_Y + 116, "DATE OF ISSUE / DATE DE DELIVRANCE",
                      identity.issued, value_size=9.5))
    ops.extend(_field(PHOTO_X + 170, CARD_Y + 116, "DATE OF EXPIRY / DATE D'EXPIRATION",
                      identity.expiry, value_size=9.5))
    ops.extend(_field(PHOTO_X + 340, CARD_Y + 116, "ISSUING COUNTRY / PAYS",
                      identity.issuing_country, value_size=9.5))
    ops.extend(_field(PHOTO_X, CARD_Y + 84, "AUTHORITY / AUTORITE", identity.authority,
                      value_size=8))

    # ---- machine-readable zone
    line1, line2 = build_mrz(identity)
    ops.append("q 0.97 0.97 0.98 rg")
    ops.append(f"{CARD_X} {CARD_Y} {CARD_W} 40 re f")
    ops.append("Q")
    ops.append("0 0 0 rg")
    # Courier at 9.6pt fits 44 characters across the card width; the MRZ is fixed-pitch by
    # definition, so a proportional font here would be immediately wrong.
    ops.append(_text("Courier", 9.6, CARD_X + 10, MRZ_Y - 8, line1))
    ops.append(_text("Courier", 9.6, CARD_X + 10, MRZ_Y - 22, line2))

    # ---- footer outside the card
    ops.append("0.45 0.45 0.5 rg")
    ops.append(_text("Helvetica", 7.5, CARD_X, CARD_Y - 22,
                     f"Persona {identity.persona_id} - synthetic data, no real subject. "
                     f"Jurisdiction: {identity.jurisdiction.upper()}."))
    ops.append(_text("Helvetica", 7.5, CARD_X, CARD_Y - 34,
                     f"Residential address on file: {identity.address}"))
    ops.append("0.70 0.12 0.10 rg")
    ops.append(_text("Helvetica-Bold", 7.5, CARD_X, CARD_Y - 50,
                     "This document is a specimen. It is not issued by any government and has no "
                     "legal validity."))

    return "\n".join(ops)


_FONTS = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Courier")


def build_pdf_bytes(identity: DemoIdentity, photo: JpegInfo | None = None) -> bytes:
    """Hand-assembled single-page PDF: catalog, page tree, one page, four base-14 fonts, one
    content stream, plus an image XObject when a photo is supplied.

    Object numbering is computed rather than hardcoded, because the photo XObject is conditional
    — a fixed object number would dangle whenever there is no photo, and a dangling reference is
    one of the few ways to produce a PDF some viewers open and others reject outright.
    """
    content = _content_stream(identity, photo)

    # 1 catalog, 2 pages, 3 page, 4..(3+len(_FONTS)) fonts, then contents, then optional photo.
    first_font_obj = 4
    contents_obj = first_font_obj + len(_FONTS)
    photo_obj = contents_obj + 1

    font_resource = " ".join(
        f"/{name} {first_font_obj + i} 0 R" for i, name in enumerate(_FONTS)
    )
    xobject_entry = f"/XObject << /Photo {photo_obj} 0 R >> " if photo is not None else ""

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /Resources << /Font << {font_resource} >> "
            f"{xobject_entry}>> /MediaBox [0 0 {PAGE_W} {PAGE_H}] /Contents {contents_obj} 0 R >>"
        ).encode("ascii"),
    ]
    for name in _FONTS:
        # WinAnsiEncoding, not the default StandardEncoding: the content stream is latin-1, and
        # StandardEncoding has no glyph at 0xB0, so the "°" in "N° DU DOCUMENT" printed as a gap.
        objects.append(
            f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} "
            f"/Encoding /WinAnsiEncoding >>".encode("ascii"))

    stream_bytes = content.encode("latin-1")
    objects.append(
        b"<< /Length " + str(len(stream_bytes)).encode("ascii") + b" >>\nstream\n"
        + stream_bytes + b"\nendstream"
    )

    if photo is not None:
        colour_space = b"/DeviceGray" if photo.components == 1 else b"/DeviceRGB"
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width "
            + str(photo.width).encode("ascii")
            + b" /Height " + str(photo.height).encode("ascii")
            + b" /ColorSpace " + colour_space
            + b" /BitsPerComponent 8 /Filter /DCTDecode /Length "
            + str(len(photo.data)).encode("ascii")
            + b" >>\nstream\n" + photo.data + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        b"trailer\n<< /Size " + str(count).encode("ascii") + b" /Root 1 0 R >>\n"
        b"startxref\n" + str(xref_offset).encode("ascii") + b"\n%%EOF"
    )
    return bytes(out)


#: Pixel height the PDF is rasterised to for the JPEG. Chosen for two competing consumers:
#:
#: * **Textract** reads the MRZ correctly at the PDF's native 612x792, so OCR does not need this.
#: * **Rekognition CompareFaces** does. At native size the portrait box is only ~140x176 px, which
#:   leaves a face barely above Rekognition's minimum. Rasterising taller recovers real detail
#:   rather than upscaling, because the embedded photo is itself far larger (1144x1245 in the
#:   sample used during development) — the PDF is a vector container, so rendering bigger genuinely
#:   resolves more of the original JPEG.
JPEG_RENDER_HEIGHT = 1700

#: JPEG quality for the rasterised document. High enough not to introduce compression artefacts
#: that OCR would have to fight.
JPEG_QUALITY = 92


def _render_jpeg(pdf_path: Path, jpeg_path: Path) -> bool:
    """Rasterise the PDF to JPEG using only tools already on macOS.

    A JPEG (or PNG) is not a nicety — it is required by both downstream consumers. Textract's
    synchronous text detection and Rekognition's `CompareFaces` neither accept PDF, so a
    PDF-only document means no field extraction and no face match.

    Two steps because neither tool does the whole job: `sips` rasterises PDF only at a fixed
    72 dpi, which is too small for face matching, while `qlmanage` renders at an arbitrary pixel
    size but only emits PNG. So `qlmanage` sizes it and `sips` converts the format.

    Returns False rather than raising if the tooling is unavailable, so a non-macOS checkout still
    gets its PDFs. The JPEG is what the pipeline needs, so its absence is reported loudly by the
    caller rather than swallowed here.
    """
    if shutil.which("qlmanage") is None or shutil.which("sips") is None:
        return False

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        result = subprocess.run(
            ["qlmanage", "-t", "-s", str(JPEG_RENDER_HEIGHT), "-o", str(tmp_dir), str(pdf_path)],
            capture_output=True, text=True,
        )
        # qlmanage names its output `<original name>.png` and reports success on stdout rather
        # than via exit code, so the file's existence is the only reliable signal.
        rendered = next(iter(tmp_dir.glob("*.png")), None)
        if rendered is None:
            LOG_LINES.append(
                f"qlmanage produced no thumbnail for {pdf_path.name}: "
                f"{(result.stderr or result.stdout).strip()[:200]}")
            return False

        convert = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(JPEG_QUALITY),
             str(rendered), "--out", str(jpeg_path)],
            capture_output=True, text=True,
        )
        if convert.returncode != 0 or not jpeg_path.exists():
            LOG_LINES.append(
                f"sips failed to convert {rendered.name}: "
                f"{(convert.stderr or convert.stdout).strip()[:200]}")
            return False
    return True


#: Non-fatal problems encountered while rendering, surfaced by `main` instead of printed from deep
#: inside a helper.
LOG_LINES: list[str] = []


@dataclass(frozen=True, kw_only=True)
class GeneratedDocument:
    identity: DemoIdentity
    pdf_path: Path
    jpeg_path: Path | None
    photo_path: Path | None


def write_all(output_dir: Path, photos_dir: Path | None = None) -> list[GeneratedDocument]:
    """Writes a PDF and a JPEG per persona.

    Both formats are kept. The JPEG is what gets uploaded and what the pipeline can actually read;
    the PDF stays because it is the higher-fidelity artefact to project on a screen, and because
    losing it would remove the only vector rendering of the layout.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    LOG_LINES.clear()
    written: list[GeneratedDocument] = []
    for identity in DEMO_IDENTITIES:
        photo_path = find_photo(photos_dir, identity)
        photo = read_jpeg(photo_path) if photo_path else None
        stem = f"{identity.persona_id.lower()}-{identity.document_type.lower()}"
        pdf_path = output_dir / f"{stem}.pdf"
        pdf_path.write_bytes(build_pdf_bytes(identity, photo))
        jpeg_path = output_dir / f"{stem}.jpg"
        rendered = _render_jpeg(pdf_path, jpeg_path)
        written.append(GeneratedDocument(
            identity=identity, pdf_path=pdf_path,
            jpeg_path=jpeg_path if rendered else None, photo_path=photo_path))
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic demo identity documents.")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).parent / "generated",
        help="Where to write the PDFs (default: demo-assets/generated)")
    parser.add_argument(
        "--photos-dir", type=Path, default=Path(__file__).parent / "photos",
        help="Where to look for persona JPEG photos (default: demo-assets/photos)")
    args = parser.parse_args()

    written = write_all(args.output_dir, args.photos_dir)
    print(f"Wrote {len(written)} synthetic document(s) to {args.output_dir}:")
    for doc in written:
        photo = f"photo: {doc.photo_path.name}" if doc.photo_path else "no photo"
        jpeg = doc.jpeg_path.name if doc.jpeg_path else "JPEG FAILED"
        print(f"  {doc.pdf_path.name}  +  {jpeg}   [{photo}]")

    for line in LOG_LINES:
        print(f"\nwarning: {line}")

    if any(doc.jpeg_path is None for doc in written):
        # Worth shouting about: the pipeline reads the JPEG, not the PDF. Neither Textract's
        # synchronous text detection nor Rekognition's CompareFaces accepts PDF, so a missing
        # JPEG means no field extraction and no face match.
        print("\nWARNING: some JPEGs were not produced. Upload the JPEG, not the PDF — "
              "document reading and face matching both require it.")

    if not any(doc.photo_path for doc in written):
        print(
            f"\nNo photos found in {args.photos_dir} — portrait frames are empty.\n"
            "Drop any JPEG in that folder and re-run.")


if __name__ == "__main__":
    main()
