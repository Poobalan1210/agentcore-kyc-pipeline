/** Steps in the applicant onboarding flow — task 14.5, R32, D-37.

 * The order a bank's digital onboarding uses: who you are, then the ID document, then a selfie
 * video that is compared with the document's photo, then one look at everything before it is sent.
 *
 * `intake` — welcome: where the applicant lives, which account, and consent (the face check too).
 * `details` — the declared identity.
 * `documents` — the ID document: photographed with the camera or uploaded, validated client-side
 *   (R23.1, R23.2, F-23) and uploaded before the selfie. Nothing is submitted yet.
 * `liveness` — Face Liveness capture (F-39: client + backend, never server-only).
 * `review` — everything the application carries, with a way to change each part, then Submit.
 * `submitting` — POST /applicants issued, waiting on the 202.
 * `status` — polling GET /cases/{id}: progress while the decision is made (R32.7), then the
 *   decision and, once it exists, the account itself (R31.9). Never stops polling, so later
 *   changes — limits lifted by Phase 2, a hold after a list update — appear by themselves.
 * `error` — a step failed in a way the applicant must be told about, with a path back.
 */
export type FlowStep =
  | "intake"
  | "details"
  | "documents"
  | "liveness"
  | "review"
  | "submitting"
  | "status"
  | "error";

/**
 * What the applicant *declares* about themselves, as distinct from what the document *proves*.
 *
 * Both are needed and they are deliberately separate. Sanctions screening runs against the
 * declared identity — `sanctions_exact_match` refuses to run at all without a name — and the
 * comparison between declared and document-extracted values is itself one of the core KYC
 * controls: a mismatch between what someone types and what their document says is a fraud
 * signal, and it cannot exist if only one of the two is captured.
 *
 * Surname and given names are separate fields rather than one "full name" because the document's
 * machine-readable zone carries them separately, and splitting a full name back apart is
 * ambiguous for exactly the compound surnames and patronymics this system has to handle.
 */
export interface DeclaredIdentity {
  givenNames: string;
  surname: string;
  dateOfBirth: string; // ISO yyyy-mm-dd
  nationality: string; // ISO 3166-1 alpha-3, as printed in the MRZ
  address: string;
  /** SSN or ITIN (`taxId.ts`). Sent once with the application and never kept in the browser:
   * `live/applications.ts` drops it before saving a tab. */
  taxId?: string;
}

export interface DocumentDraft {
  file: File;
  type: string;
  issuingCountry: string;
  /** How the image got here: a photo taken with the camera, or a file the applicant chose. */
  source?: "camera" | "upload";
  uploadedKey?: string;
  uploadedBucket?: string;
}

export interface IntakeSelection {
  jurisdiction: string;
  productApplied: string;
}
