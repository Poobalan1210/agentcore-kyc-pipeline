/** Steps in the applicant capture flow — task 14.5, R32.

 * `intake` — jurisdiction and product selection.
 * `liveness` — Face Liveness capture (F-39: client + backend, never server-only).
 * `documents` — client-side-validated upload (R23.1, R23.2, F-23).
 * `submitting` — POST /applicants issued, waiting on the 202.
 * `progress` — polling GET /cases/{id} while Phase 1 runs (R32.7: live progress + elapsed time).
 * `result` — provisional decision rendered (R31.9: outcome, restrictions, and that they are
 *   temporary).
 * `error` — a step failed in a way the applicant must be told about, with a path back.
 */
export type FlowStep =
  | "intake"
  | "details"
  | "liveness"
  | "documents"
  | "submitting"
  | "progress"
  | "result"
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
}

export interface DocumentDraft {
  file: File;
  type: string;
  issuingCountry: string;
  uploadedKey?: string;
  uploadedBucket?: string;
}

export interface IntakeSelection {
  jurisdiction: string;
  productApplied: string;
}
