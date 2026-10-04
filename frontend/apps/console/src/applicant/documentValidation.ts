/**
 * Client-side document validation — R23.1, R23.2, R32.6, F-23.
 *
 * "SHALL validate format and size **client-side before upload**" (R32.6) is the operative word:
 * this must run before any presigned PUT, not as a courtesy check that the backend also
 * re-validates. The backend (`api/applicants.py`) deliberately does not re-check format/size on
 * presign — R32.6 places that responsibility here, and a malformed document that slips past the
 * browser anyway still fails visibly inside the Phase 1 state machine (design §10.2's "fail
 * loudly" principle applied one layer up).
 */

export const MAX_DOCUMENTS = 5; // F-23
export const MAX_MEDIA_BYTES = 25 * 1024 * 1024; // F-23: 25 MB direct upload limit

const ALLOWED_MIME_TYPES = new Set(["image/jpeg", "image/png", "application/pdf"]);

//: F-23 — no CMYK or SVG in PDFs, no JPEG2000, no JBIG2. Browsers do not expose colour-space or
//: sub-format metadata through the File API without decoding the file, so this checks what is
//: cheaply knowable client-side (MIME type, extension, size) and documents the gap rather than
//: silently claiming full coverage of F-23's list.
const REJECTED_EXTENSIONS = [".svg", ".jp2", ".jpx", ".jpf", ".jbig2", ".jb2"];

export interface ValidationResult {
  valid: boolean;
  reason?: string;
}

export function validateDocument(file: File): ValidationResult {
  const lowerName = file.name.toLowerCase();

  if (REJECTED_EXTENSIONS.some((ext) => lowerName.endsWith(ext))) {
    return {
      valid: false,
      reason:
        "This file type is not accepted. Please upload a JPEG, PNG, or standard PDF — " +
        "SVG, JPEG2000, and JBIG2 files cannot be processed.",
    };
  }

  if (!ALLOWED_MIME_TYPES.has(file.type)) {
    return {
      valid: false,
      reason: `"${file.type || "unknown"}" is not a supported format. Please upload a JPEG, PNG, or PDF.`,
    };
  }

  if (file.size > MAX_MEDIA_BYTES) {
    const mb = (file.size / (1024 * 1024)).toFixed(1);
    return {
      valid: false,
      reason: `This file is ${mb} MB, which is over the 25 MB limit. Please use a smaller scan or photo.`,
    };
  }

  return { valid: true };
}

export function validateDocumentCount(count: number): ValidationResult {
  if (count > MAX_DOCUMENTS) {
    return { valid: false, reason: `You can upload at most ${MAX_DOCUMENTS} documents.` };
  }
  return { valid: true };
}
