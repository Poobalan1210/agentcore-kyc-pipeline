import { describe, expect, it } from "vitest";
import { maskTaxId, taxIdKind, taxIdRequired } from "./taxId";

// The same cases as `api/tests/test_applicants.py::TestTaxId`: the two rules must agree, or a
// number the form accepts is refused by the API on submit.
describe("taxIdKind", () => {
  it.each([
    ["123-45-6789", "SSN"],
    ["123456789", "SSN"],
    ["987-65-4320", "ITIN"],
    ["912-70-1234", "ITIN"],
  ])("accepts %s as %s", (raw, kind) => {
    expect(taxIdKind(raw)).toBe(kind);
  });

  it.each(["000-12-3456", "666-12-3456", "123-00-4567", "123-45-0000", "912-93-1234", "12345678", "12345678a"])(
    "refuses %s",
    (raw) => {
      expect(taxIdKind(raw)).toBeNull();
    },
  );
});

describe("taxIdRequired", () => {
  it("is required of a U.S. national applying in the U.S. only", () => {
    expect(taxIdRequired("us", "USA")).toBe(true);
    expect(taxIdRequired("us", "GBR")).toBe(false);
    expect(taxIdRequired("uk", "USA")).toBe(false);
  });
});

describe("maskTaxId", () => {
  it("shows only the last four digits", () => {
    expect(maskTaxId("987-65-4320")).toBe("•••-••-4320");
    expect(maskTaxId("")).toBe("");
  });
});
