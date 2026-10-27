/**
 * The taxpayer identification number a U.S. person gives when opening an account (CIP, 31 CFR
 * 1020.220). Mirrors `api/applicants.py`'s `_tax_id`, which is the rule that counts: this copy
 * only spares the applicant a round trip, so a number it accepts must be one the API accepts.
 *
 * Format only. Whether the number belongs to the applicant is SSA's question to answer.
 */

/** ITIN group numbers (the fourth and fifth digits), per the IRS. */
function isItinGroup(group: number): boolean {
  return (
    (group >= 50 && group <= 65) ||
    (group >= 70 && group <= 88) ||
    (group >= 90 && group <= 92) ||
    (group >= 94 && group <= 99)
  );
}

export function taxIdKind(raw: string): "SSN" | "ITIN" | null {
  const digits = raw.replace(/[\s-]/g, "");
  if (!/^\d{9}$/.test(digits)) return null;
  const area = digits.slice(0, 3);
  const group = Number(digits.slice(3, 5));
  const serial = digits.slice(5);
  if (area.startsWith("9")) return isItinGroup(group) ? "ITIN" : null;
  if (area === "000" || area === "666" || group === 0 || serial === "0000") return null;
  return "SSN";
}

/** A U.S. national applying to the U.S. entity; anyone else may rely on their passport. */
export function taxIdRequired(jurisdiction: string, nationality: string): boolean {
  return jurisdiction === "us" && nationality.trim().toUpperCase() === "USA";
}

/** What a screen shows of the number: its last four digits, never the rest. */
export function maskTaxId(raw: string | null | undefined): string {
  const digits = (raw ?? "").replace(/\D/g, "");
  return digits.length >= 4 ? `•••-••-${digits.slice(-4)}` : "";
}
