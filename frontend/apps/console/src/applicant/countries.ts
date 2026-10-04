/**
 * Countries the onboarding forms offer: where the applicant lives (the four jurisdictions this
 * build runs), their nationality (ISO 3166-1 alpha-3, as the MRZ prints it) and the country that
 * issued their ID (alpha-2, as the document's metadata records it).
 *
 * A short list rather than every country, kept to the demo personas' countries, the nationalities
 * the rules treat as high risk (so that path can be shown by changing one field) and a handful of
 * common ones, so the select reads like a real form without a 250-row scroll on stage.
 */
export interface Country {
  iso2: string;
  iso3: string;
  name: string;
}

export const COUNTRIES: Country[] = [
  { iso2: "AU", iso3: "AUS", name: "Australia" },
  { iso2: "BR", iso3: "BRA", name: "Brazil" },
  { iso2: "CA", iso3: "CAN", name: "Canada" },
  { iso2: "CN", iso3: "CHN", name: "China" },
  { iso2: "FR", iso3: "FRA", name: "France" },
  { iso2: "DE", iso3: "DEU", name: "Germany" },
  { iso2: "IN", iso3: "IND", name: "India" },
  { iso2: "IR", iso3: "IRN", name: "Iran" },
  { iso2: "IE", iso3: "IRL", name: "Ireland" },
  { iso2: "IT", iso3: "ITA", name: "Italy" },
  { iso2: "JP", iso3: "JPN", name: "Japan" },
  { iso2: "MX", iso3: "MEX", name: "Mexico" },
  { iso2: "MM", iso3: "MMR", name: "Myanmar" },
  { iso2: "NL", iso3: "NLD", name: "Netherlands" },
  { iso2: "NZ", iso3: "NZL", name: "New Zealand" },
  { iso2: "KP", iso3: "PRK", name: "North Korea" },
  { iso2: "PH", iso3: "PHL", name: "Philippines" },
  { iso2: "SG", iso3: "SGP", name: "Singapore" },
  { iso2: "ZA", iso3: "ZAF", name: "South Africa" },
  { iso2: "KR", iso3: "KOR", name: "South Korea" },
  { iso2: "ES", iso3: "ESP", name: "Spain" },
  { iso2: "UA", iso3: "UKR", name: "Ukraine" },
  { iso2: "GB", iso3: "GBR", name: "United Kingdom" },
  { iso2: "US", iso3: "USA", name: "United States" },
];

/** The jurisdictions this build runs, as the intake offers them, in the order a US talk wants. */
export const RESIDENCES: Array<{ jurisdiction: string; iso2: string }> = [
  { jurisdiction: "us", iso2: "US" },
  { jurisdiction: "uk", iso2: "GB" },
  { jurisdiction: "au", iso2: "AU" },
  { jurisdiction: "jp", iso2: "JP" },
];

/** The ISO alpha-2 code of a jurisdiction's own country (`uk` is `GB`). */
export function iso2For(jurisdiction: string): string {
  return RESIDENCES.find((r) => r.jurisdiction === jurisdiction)?.iso2 ?? jurisdiction.toUpperCase();
}

export function countryName(code: string | null | undefined): string {
  if (!code) return "—";
  const upper = code.toUpperCase();
  return COUNTRIES.find((c) => c.iso2 === upper || c.iso3 === upper)?.name ?? upper;
}
