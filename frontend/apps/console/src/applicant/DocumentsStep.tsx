import { useEffect, useRef, useState } from "react";
import { Button } from "@shared/components/Button";
import { api } from "@shared/api/client";
import { Icon } from "../console/icons";
import { AppScreen, StepHeader, WaitingArt } from "./AppShell";
import { DOCUMENT_COPY, DOCUMENT_TYPE_COPY } from "./copy";
import { COUNTRIES, iso2For } from "./countries";
import { DocumentCapture } from "./DocumentCapture";
import { validateDocument } from "./documentValidation";
import { DocumentDraft } from "./types";

/**
 * Base64-encoded SHA-256 of a file, in the form S3's `x-amz-checksum-sha256` header expects.
 *
 * Web Crypto (`crypto.subtle`) is built into the browser and deliberately exposes no MD5, which
 * is why SHA-256 is used here rather than the `Content-MD5` header S3 also accepts — this keeps
 * the applicant app free of a hashing dependency. `crypto.subtle` requires a secure context;
 * `localhost` counts as one, so this works in local development as well as over HTTPS.
 */
async function sha256Base64(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  const bytes = new Uint8Array(digest);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

/**
 * The ID document — task 14.5, R32.6, R23.1/R23.2/F-23, D-37.
 *
 * As a bank's onboarding does it: which document and who issued it, then a photo of it, taken
 * with the camera (`DocumentCapture`) or chosen from a file, then a look at the photo before it
 * is used. The upload happens here, before the selfie; nothing is submitted until the review.
 *
 * Every file is validated client-side (`documentValidation.ts`) the moment it is chosen, per
 * R32.6 — rejection is immediate, before any presigned URL is even requested.
 */
export function DocumentsStep({
  jurisdiction,
  applicationId,
  initialType,
  active = true,
  onBack,
  onComplete,
}: {
  jurisdiction: string;
  /** Keeps the uploaded object's S3 prefix aligned with the DynamoDB key the case is created
   * under — the server derives both from `APP-{sub}-{applicationId}`. */
  applicationId: string;
  /** The presenter's persona's document, or the one chosen before. */
  initialType?: string;
  /** This application is on screen; only then may the camera stage open. */
  active?: boolean;
  onBack: () => void;
  onComplete: (documents: DocumentDraft[]) => void;
}) {
  const [documentType, setDocumentType] = useState(initialType ?? "PASSPORT");
  const [issuingCountry, setIssuingCountry] = useState(iso2For(jurisdiction));
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState<"camera" | "upload">("camera");
  const [capturing, setCapturing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!file || !file.type.startsWith("image/")) {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  // Leaving the application (another tab, another view) closes the camera.
  useEffect(() => {
    if (!active) setCapturing(false);
  }, [active]);

  function accept(next: File, from: "camera" | "upload") {
    const check = validateDocument(next);
    if (!check.valid) {
      setError(check.reason ?? "This file cannot be used.");
      return;
    }
    setError(null);
    setFile(next);
    setSource(from);
  }

  function chooseFile() {
    setCapturing(false);
    inputRef.current?.click();
  }

  async function handleContinue() {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      // The evidence buckets have Object Lock with a default retention, and S3 rejects any PUT
      // into such a bucket that carries no integrity header. The checksum has to be known
      // *before* the presign so the API can include it in the signature — the browser cannot
      // add an unsigned `x-amz-checksum-*` header to an already-signed URL.
      const checksumSha256 = await sha256Base64(file);

      const presign = await api.presignDocument({
        jurisdiction,
        documentType,
        contentType: file.type,
        checksumSha256,
        applicationId,
        issuingCountry,
        // Every document in this demo is fabricated. Declared explicitly rather than inferred,
        // so a genuine upload would not silently inherit the marking.
        synthetic: true,
      });

      // The metadata was part of the signature, so it has to be replayed exactly. Building the
      // headers from the response rather than from local state guarantees that: re-deriving
      // them here would risk a divergence S3 reports only as an opaque signature mismatch.
      const metadataHeaders: Record<string, string> = {};
      for (const [key, value] of Object.entries(presign.metadata ?? {})) {
        metadataHeaders[`x-amz-meta-${key}`] = value;
      }

      const putResponse = await fetch(presign.uploadUrl, {
        method: "PUT",
        headers: {
          "Content-Type": file.type,
          "x-amz-checksum-sha256": presign.checksumSha256,
          ...metadataHeaders,
        },
        body: file,
      });
      if (!putResponse.ok) {
        // S3 reports upload failures in an XML body, not a status line alone. Surfacing it is
        // the difference between a debuggable failure and "please try again" on a loop.
        const detail = await putResponse.text();
        throw new Error(`S3 upload failed (${putResponse.status}): ${detail.slice(0, 500)}`);
      }

      onComplete([
        {
          file,
          type: documentType,
          issuingCountry,
          source,
          uploadedKey: presign.key,
          uploadedBucket: presign.bucket,
        },
      ]);
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? `Upload failed: ${err.message}`
          : "Your document could not be uploaded. Please try again.",
      );
      // eslint-disable-next-line no-console
      console.error(err);
    } finally {
      setUploading(false);
    }
  }

  const fileInput = (
    <input
      ref={inputRef}
      type="file"
      accept="image/jpeg,image/png,application/pdf"
      hidden
      onChange={(e) => {
        const chosen = e.target.files?.[0];
        if (chosen) accept(chosen, "upload");
        e.target.value = "";
      }}
    />
  );

  // The file input stays first and mounted whichever screen shows: "upload instead" clicks it from
  // the camera stage, which then closes, and a remounted input would lose the file it opened.
  if (capturing && active) {
    // While the camera stage is open, the phone says what is happening, as an app would.
    return (
      <>
        {fileInput}
        <AppScreen header={<StepHeader index={2} onBack={() => setCapturing(false)} />} centered>
          <WaitingArt icon="document" tone="live" />
          <h2 className="app-title">{DOCUMENT_COPY.capturing}</h2>
          <p className="app-lead">{DOCUMENT_COPY.capturingHint}</p>
        </AppScreen>
        <DocumentCapture
          documentType={documentType}
          onCapture={(captured) => {
            setCapturing(false);
            accept(captured, "camera");
          }}
          onCancel={() => setCapturing(false)}
          onUploadInstead={chooseFile}
        />
      </>
    );
  }

  // The main action is always the button at the bottom, as in a banking app: take the photo
  // until there is one, then send it. The photo, once taken, is the first thing on the screen.
  const footer = file ? (
    <Button
      variant="primary"
      className="app-cta"
      onClick={handleContinue}
      loading={uploading}
    >
      {uploading ? DOCUMENT_COPY.uploading : DOCUMENT_COPY.continue}
    </Button>
  ) : (
    <>
      <Button variant="primary" className="app-cta" onClick={() => setCapturing(true)}>
        <Icon name="document" size={18} /> {DOCUMENT_COPY.takePhoto}
      </Button>
      <button type="button" className="app-link app-link--center" onClick={chooseFile}>
        {DOCUMENT_COPY.upload}
      </button>
    </>
  );

  const preview = file ? (
    <div className="app-preview app-preview--top">
      {previewUrl ? (
        <img src={previewUrl} alt="The photo of your ID" />
      ) : (
        <div className="app-preview__pdf">PDF</div>
      )}
      <div className="app-preview__bar">
        <span className="app-preview__ok">
          <Icon name="check" size={15} /> {DOCUMENT_TYPE_COPY[documentType]?.title ?? "ID"}
        </span>
        <button
          type="button"
          className="app-link"
          disabled={uploading}
          onClick={() => (source === "camera" ? setCapturing(true) : chooseFile())}
        >
          {source === "camera" ? DOCUMENT_COPY.retake : DOCUMENT_COPY.replace}
        </button>
      </div>
    </div>
  ) : null;

  const form = (
    <AppScreen
      header={<StepHeader index={2} onBack={onBack} />}
      title={DOCUMENT_COPY.title}
      lead={DOCUMENT_COPY.lead}
      footer={footer}
    >
      {preview}
      {file?.type === "application/pdf" ? (
        <p className="app-warning" role="status">
          {DOCUMENT_COPY.pdfSlowPath}
        </p>
      ) : null}
      {error ? <p className="app-error" role="alert">{error}</p> : null}

      <p className="app-label">{DOCUMENT_COPY.typeLabel}</p>
      <div className="app-choices" role="radiogroup" aria-label={DOCUMENT_COPY.typeLabel}>
        {Object.entries(DOCUMENT_TYPE_COPY).map(([code, copy]) => (
          <button
            key={code}
            type="button"
            role="radio"
            aria-checked={documentType === code}
            className={"app-choice" + (documentType === code ? " app-choice--selected" : "")}
            onClick={() => setDocumentType(code)}
          >
            <span className="app-choice__icon">
              <Icon name={code === "PASSPORT" ? "globe" : "document"} size={20} />
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

      <label className="app-field">
        <span>{DOCUMENT_COPY.issuerLabel}</span>
        <select value={issuingCountry} onChange={(e) => setIssuingCountry(e.target.value)}>
          {COUNTRIES.map((c) => (
            <option key={c.iso2} value={c.iso2}>
              {c.name}
            </option>
          ))}
        </select>
      </label>

      {file ? null : (
        <div className="app-capture">
          <span className="app-capture__art">
            <Icon name="document" size={30} />
          </span>
          <p className="app-capture__hint">{DOCUMENT_COPY.previewHint}</p>
        </div>
      )}
    </AppScreen>
  );

  return (
    <>
      {fileInput}
      {form}
    </>
  );
}
