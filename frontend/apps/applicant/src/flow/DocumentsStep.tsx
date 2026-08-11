import { useRef, useState } from "react";
import { Button } from "@shared/components/Button";
import { Badge } from "@shared/components/Badge";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { api } from "@shared/api/client";
import { DocumentDraft } from "./types";
import { MAX_DOCUMENTS, validateDocument, validateDocumentCount } from "./documentValidation";

const DOCUMENT_TYPES = [
  { code: "PASSPORT", label: "Passport" },
  { code: "DRIVERS_LICENSE", label: "Driver's licence" },
  { code: "NATIONAL_ID", label: "National ID card" },
];

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
 * Document capture and upload — task 14.5, R32.6, R23.1/R23.2/F-23.
 *
 * Every file is validated client-side (`documentValidation.ts`) the moment it is selected, per
 * R32.6 — rejection is immediate, before any presigned URL is even requested, not discovered
 * after an upload completes.
 */
export function DocumentsStep({
  jurisdiction,
  applicationId,
  onComplete,
}: {
  jurisdiction: string;
  /** Keeps the uploaded object's S3 prefix aligned with the DynamoDB key the case is created
   * under — the server derives both from `APP-{sub}-{applicationId}`. */
  applicationId: string;
  onComplete: (documents: DocumentDraft[]) => void;
}) {
  const [drafts, setDrafts] = useState<DocumentDraft[]>([]);
  const [documentType, setDocumentType] = useState(DOCUMENT_TYPES[0].code);
  const [issuingCountry, setIssuingCountry] = useState(jurisdiction.toUpperCase());
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function handleFileSelect(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    const file = fileList[0];

    const countCheck = validateDocumentCount(drafts.length + 1);
    if (!countCheck.valid) {
      setError(countCheck.reason ?? "Too many documents.");
      return;
    }

    const fileCheck = validateDocument(file);
    if (!fileCheck.valid) {
      setError(fileCheck.reason ?? "This file cannot be used.");
      return;
    }

    setError(null);
    setDrafts((prev) => [...prev, { file, type: documentType, issuingCountry }]);
    if (inputRef.current) inputRef.current.value = "";
  }

  function removeDraft(index: number) {
    setDrafts((prev) => prev.filter((_, i) => i !== index));
  }

  async function handleSubmit() {
    if (drafts.length === 0) {
      setError("Please add at least one document.");
      return;
    }
    setUploading(true);
    setError(null);
    try {
      const uploaded: DocumentDraft[] = [];
      for (const draft of drafts) {
        // The evidence buckets have Object Lock with a default retention, and S3 rejects any PUT
        // into such a bucket that carries no integrity header. The checksum has to be known
        // *before* the presign so the API can include it in the signature — the browser cannot
        // add an unsigned `x-amz-checksum-*` header to an already-signed URL.
        const checksumSha256 = await sha256Base64(draft.file);

        const presign = await api.presignDocument({
          jurisdiction,
          documentType: draft.type,
          contentType: draft.file.type,
          checksumSha256,
          applicationId,
          issuingCountry: draft.issuingCountry,
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
            "Content-Type": draft.file.type,
            "x-amz-checksum-sha256": presign.checksumSha256,
            ...metadataHeaders,
          },
          body: draft.file,
        });
        if (!putResponse.ok) {
          // S3 reports upload failures in an XML body, not a status line alone. Surfacing it is
          // the difference between a debuggable failure and "please try again" on a loop.
          const detail = await putResponse.text();
          throw new Error(`S3 upload failed (${putResponse.status}): ${detail.slice(0, 500)}`);
        }

        uploaded.push({ ...draft, uploadedKey: presign.key, uploadedBucket: presign.bucket });
      }
      onComplete(uploaded);
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? `Upload failed: ${err.message}`
          : "One of your documents could not be uploaded. Please try again.",
      );
      // eslint-disable-next-line no-console
      console.error(err);
    } finally {
      setUploading(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <h2>Upload an identity document</h2>
      </CardHeader>
      <CardBody>
        <p className="kyc-hint">
          JPEG, PNG, or PDF. Up to {MAX_DOCUMENTS} files, 25 MB each.
        </p>

        <div className="kyc-form-row">
          <label htmlFor="doc-type">Document type</label>
          <select id="doc-type" value={documentType} onChange={(e) => setDocumentType(e.target.value)}>
            {DOCUMENT_TYPES.map((t) => (
              <option key={t.code} value={t.code}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
        <div className="kyc-form-row">
          <label htmlFor="doc-country">Issuing country</label>
          <input
            id="doc-country"
            value={issuingCountry}
            onChange={(e) => setIssuingCountry(e.target.value.toUpperCase())}
            maxLength={2}
          />
        </div>

        <input
          ref={inputRef}
          type="file"
          accept="image/jpeg,image/png,application/pdf"
          onChange={(e) => handleFileSelect(e.target.files)}
        />

        {error ? <p className="kyc-error-text">{error}</p> : null}

        {drafts.length > 0 ? (
          <ul className="kyc-document-list">
            {drafts.map((draft, index) => (
              <li key={index}>
                <span>{draft.file.name}</span>
                <Badge tone="neutral">{draft.type}</Badge>
                <button
                  type="button"
                  className="kyc-remove-link"
                  onClick={() => removeDraft(index)}
                  aria-label={`Remove ${draft.file.name}`}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        <Button variant="primary" onClick={handleSubmit} loading={uploading} disabled={drafts.length === 0}>
          Continue
        </Button>
      </CardBody>
    </Card>
  );
}
