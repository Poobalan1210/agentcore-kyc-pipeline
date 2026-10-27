import { fetchAuthSession } from "aws-amplify/auth";

/**
 * Thin wrapper over the Public API (task 14.3). Attaches the Cognito ID token as a Bearer
 * token — the HTTP API's JWT authorizer (`api_stack.py`) validates it against the user pool
 * issuer/audience already configured in each app's `main.tsx`.
 *
 * No AWS SDK calls happen here. R33.7 restricts AWS credentials in the browser to the scoped
 * identity-pool credentials `FaceLivenessDetector` uses directly; every other capability —
 * presigned uploads, case status, review actions, the account, the demo controls — goes through
 * this API client instead of a direct AWS SDK call from the browser.
 *
 * The types below mirror what the Python handlers return. Where a handler deliberately withholds
 * something from a caller (the applicant never sees a reason code outside the step-up allowlist,
 * an account's holds, or a recorded fact), the type leaves it out too, so a component cannot
 * start depending on a field the server will never send it.
 */

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL as string | undefined;

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(`API error ${status}`);
  }

  /** The handler's machine-readable error code (`{"error": "..."}`), if it sent one. */
  get code(): string | undefined {
    const body = this.body as { error?: unknown } | null;
    return typeof body?.error === "string" ? body.error : undefined;
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
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // A gateway error page is not JSON. Keep the text so the error is still diagnosable.
      body = { error: "NON_JSON_RESPONSE", text: text.slice(0, 500) };
    }
  }
  if (!response.ok) {
    throw new ApiError(response.status, body);
  }
  return body as T;
}

// ------------------------------------------------------------------------------ applicant

/** The two Face Liveness challenges (D-36). The light one adds a sequence of flashing colours. */
export type LivenessChallenge = "FaceMovementAndLightChallenge" | "FaceMovementChallenge";

export interface LivenessSessionResponse {
  sessionId: string;
  region: string;
  /** What this session will ask of the applicant, so the page can say so. Optional: an API
   * deployed before D-36 omits it, and its sessions run the light challenge. */
  challenge?: LivenessChallenge;
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

/**
 * What the applicant is told about their application (`applicants._APPLICANT_STATES`). A status,
 * never the rule, tier or reason that produced it — telling a customer why their account is
 * limited can itself be an offence (tipping off).
 */
export type ApplicantStatus =
  | "SUBMITTED"
  | "CHECKING"
  | "MORE_INFORMATION_NEEDED"
  | "IN_REVIEW"
  | "OPEN_WITH_LIMITS"
  | "OPEN"
  | "ON_HOLD"
  | "DECLINED";

/** A message written for the applicant, already passed through the tipping-off guard. */
export interface ApplicantNotification {
  kind: string;
  text: string;
  at: string;
}

export interface CaseStatusResponse {
  /** The internal case state. Shown nowhere; `status` is what the applicant reads. */
  state: string;
  status: ApplicantStatus;
  /** True only when the applicant has been asked for something and may resubmit. */
  canContinue: boolean;
  /** Step-ups only, and only from an allowlist of reasons about what the applicant sent. */
  reasonCode?: string;
  requiredItem?: string;
  phase2Pending?: string;
  ingressLatencyMs?: number | null;
  liveness?: { result: string | null };
  notifications: ApplicantNotification[];
  createdAt?: string;
  resubmittedAt?: string;
  provisionalDecidedAt?: string;
  fullDecidedAt?: string;
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
  /** SSN or ITIN. Stored apart from the case; only its last four digits are ever returned. */
  taxId?: string;
}

// -------------------------------------------------------------------------------- account

export interface RestrictionDetail {
  code: string;
  /** Plain language, written to say what is limited and never why. */
  description: string;
}

/** `account_stub.AccountState`. */
export type AccountState = "OPEN_RESTRICTED" | "OPEN_ENHANCED" | "OPEN_STANDARD" | "FROZEN";

/**
 * The account as the platform holds it. The applicant receives the fields up to `updatedAt`;
 * reviewers and presenters also receive the platform's own view — posture, holds and recent
 * events — which name the risk event that placed a restriction.
 */
export interface AccountView {
  applicantId: string;
  state: AccountState;
  restrictions: string[];
  restrictionDetails: RestrictionDetail[];
  limits: { currency: string; depositCeiling: string; depositsRemaining: string | null };
  balance: string;
  cumulativeDeposits: string;
  openedAt: string;
  updatedAt: string;
  posture?: string;
  holds?: Array<{ holdId: string; posture: string }>;
  recentEvents?: Array<Record<string, unknown>>;
  decisionId?: string | null;
  version?: number;
}

export type TransactionType =
  | "DEPOSIT"
  | "DOMESTIC_TRANSFER"
  | "INTERNATIONAL_TRANSFER"
  | "THIRD_PARTY_PAYMENT"
  | "CASH_WITHDRAWAL";

export interface TransactionRecord {
  transactionId: string;
  at: string;
  type: string;
  amount: string;
  currency: string;
  allowed: boolean;
  refusedBy: string | null;
  reasonCode: string | null;
  counterparty: string | null;
}

/** A refusal is an answer, not an error: `allowed: false` and the restriction that refused. */
export interface TransactionResult {
  allowed: boolean;
  refusedBy: string | null;
  reasonCode: string | null;
  reason?: string;
  transaction: TransactionRecord;
  account: AccountView;
}

// ------------------------------------------------------------------------------- reviewer

export interface QueuedCaseSummary {
  applicantId: string;
  caseId: string;
  jurisdiction: string;
  createdAt: string;
  /** Which phase's review this is. Defaulted to `phase1` server-side for legacy rows. */
  phase: string;
  /** Why the case is here, in the state machine's terms (`ENGINE_MANUAL_REVIEW`, ...). */
  reason: string | null;
  /** When this review was queued. A Phase 3 review is queued long after the case was created. */
  queuedAt: string | null;
  hasBriefing: boolean;
  /** The Step Functions task token, written once to the case record by
   * `review/token_materializer.py` when the review is handed off. Absent for a moment after the
   * case is queued, and for good once the review window has expired. */
  taskToken?: string;
}

export interface ReviewQueueResponse {
  depth: number;
  cases: QueuedCaseSummary[];
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
  riskState: string | null;
  jurisdiction: string | null;
  productApplied: string | null;
  createdAt: string | null;
  documents: DocumentSummary[];
  declaredIdentity: Record<string, string> | null;
  /** The last four digits of the SSN or ITIN on file, or null when none was given. */
  taxIdLast4: string | null;
  liveness: LivenessSummary | null;
  livenessFailures: number | null;
  previousAttempts: number;
}

export type AlertClassification = "CONFIRMED" | "PROBABLE" | "POSSIBLE" | "NO_MATCH";

export interface ScreeningHit {
  entryId: string;
  matchedName: string;
  classification: AlertClassification;
  score?: number;
  programme?: string;
  reason: string;
  dobMatch?: boolean;
  countryMatch?: boolean;
  nameExact?: boolean;
  nameReordered?: boolean;
}

/** Every fact shares these. `performed: false` is a check that did not happen, and says why. */
export interface FactBase {
  performed?: boolean;
  status?: string;
  reasonCode?: string;
  reason?: string;
  recordedAt?: string;
  listVersion?: string;
  [field: string]: unknown;
}

export interface ScreeningFact extends FactBase {
  strongest?: string;
  hits?: ScreeningHit[];
  excluded?: Array<{ entryId: string; matchedName: string; reason: string }>;
  screened?: { names: string[]; dob: string | null; country: string | null };
  agentSuppliedName?: string | null;
  agentNameMatchesRecord?: boolean | null;
}

/** The facts the tools recorded and the rules decided on, by the name the console uses. */
export interface RecordedFacts {
  documentExtraction: FactBase | null;
  faceMatch: FactBase | null;
  screening: ScreeningFact | null;
  velocity: FactBase | null;
  pep: FactBase | null;
  adverseMedia: FactBase | null;
}

export interface Briefing {
  phase: string;
  text: string;
  generatedAt: string;
}

export interface ScreeningDisposition {
  entryId: string;
  classification: string;
  listVersion: string;
  phase: string;
  reviewerId: string;
  decidedAt: string;
}

/** What an agent said. Narrative only: the rules never read it. */
export interface AgentNarrative {
  phase: string;
  agent: string;
  status: string | null;
  stopReason: string | null;
  latencyMs: number | null;
  /** Tokens the run used, as the harness reported them. */
  totalTokens?: number | null;
  /** The model the run's task asked for. Absent on runs recorded before it was. */
  model?: string | null;
  reason: string | null;
  narrative: string | null;
}

export interface ReviewerNotification {
  kind: string;
  /** The message the Notify agent wrote: its reply's `body` when it answered in fields. */
  written: string;
  shown: string;
  replacedByGuard: boolean;
  /**
   * Why the guard replaced it (`api/notifications.py` `check`): `TIPPING_OFF`,
   * `CONTRADICTS_ACCOUNT`, `UNVERIFIABLE_DETAIL` or `NO_MESSAGE`. Null when it was sent as
   * written; absent from an API that predates it.
   */
  replacedBecause?: string | null;
  accountState: string | null;
  at: string;
}

export interface ExecutionStep {
  name: string;
  /** `SUCCEEDED`, `RUNNING` (entered, not yet exited) or `FAILED` (an attempt failed). */
  status: string;
  startedAt?: string;
  /** Absent while the state is still running. */
  endedAt?: string;
  /** Inside a Parallel or Map state: 1 for one level down. Absent at the top level. */
  depth?: number;
  /** What a task state called (`lambda:invoke`, `aws-sdk:dynamodb:updateItem`, ...). */
  resource?: string;
  /** For a Lambda function or an agent harness, its name. */
  target?: string;
}

export interface ExecutionTrace {
  executionArn: string;
  phase: string | null;
  status: string | null;
  startDate: string;
  stopDate: string | null;
  steps: ExecutionStep[];
}

/** The sealed decision record (`audit/record.py` `to_payload`): the decision is nested. */
export interface DecisionRecordPayload {
  decisionId: string;
  phase: string;
  decidedAt: string;
  actor: string;
  executionName: string | null;
  supersedesDecisionId?: string | null;
  decision: {
    outcome: string;
    rule: string;
    reasonCode: string | null;
    tier: string | null;
    engineOutcome?: string | null;
    reviewReason?: string | null;
    clearedAlerts?: string[];
    /** The reviewer's rationale, sealed with a person's decision (D-42). */
    rationale?: string | null;
  };
  versions?: Record<string, string>;
}

/** One state change on the case record, as the state machine or the API wrote it. */
export interface CaseTransition {
  from: string | null;
  to: string;
  at: string | null;
  /** `system`, `applicant:<sub>`, or a reviewer's bare Cognito sub. */
  actor: string | null;
  reason: string | null;
}

export interface CaseTraceResponse {
  /** Every state change, oldest first. Optional: an API deployed before it was added omits it. */
  transitions?: CaseTransition[];
  /** When each phase's state machine started on this case. */
  phaseStartedAt?: Record<string, string | null>;
  applicant: ApplicantTrace;
  review: {
    phase: string | null;
    reason: string | null;
    queuedAt: string | null;
    briefing: Briefing | null;
  };
  facts: RecordedFacts;
  documentExtraction: FactBase | null;
  /** Each phase's decision as the state machine recorded it on the case. */
  decisions: Record<string, Record<string, unknown> | null>;
  reviews: Record<string, Record<string, unknown> | null>;
  screeningDispositions: ScreeningDisposition[];
  agents: AgentNarrative[];
  notifications: ReviewerNotification[];
  account: AccountView | null;
  decision: DecisionRecordPayload | null;
  /** The execution that wrote the newest decision record. */
  execution: ExecutionTrace | null;
  /** Each phase's latest execution, running or finished: the live view's timeline. Optional: an
   * API deployed before it was added omits it. */
  executions?: Partial<Record<"phase1" | "phase2" | "phase3", ExecutionTrace | null>>;
  /** The mode this deployment attached its Cedar policy engines in: `LOG_ONLY` or `ENFORCE`
   * (D-40). Null when the API was not told; absent from an API deployed before it was added.
   * The decision record pins the mode each decision was made under (`versions`). */
  policyEnforcement?: string | null;
}

// ----------------------------------------------------------------------------------- demo

export interface DemoFaults {
  unavailableTools?: string[];
  agentTimeouts?: Record<string, number>;
}

export interface ListEntry {
  entryId: string;
  name: string;
  dob?: string;
  country?: string;
  aliases?: string[];
  programme?: string;
}

export interface DemoControls {
  faults: DemoFaults;
  listOverlay: { version?: string; entries: ListEntry[] };
}

export interface ListUpdateResponse {
  version: string;
  entries: ListEntry[];
  accounts: number;
  eventsSent: number;
  eventsFailed: number;
}

export interface BoardCase {
  applicantId: string;
  caseId: string | null;
  name: string | null;
  state: string | null;
  riskState: string | null;
  submittedAt: string | null;
  provisionalDecidedAt: string | null;
  fullDecidedAt: string | null;
  /** Measured server-side from the case record's own timestamps. */
  secondsToProvisional: number | null;
  reviewPhase?: string | null;
  reviewReason?: string | null;
}

// ------------------------------------------------------------------------------------ api

export const api = {
  /**
   * `applicationId` binds the session to this application's case, so the result can only be
   * recorded there, once. The server composes the real key as `APP-{cognito sub}-{applicationId}`.
   */
  createLivenessSession: (jurisdiction: string, applicationId: string) =>
    request<LivenessSessionResponse>("/liveness/session", {
      method: "POST",
      body: JSON.stringify({ jurisdiction, applicationId }),
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
      `/liveness/${encodeURIComponent(sessionId)}/result?jurisdiction=${jurisdiction}` +
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

  /** Creates the case, or — when the case is waiting on the applicant after a step-up — submits
   * the next attempt. Any other existing case is a 409 `CASE_ALREADY_SUBMITTED`. */
  createApplicant: (params: {
    jurisdiction: string;
    productApplied: string;
    documents: Array<{ bucket: string; key: string; type: string; issuingCountry: string }>;
    applicationId: string;
    declaredIdentity: DeclaredIdentityPayload;
    /** This browser's random id (D-41). The API stores only a keyed hash of it, for velocity;
     * without one, velocity is not established for the case. */
    deviceId?: string | null;
  }) =>
    request<CreateApplicantResponse>("/applicants", {
      method: "POST",
      body: JSON.stringify(params),
    }),

  getCase: (applicantId: string) =>
    request<CaseStatusResponse>(`/cases/${encodeURIComponent(applicantId)}`),

  getAccount: (applicantId: string) =>
    request<{ account: AccountView | null }>(`/accounts/${encodeURIComponent(applicantId)}`),

  listTransactions: (applicantId: string) =>
    request<{ account: AccountView; transactions: TransactionRecord[] }>(
      `/accounts/${encodeURIComponent(applicantId)}/transactions`,
    ),

  createTransaction: (
    applicantId: string,
    params: { type: TransactionType; amount: number; counterparty?: string },
  ) =>
    request<TransactionResult>(`/accounts/${encodeURIComponent(applicantId)}/transactions`, {
      method: "POST",
      body: JSON.stringify(params),
    }),

  getReviewQueue: () => request<ReviewQueueResponse>("/review/queue"),

  submitReviewDecision: (
    caseId: string,
    params: {
      applicantId: string;
      phase: string;
      action: string;
      rationale: string;
      taskToken: string;
    },
  ) =>
    request<{ resolution: string; detail?: string }>(
      `/review/${encodeURIComponent(caseId)}/decision`,
      { method: "POST", body: JSON.stringify(params) },
    ),

  /** Everything a reviewer needs to decide one case: the recorded facts, each phase's decision,
   * the briefing, the agents' narrative, the account, and the execution timeline. */
  getCaseTrace: (caseId: string) =>
    request<CaseTraceResponse>(`/review/${encodeURIComponent(caseId)}/trace`),

  getAudit: (applicantId: string) =>
    request<{
      applicantId: string;
      decisions: DecisionRecordPayload[];
      total: number;
      truncated: boolean;
      unreadable: string[];
    }>(`/audit/${encodeURIComponent(applicantId)}`),

  // Presenter controls. Deployed only with `-c demoControls=true`; presenters group only.
  getDemoCases: () => request<{ cases: BoardCase[] }>("/demo/cases"),

  getDemoControls: () => request<DemoControls>("/demo/controls"),

  putDemoFaults: (faults: DemoFaults) =>
    request<{ faults: DemoFaults }>("/demo/faults", {
      method: "PUT",
      body: JSON.stringify(faults),
    }),

  publishListUpdate: (params: { version?: string; entries: ListEntry[] }) =>
    request<ListUpdateResponse>("/demo/list-update", {
      method: "POST",
      body: JSON.stringify(params),
    }),

  resetDemo: () =>
    request<{
      faults: DemoFaults;
      listOverlay: DemoControls["listOverlay"];
      note: string;
      /** Device and network count entries deleted (D-41). Null when none could be, or when the
       * API has no velocity store; `velocityError` says which. Absent from an older API. */
      velocityCleared?: number | null;
      velocityError?: string;
    }>("/demo/reset", { method: "POST" }),
};
