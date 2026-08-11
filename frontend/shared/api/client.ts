import { fetchAuthSession } from "aws-amplify/auth";

/**
 * Thin wrapper over the Public API (task 14.3). Attaches the Cognito ID token as a Bearer
 * token — the HTTP API's JWT authorizer (`api_stack.py`) validates it against the user pool
 * issuer/audience already configured in each app's `main.tsx`.
 *
 * No AWS SDK calls happen here. R33.7 restricts AWS credentials in the browser to the scoped
 * identity-pool credentials `FaceLivenessDetector` uses directly; every other capability —
 * presigned uploads, case status, review actions — goes through this API client instead of a
 * direct AWS SDK call from the browser.
 */

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL as string | undefined;

export class ApiError extends Error {
  constructor(public status: number, public body: unknown) {
    super(`API error ${status}`);
  }
}

async function authHeader(): Promise<Record<string, string>> {
  const session = await fetchAuthSession();
  const token = session.tokens?.idToken?.toString();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!API_BASE_URL) {
    throw new Error(
      "VITE_API_BASE_URL is not configured — set it in .env.local to the KycApi ApiUrl output",
    );
  }
  const headers = {
    "Content-Type": "application/json",
    ...(await authHeader()),
    ...(init?.headers as Record<string, string> | undefined),
  };
  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new ApiError(response.status, body);
  }
  return body as T;
}

export interface LivenessSessionResponse {
  sessionId: string;
  region: string;
}

export interface LivenessResultResponse {
  isLive: boolean;
}

export interface PresignResponse {
  uploadUrl: string;
  bucket: string;
  key: string;
  expiresInSeconds: number;
  /** Echoed back by the API — send this exact value as the `x-amz-checksum-sha256` header on the
   * PUT. It is part of the signature, so a recomputed or omitted value is rejected by S3. */
  checksumSha256: string;
  /** S3 user metadata that was included in the signature. Must be replayed verbatim as
   * `x-amz-meta-<key>` headers on the PUT — these are signed, so adding, dropping or changing any
   * of them makes S3 reject the request as a signature mismatch. */
  metadata: Record<string, string>;
}

export interface CreateApplicantResponse {
  applicantId: string;
  eventId: string;
}

export interface CaseStatusResponse {
  state: string;
  outcome?: string;
  reasonCode?: string;
  requiredItem?: string;
  phase2Pending?: string;
  ingressLatencyMs?: number;
}

/**
 * What the applicant declares about themselves, as opposed to what their document proves. Both
 * are required: sanctions screening runs against the declared identity, and the comparison
 * between declared and document-extracted values is itself a fraud control.
 */
export interface DeclaredIdentityPayload {
  givenNames: string;
  surname: string;
  dateOfBirth: string;
  nationality: string;
  address: string;
}

export interface QueuedCaseSummary {
  applicantId: string;
  caseId: string;
  jurisdiction: string;
  createdAt: string;
  /** Merged in by `review_api.py`'s `_peek_task_tokens` from the SQS message body (ReceiveMessage,
   * not Delete) — the Step Functions task token a decision must be submitted with to resume the
   * `ManualReview` state. Absent if the queue peek raced the message becoming visible again. */
  taskToken?: string;
  phase?: string;
}

export interface ReviewQueueResponse {
  depth: number;
  cases: QueuedCaseSummary[];
}

export interface AgentTraceEntry {
  agent: string;
  finding: Record<string, unknown>;
}

export interface DocumentSummary {
  type: string | null;
  issuingCountry: string | null;
}

export interface LivenessSummary {
  result: string | null;
  region: string | null;
  retrievedAt: string | null;
}

export interface ApplicantTrace {
  applicantId: string;
  caseId: string | null;
  state: string | null;
  jurisdiction: string | null;
  productApplied: string | null;
  createdAt: string | null;
  documents: DocumentSummary[];
  declaredIdentity: Record<string, string> | null;
  liveness: LivenessSummary | null;
}

export interface ExecutionStep {
  name: string;
  status: string;
  startedAt?: string;
  endedAt?: string;
}

export interface ExecutionTrace {
  executionArn: string;
  status: string | null;
  startDate: string;
  stopDate: string | null;
  steps: ExecutionStep[];
}

export interface CaseTraceResponse {
  applicant: ApplicantTrace;
  agents: AgentTraceEntry[];
  documentExtraction: Record<string, unknown> | null;
  decision: Record<string, unknown> | null;
  execution: ExecutionTrace | null;
}

export const api = {
  createLivenessSession: (jurisdiction: string) =>
    request<LivenessSessionResponse>("/liveness/session", {
      method: "POST",
      body: JSON.stringify({ jurisdiction }),
    }),

  /**
   * `applicationId` — not an applicantId. The server composes the real key as
   * `APP-{cognito sub}-{applicationId}`, so this value cannot be used to address another
   * applicant's case; it only distinguishes one application of *this* user from the next. It has
   * to be passed here as well as to `createApplicant` because the liveness result is persisted
   * before the case exists (F-40's 3-minute session window), and both writes must land on the
   * same record.
   */
  getLivenessResult: (sessionId: string, jurisdiction: string, applicationId: string) =>
    request<LivenessResultResponse>(
      `/liveness/${sessionId}/result?jurisdiction=${jurisdiction}` +
        `&applicationId=${encodeURIComponent(applicationId)}`,
    ),

  presignDocument: (params: {
    jurisdiction: string;
    documentType: string;
    contentType: string;
    /** Base64 SHA-256 of the file body. Required: the evidence buckets have Object Lock, and S3
     * refuses any PUT into them without an integrity header. */
    checksumSha256: string;
    /** Keeps the uploaded object's S3 prefix aligned with the case record's key. */
    applicationId: string;
    /** Stamped onto the object as S3 metadata so the Documents agent has fields to evaluate —
     * `s3_get_document` reads exactly this. */
    issuingCountry: string;
    /** Marks the object as fabricated at the storage layer, so the audit record can never
     * present a demo document as a real one. */
    synthetic?: boolean;
  }) =>
    request<PresignResponse>("/documents/presign", {
      method: "POST",
      body: JSON.stringify(params),
    }),

  createApplicant: (params: {
    jurisdiction: string;
    productApplied: string;
    documents: Array<{ bucket: string; key: string; type: string; issuingCountry: string }>;
    applicationId: string;
    declaredIdentity: DeclaredIdentityPayload;
  }) =>
    request<CreateApplicantResponse>("/applicants", {
      method: "POST",
      body: JSON.stringify(params),
    }),

  getCase: (applicantId: string) => request<CaseStatusResponse>(`/cases/${applicantId}`),

  getReviewQueue: () => request<ReviewQueueResponse>("/review/queue"),

  submitReviewDecision: (
    caseId: string,
    params: { applicantId: string; phase: string; action: string; rationale: string; taskToken: string },
  ) =>
    request<{ resolution: string }>(`/review/${caseId}/decision`, {
      method: "POST",
      body: JSON.stringify(params),
    }),

  /** Task 14.9 — the reviewer trace view: which agents ran, their structured findings (never raw
   * prose presented as the decision, R18.2), the document extraction, the decision record, and
   * the Step Functions timeline when one is available. */
  getCaseTrace: (caseId: string) => request<CaseTraceResponse>(`/review/${caseId}/trace`),

  getAudit: (applicantId: string) =>
    request<{ applicantId: string; decisions: unknown[] }>(`/audit/${applicantId}`),
};
