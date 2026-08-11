import { useState } from "react";
import { Button } from "@shared/components/Button";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { DeclaredIdentity } from "./types";
import { personaFor } from "./demoPersonas";

/**
 * The applicant's declared details — the half of KYC that was missing entirely.
 *
 * Nothing downstream can work without this. `sanctions_exact_match` raises `MISSING_NAME` and
 * refuses to run without a name, so the Phase 1 pre-screen had nothing to screen; and the
 * declared-vs-document comparison — a core anti-fraud control — has no left-hand side unless the
 * applicant states who they claim to be.
 *
 * Pre-filled from the demo persona for the chosen jurisdiction so the clean path needs no typing.
 * Every field stays editable on purpose: changing the date of birth to a wrong value is how the
 * mismatch-detection path gets demonstrated.
 */
export function DetailsStep({
  jurisdiction,
  onContinue,
}: {
  jurisdiction: string;
  onContinue: (identity: DeclaredIdentity) => void;
}) {
  const [identity, setIdentity] = useState<DeclaredIdentity>(() => personaFor(jurisdiction));
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
        ["givenNames", "given names"],
        ["surname", "surname"],
        ["dateOfBirth", "date of birth"],
        ["nationality", "nationality"],
      ] as Array<[keyof DeclaredIdentity, string]>
    ).filter(([field]) => !identity[field].trim());

    if (missing.length > 0) {
      setError(`Please provide your ${missing.map(([, label]) => label).join(", ")}.`);
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

  return (
    <Card>
      <CardHeader>
        <h2>Your details</h2>
      </CardHeader>
      <CardBody>
        <p className="kyc-hint">
          Enter your details exactly as they appear on the identity document you are about to
          upload. We check the two against each other.
        </p>

        <div className="kyc-form-row">
          <label htmlFor="givenNames">Given names</label>
          <input
            id="givenNames"
            value={identity.givenNames}
            onChange={(e) => update("givenNames", e.target.value)}
            autoComplete="given-name"
          />
        </div>

        <div className="kyc-form-row">
          <label htmlFor="surname">Surname</label>
          <input
            id="surname"
            value={identity.surname}
            onChange={(e) => update("surname", e.target.value)}
            autoComplete="family-name"
          />
        </div>

        <div className="kyc-form-row">
          <label htmlFor="dateOfBirth">Date of birth</label>
          <input
            id="dateOfBirth"
            type="date"
            value={identity.dateOfBirth}
            onChange={(e) => update("dateOfBirth", e.target.value)}
            autoComplete="bday"
          />
        </div>

        <div className="kyc-form-row">
          <label htmlFor="nationality">Nationality (3-letter code)</label>
          <input
            id="nationality"
            value={identity.nationality}
            onChange={(e) => update("nationality", e.target.value.toUpperCase())}
            maxLength={3}
            autoComplete="country"
          />
        </div>

        <div className="kyc-form-row">
          <label htmlFor="address">Residential address</label>
          <input
            id="address"
            value={identity.address}
            onChange={(e) => update("address", e.target.value)}
            autoComplete="street-address"
          />
        </div>

        {error ? <p className="kyc-error-text">{error}</p> : null}

        <Button variant="primary" onClick={handleContinue}>
          Continue
        </Button>
      </CardBody>
    </Card>
  );
}
