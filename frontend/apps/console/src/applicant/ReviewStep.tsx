import { useEffect, useState } from "react";
import { Button } from "@shared/components/Button";
import { Icon } from "../console/icons";
import { AppScreen, StepHeader } from "./AppShell";
import { DOCUMENT_TYPE_COPY, PRODUCT_COPY, REVIEW_COPY } from "./copy";
import { countryName, iso2For } from "./countries";
import { DeclaredIdentity, DocumentDraft, IntakeSelection } from "./types";

/**
 * One look at everything the application carries before it is sent, with a way to change each
 * part: the declared details, the ID document and the selfie video. Nothing reaches the bank
 * until Submit, which is the moment the four agents start (D-37).
 */
export function ReviewStep({
  identity,
  selection,
  documents,
  onEditDetails,
  onRetakeDocument,
  onRedoSelfie,
  onSubmit,
}: {
  identity: DeclaredIdentity;
  selection: IntakeSelection;
  documents: DocumentDraft[];
  onEditDetails: () => void;
  onRetakeDocument: () => void;
  onRedoSelfie: () => void;
  onSubmit: () => void;
}) {
  const document = documents[0];
  const [thumb, setThumb] = useState<string | null>(null);

  useEffect(() => {
    if (!document || !document.file.type.startsWith("image/")) {
      setThumb(null);
      return;
    }
    const url = URL.createObjectURL(document.file);
    setThumb(url);
    return () => URL.revokeObjectURL(url);
  }, [document]);

  return (
    <AppScreen
      header={<StepHeader index={4} />}
      title={REVIEW_COPY.title}
      lead={REVIEW_COPY.lead}
      footer={
        <>
          <p className="app-fine">{REVIEW_COPY.declaration}</p>
          <Button variant="primary" className="app-cta" onClick={onSubmit}>
            {REVIEW_COPY.submit}
          </Button>
        </>
      }
    >
      <section className="app-card">
        <div className="app-card__head">
          <span>{REVIEW_COPY.aboutYou}</span>
          <button type="button" className="app-link" onClick={onEditDetails}>
            {REVIEW_COPY.edit}
          </button>
        </div>
        <dl className="app-rows">
          <div>
            <dt>{REVIEW_COPY.name}</dt>
            <dd>{titleCase(`${identity.givenNames} ${identity.surname}`)}</dd>
          </div>
          <div>
            <dt>{REVIEW_COPY.dob}</dt>
            <dd>{formatDate(identity.dateOfBirth)}</dd>
          </div>
          <div>
            <dt>{REVIEW_COPY.nationality}</dt>
            <dd>{countryName(identity.nationality)}</dd>
          </div>
          {identity.address ? (
            <div>
              <dt>{REVIEW_COPY.address}</dt>
              <dd>{identity.address}</dd>
            </div>
          ) : null}
        </dl>
      </section>

      <section className="app-card">
        <div className="app-card__head">
          <span>{REVIEW_COPY.idDocument}</span>
          <button type="button" className="app-link" onClick={onRetakeDocument}>
            {REVIEW_COPY.retake}
          </button>
        </div>
        <div className="app-item">
          {thumb ? (
            <img className="app-item__thumb" src={thumb} alt="" />
          ) : (
            <span className="app-item__icon">
              <Icon name="document" size={20} />
            </span>
          )}
          <span className="app-item__text">
            {DOCUMENT_TYPE_COPY[document?.type ?? ""]?.title ?? "ID document"}
            <small>{countryName(document?.issuingCountry)}</small>
          </span>
        </div>
      </section>

      <section className="app-card">
        <div className="app-card__head">
          <span>{REVIEW_COPY.selfie}</span>
          <button type="button" className="app-link" onClick={onRedoSelfie}>
            {REVIEW_COPY.redo}
          </button>
        </div>
        <div className="app-item">
          <span className="app-item__icon">
            <Icon name="check" size={20} />
          </span>
          <span className="app-item__text">{REVIEW_COPY.selfieDone}</span>
        </div>
      </section>

      <section className="app-card">
        <div className="app-card__head">
          <span>{REVIEW_COPY.account}</span>
        </div>
        <div className="app-item">
          <span className="app-item__text">
            {PRODUCT_COPY[selection.productApplied]?.title ?? selection.productApplied}
            <small>{countryName(iso2For(selection.jurisdiction))}</small>
          </span>
        </div>
      </section>
    </AppScreen>
  );
}

function titleCase(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/(^|[\s-])([a-z])/g, (_, sep: string, ch: string) => sep + ch.toUpperCase());
}

/** A date of birth as a reader expects it, not as the form stores it. */
export function formatDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso || "—";
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}
