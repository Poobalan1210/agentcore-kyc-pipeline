import { useState } from "react";
import { Button } from "@shared/components/Button";
import { AppScreen, StepHeader } from "./AppShell";
import { DETAILS_COPY } from "./copy";
import { COUNTRIES } from "./countries";
import { BLANK_IDENTITY } from "./demoPersonas";
import { DeclaredIdentity } from "./types";

/**
 * The applicant's declared details — the half of KYC that was missing entirely.
 *
 * Nothing downstream can work without this. `sanctions_exact_match` raises `MISSING_NAME` and
 * refuses to run without a name, so the Phase 1 pre-screen had nothing to screen; and the
 * declared-vs-document comparison — a core anti-fraud control — has no left-hand side unless the
 * applicant states who they claim to be.
 *
 * The form is the customer's: it carries no demo scaffolding. The presenter picks a persona from
 * the console, outside the phone, and its details arrive here filled in, so no path needs typing
 * on stage. Every field stays editable on purpose: changing the date of birth to a wrong value is
 * how the mismatch-detection path gets demonstrated.
 */
export function DetailsStep({
  initial,
  onBack,
  onContinue,
}: {
  /** The presenter's persona, or what was declared before coming back to this screen. */
  initial?: DeclaredIdentity | null;
  onBack: () => void;
  onContinue: (identity: DeclaredIdentity) => void;
}) {
  const [identity, setIdentity] = useState<DeclaredIdentity>(() => ({ ...(initial ?? BLANK_IDENTITY) }));
  const [error, setError] = useState<string | null>(null);

  function update<K extends keyof DeclaredIdentity>(field: K, value: string) {
    setIdentity((prev) => ({ ...prev, [field]: value }));
  }

  function handleContinue() {
    // Only presence is checked here, never plausibility. Whether the declared values are *true*
    // is decided by comparing them against the document, which is the point of collecting them —
    // rejecting an unusual-looking name in the browser would discard the very signal the pipeline
    // exists to evaluate.
    const missing = (
      [
        ["givenNames", DETAILS_COPY.givenNames],
        ["surname", DETAILS_COPY.surname],
        ["dateOfBirth", DETAILS_COPY.dateOfBirth],
        ["nationality", DETAILS_COPY.nationality],
      ] as Array<[keyof DeclaredIdentity, string]>
    ).filter(([field]) => !identity[field].trim());

    if (missing.length > 0) {
      setError(`${DETAILS_COPY.missing} ${missing.map(([, label]) => label.toLowerCase()).join(", ")}.`);
      return;
    }
    setError(null);
    onContinue({
      ...identity,
      // Normalised the way the document's machine-readable zone carries them, so the comparison
      // downstream is not just a case-sensitivity test.
      givenNames: identity.givenNames.trim().toUpperCase(),
      surname: identity.surname.trim().toUpperCase(),
      nationality: identity.nationality.trim().toUpperCase(),
    });
  }

  // A nationality the short list does not carry (typed into an earlier version, say) stays
  // selectable rather than silently changing.
  const nationalities = COUNTRIES.some((c) => c.iso3 === identity.nationality) || !identity.nationality
    ? COUNTRIES
    : [...COUNTRIES, { iso2: "", iso3: identity.nationality, name: identity.nationality }];

  return (
    <AppScreen
      header={<StepHeader index={1} onBack={onBack} />}
      title={DETAILS_COPY.title}
      lead={DETAILS_COPY.lead}
      footer={
        <Button variant="primary" className="app-cta" onClick={handleContinue}>
          {DETAILS_COPY.continue}
        </Button>
      }
    >
      <label className="app-field">
        <span>{DETAILS_COPY.givenNames}</span>
        <input
          value={identity.givenNames}
          onChange={(e) => update("givenNames", e.target.value)}
          autoComplete="given-name"
        />
      </label>
      <label className="app-field">
        <span>{DETAILS_COPY.surname}</span>
        <input
          value={identity.surname}
          onChange={(e) => update("surname", e.target.value)}
          autoComplete="family-name"
        />
      </label>
      <label className="app-field">
        <span>{DETAILS_COPY.dateOfBirth}</span>
        <input
          type="date"
          value={identity.dateOfBirth}
          onChange={(e) => update("dateOfBirth", e.target.value)}
          autoComplete="bday"
        />
      </label>
      <label className="app-field">
        <span>{DETAILS_COPY.nationality}</span>
        <select value={identity.nationality} onChange={(e) => update("nationality", e.target.value)}>
          <option value="">Select</option>
          {nationalities.map((c) => (
            <option key={c.iso3} value={c.iso3}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      <label className="app-field">
        <span>{DETAILS_COPY.address}</span>
        <input
          value={identity.address}
          onChange={(e) => update("address", e.target.value)}
          autoComplete="street-address"
        />
      </label>

      {error ? <p className="app-error" role="alert">{error}</p> : null}
    </AppScreen>
  );
}
