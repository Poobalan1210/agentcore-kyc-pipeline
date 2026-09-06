"""Generate a plain black-and-white Word brief on India KYC volumes.

Run:  .venv/bin/python docs/build_india_kyc_brief.py
Out:  docs/India-KYC-Research-Brief.docx

Deliberately monochrome: no colour, no shading, no logos. One serif body font,
one table style, three heading levels. Every figure carries a source.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

OUT = Path(__file__).parent / "India-KYC-Research-Brief.docx"

BLACK = RGBColor(0x00, 0x00, 0x00)
BODY_FONT = "Times New Roman"
BODY_SIZE = Pt(10.5)


# --------------------------------------------------------------------------
# document setup
# --------------------------------------------------------------------------
def new_document() -> Document:
    doc = Document()

    section = doc.sections[0]
    section.page_width = Inches(8.27)  # A4
    section.page_height = Inches(11.69)
    for margin in ("top_margin", "bottom_margin"):
        setattr(section, margin, Inches(0.9))
    for margin in ("left_margin", "right_margin"):
        setattr(section, margin, Inches(0.85))

    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = BODY_SIZE
    normal.font.color.rgb = BLACK
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15

    for name, size, bold in (
        ("Heading 1", Pt(15), True),
        ("Heading 2", Pt(12), True),
        ("Heading 3", Pt(10.5), True),
    ):
        style = doc.styles[name]
        style.font.name = BODY_FONT
        style.font.size = size
        style.font.bold = bold
        style.font.italic = False
        style.font.color.rgb = BLACK
        style.paragraph_format.space_before = Pt(14)
        style.paragraph_format.space_after = Pt(4)
        style.paragraph_format.keep_with_next = True

    return doc


def title_block(doc: Document, title: str, subtitle: str, meta: str) -> None:
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run(title)
    run.bold = True
    run.font.size = Pt(18)

    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run(subtitle)
    run.font.size = Pt(11)

    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run(meta)
    run.font.size = Pt(9)
    run.italic = True
    para.paragraph_format.space_after = Pt(16)


def heading(doc: Document, text: str, level: int = 1) -> None:
    doc.add_heading(text, level=level)


def body(doc: Document, text: str) -> None:
    doc.add_paragraph(text)


def bullets(doc: Document, items: list[str]) -> None:
    for item in items:
        para = doc.add_paragraph(item, style="List Bullet")
        para.paragraph_format.space_after = Pt(2)


def numbers(doc: Document, items: list[str]) -> None:
    for item in items:
        para = doc.add_paragraph(item, style="List Number")
        para.paragraph_format.space_after = Pt(2)


def table(doc: Document, headers: list[str], rows: list[list[str]],
          widths: list[float] | None = None) -> None:
    """Plain single-line grid table, black text, bold header row."""
    tbl = doc.add_table(rows=1, cols=len(headers))
    tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = True

    for cell, text in zip(tbl.rows[0].cells, headers):
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        run.bold = True
        run.font.size = Pt(9)

    for row in rows:
        cells = tbl.add_row().cells
        for cell, text in zip(cells, row):
            cell.text = ""
            run = cell.paragraphs[0].add_run(text)
            run.font.size = Pt(9)

    if widths:
        for row in tbl.rows:
            for cell, width in zip(row.cells, widths):
                cell.width = Inches(width)

    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def source_note(doc: Document, text: str) -> None:
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.font.size = Pt(8)
    run.italic = True
    para.paragraph_format.space_after = Pt(10)


# --------------------------------------------------------------------------
# content
# --------------------------------------------------------------------------
def build() -> None:
    doc = new_document()

    title_block(
        doc,
        "KYC in India: Volume, Sector and Failure Data",
        "Research brief for the AWS re:Invent 2026 session \u201cKYC in 90 Seconds\u201d",
        "Compiled 18 August 2026  |  All figures carry a named source  |  "
        "Derived figures are marked",
    )

    # ---------------------------------------------------------------- scope
    heading(doc, "1. Scope and one structural note", 1)
    body(
        doc,
        "This brief answers a single question: how much KYC does India actually do "
        "each year, in which sector, and on which digital rail. It is framing "
        "material for the opening of the session, not evidence for the demo.",
    )
    body(
        doc,
        "Note: the build covers four jurisdictions \u2014 US, UK, AU and JP. India is "
        "not one of them. Everything below supports the problem statement. Adding "
        "India as a fifth jurisdiction would be a specification change, not a "
        "research task.",
    )

    # ----------------------------------------------------------- rail layer
    heading(doc, "2. The rail layer", 1)
    body(
        doc,
        "India does not run identity verification at national scale as a pilot. It "
        "finished adopting it and now maintains it.",
    )
    table(
        doc,
        ["Metric", "Value", "Period", "Source"],
        [
            ["Aadhaar authentications", "2,707 crore (27.07 bn)", "FY2024-25", "UIDAI"],
            ["Monthly authentications", "231 crore, +8.47% YoY", "Nov 2025", "UIDAI"],
            ["Cumulative authentications", "Over 17,000 crore", "Mar 2026", "Lok Sabha / MeitY"],
            ["Monthly e-KYC", "44.63 crore", "Mar 2025", "UIDAI"],
            ["Annual e-KYC run rate", "About 520 crore (DERIVED)", "2025-26", "From monthly figures"],
            ["Face authentication", "19.36 crore per month; 200 crore cumulative", "Jul 2025", "UIDAI"],
            ["Live Aadhaar holders", "About 134 crore", "Mar 2026", "Lok Sabha / MeitY"],
            ["CKYC records", "About 112 crore digitised records", "Apr 2026", "CKYCRR notification"],
            ["Account Aggregator", "45 crore consents; 500 crore data fetches", "FY26", "Sahamati"],
            ["DigiLocker", "72.43 crore users; 934 crore documents issued", "31 Jul 2026", "Parliament reply"],
        ],
        widths=[2.0, 2.3, 1.0, 1.3],
    )
    body(
        doc,
        "For comparison: Singapore\u2019s Singpass handles about 41 million "
        "transactions a month and Hong Kong\u2019s iAM Smart about 180,000 a day. "
        "India handles roughly 2.3 billion authentications a month.",
    )

    # ---------------------------------------------------------- sector view
    heading(doc, "3. How much KYC per year, by sector", 1)
    body(
        doc,
        "Onboarding events, not authentications. P = primary source, "
        "D = derived, N = no public data.",
    )
    table(
        doc,
        ["Sector", "Annual KYC events", "Stock context", "Tag"],
        [
            ["Banking (Jan Dhan)", "About 2.9 crore new accounts", "58.90 crore accounts", "D"],
            ["Banking (all accounts)", "Not published", "72.42 crore BSBDA", "N"],
            ["Lending / fintech", "13.2 crore loan sanctions, Rs 2.15 lakh cr", "Avg ticket Rs 15,177", "P"],
            ["Securities (demat)", "About 2.9 crore new accounts", "23.44 crore accounts", "P"],
            ["Securities (mutual funds)", "1.2 to 1.5 crore net new folios", "27.86 crore folios", "D"],
            ["Insurance (life)", "About 2.7 to 2.9 crore policies", "KYC on issue and renewal", "D"],
            ["Insurance (health)", "2.65 crore policies", "58 crore lives covered", "P"],
            ["Telecom", "Gross SIM adds not published", "120.1 crore subscribers", "N"],
            ["Payments", "Not published", "55.49 crore UPI users", "N"],
            ["Health ID", "Not published", "93.95 crore ABHA accounts", "N"],
        ],
        widths=[1.6, 2.4, 1.7, 0.5],
    )
    body(
        doc,
        "Defensible financial-sector aggregate: roughly 22 to 24 crore new "
        "regulated-relationship KYC events per year, against a rail carrying "
        "about 520 crore e-KYC transactions per year. The difference is re-KYC, "
        "re-authentication and government scheme load.",
    )

    # ------------------------------------------------------------ government
    heading(doc, "4. Government sector", 1)
    body(
        doc,
        "The largest single consumer of KYC in India, and the one that publishes "
        "value but not transaction counts.",
    )
    table(
        doc,
        ["Programme", "Figure", "Period"],
        [
            ["Direct Benefit Transfer", "Rs 6.83 lakh crore", "FY2024-25"],
            ["Direct Benefit Transfer", "Rs 6.17 lakh crore to 16 Mar 2026", "FY2025-26"],
            ["DBT cumulative", "Over Rs 47 lakh crore, 320 schemes, 54 ministries", "to Oct 2025"],
            ["Claimed deduplication savings", "Rs 3.48 lakh crore", "Apr 2025"],
            ["Public Distribution System", "100% e-KYC drive; 5-year re-eKYC cycle now live", "2025-26"],
            ["PM-Kisan", "Over 9.44 crore farmers, payment gated on e-KYC", "Jun 2026"],
            ["MGNREGA", "Over 1.55 crore workers deleted 2022-24", "2025"],
            ["LPG", "Biometric e-KYC deadline slipped 16 Aug to 23 Aug 2026", "Aug 2026"],
            ["Pensions", "25 lakh digital life certificates in five days", "Nov 2025"],
            ["e-Shram", "31.82 crore registered workers", "Aug 2026"],
        ],
        widths=[1.9, 3.3, 1.0],
    )
    body(
        doc,
        "Private-sector access to Aadhaar authentication opened on 31 January 2025 "
        "under the amended Good Governance Rules. The first confirmed private "
        "approval on the public register is a matrimony platform operator, dated "
        "7 August 2026.",
    )

    # -------------------------------------------------------------- election
    heading(doc, "5. Election sector", 1)
    table(
        doc,
        ["Metric", "Value", "Period"],
        [
            ["Registered electors", "96.88 crore final roll", "2024 general election"],
            ["Special Intensive Revision, phase 2 scope", "About 51 crore electors", "From Nov 2025"],
            ["Phase 2 draft outcome", "50.97 crore down to 44.40 crore, about 13% removed", "Dec 2025 to Jan 2026"],
            ["Phase 2 final", "Net deletions about 5.2 crore, 10.2%", "Apr 2026"],
            ["One-year cumulative", "Over 5.8 crore names deleted, 12 states and UTs", "Jun 2026"],
            ["Live position", "Maharashtra 2.07 crore forms uncollected; Delhi 33% uncollectible", "17 Aug 2026"],
            ["ECINET", "10 crore downloads; over 68 lakh cyberattacks countered", "2026"],
            ["Aadhaar to EPIC linkage", "About 55 to 56 crore \u2014 last hard figure is Dec 2022", "Stale"],
        ],
        widths=[2.1, 3.1, 1.0],
    )
    body(
        doc,
        "The Supreme Court ordered Aadhaar accepted as the twelfth identity "
        "document in September 2025, then held on 27 May 2026 that Aadhaar is not "
        "proof of citizenship or domicile. The same credential is authoritative "
        "for one claim and inadmissible for another. That is precisely what a "
        "policy layer has to encode.",
    )

    # ---------------------------------------------------------------- failure
    heading(doc, "6. The cost of KYC that does not complete", 1)
    body(doc, "The strongest India argument, and fully primary-sourced.")
    table(
        doc,
        ["Failure", "Value", "Period", "Source"],
        [
            ["Jan Dhan accounts inoperative", "13.04 crore of 56.03 crore (23%)", "31 Jul 2025", "Lok Sabha"],
            ["Securities KYCs on hold", "About 1.3 crore investor accounts", "Apr 2024", "KRA joint release"],
            ["Valid KYC share at KRAs", "7.9 crore of 10.83 crore (73%)", "Apr 2024", "KRA joint release"],
            ["Unclaimed deposits", "Rs 74,580.45 crore in RBI DEA Fund", "FY2024-25", "Lok Sabha"],
            ["MGNREGA deletions post e-KYC", "27 lakh in five weeks", "Nov 2025", "Press, citing ministry"],
            ["RBI Ombudsman complaints", "13.34 lakh, up 13.55% YoY", "FY2024-25", "RBI"],
        ],
        widths=[2.0, 2.3, 1.0, 1.3],
    )
    body(
        doc,
        "Headline: about 14.3 crore Indians are locked out of accounts they "
        "already own because a verification did not complete. That is larger than "
        "the population of most countries. Section 8 explains the mechanism: "
        "about 20.3 million biometric authentication failures every month, plus "
        "roughly 30 million ration cards cancelled on data mismatch rather than "
        "identity fraud.",
    )

    # ------------------------------------------------------------------ fraud
    heading(doc, "7. The cost of KYC that completes but is wrong", 1)
    table(
        doc,
        ["Metric", "Value", "Period", "Source"],
        [
            ["Bank fraud", "10,114 cases, Rs 48,021 crore, up 46% YoY", "FY2025-26", "RBI Annual Report"],
            ["Prior years", "Rs 32,803 crore FY25; 35,800 cases FY24", "FY24-FY25", "RBI Annual Report"],
            ["Mule accounts identified", "32.08 lakh; Rs 25,698 crore blocked", "30 Jun 2026", "I4C Suspect Registry"],
            ["Mule accounts frozen", "About 8.5 lakh across 700 branches", "Nov 2025", "Press, bank sources"],
            ["MuleHunter.AI detections", "About 20,000 accounts per month", "Nov 2025", "RBI / RBIH"],
            ["Deepfake attacks on Indian firms", "65% have experienced them", "Feb 2026", "Thales Data Threat Report"],
            ["Injection attacks on iOS", "Up 1,151% in H2 2025", "2026", "iProov"],
            ["Deepfake share of biometric fraud", "One in five attempts", "2026", "Entrust / Onfido"],
        ],
        widths=[2.0, 2.3, 1.0, 1.3],
    )
    body(
        doc,
        "Caveat for the FY26 fraud figure: it includes 314 reclassified cases "
        "worth Rs 30,199 crore, so it is not like-for-like with FY25.",
    )
    body(
        doc,
        "Standards gap worth stating plainly: ISO/IEC 30107-3:2023 covers only "
        "attacks at the capture device during presentation. Injection attacks are "
        "out of scope. The first framework for testing injection-attack detection, "
        "CEN/TS 18099, was approved on 30 November 2024. ISO/IEC 25456 is still in "
        "development. Client-side liveness is therefore a bounded claim.",
    )

    # ------------------------------------------------------------- accuracy
    heading(doc, "8. Accuracy", 1)
    body(
        doc,
        "The rail is fast. It is not accurate in the way the headline volumes "
        "imply, and the error is not evenly distributed.",
    )
    table(
        doc,
        ["Metric", "Value", "Source"],
        [
            ["Monthly biometric authentications for welfare, banking and public services",
             "About 312 million", "Policy Circle, citing UIDAI parliamentary replies"],
            ["Monthly failures", "About 20.3 million", "same"],
            ["Failure rate", "6.5%, unchanged in over a decade; success 93.5 to 95%", "same"],
            ["Laboratory versus field", "Controlled accuracy 98 to 99% for fingerprint and iris; "
             "real world falls short by 3 to 6.5 percentage points", "UIDAI parliamentary replies"],
            ["Historical UIDAI figures", "Fingerprint 6% failure; iris 8.54%", "Identity Week citing UIDAI, 2018"],
            ["Face authentication share", "About 1.5 crore transactions in Sep 2025 against "
             "about 9.6 crore daily biometric authentications", "Policy Circle"],
            ["Seeding failures, not biometric", "About 30 million ration cards cancelled on "
             "name, address or legacy record mismatch", "Policy Circle"],
        ],
        widths=[2.0, 2.6, 1.4],
    )
    body(
        doc,
        "The distribution matters more than the average. Failures cluster among "
        "agricultural, construction and domestic workers whose fingerprints "
        "degrade with manual work, so the people most dependent on welfare are "
        "the most likely to fail authentication. No demographic breakdown of "
        "failure rates is published. The system measures success and does not "
        "measure exclusion.",
    )
    body(
        doc,
        "This explains section 6. Verification failure at national scale is not "
        "an edge case; it is a monthly event affecting more people than the "
        "combined population of Delhi and Mumbai.",
    )
    source_note(
        doc,
        "Sourcing caveat: Policy Circle is analysis by a public policy researcher "
        "citing UIDAI parliamentary responses, not a UIDAI release. Pull the "
        "underlying parliamentary answers before publishing the 6.5% figure.",
    )
    heading(doc, "8.1 Screening accuracy, where the cost sits", 2)
    table(
        doc,
        ["Metric", "Value", "Source"],
        [
            ["AML and sanctions screening false positive rate", "90 to 95%",
             "Accenture, McKinsey, ACAMS; Datos Insights"],
            ["Alternative benchmark", "85 to 90% false positive; 30 to 45 minutes per "
             "Level 1 review", "Everest Group 2025"],
            ["Regulatory shift", "Under direct EU supervision the test becomes whether "
             "escalation or dismissal was defensibly reasoned, so a 95% noise floor "
             "becomes a control deficiency rather than a resource constraint", "Reported Apr 2026"],
            ["Face match upper bound", "False non-match below 0.001 at a false match rate of "
             "0.00001, but only on conformant images", "NIST FRTE"],
            ["Human deepfake detection", "0.1% of study participants identified all deepfake "
             "and genuine content correctly", "iProov"],
        ],
        widths=[2.0, 2.6, 1.4],
    )
    body(
        doc,
        "The regulatory shift is the strongest available argument for spending "
        "agent cost on explanation. The obligation is not to reduce false "
        "positives; it is to show defensible reasoning for each alert. At 30 to "
        "45 minutes of analyst time per alert against a 90 to 95% noise rate, "
        "generating an auditable reason is where that cost is repaid.",
    )

    # ----------------------------------------------------------------- cost
    heading(doc, "9. Cost", 1)
    heading(doc, "9.1 What is published", 2)
    table(
        doc,
        ["Item", "Rate", "Source"],
        [
            ["Aadhaar e-KYC, requesting entity other than a telecom service provider",
             "Rs 3 per transaction", "Aadhaar (Payment of Fees for Performance of "
             "Authentication) Regulations 2023"],
            ["Telecom service provider e-KYC", "Rs 1", "same regulations"],
            ["Yes or No authentication", "Rs 0.50", "Aadhaar (Pricing) Regulations 2021"],
            ["Possibly superseded", "UIDAI Circular 15 of 2025 revised pricing with effect "
             "from Nov 2025; amounts not machine-readable", "UIDAI"],
            ["CKYC transaction", "About Rs 1 per upload, download or update, from a 2016 "
             "launch statement", "Weak sourcing, treat with caution"],
            ["CKYCRR 2.0", "Go-live tentatively August 2026; reporting entity mock run "
             "10 to 14 August 2026", "CERSAI circular, 29 July 2026"],
        ],
        widths=[2.0, 2.6, 1.4],
    )
    heading(doc, "9.2 Why vendor pricing cannot be used", 2)
    body(
        doc,
        "One vendor advertises eKYC from Rs 1 per verification on its India "
        "pricing page and from Rs 10 per verification on its global page, for the "
        "same product. Same company, ten times apart. That is the reason the "
        "cost-per-KYC gap in section 11 stands.",
    )
    heading(doc, "9.3 Global comparators", 2)
    bullets(
        doc,
        [
            "Average annual AML and KYC operations spend per institution: USD 72.9 "
            "million; United Kingdom 78.4 million, United States 72.2 million, "
            "Singapore 68.2 million (Fenergo, survey of 600 senior decision-makers, "
            "October 2025).",
            "Financial-crime compliance cost per institution in Asia Pacific: USD "
            "11.3 million annually, from 253 institutions across five markets "
            "including India (LexisNexis Risk Solutions).",
        ],
    )
    heading(doc, "9.4 The finding that matters", 2)
    body(
        doc,
        "The identity check costs three rupees. The screening review costs 30 to "
        "45 minutes of analyst time per alert at a 90 to 95% false positive rate. "
        "Cost per KYC in India is not dominated by identity verification. It is "
        "dominated by people clearing screening noise.",
    )
    body(
        doc,
        "That supports spending at the edges rather than the middle: rails and "
        "tools at the bottom where the marginal cost is rupees, reasoning at the "
        "top where the marginal cost is analyst hours. It also means the cost "
        "claim should never be that identity verification was made cheaper. It "
        "should be that the expensive part was made explainable.",
    )
    body(
        doc,
        "No primary Indian figure for cost per KYC exists. The defensible "
        "alternative is a measured cost per applicant taken from tagged resources "
        "in a cost report, published with the query behind it. A measured number "
        "is falsifiable; a benchmark that cannot be sourced is not.",
    )

    # ----------------------------------------------------------- regulation
    heading(doc, "10. The regulatory clock", 1)
    table(
        doc,
        ["Jurisdiction", "What changes", "When"],
        [
            ["India", "2016 KYC Master Direction repealed; 9,445 circulars replaced by 244 Master Directions", "28 Nov 2025"],
            ["India", "Low-risk re-KYC grace period expires", "30 Jun 2026, passed"],
            ["India", "Payment aggregators must do merchant KYC, may use CKYC rails", "15 Sep 2025"],
            ["India", "DPDP Rules notified; substantive obligations bite", "Nov 2025 to May 2027"],
            ["India", "RBI FREE-AI framework, 26 recommendations, human accountability retained", "13 Aug 2025"],
            ["United Kingdom", "Certified digital ID sufficient for MLR Regulation 28; uncertified unsuitable", "Feb 2026"],
            ["United Kingdom", "Companies House identity verification mandatory", "18 Nov 2025"],
            ["Australia", "Tranche 2 live; regulator says stop retaining full ID document copies", "1 Jul 2026"],
            ["Australia", "Private sector may join the government digital ID system", "30 Nov 2026"],
            ["Japan", "Photo-upload eKYC abolished; verification moves to My Number Card chip", "1 Apr 2027"],
            ["European Union", "Remote CDD must anchor in notified eID at substantial or high assurance", "10 Jul 2027"],
        ],
        widths=[1.3, 4.1, 1.2],
    )
    body(
        doc,
        "The thesis this supports: every demo jurisdiction is legislating its way "
        "towards what India already does \u2014 authenticate against a government "
        "credential instead of inspecting a document image.",
    )
    body(
        doc,
        "The counterpoint that keeps it honest: no regulator anywhere sanctions "
        "autonomous model-led approve or reject decisions. RBI FREE-AI, MAS AI "
        "Risk Management guidelines, FCA AI Live Testing and the FSB sound "
        "practices are all governance-based and all retain human accountability. "
        "That is independent support for a deterministic orchestrator rather than "
        "a model supervisor.",
    )

    # ------------------------------------------------------------------ gaps
    heading(doc, "11. Gaps", 1)

    heading(doc, "11.1 Blocking for the session", 2)
    numbers(
        doc,
        [
            "Cost per KYC in India. No regulator or government figure exists. "
            "Every rupee figure in circulation is vendor marketing.",
            "National video KYC session volume. Not published by any regulator, "
            "association or bank.",
            "Video KYC completion time from an authoritative source. Only one "
            "bank marketing claim of about three minutes. The 90-second claim "
            "therefore has no published Indian baseline to beat and should be "
            "framed against measured pipeline latency instead.",
            "Drop-off and abandonment rates. All vendor-sourced and circularly "
            "cited.",
        ],
    )

    heading(doc, "11.2 Published but not machine-readable", 2)
    numbers(
        doc,
        [
            "UIDAI monthly authentication and e-KYC series for Dec 2025 to Jul "
            "2026. Published only inside PDFs. Highest-value recoverable gap.",
            "Revised UIDAI per-transaction fees after Circular 15 of 2025. The "
            "older Rs 20, Rs 0.50 and Rs 1 rates may be superseded.",
            "Discrete annual e-KYC totals for FY2023-24 and FY2025-26; DBT "
            "transaction counts.",
            "Current KYC Registration Agency record counts. Last joint "
            "disclosure was April 2024.",
            "FIU-IND suspicious transaction report counts for FY24 to FY26.",
            "Aadhaar to EPIC linkage count for 2025-26. Freshest figure is "
            "December 2022.",
            "Gross new bank accounts per year; gross annual SIM activations; "
            "total accounts pending re-KYC across all banks.",
            "UIDAI Circulars 5 and 6 of 2026, which issued and then withdrew a "
            "position on mandatory Aadhaar for government certificates.",
        ],
    )

    heading(doc, "11.3 Figures to discard", 2)
    bullets(
        doc,
        [
            "\u201cOne in 72 deepfake attempts in Indian onboarding.\u201d Source "
            "URL returns 404 and the dataset is unidentified.",
            "\u201cRs 5 per Aadhaar authentication.\u201d Matches no UIDAI "
            "regulation.",
            "\u201cFY25 DBT crossed Rs 8.5 lakh crore.\u201d Contradicts the "
            "Finance Minister\u2019s Rs 6.83 lakh crore.",
            "The FATF India page ratings line. It indexes to an earlier "
            "evaluation round, not the 2024 mutual evaluation.",
            "Deposit Education and Awareness Fund figures for 2026. Three "
            "Parliament-sourced values are mutually inconsistent.",
        ],
    )

    heading(doc, "11.4 Research not completed", 2)
    body(
        doc,
        "Named vendor volumes, straight-through processing rates, and Indian bank "
        "onboarding case studies on cloud infrastructure remain open. Aadhaar "
        "biometric failure rates and the published fee schedule are now covered "
        "in sections 8 and 9.",
    )

    # --------------------------------------------------------------- closing
    heading(doc, "12. The numbers for the slide", 1)
    numbers(
        doc,
        [
            "2,707 crore Aadhaar authentications in FY2024-25. About 520 crore "
            "e-KYC a year. 2.31 billion authentications in November 2025 alone.",
            "About 14.3 crore Indians locked out of their own accounts, with "
            "Rs 74,580 crore of unclaimed deposits sitting behind incomplete "
            "verification.",
            "Rs 48,021 crore of bank fraud in FY2025-26, 32.08 lakh mule "
            "accounts on the national registry, and 65% of Indian organisations "
            "hit by deepfake attacks.",
            "Japan abolishes photo-upload eKYC on 1 April 2027. The European "
            "Union mandates notified eID for remote due diligence on 10 July "
            "2027. The United Kingdom made certified digital ID sufficient in "
            "February 2026. Australia told firms to stop keeping copies of "
            "identity documents.",
            "The rail is fast and it is 93.5 to 95% accurate, a figure that has "
            "not moved in a decade. About 20.3 million authentication failures a "
            "month, concentrated among manual workers whose fingerprints degrade "
            "with use, and no published demographic breakdown of who fails.",
            "The identity check costs three rupees. Clearing one screening alert "
            "costs 30 to 45 minutes of analyst time at a 90 to 95% false "
            "positive rate. The expensive part of KYC was never the identity "
            "check.",
        ],
    )

    heading(doc, "Sources", 1)
    body(
        doc,
        "Primary sources used throughout: UIDAI press releases and regulations; "
        "Reserve Bank of India circulars, Master Directions and Annual Report "
        "2025-26; Lok Sabha and Rajya Sabha written replies via sansad.in; "
        "Pradhan Mantri Jan Dhan Yojana dashboard; CKYCRR notifications; the "
        "joint press release of the KYC Registration Agencies; CDSL and AMFI "
        "disclosures; IRDAI Annual Report FY2024-25; TRAI press releases; "
        "Election Commission of India material and Supreme Court reporting; "
        "Indian Cyber Crime Coordination Centre figures; Press Information "
        "Bureau releases. Vendor research is attributed where used: Thales, "
        "iProov, Entrust, Sumsub, Fenergo, LexisNexis Risk Solutions. Vendor "
        "telemetry is proprietary and unaudited; label it as such when quoted.",
    )

    doc.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()
