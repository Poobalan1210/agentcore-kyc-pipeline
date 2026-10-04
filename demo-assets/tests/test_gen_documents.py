"""Tests for the synthetic demo document generator.

Three things are worth asserting here and are all easy to get silently wrong:

* the hand-written PDF bytes are structurally sound (header, xref, trailer, one page),
* the MRZ is a real ICAO 9303 TD3 zone with correct check digits, verified against the
  specification's own published test vectors rather than against this implementation's output,
* no fabricated persona collides with `sanctions.py`'s screening data, so a demo run cannot
  produce an unintended sanctions hit.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))

from gen_documents import (  # noqa: E402
    ACT3_BATCH,
    ACT3_LIST_UPDATE,
    ACT3_OTHER_LISTINGS,
    DEMO_IDENTITIES,
    build_mrz,
    build_pdf_bytes,
    find_photo,
    mrz_check_digit,
    read_jpeg,
    write_all,
)


# ------------------------------------------------------------------ MRZ correctness
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        # ICAO 9303 Part 3, published check-digit examples.
        ("L898902C3", "6"),
        ("690806", "1"),
        ("940623", "6"),
        ("<<<<<<<<<<<<<<", "0"),
    ],
)
def test_check_digit_matches_icao_published_examples(field, expected):
    assert mrz_check_digit(field) == expected


def test_mrz_lines_are_exactly_44_characters(): 
    for identity in DEMO_IDENTITIES:
        line1, line2 = build_mrz(identity)
        assert len(line1) == 44, f"{identity.persona_id} line 1 is {len(line1)}"
        assert len(line2) == 44, f"{identity.persona_id} line 2 is {len(line2)}"


def test_mrz_check_digits_are_self_consistent():
    """Recompute every check digit from the field it guards, independently of `build_mrz`. A
    wrong check digit is the detail that makes a document look fake to anyone who knows the
    format, and it cannot be spotted by eye."""
    for identity in DEMO_IDENTITIES:
        _, line2 = build_mrz(identity)
        number, number_cd = line2[0:9], line2[9]
        dob, dob_cd = line2[13:19], line2[19]
        expiry, expiry_cd = line2[21:27], line2[27]
        personal, personal_cd = line2[28:42], line2[42]
        composite_cd = line2[43]

        assert mrz_check_digit(number) == number_cd, identity.persona_id
        assert mrz_check_digit(dob) == dob_cd, identity.persona_id
        assert mrz_check_digit(expiry) == expiry_cd, identity.persona_id
        assert mrz_check_digit(personal) == personal_cd, identity.persona_id
        composite_source = (
            number + number_cd + dob + dob_cd + expiry + expiry_cd + personal + personal_cd
        )
        assert mrz_check_digit(composite_source) == composite_cd, identity.persona_id


def test_mrz_encodes_the_right_identity_fields():
    for identity in DEMO_IDENTITIES:
        line1, line2 = build_mrz(identity)
        assert line1.startswith(f"{'PDI'[('PASSPORT', 'DRIVERS_LICENSE', 'NATIONAL_ID').index(identity.document_type)]}<{identity.mrz_country}")
        assert identity.surname.replace("-", "<") in line1
        assert line2[13:19] == identity.dob[2:4] + identity.dob[5:7] + identity.dob[8:10]
        assert line2[20] == identity.sex
        assert line2[10:13] == identity.mrz_country


def test_hyphenated_surname_becomes_filler_in_mrz_but_keeps_hyphen_in_print():
    """9303 transliteration turns `-` into `<`. The printed field must keep the real spelling —
    conflating the two would either corrupt the MRZ or misspell the name on the page."""
    uk = next(i for i in DEMO_IDENTITIES if i.persona_id == "DEMO-UK-01")
    assert uk.surname == "OSEI-BRIGHT"
    line1, _ = build_mrz(uk)
    assert "OSEI<BRIGHT" in line1
    assert "OSEI-BRIGHT" not in line1


def test_check_digit_rejects_characters_not_valid_in_an_mrz():
    with pytest.raises(ValueError, match="not valid in an MRZ"):
        mrz_check_digit("abc")  # lowercase is not an MRZ character


# ------------------------------------------------------------------ PDF structure
def test_every_identity_produces_a_valid_single_page_pdf():
    for identity in DEMO_IDENTITIES:
        pdf = build_pdf_bytes(identity)
        assert pdf.startswith(b"%PDF-1.4")
        assert pdf.rstrip().endswith(b"%%EOF")
        assert b"xref" in pdf
        assert b"trailer" in pdf
        assert b"/Count 1" in pdf
        # The MRZ needs a fixed-pitch font; a proportional MRZ is immediately wrong.
        assert b"/BaseFont /Courier" in pdf


def test_pdf_carries_the_specimen_marking():
    """These documents must never be mistakable for genuine ones. The marking is asserted, not
    left to the layout, because removing it would be a one-line change with real consequences."""
    for identity in DEMO_IDENTITIES:
        pdf = build_pdf_bytes(identity)
        assert b"SPECIMEN - NOT A REAL " in pdf
        assert b"no legal validity" in pdf
        # The banner names the document kind: a passport warning printed on a driving licence
        # reads as unchanged boilerplate, and boilerplate warnings stop being read.
        expected = {
            "PASSPORT": b"NOT A REAL TRAVEL DOCUMENT",
            "DRIVERS_LICENSE": b"NOT A REAL DRIVING LICENCE",
            "NATIONAL_ID": b"NOT A REAL IDENTITY DOCUMENT",
        }[identity.document_type]
        assert expected in pdf, f"{identity.persona_id} banner does not match document type"


def test_printed_fields_appear_in_the_pdf():
    for identity in DEMO_IDENTITIES:
        pdf = build_pdf_bytes(identity)
        for value in (identity.surname, identity.given_names, identity.document_number,
                      identity.dob, identity.expiry):
            assert value.encode("latin-1") in pdf, f"{identity.persona_id} missing {value}"


# ------------------------------------------------------------------ dataset safety
#: What screening must find for each persona: (strongest alert, alert ids, excluded ids). Run
#: through the real matcher, against the identity the screening tool builds from the case record,
#: so a persona cannot drift into an unintended hit — or out of its intended one.
EXPECTED_SCREENING = {
    "DEMO-US-01": ("NO_MATCH", [], []),
    "DEMO-US-02": ("POSSIBLE", ["SYN-0106"], []),
    "DEMO-US-03": ("NO_MATCH", [], []),
    "DEMO-US-04": ("NO_MATCH", [], ["SYN-0001"]),
    "DEMO-UK-01": ("NO_MATCH", [], []),
    "DEMO-AU-01": ("NO_MATCH", [], []),
    "DEMO-JP-01": ("NO_MATCH", [], []),
}


def _screen(identity, entries=None):
    from kyctools.case_record import screening_identity
    from kyctools.data.sanctions import ALL_ENTRIES
    from kyctools.matching import Candidate, screen, worst

    declared = screening_identity({"declaredIdentity": {
        "givenNames": identity.given_names, "surname": identity.surname,
        "dateOfBirth": identity.dob, "nationality": identity.mrz_country}})
    candidate = Candidate(name=declared.name, dob=declared.dob, country=declared.country,
                          alternate_names=declared.alternate_names)
    alerts, exclusions = screen(candidate, ALL_ENTRIES if entries is None else entries)
    return (worst(alerts).value, [h.entry_id for h in alerts],
            [h.entry_id for h in exclusions])


@pytest.mark.parametrize("identity", DEMO_IDENTITIES, ids=lambda i: i.persona_id)
def test_each_persona_meets_the_list_exactly_as_its_scenario_says(identity):
    assert identity.persona_id in EXPECTED_SCREENING, (
        f"{identity.persona_id} has no expected screening outcome; add one before using it")
    assert _screen(identity) == EXPECTED_SCREENING[identity.persona_id]


def test_the_namesake_is_excluded_on_date_of_birth_alone():
    """Same name as SYN-0001, so the exclusion must come from the dates, not the spelling."""
    from kyctools.data.sanctions import ALL_ENTRIES

    volkov = next(i for i in DEMO_IDENTITIES if i.persona_id == "DEMO-US-04")
    listed = next(e for e in ALL_ENTRIES if e.entry_id == "SYN-0001")
    assert volkov.full_name.upper() == listed.name.upper()
    assert volkov.dob != listed.dob


def test_act3_list_update_confirms_nadia_and_no_one_else():
    """Act 3 publishes the whole batch, `ACT3_BATCH`. Against the updated list, DEMO-US-03 is a
    CONFIRMED match on SYN-0901 alone, and every other persona's screening is exactly what it was:
    the other listings must not raise or exclude anyone."""
    from kyctools.controls import parse_overlay
    from kyctools.data.sanctions import ALL_ENTRIES

    _, overlay = parse_overlay(json.dumps({"version": "ACT3", "entries": list(ACT3_BATCH)}))
    assert [e.entry_id for e in overlay] == [e["entryId"] for e in ACT3_BATCH]
    updated = ALL_ENTRIES + overlay
    for identity in DEMO_IDENTITIES:
        result = _screen(identity, updated)
        if identity.persona_id == "DEMO-US-03":
            assert result == ("CONFIRMED", ["SYN-0901"], [])
        else:
            assert result == EXPECTED_SCREENING[identity.persona_id], identity.persona_id


def test_act3_other_listings_name_no_persona():
    """Screened against the other listings alone, no persona is raised or excluded: not even a
    weak alert, which a reviewer on stage would have to explain."""
    from kyctools.controls import parse_overlay

    _, overlay = parse_overlay(json.dumps({"version": "ACT3",
                                           "entries": list(ACT3_OTHER_LISTINGS)}))
    for identity in DEMO_IDENTITIES:
        assert _screen(identity, overlay) == ("NO_MATCH", [], []), identity.persona_id


def test_act3_list_update_is_one_the_demo_route_accepts():
    """The same validation `POST /demo/list-update` applies, so the panel's default publishes."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("demo_route", REPO / "api" / "demo.py")
    sys.path.insert(0, str(REPO / "api"))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(REPO / "api"))
    assert module.validate_entries(list(ACT3_BATCH)) == [
        {**entry, "programme": "SYN-UPDATE"} for entry in ACT3_BATCH]


def test_the_demo_controls_publish_the_same_listings():
    """The console's Demo controls publish Act 3's batch. If the named listing drifted, the act
    would publish a list that names no one; if another drifted, it might name someone else."""
    panel = (REPO / "frontend" / "apps" / "console" / "src" / "controls"
             / "ControlsView.tsx").read_text()
    block = re.search(r"const ACT3_ENTRY: ListEntry = \{(.*?)\};", panel, re.S)
    assert block, "ACT3_ENTRY not found in ControlsView.tsx"
    fields = dict(re.findall(r'(\w+): "([^"]*)"', block.group(1)))
    assert fields == ACT3_LIST_UPDATE

    others = re.search(r"const ACT3_OTHER_ENTRIES: ListEntry\[\] = \[(.*?)\];", panel, re.S)
    assert others, "ACT3_OTHER_ENTRIES not found in ControlsView.tsx"
    listed = [dict(re.findall(r'(\w+): "([^"]*)"', body))
              for body in re.findall(r"\{(.*?)\}", others.group(1), re.S)]
    assert listed == list(ACT3_OTHER_LISTINGS)


def test_the_applicant_apps_personas_match_the_documents():
    """The details form is pre-filled from `demoPersonas.ts`. A field that differs from the
    document is a declared-vs-document mismatch on stage, which routes the case to a reviewer."""
    source = (REPO / "frontend" / "apps" / "console" / "src" / "applicant"
              / "demoPersonas.ts").read_text()
    blocks = re.findall(r'id: "(DEMO-[A-Z]{2}-\d{2})",(.*?)\n  \}', source, re.S)
    found = {pid: dict(re.findall(r'(\w+): "([^"]*)"', body)) for pid, body in blocks}
    assert set(found) == {i.persona_id for i in DEMO_IDENTITIES}
    for identity in DEMO_IDENTITIES:
        persona = found[identity.persona_id]
        assert persona["jurisdiction"] == identity.jurisdiction, identity.persona_id
        assert persona["givenNames"] == identity.given_names, identity.persona_id
        assert persona["surname"] == identity.surname, identity.persona_id
        assert persona["dateOfBirth"] == identity.dob, identity.persona_id
        assert persona["nationality"] == identity.mrz_country, identity.persona_id
        assert persona["address"] == identity.address, identity.persona_id
        assert persona["scenario"] == identity.scenario, identity.persona_id
        # The file `write_all` produces for this persona, so the picker names the right upload.
        assert persona["documentFile"] == (
            f"{identity.persona_id.lower()}-{identity.document_type.lower()}.jpg")


def test_persona_ids_are_unique_and_every_jurisdiction_has_one():
    ids = [i.persona_id for i in DEMO_IDENTITIES]
    assert len(ids) == len(set(ids))
    # Matches IntakeStep.tsx's jurisdiction options exactly.
    assert {i.jurisdiction for i in DEMO_IDENTITIES} == {"us", "uk", "au", "jp"}
    for identity in DEMO_IDENTITIES:
        assert identity.persona_id.startswith(f"DEMO-{identity.jurisdiction.upper()}-")
        assert identity.scenario, f"{identity.persona_id} has no scenario"


def test_only_the_decided_jurisdiction_carries_screening_scenarios():
    """Outside `DECIDED_JURISDICTIONS` every case goes to a reviewer for the jurisdiction alone,
    so a screening scenario there would never reach the screening's own outcome on stage."""
    sys.path.insert(0, str(REPO / "infra"))
    try:
        from kyc.config import DECIDED_JURISDICTIONS
    finally:
        sys.path.remove(str(REPO / "infra"))
    for identity in DEMO_IDENTITIES:
        if EXPECTED_SCREENING[identity.persona_id] != ("NO_MATCH", [], []):
            assert identity.jurisdiction in DECIDED_JURISDICTIONS, identity.persona_id


def test_document_numbers_are_unique():
    numbers = [i.document_number for i in DEMO_IDENTITIES]
    assert len(numbers) == len(set(numbers))


def test_no_document_expires_before_the_talk_or_soon_after():
    for identity in DEMO_IDENTITIES:
        assert identity.expiry > "2027-12-31", identity.persona_id
        assert identity.issued < "2026-12-01", identity.persona_id


def test_document_numbers_fit_the_mrz_field():
    for identity in DEMO_IDENTITIES:
        assert len(identity.document_number) <= 9, (
            f"{identity.persona_id}: document number longer than the MRZ's 9-char field")
        assert identity.document_number.isalnum()


# ------------------------------------------------------------------ photo handling
def test_write_all_creates_one_file_per_identity(tmp_path):
    written = write_all(tmp_path)
    assert len(written) == len(DEMO_IDENTITIES)
    for doc in written:
        assert doc.pdf_path.exists()
        assert doc.pdf_path.read_bytes().startswith(b"%PDF-1.4")
        assert doc.photo_path is None


def test_write_all_also_produces_a_jpeg(tmp_path):
    """The JPEG is not a convenience copy — it is the only format the pipeline can read. Neither
    Textract's synchronous text detection nor Rekognition's CompareFaces accepts PDF, so a
    PDF-only document means no field extraction and no face match."""
    written = write_all(tmp_path)
    for doc in written:
        if doc.jpeg_path is None:
            pytest.skip("qlmanage/sips unavailable in this environment")
        assert doc.jpeg_path.exists()
        assert doc.jpeg_path.read_bytes().startswith(b"\xff\xd8"), "not a JPEG"
        assert doc.jpeg_path.suffix == ".jpg"


def test_missing_photos_directory_is_not_an_error(tmp_path):
    written = write_all(tmp_path, tmp_path / "does-not-exist")
    for doc in written:
        assert doc.photo_path is None
        pdf = doc.pdf_path.read_bytes()
        assert b"/XObject" not in pdf
        assert b"NO PHOTO" in pdf


def test_photo_is_embedded_when_present(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "default.jpg").write_bytes(_minimal_jpeg(width=120, height=160))

    written = write_all(tmp_path / "generated", photos)
    for doc in written:
        assert doc.photo_path is not None
        pdf = doc.pdf_path.read_bytes()
        assert b"/XObject << /Photo" in pdf
        assert b"/Subtype /Image" in pdf
        assert b"/Filter /DCTDecode" in pdf
        assert b"/Photo Do" in pdf
        assert b"/Width 120" in pdf and b"/Height 160" in pdf


def test_persona_specific_photo_wins_over_default(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "default.jpg").write_bytes(_minimal_jpeg(width=100, height=100))
    (photos / "demo-us-01.jpg").write_bytes(_minimal_jpeg(width=222, height=333))

    written = {doc.pdf_path.name: doc.photo_path
               for doc in write_all(tmp_path / "out", photos)}
    us_pdf = (tmp_path / "out" / "demo-us-01-passport.pdf").read_bytes()
    assert b"/Width 222" in us_pdf and b"/Height 333" in us_pdf
    assert written["demo-us-01-passport.pdf"].name == "demo-us-01.jpg"
    assert written["demo-uk-01-drivers_license.pdf"].name == "default.jpg"


def test_document_numbers_avoid_ocr_ambiguous_characters():
    """Found live: `USDEMO001` contains both the letter O and the digit 0, and Textract read it
    back as `USDEM0001` — which fails the MRZ check digit and looks exactly like tampering. Real
    US passport numbers are nine digits, so digits-only is both realistic and unambiguous."""
    for identity in DEMO_IDENTITIES:
        number = identity.document_number
        assert number.isdigit(), (
            f"{identity.persona_id}: {number!r} mixes letters and digits; OCR confuses O/0, I/1 "
            "and S/5, which produces a false check-digit failure")
        assert len(number) == 9


def test_any_loose_jpeg_is_used_when_no_conventional_name_matches(tmp_path):
    """The realistic case is one photo dropped in with whatever name the camera gave it.
    Requiring an exact filename would make "add your photo" a rename step for no benefit."""
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "RANI04078(1).jpg").write_bytes(_minimal_jpeg(width=144, height=200))

    for identity in DEMO_IDENTITIES:
        found = find_photo(photos, identity)
        assert found is not None and found.name == "RANI04078(1).jpg"


def test_non_jpeg_files_in_photos_dir_are_ignored(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "README.md").write_text("not a photo")
    (photos / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert find_photo(photos, DEMO_IDENTITIES[0]) is None


def test_read_jpeg_rejects_non_jpeg(tmp_path):
    bad = tmp_path / "not-a-photo.jpg"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    with pytest.raises(ValueError, match="not a JPEG"):
        read_jpeg(bad)


def test_read_jpeg_rejects_cmyk(tmp_path):
    path = tmp_path / "cmyk.jpg"
    path.write_bytes(_minimal_jpeg(width=10, height=10, components=4))
    with pytest.raises(ValueError, match="colour components"):
        read_jpeg(path)


def test_read_jpeg_reads_dimensions_and_components(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(_minimal_jpeg(width=640, height=480))
    info = read_jpeg(path)
    assert (info.width, info.height, info.components) == (640, 480, 3)


def _minimal_jpeg(*, width: int, height: int, components: int = 3) -> bytes:
    """A byte-valid JPEG *header* with an SOF0 frame marker — enough for `read_jpeg` to parse
    dimensions from. Not a decodable image, which is fine: these tests assert on the PDF
    structure the generator emits, and building a real encoded JPEG would need the image library
    this module exists to avoid depending on."""
    sof_payload = (
        bytes([8])
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes([components])
        + bytes([1, 0x11, 0]) * components
    )
    return (
        b"\xff\xd8"
        + b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
        + b"\xff\xc0" + (len(sof_payload) + 2).to_bytes(2, "big") + sof_payload
        + b"\xff\xda" + (8).to_bytes(2, "big") + b"\x00" * 6
        + b"\xff\xd9"
    )
