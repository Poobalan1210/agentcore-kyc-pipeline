"""ISO 3166-1 alpha-3 to alpha-2, for the countries this system can meet.

Screening lists record country as alpha-2 while a passport's machine-readable zone and the
applicant form use alpha-3, so the matcher needs a bridge. Deliberately a curated table rather
than a dependency: country only ever *corroborates* a screening alert (PROBABLE rather than
POSSIBLE, both of which go to a human), so an unmapped country degrades to "no corroboration" and
can never clear an alert that should have been raised.
"""

from __future__ import annotations

ALPHA3_TO_ALPHA2: dict[str, str] = {
    # The four supported jurisdictions, plus the British nationality classes ICAO 9303 defines.
    "USA": "US", "GBR": "GB", "GBD": "GB", "GBN": "GB", "GBO": "GB", "GBP": "GB", "GBS": "GB",
    "AUS": "AU", "JPN": "JP",
    # Countries that appear in the synthetic screening data.
    "RUS": "RU", "SYR": "SY", "GHA": "GH", "BLR": "BY", "PRK": "KP", "VEN": "VE", "CHN": "CN",
    "POL": "PL", "MYS": "MY", "NGA": "NG", "ITA": "IT", "ESP": "ES", "EGY": "EG", "UKR": "UA",
    "MEX": "MX",
    # Common nationalities among applicants in the four markets.
    "CAN": "CA", "IRL": "IE", "NZL": "NZ", "DEU": "DE", "D": "DE", "FRA": "FR", "NLD": "NL",
    "BEL": "BE", "CHE": "CH", "AUT": "AT", "SWE": "SE", "NOR": "NO", "DNK": "DK", "FIN": "FI",
    "PRT": "PT", "GRC": "GR", "ROU": "RO", "BGR": "BG", "HUN": "HU", "CZE": "CZ", "SVK": "SK",
    "IND": "IN", "PAK": "PK", "BGD": "BD", "LKA": "LK", "NPL": "NP", "PHL": "PH", "VNM": "VN",
    "THA": "TH", "IDN": "ID", "SGP": "SG", "KOR": "KR", "TWN": "TW", "HKG": "HK", "BRA": "BR",
    "ARG": "AR", "COL": "CO", "PER": "PE", "CHL": "CL", "ZAF": "ZA", "KEN": "KE", "ETH": "ET",
    "TUR": "TR", "ISR": "IL", "SAU": "SA", "ARE": "AE", "QAT": "QA", "IRN": "IR", "IRQ": "IQ",
    "AFG": "AF", "MMR": "MM", "CUB": "CU", "LBN": "LB", "JOR": "JO", "MAR": "MA", "DZA": "DZ",
    "TUN": "TN", "LBY": "LY", "SDN": "SD", "SOM": "SO", "YEM": "YE",
}

_ALPHA2 = frozenset(ALPHA3_TO_ALPHA2.values())


def to_alpha2(code: object) -> str | None:
    """Alpha-2 for an alpha-2 or alpha-3 code, or None when it cannot be resolved."""
    if not isinstance(code, str):
        return None
    value = code.strip().upper().replace("<", "")
    if not value:
        return None
    if len(value) == 2 and value.isalpha():
        return value
    return ALPHA3_TO_ALPHA2.get(value)


assert all(len(v) == 2 for v in ALPHA3_TO_ALPHA2.values())
