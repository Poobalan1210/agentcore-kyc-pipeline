import { useState } from "react";
import { Button } from "@shared/components/Button";
import { Icon } from "../console/icons";
import { AppScreen, BankMark, BrandBar } from "./AppShell";
import { PRODUCT_COPY, WELCOME_COPY } from "./copy";
import { countryName, RESIDENCES } from "./countries";
import { IntakeSelection } from "./types";

/**
 * The welcome screen: what the applicant will need, where they live, which account, and their
 * consent, the face check included, before anything about them is collected (D-37).
 *
 * Consent is an explicit tick, never pre-ticked: the selfie video is biometric data, and a bank
 * asks before it records one. The notice says where the video goes and what happens to our copy.
 */
export function IntakeStep({
  initial,
  onContinue,
}: {
  /** The presenter's persona, or what was chosen before coming back to this screen. */
  initial?: IntakeSelection | null;
  onContinue: (selection: IntakeSelection) => void;
}) {
  const [jurisdiction, setJurisdiction] = useState(initial?.jurisdiction ?? RESIDENCES[0].jurisdiction);
  const [productApplied, setProductApplied] = useState(initial?.productApplied ?? "current_account");
  const [consent, setConsent] = useState(false);
  const [showNotice, setShowNotice] = useState(false);

  return (
    <AppScreen
      header={<BrandBar />}
      footer={
        <Button
          variant="primary"
          className="app-cta"
          disabled={!consent}
          onClick={() => onContinue({ jurisdiction, productApplied })}
        >
          {WELCOME_COPY.start}
        </Button>
      }
    >
      <div className="app-hero">
        <BankMark large />
        <h2 className="app-title">{WELCOME_COPY.title}</h2>
      </div>
      <p className="app-lead">{WELCOME_COPY.lead}</p>

      <p className="app-label">{WELCOME_COPY.needHeading}</p>
      <ul className="app-needs">
        <li>
          <Icon name="document" size={19} /> {WELCOME_COPY.needDocument}
        </li>
        <li>
          <Icon name="face" size={19} /> {WELCOME_COPY.needCamera}
        </li>
        <li>
          <Icon name="clock" size={19} /> {WELCOME_COPY.needTime}
        </li>
      </ul>

      <label className="app-field">
        <span>{WELCOME_COPY.countryLabel}</span>
        <select value={jurisdiction} onChange={(e) => setJurisdiction(e.target.value)}>
          {RESIDENCES.map((r) => (
            <option key={r.jurisdiction} value={r.jurisdiction}>
              {countryName(r.iso2)}
            </option>
          ))}
        </select>
      </label>

      <p className="app-label">{WELCOME_COPY.productLabel}</p>
      <div className="app-choices" role="radiogroup" aria-label={WELCOME_COPY.productLabel}>
        {Object.entries(PRODUCT_COPY).map(([code, copy]) => (
          <button
            key={code}
            type="button"
            role="radio"
            aria-checked={productApplied === code}
            className={"app-choice" + (productApplied === code ? " app-choice--selected" : "")}
            onClick={() => setProductApplied(code)}
          >
            <span className="app-choice__icon">
              <Icon name={code === "savings_account" ? "account" : "cases"} size={20} />
            </span>
            <span className="app-choice__text">
              <span className="app-choice__title">{copy.title}</span>
              <span className="app-choice__sub">{copy.body}</span>
            </span>
            <span className="app-choice__tick" aria-hidden="true">
              <Icon name="check" size={13} />
            </span>
          </button>
        ))}
      </div>

      <label className="app-consent">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        <span>{WELCOME_COPY.consent}</span>
      </label>
      <button
        type="button"
        className="app-link"
        aria-expanded={showNotice}
        onClick={() => setShowNotice((v) => !v)}
      >
        <Icon name="info" size={15} /> {WELCOME_COPY.noticeToggle}
      </button>
      {showNotice ? <p className="app-notice">{WELCOME_COPY.notice}</p> : null}
    </AppScreen>
  );
}
