import type {
  AccountView,
  AgentNarrative,
  CaseStatusResponse,
  CaseTraceResponse,
  CaseTransition,
  DecisionRecordPayload,
  FactBase,
  ScreeningFact,
} from "@shared/api/client";
import type { FlowStep } from "../applicant/types";
import type { IconName } from "../console/icons";
import { Backstage, buildBackstage } from "./timeline";

/**
 * The process view's model: what the case record says happened, as stages a presenter can read
 * out. Pure, so it can be reasoned about apart from the polling: everything here is derived from
 * the case record (status, trace, state transitions) and the device's step, never from a guess.
 *
 * A stage is only ever `done` because a tool recorded a fact, a rule recorded a decision, or the
 * platform holds the account. Time offsets are the case record's own timestamps relative to the
 * submission of this attempt, the same clock the "decided in N seconds" figure uses.
 */

export type StageStatus = "idle" | "active" | "done" | "warn" | "fail" | "skip";

export interface Stage {
  id: string;
  title: string;
  /** A short name for compact layouts ("Documents"). The title when absent. */
  name?: string;
  /** What did the work: the agent and tool, or the service, in a few words. */
  by: string;
  /** The route in a few words, for compact layouts ("Amazon Textract · MRZ check digits"). */
  via?: string;
  icon: IconName;
  status: StageStatus;
  /** What happened, in plain words. */
  summary: string;
  /** The same in a few words, for compact layouts. The summary when absent. */
  short?: string;
  /**
   * How a timeline draws it: an evidence agent is a bar from the phase start to the moment its
   * fact was recorded, a milestone is a point, a wait is a span that a person ends.
   */
  kind?: "agent" | "milestone" | "wait";
  /** Seconds on the stage's clock when its work began: bars and waits. */
  start?: number | null;
  /** Seconds after this attempt was submitted (or after the phase started) when it finished. */
  at?: number | null;
  /** How long the agent that did this ran, in seconds. */
  took?: number | null;
  details?: Array<[string, string]>;
  /** Set when a person is needed: the case to open in the review queue. */
  reviewCaseId?: string | null;
  /** Active by design and with no end, like monitoring: shown calm, without a spinner. */
  steady?: boolean;
}

export interface StageGroup {
  id: "applicant" | "phase1" | "phase2" | "phase3";
  title: string;
  subtitle: string;
  status: StageStatus;
  stages: Stage[];
  /** Active with no end (monitoring): labelled "Watching", without a spinner. */
  steady?: boolean;
  /** What the group's state is called when the status alone would mislead: a freeze is the
   * phase doing its job, not failing at it. */
  label?: string;
  /** Seconds on this group's clock now, while something in it is still growing; else null. */
  clock?: number | null;
  /** When the group started, in seconds after submission (the Phase 1 clock). */
  startedAt?: number | null;
  /** How long the group took to decide, once it has. */
  took?: number | null;
}

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export interface ProcessHeadline {
  name: string;
  caseId: string | null;
  applicantId: string | null;
  jurisdiction: string | null;
  stateLabel: string;
  stateTone: Tone;
  /** Where the case stands, in a few words, for the headline ("Account open, with limits"). */
  outcome: string;
  /** One line under it: why, or what happens next. */
  detail: string | null;
  timer: {
    mode: "idle" | "running" | "stopped";
    seconds: number | null;
    /** What the number measures ("Account opened in", "Referred to a person in"). */
    label: string;
    tone: Tone;
    /** Set when a person decided: how much of the time was the case waiting for them. */
    note?: string | null;
  };
}

export interface ProcessEvent {
  at: string;
  label: string;
  actor: string;
  tone: Tone;
}

export interface ProcessView {
  headline: ProcessHeadline;
  groups: StageGroup[];
  events: ProcessEvent[];
  /** The same case as timelines and a feed, from the execution history (`timeline.ts`). */
  backstage: Backstage;
}

export interface ProcessInput {
  /** The device's step, when the application is on the device in front of the presenter. */
  deviceStep?: FlowStep | null;
  /** The name typed on the device, before the case record exists. */
  declaredName?: string | null;
  jurisdiction?: string | null;
  status: CaseStatusResponse | null;
  trace: CaseTraceResponse | null;
  now: number;
  /** The trace route answers this user (a reviewer). Without it there is no execution history to
   * draw, so the view offers the checklist only. Defaults to true. */
  traceAvailable?: boolean;
}

export const TARGET_SECONDS = 90;

// --------------------------------------------------------------------------------- helpers

function ms(iso?: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

function secondsSince(startMs: number | null, iso?: string | null): number | null {
  const end = ms(iso);
  if (startMs === null || end === null) return null;
  return Math.max(0, (end - startMs) / 1000);
}

function str(value: unknown): string | null {
  return value === undefined || value === null || value === "" ? null : String(value);
}

function titleCase(value: string): string {
  return value
    .toLowerCase()
    .replace(/(^|[\s-])([a-z])/g, (_, sep: string, ch: string) => sep + ch.toUpperCase());
}

function agentRun(trace: CaseTraceResponse | null, phase: string, agent: string) {
  const runs = (trace?.agents ?? []).filter((a) => a.phase === phase && a.agent === agent);
  return runs.length ? runs[runs.length - 1] : undefined;
}

function took(run?: AgentNarrative): number | null {
  return run?.latencyMs ? run.latencyMs / 1000 : null;
}

function agentFailed(run?: AgentNarrative): boolean {
  return Boolean(run && run.status && run.status !== "OK");
}

/** A fact recorded before this attempt began belongs to an earlier attempt. */
function current<T extends FactBase>(fact: T | null | undefined, since: number | null): T | null {
  if (!fact) return null;
  const at = ms(str(fact.recordedAt));
  if (since !== null && at !== null && at < since - 2000) return null;
  return fact;
}

function performed(fact: FactBase | null): boolean {
  return Boolean(fact) && fact?.performed !== false;
}

function notPerformed(fact: FactBase | null): string {
  const why = str(fact?.reason) ?? str(fact?.reasonCode) ?? "the check did not run";
  return `Not performed: ${why}`;
}

/**
 * The same in a few words, with the status exactly as the tool recorded it. UNAVAILABLE covers a
 * source that could not be reached and an input the case lacks (`tools_impl.py`), so the screen
 * repeats the status rather than guessing which; the reason is in the detail.
 */
function notPerformedShort(fact: FactBase | null): string {
  const status = str(fact?.status);
  return status && status !== "OK" ? `Not performed · ${status}` : "Not performed";
}

function lastTransition(
  transitions: CaseTransition[],
  pick: (t: CaseTransition) => boolean,
): CaseTransition | undefined {
  for (let i = transitions.length - 1; i >= 0; i -= 1) {
    if (pick(transitions[i])) return transitions[i];
  }
  return undefined;
}

function firstTransitionAfter(
  transitions: CaseTransition[],
  sinceMs: number | null,
  pick: (t: CaseTransition) => boolean,
): CaseTransition | undefined {
  return transitions.find((t) => {
    const at = ms(t.at);
    return pick(t) && (sinceMs === null || (at !== null && at >= sinceMs - 1000));
  });
}

function groupStatus(stages: Stage[], running: boolean): StageStatus {
  if (stages.some((s) => s.status === "fail")) return "fail";
  if (running || stages.some((s) => s.status === "active")) return "active";
  if (stages.some((s) => s.status === "warn")) return "warn";
  if (stages.length > 0 && stages.every((s) => s.status === "done" || s.status === "skip")) {
    return "done";
  }
  if (stages.some((s) => s.status === "done")) return "warn";
  return "idle";
}

/**
 * A decided phase is summarised by its outcome: a check that failed inside a phase a reviewer
 * then approved stays red on its own row, but the phase itself is done, not failed.
 */
function phaseGroupStatus(
  decision: Record<string, unknown> | null | undefined,
  stages: Stage[],
  running: boolean,
): StageStatus {
  if (running) return "active";
  if (stages.some((s) => s.reviewCaseId)) return "warn";
  const outcome = str(decision?.outcome);
  const mapped = outcome ? OUTCOME_TEXT[outcome] : undefined;
  return mapped ? mapped.status : groupStatus(stages, false);
}

/** Waiting, running, or — once the phase has decided — a check that never recorded anything. */
function pendingStatus(running: boolean, decided: boolean): StageStatus {
  if (running) return "active";
  return decided ? "warn" : "idle";
}

function pendingSummary(running: boolean, decided: boolean, idle: string): string {
  if (running) return "Running…";
  return decided ? "Not recorded: a missing check can only send the case to a person" : idle;
}

function pendingShort(running: boolean, decided: boolean): string {
  if (running) return "Working…";
  return decided ? "Not recorded" : "Waiting";
}

const OUTCOME_TEXT: Record<string, { text: string; short: string; status: StageStatus }> = {
  PROVISIONAL_APPROVE_RESTRICTED: { text: "Approve, with limits until the full checks", short: "Approve, with limits", status: "done" },
  FULL_APPROVE_STANDARD: { text: "Approve: lift the limits", short: "Approve · lift the limits", status: "done" },
  FULL_APPROVE_ENHANCED_MONITORING: { text: "Approve: lift limits, enhanced monitoring", short: "Approve · enhanced monitoring", status: "done" },
  MANUAL_REVIEW: { text: "Refer to a person", short: "Refer to a person", status: "warn" },
  STEP_UP: { text: "Ask the applicant for more", short: "Ask the applicant for more", status: "warn" },
  REJECT: { text: "Decline", short: "Decline", status: "fail" },
  RISK_UNCHANGED: { text: "No change", short: "No change", status: "done" },
  RESTRICT_FURTHER: { text: "Restrict the account", short: "Restrict the account", status: "warn" },
  RISK_INCREASED_RESTRICT: { text: "Risk went up: restrict", short: "Risk up · restrict", status: "warn" },
  RISK_INCREASED_REVIEW: { text: "Risk went up: refer to a person", short: "Risk up · refer to a person", status: "warn" },
  FREEZE: { text: "Freeze the account", short: "Freeze the account", status: "fail" },
  SYSTEM_ERROR: { text: "Could not decide", short: "Could not decide", status: "fail" },
};

/** The first block of a decision id: enough to tell two apart on screen. */
function shortId(id: string | null | undefined): string {
  return id ? id.split("-")[0] : "—";
}

const STATE_LABEL: Record<string, { label: string; tone: Tone }> = {
  RECEIVED: { label: "Queued", tone: "info" },
  IN_PROGRESS: { label: "Checking", tone: "info" },
  AWAITING_APPLICANT: { label: "Waiting for the applicant", tone: "warning" },
  AWAITING_REVIEW: { label: "With a reviewer", tone: "warning" },
  PROVISIONAL: { label: "Open with limits", tone: "success" },
  MONITORING: { label: "Fully open", tone: "success" },
  RESTRICTED: { label: "Restricted", tone: "warning" },
  FROZEN: { label: "Frozen", tone: "danger" },
  DECIDED: { label: "Decided", tone: "neutral" },
};

/** True when a reviewer, not the rules, made the phase's recorded decision. */
function decidedByPerson(decision: Record<string, unknown> | null | undefined): boolean {
  return str(decision?.decidedBy) === "REVIEWER";
}

function decisionStage(
  id: string,
  decision: Record<string, unknown> | null | undefined,
  running: boolean,
  at: number | null,
  idleText: string,
): Stage {
  // After a review, the recorded outcome is the reviewer's and `engineOutcome` is what the rules
  // said. This row is the rules' answer; the reviewer's has its own row.
  const byPerson = decidedByPerson(decision);
  const outcome = (byPerson ? str(decision?.engineOutcome) : null) ?? str(decision?.outcome);
  const mapped = outcome ? OUTCOME_TEXT[outcome] : undefined;
  const details: Array<[string, string]> = [];
  for (const [key, label] of [
    ["rule", "Rule"],
    ["reasonCode", "Reason"],
    ["tier", "Risk tier"],
    ["requiredItem", "Asked for"],
  ] as const) {
    // The rule on a reviewed decision is the review's (`REVIEW.APPROVE`), not the rules'.
    if (key === "rule" && byPerson) continue;
    const value = str(decision?.[key]);
    if (value) details.push([label, value]);
  }
  return {
    id,
    title: "Rules decide",
    by: "Decision engine · deterministic rules on the recorded facts, no model",
    via: "Decision engine · deterministic, no model",
    icon: "rules",
    kind: "milestone",
    // The rules run in milliseconds once the evidence is in, so while the phase runs they are
    // waiting, not working: no spinner.
    status: outcome ? mapped?.status ?? "done" : "idle",
    summary: outcome
      ? `${mapped?.text ?? outcome}${str(decision?.explanation) ? ` · ${str(decision?.explanation)}` : ""}`
      : running
        ? "Waiting for the evidence"
        : idleText,
    short: outcome ? mapped?.short ?? outcome : running ? "Waiting for the evidence" : "Waiting",
    at: outcome ? at : null,
    details,
  };
}

function reviewStage(
  id: string,
  phase: string,
  trace: CaseTraceResponse | null,
  caseId: string | null,
  start: number | null,
): Stage | null {
  const recorded = trace?.reviews?.[phase] as Record<string, unknown> | null | undefined;
  // A review decided before this attempt began belongs to an earlier attempt.
  const decidedAt = ms(str(recorded?.decidedAt));
  const review = recorded && !(start !== null && decidedAt !== null && decidedAt < start - 2000)
    ? recorded
    : null;
  const waiting = trace?.review?.phase === phase && trace?.applicant.state === "AWAITING_REVIEW";
  if (!review && !waiting) return null;
  // When the case joined the queue: the start of the wait a person ends.
  const queuedAt = trace?.review?.phase === phase ? str(trace?.review?.queuedAt) : null;
  const queuedOffset = secondsSince(start, queuedAt);
  if (review) {
    const action = str(review.action) ?? "Decided";
    const cleared = Array.isArray(review.clearedAlerts) && review.clearedAlerts.length
      ? ` · cleared ${review.clearedAlerts.join(", ")}`
      : "";
    const rationale = str(review.rationale);
    return {
      id,
      title: "A person decides",
      name: "A person decides",
      by: "Reviewer console · decision sealed with the reviewer's identity",
      via: "Reviewer · rationale sealed with the decision",
      icon: "person",
      kind: "wait",
      status: action === "REJECT" ? "fail" : "done",
      summary: `${titleCase(action.replace(/_/g, " "))}${cleared}${rationale ? ` · “${rationale}”` : ""}`,
      short: `${titleCase(action.replace(/_/g, " "))}${rationale ? ` · “${rationale}”` : ""}`,
      start: queuedOffset,
      at: secondsSince(start, str(review.decidedAt)),
    };
  }
  return {
    id,
    title: "A person decides",
    name: "A person decides",
    by: "Review queue · Step Functions waits on the reviewer's task token",
    via: "Review queue · Step Functions task token",
    icon: "person",
    kind: "wait",
    status: "warn",
    summary: `Waiting for a reviewer: ${reasonText(trace?.review?.reason)}`,
    short: `Waiting · ${reasonText(trace?.review?.reason)}`,
    start: queuedOffset,
    reviewCaseId: caseId,
  };
}

const PHASE_ORDER: Record<string, number> = { phase1: 1, phase2: 2, phase3: 3 };

/**
 * The phase's sealed decision record. The trace carries only the newest record, so an earlier
 * phase's row reads its decision id from the case record instead: a later phase starts only
 * after the earlier one's record is written.
 */
function sealedStage(
  id: string,
  phase: "phase1" | "phase2",
  decision: Record<string, unknown> | null | undefined,
  latest: DecisionRecordPayload | null,
  since: number | null,
  decided: boolean,
): Stage {
  const base = {
    id,
    title: "Decision record sealed",
    name: "Record sealed",
    by: "Audit writer → Amazon S3 Object Lock",
    via: "Audit writer · Amazon S3 Object Lock",
    icon: "lock" as IconName,
    kind: "milestone" as const,
  };
  const latestPhase = str(latest?.phase);
  const latestAt = ms(latest?.decidedAt);
  const forThisAttempt = since === null || latestAt === null || latestAt >= since - 2000;
  if (decided && latest && latestPhase === phase && forThisAttempt) {
    const by = actorLabel(latest.actor);
    return {
      ...base,
      status: "done",
      summary: `${latest.decisionId} · ${latest.decision?.outcome ?? ""} · decided by ${by}`,
      short: `${shortId(latest.decisionId)} · decided by ${by}`,
      at: secondsSince(since, latest.decidedAt),
    };
  }
  const decisionId = str(decision?.decisionId);
  if (decided && decisionId && latestPhase && (PHASE_ORDER[latestPhase] ?? 0) > PHASE_ORDER[phase]) {
    const by = decidedByPerson(decision) ? "reviewer" : "system";
    return {
      ...base,
      status: "done",
      summary: `${decisionId} · ${str(decision?.outcome) ?? ""} · decided by ${by}`,
      short: `${shortId(decisionId)} · decided by ${by}`,
    };
  }
  return {
    ...base,
    status: decided ? "active" : "idle",
    summary: decided ? "Being written" : "Every decision, with the facts and the versions that made it",
    short: decided ? "Being written" : "Waiting",
  };
}

/** A record a reviewer's decision will write: waiting on them, not being written. */
function waitForReviewer(stage: Stage): void {
  stage.status = "idle";
  stage.summary = "Written when the reviewer decides, with the reviewer's rationale";
  stage.short = "Written when the reviewer decides";
}

function reasonText(reason?: string | null): string {
  const map: Record<string, string> = {
    ENGINE_MANUAL_REVIEW: "the rules referred it",
    DEGRADED_PIPELINE: "a check could not run",
    JURISDICTION_NOT_DECIDED: "this jurisdiction is not decided automatically",
    ACCOUNT_FROZEN: "frozen by the rules, confirm or exit",
    RISK_INCREASED: "risk went up after a new event",
  };
  return (reason && map[reason]) || (reason ? reason.toLowerCase().replace(/_/g, " ") : "referred");
}

// ------------------------------------------------------------------------------- the model

export function buildProcess(input: ProcessInput): ProcessView {
  const { status, trace, now, deviceStep } = input;
  const transitions = trace?.transitions ?? [];
  const caseExists = Boolean(status || trace);
  const state = trace?.applicant.state ?? status?.state ?? null;
  const caseId = trace?.applicant.caseId ?? null;
  const applicantId = trace?.applicant.applicantId ?? null;

  // This attempt: the last time the case entered RECEIVED (a step-up resubmission starts a new
  // attempt), else the submission the status reports.
  const attemptStartIso =
    lastTransition(transitions, (t) => t.to === "RECEIVED")?.at ??
    status?.resubmittedAt ??
    status?.createdAt ??
    trace?.applicant.createdAt ??
    null;
  const attemptStart = ms(attemptStartIso);
  const phase1Running = caseExists && (state === null || state === "RECEIVED" || state === "IN_PROGRESS");
  const phase1Outcome = firstTransitionAfter(
    transitions,
    attemptStart,
    (t) => t.from === "IN_PROGRESS",
  );
  const phase1DecidedIso = status?.provisionalDecidedAt ?? phase1Outcome?.at ?? null;
  const phase1Decided = caseExists && !phase1Running;
  const facts = trace?.facts;
  // When Step Functions started this attempt's Phase 1: where the four agents' bars begin. The
  // case's `phase1StartedAt` is the latest start, so it only counts if it is this attempt's.
  const recordedPhase1Start = trace?.phaseStartedAt?.phase1 ?? null;
  const phase1StartIso =
    firstTransitionAfter(transitions, attemptStart, (t) => t.to === "IN_PROGRESS")?.at ??
    (attemptStart !== null && (ms(recordedPhase1Start) ?? -Infinity) >= attemptStart - 1000
      ? recordedPhase1Start
      : null);
  const phase1StartOffset = secondsSince(attemptStart, phase1StartIso);
  const nowOffset = attemptStart !== null ? Math.max(0, (now - attemptStart) / 1000) : null;

  // ---------------------------------------------------------------- on the device
  const onDevice = (steps: FlowStep[]) => Boolean(deviceStep && steps.includes(deviceStep));
  const liveness = trace?.applicant.liveness?.result ?? status?.liveness?.result ?? null;
  const documents = trace?.applicant.documents ?? [];
  const identity = trace?.applicant.declaredIdentity;
  const declaredName =
    input.declaredName ??
    (identity ? titleCase(`${identity.givenNames ?? ""} ${identity.surname ?? ""}`.trim()) : null);

  const documentLabel = documents.length
    ? documents
        .map((d) => `${titleCase((d.type ?? "document").replace(/_/g, " "))} (${d.issuingCountry ?? "—"})`)
        .join(", ")
    : null;
  // In the order the applicant does them (D-37): the details, the ID document, then the selfie.
  // Before submission the case does not exist yet, so the device's step is all there is to go on.
  const detailsActive = onDevice(["intake", "details"]);
  const documentActive = onDevice(["documents"]);
  const livenessActive = onDevice(["liveness"]);
  const applicantStages: Stage[] = [
    {
      id: "details",
      title: "Details declared",
      name: "Details",
      by: "Applicant, on the device",
      via: "Declared by the applicant",
      icon: "device",
      status: detailsActive ? "active" : caseExists || deviceStep ? "done" : "idle",
      summary: declaredName
        ? `${declaredName}${identity?.dateOfBirth ? ` · born ${identity.dateOfBirth}` : ""}`
        : "Name, date of birth, nationality and address",
      short: detailsActive ? "Entering details" : declaredName ?? "Name, birth date, address",
    },
    {
      id: "document",
      title: "ID document",
      name: documentLabel ?? "ID document",
      by: "Photographed with the camera, then a presigned upload straight to the Amazon S3 evidence bucket",
      via: "Presigned upload · Amazon S3",
      icon: "document",
      status: documentActive
        ? "active"
        : documents.length || caseExists || onDevice(["liveness", "review", "submitting", "status"])
          ? "done"
          : "idle",
      summary: documentLabel
        ? documentLabel
        : documentActive
          ? "Photographing the ID; it uploads with an integrity checksum"
          : "One ID document, checked in the browser before upload",
      short: documentActive
        ? "On the phone now"
        : documents.length || caseExists || onDevice(["liveness", "review", "submitting"])
          ? "In the evidence bucket"
          : "Not yet",
    },
    {
      id: "liveness",
      title: "Selfie video",
      name: "Live face check",
      by: "Amazon Rekognition Face Liveness · video streamed from the browser",
      via: "Amazon Rekognition Face Liveness",
      short: livenessActive
        ? "In progress"
        : liveness === "PASS"
          ? "Live person confirmed"
          : liveness
            ? `Result: ${liveness}`
            : caseExists
              ? "Not recorded"
              : onDevice(["review", "submitting"])
                ? "Done"
                : "Not yet",
      icon: "face",
      status: livenessActive
        ? "active"
        : liveness === "PASS"
          ? "done"
          : liveness
            ? "fail"
            : caseExists
              ? "warn"
              : onDevice(["review", "submitting", "status"])
                ? "done"
                : "idle",
      summary: livenessActive
        ? "The applicant is following the prompts"
        : liveness === "PASS"
          ? "Live person confirmed. One still kept for 24 hours, to compare with the document"
          : liveness
            ? `Result: ${liveness}`
            : caseExists
              ? "No face check recorded"
              : "Proves a live person is present, not a photo or a replay",
    },
  ];

  // ---------------------------------------------------------------- phase 1
  const docFact = current(facts?.documentExtraction, attemptStart);
  const faceFact = current(facts?.faceMatch, attemptStart);
  const velocityFact = current(facts?.velocity, attemptStart);
  const screeningFact = current(facts?.screening as ScreeningFact | null, attemptStart);
  const evidenceRunning = phase1Running && state === "IN_PROGRESS";
  /** An evidence bar starts when Phase 1 does: the four agents run in one Parallel state. */
  const barStart = (recorded: boolean): number | null =>
    recorded || evidenceRunning ? phase1StartOffset ?? 0 : null;

  const docsRun = agentRun(trace, "phase1", "documents");
  const docStage: Stage = (() => {
    const base = {
      id: "doc-read",
      title: "Document read",
      name: "Documents",
      by: "Documents agent (AgentCore harness) → AgentCore Gateway → s3_get_document → Amazon Textract + MRZ check digits",
      via: "Amazon Textract · MRZ check digits",
      icon: "document" as IconName,
      kind: "agent" as const,
      took: took(docsRun),
      start: barStart(Boolean(docFact)),
      at: secondsSince(attemptStart, str(docFact?.recordedAt)),
    };
    if (!docFact) {
      const failed = agentFailed(docsRun) && phase1Decided;
      return {
        ...base,
        status: failed ? "fail" : pendingStatus(evidenceRunning, phase1Decided),
        summary: pendingSummary(evidenceRunning, phase1Decided, "Reads the text and the machine-readable zone"),
        short: failed ? "Agent failed" : pendingShort(evidenceRunning, phase1Decided),
      } as Stage;
    }
    if (!performed(docFact) || docFact.documentReadable === false) {
      const reason = str(docFact.unreadableReason);
      return {
        ...base,
        status: "fail",
        summary: reason ?? notPerformed(docFact),
        short: !performed(docFact)
          ? notPerformedShort(docFact)
          : reason && /pdf/i.test(reason)
            ? "Unreadable · a PDF, not a photo"
            : "Document unreadable",
      } as Stage;
    }
    const mrz = (docFact.mrz ?? null) as Record<string, unknown> | null;
    const doc = (docFact.document ?? {}) as Record<string, unknown>;
    const details: Array<[string, string]> = [];
    if (doc.documentType) details.push(["Type", `${String(doc.documentType)} · ${String(doc.issuingCountry ?? "—")}`]);
    if (mrz?.expiryDate) details.push(["Expiry (from the MRZ)", String(mrz.expiryDate)]);
    if (mrz) details.push(["Check digits", mrz.checkDigitsValid ? "all valid" : "not valid"]);
    return {
      ...base,
      status: mrz && mrz.checkDigitsValid ? "done" : "warn",
      summary: mrz
        ? mrz.checkDigitsValid
          ? "Text extracted; machine-readable zone parsed and every check digit valid"
          : "Machine-readable zone found, but its check digits do not verify"
        : "Text extracted; no machine-readable zone found",
      short: mrz
        ? mrz.checkDigitsValid
          ? "MRZ read · every check digit valid"
          : "MRZ check digits fail"
        : "No machine-readable zone",
      details,
    } as Stage;
  })();

  const identityRun = agentRun(trace, "phase1", "identity");
  const faceStage: Stage = (() => {
    const base = {
      id: "face-match",
      title: "Face match",
      name: "Identity",
      by: "Identity agent (AgentCore harness) → AgentCore Gateway → rekognition_compare_faces → Amazon Rekognition",
      via: "Amazon Rekognition · CompareFaces",
      icon: "face" as IconName,
      kind: "agent" as const,
      took: took(identityRun),
      start: barStart(Boolean(faceFact)),
      at: secondsSince(attemptStart, str(faceFact?.recordedAt)),
    };
    if (!faceFact) {
      const failed = agentFailed(identityRun) && phase1Decided;
      return {
        ...base,
        status: failed ? "fail" : pendingStatus(evidenceRunning, phase1Decided),
        summary: pendingSummary(evidenceRunning, phase1Decided, "Compares the document photo with the live face"),
        short: failed ? "Agent failed" : pendingShort(evidenceRunning, phase1Decided),
      } as Stage;
    }
    if (!performed(faceFact)) {
      return { ...base, status: "fail", summary: notPerformed(faceFact), short: notPerformedShort(faceFact) } as Stage;
    }
    const band = str(faceFact.similarityBand);
    if (faceFact.match === true) {
      return {
        ...base,
        status: "done",
        summary: `Same person${band ? ` · similarity ${band}` : ""}`,
        short: `Same person${band ? ` · similarity ${band}` : ""}`,
      } as Stage;
    }
    return {
      ...base,
      status: "warn",
      summary: faceFact.faceDetected === false
        ? `${sentence(str(faceFact.reason) ?? "no detectable face in one of the images")}, so nothing to compare`
        : `The faces do not match${band ? ` · similarity ${band}` : ""}`,
      short: faceFact.faceDetected === false ? "No face to compare" : "The faces do not match",
    } as Stage;
  })();

  const fraudRun = agentRun(trace, "phase1", "fraud");
  const fraudStage: Stage = (() => {
    const base = {
      id: "fraud",
      title: "Fraud signals",
      name: "Fraud",
      by: "Fraud agent (AgentCore harness) → AgentCore Gateway → velocity_check → Amazon DynamoDB",
      via: "Velocity check · Amazon DynamoDB",
      icon: "eye" as IconName,
      kind: "agent" as const,
      took: took(fraudRun),
      start: barStart(Boolean(velocityFact)),
      at: secondsSince(attemptStart, str(velocityFact?.recordedAt)),
    };
    if (!velocityFact) {
      const failed = agentFailed(fraudRun) && phase1Decided;
      return {
        ...base,
        status: failed ? "fail" : pendingStatus(evidenceRunning, phase1Decided),
        summary: pendingSummary(evidenceRunning, phase1Decided, "Repeat applications from one device or address"),
        short: failed ? "Agent failed" : pendingShort(evidenceRunning, phase1Decided),
      } as Stage;
    }
    if (velocityFact.status === "NOT_ESTABLISHED" || velocityFact.reasonCode === "NO_DEVICE_OR_IP_CAPTURED") {
      return {
        ...base,
        status: "skip",
        summary: "Not established: no device or network was captured with this application",
        short: "Not established · nothing captured at intake",
      } as Stage;
    }
    if (!performed(velocityFact)) {
      return { ...base, status: "fail", summary: notPerformed(velocityFact), short: notPerformedShort(velocityFact) } as Stage;
    }
    // D-41: the tool counts the different people who applied from this device and this network
    // in its window, this application included, and sets a signal when either count is critical.
    const signals = (velocityFact.signals ?? {}) as Record<string, unknown>;
    const deviceCritical = signals.criticalDeviceReuse === true;
    const critical = deviceCritical || signals.criticalIpReuse === true;
    const fromDevice = people(velocityFact.deviceApplications);
    const fromNetwork = people(velocityFact.ipApplications);
    const hours = Number(velocityFact.windowHours) || 24;
    const counted = `${fromDevice} from this device and ${fromNetwork} from this network in ${hours} h`;
    return {
      ...base,
      status: critical ? "warn" : "done",
      summary: critical
        ? `Velocity critical: ${counted}, so the rules send the case to a person`
        : `Within limits: ${counted}`,
      short: critical
        ? `Velocity critical · ${deviceCritical ? `${fromDevice} from this device` : `${fromNetwork} from this network`}`
        : `Within limits · ${fromDevice} from this device in ${hours} h`,
    } as Stage;
  })();

  const prescreenRun = agentRun(trace, "phase1", "prescreen");
  const screeningStage: Stage = (() => {
    const base = {
      id: "screening",
      title: "Sanctions screening",
      name: "Pre-Screen",
      by: "Pre-Screen agent (AgentCore harness) → AgentCore Gateway → sanctions_screen → synthetic consolidated list",
      via: "Sanctions list · exact and variant names",
      icon: "globe" as IconName,
      kind: "agent" as const,
      took: took(prescreenRun),
      start: barStart(Boolean(screeningFact)),
      at: secondsSince(attemptStart, str(screeningFact?.recordedAt)),
    };
    if (!screeningFact) {
      const failed = agentFailed(prescreenRun) && phase1Decided;
      return {
        ...base,
        status: failed ? "fail" : pendingStatus(evidenceRunning, phase1Decided),
        summary: pendingSummary(evidenceRunning, phase1Decided, "Exact and variant name matching, every alert classified"),
        short: failed ? "Agent failed" : pendingShort(evidenceRunning, phase1Decided),
      } as Stage;
    }
    if (!performed(screeningFact)) {
      return { ...base, status: "fail", summary: notPerformed(screeningFact), short: notPerformedShort(screeningFact) } as Stage;
    }
    const hits = screeningFact.hits ?? [];
    const excluded = screeningFact.excluded ?? [];
    const details: Array<[string, string]> = [];
    if (screeningFact.listVersion) details.push(["List version", String(screeningFact.listVersion)]);
    for (const hit of hits) details.push([hit.classification, `${hit.entryId} ${hit.matchedName}: ${hit.reason}`]);
    for (const ex of excluded) details.push(["Excluded", `${ex.entryId} ${ex.matchedName}: ${ex.reason}`]);
    const strongest = str(screeningFact.strongest);
    const excludedText = excluded.length
      ? ` · ${excluded.length} namesake${excluded.length === 1 ? "" : "s"} excluded, with reasons`
      : "";
    const excludedShort = excluded.length
      ? ` · ${excluded.length} namesake${excluded.length === 1 ? "" : "s"} excluded`
      : "";
    if (!hits.length) {
      return {
        ...base,
        status: "done",
        summary: `No alerts${excludedText}`,
        short: `No alerts${excludedShort}`,
        details,
      } as Stage;
    }
    const top = hits[0];
    return {
      ...base,
      status: strongest === "CONFIRMED" ? "fail" : "warn",
      summary: `${strongest ?? top.classification} match on ${top.entryId} “${top.matchedName}”${excludedText}`,
      short: `${strongest ?? top.classification} · ${top.entryId} “${top.matchedName}”`,
      details,
    } as Stage;
  })();

  const phase1Decision = phase1Running ? null : trace?.decisions?.phase1;
  const ingressSeconds =
    status?.ingressLatencyMs !== undefined && status?.ingressLatencyMs !== null
      ? status.ingressLatencyMs / 1000
      : null;
  const queuedStage: Stage = {
    id: "queued",
    title: "Submitted to the pipeline",
    name: "Submitted",
    by: "Amazon SQS FIFO → EventBridge Pipes → AWS Step Functions",
    via: "SQS FIFO · EventBridge Pipes · Step Functions",
    icon: "queue",
    status: !caseExists ? "idle" : state === "RECEIVED" ? "active" : "done",
    summary: !caseExists
      ? "Starts when the applicant submits"
      : ingressSeconds !== null
        ? `Handed to Step Functions in ${ingressSeconds.toFixed(2)} s`
        : state === "RECEIVED"
          ? "Queued for Phase 1"
          : "Phase 1 started",
    short: !caseExists
      ? "Not yet"
      : ingressSeconds !== null
        ? `${ingressSeconds.toFixed(2)} s to Step Functions`
        : state === "RECEIVED"
          ? "Queued"
          : "Phase 1 started",
    at: caseExists ? 0 : null,
  };

  const account = trace?.account ?? null;
  const accountStage: Stage = (() => {
    const outcome = str(phase1Decision?.outcome);
    const base = {
      id: "account",
      title: "Account opened",
      name: "Account opened",
      by: "Account platform · enforces the limits itself",
      via: "Account platform · enforces the limits itself",
      icon: "account" as IconName,
      kind: "milestone" as const,
    };
    if (account || status?.provisionalDecidedAt) {
      const limits = account?.restrictionDetails?.length ?? 0;
      const limitText = `${limits} limit${limits === 1 ? "" : "s"}`;
      return {
        ...base,
        status: account?.state === "FROZEN" ? "fail" : "done",
        summary: account
          ? account.state === "OPEN_RESTRICTED"
            ? `Open with ${limitText} until the full checks finish`
            : account.state === "FROZEN"
              ? "Open, now frozen"
              : "Open"
          : "Open with limits",
        short: account
          ? account.state === "OPEN_RESTRICTED"
            ? `Open · ${limitText} until the full checks`
            : account.state === "FROZEN"
              ? "Opened · now frozen"
              : "Open"
          : "Open with limits",
        at: secondsSince(attemptStart, status?.provisionalDecidedAt),
        details: (account?.restrictionDetails ?? []).map((r) => ["Limited", r.description] as [string, string]),
      };
    }
    return {
      ...base,
      status: outcome === "REJECT" ? "fail" : outcome ? "skip" : "idle",
      summary: outcome === "REJECT"
        ? "No account: the application was declined"
        : outcome === "STEP_UP"
          ? "Not yet: waiting for the applicant"
          : outcome === "MANUAL_REVIEW"
            ? "Not yet: waiting for the reviewer's decision"
            : "Opens with limits when the rules approve",
      short: outcome === "REJECT"
        ? "No account"
        : outcome === "STEP_UP"
          ? "Not yet · waiting for the applicant"
          : outcome === "MANUAL_REVIEW"
            ? "Not yet · waiting for a reviewer"
            : "Waiting",
    };
  })();

  const latestRecord = trace?.decision ?? null;
  const sealed1 = sealedStage("sealed-1", "phase1", phase1Decision, latestRecord, attemptStart, phase1Decided);

  const phase1Stages: Stage[] = [
    queuedStage,
    docStage,
    faceStage,
    fraudStage,
    screeningStage,
    // Timed at the rules' own answer: the first state change out of IN_PROGRESS. After a review
    // the provisional decision lands much later, and that time belongs to the reviewer's row.
    decisionStage("rules-1", phase1Decision, evidenceRunning, secondsSince(attemptStart, phase1Outcome?.at ?? phase1DecidedIso), "Reads the four facts above"),
  ];
  const review1 = reviewStage("review-1", "phase1", trace, caseId, attemptStart);
  if (review1) {
    // The wait starts when the rules referred the case, if the queue time is not on the record.
    if (review1.start === null || review1.start === undefined) {
      review1.start = secondsSince(attemptStart, phase1Outcome?.at);
    }
    phase1Stages.push(review1);
  }
  const awaitingReview1 = Boolean(review1 && review1.status === "warn" && review1.reviewCaseId);
  // A referred case's record is written once the reviewer decides, with their rationale: until
  // then nothing is being written.
  if (awaitingReview1 && sealed1.status === "active") waitForReviewer(sealed1);
  phase1Stages.push(accountStage, sealed1);

  // ---------------------------------------------------------------- phase 2
  const opened = Boolean(account || status?.provisionalDecidedAt);
  const phase2StartIso = trace?.phaseStartedAt?.phase2 ?? status?.provisionalDecidedAt ?? null;
  const phase2Start = ms(phase2StartIso);
  const phase2Decision = trace?.decisions?.phase2;
  const phase2Running = opened && !phase2Decision && (state === "PROVISIONAL" || status?.phase2Pending === "PENDING");
  const pepFact = facts?.pep ?? null;
  const mediaFact = facts?.adverseMedia ?? null;
  const complianceRun = agentRun(trace, "phase2", "compliance");
  const phase2Idle = "Runs once the account is open";

  const pepBase = {
    id: "pep",
    title: "Politically exposed persons",
    name: "PEP check",
    by: "Compliance agent (AgentCore harness) → AgentCore Gateway → pep_lookup",
    via: "Compliance agent · pep_lookup",
    icon: "person" as IconName,
  };
  const pepStage: Stage = pepFact
    ? !performed(pepFact)
      ? { ...pepBase, status: "fail", summary: notPerformed(pepFact), short: notPerformedShort(pepFact) }
      : {
          ...pepBase,
          status: str(pepFact.pepStatus) === "NONE" ? "done" : "warn",
          summary: str(pepFact.pepStatus) === "NONE" ? "Not a politically exposed person" : `PEP status: ${str(pepFact.pepStatus) ?? "—"}`,
          short: str(pepFact.pepStatus) === "NONE" ? "Not a PEP" : `PEP · ${str(pepFact.pepStatus) ?? "—"}`,
          at: secondsSince(phase2Start, str(pepFact.recordedAt)),
          took: took(complianceRun),
        }
    : {
        ...pepBase,
        status: phase2Running ? "active" : phase2Decision ? "warn" : "idle",
        summary: phase2Running ? "Running…" : phase2Decision ? "Not recorded" : phase2Idle,
        short: phase2Running ? "Working…" : phase2Decision ? "Not recorded" : "Waiting",
      };

  const severity = str(mediaFact?.maxSeverity);
  const mediaBase = {
    id: "media",
    title: "Adverse media",
    name: "Adverse media",
    by: "Compliance agent (AgentCore harness) → AgentCore Gateway → adverse_media",
    via: "Compliance agent · adverse_media",
    icon: "document" as IconName,
  };
  const mediaStage: Stage = mediaFact
    ? !performed(mediaFact)
      ? { ...mediaBase, status: "fail", summary: notPerformed(mediaFact), short: notPerformedShort(mediaFact) }
      : {
          ...mediaBase,
          status: !severity || severity === "NONE" || severity === "LOW" ? "done" : "warn",
          summary: !severity || severity === "NONE" ? "Nothing attributed to this person" : `Highest severity: ${severity}`,
          short: !severity || severity === "NONE" ? "Nothing attributed" : `Severity ${severity}`,
          at: secondsSince(phase2Start, str(mediaFact.recordedAt)),
        }
    : {
        ...mediaBase,
        status: phase2Running ? "active" : phase2Decision ? "warn" : "idle",
        summary: phase2Running ? "Running…" : phase2Decision ? "Not recorded" : phase2Idle,
        short: phase2Running ? "Working…" : phase2Decision ? "Not recorded" : "Waiting",
      };

  const lifted = account && (account.state === "OPEN_STANDARD" || account.state === "OPEN_ENHANCED");
  // Phase 2 lifted the limits and a later event froze the account: the lift happened, and the
  // freeze is Phase 3's to explain, not this row's.
  const liftedThenFrozen =
    !lifted && account?.state === "FROZEN" && /^FULL_APPROVE/.test(str(phase2Decision?.outcome) ?? "");
  const liftStage: Stage = {
    id: "lift",
    title: "Limits lifted",
    name: "Limits lifted",
    by: "Account platform",
    via: "Account platform",
    icon: "account",
    status: lifted || liftedThenFrozen ? "done" : str(phase2Decision?.outcome) === "REJECT" ? "fail" : "idle",
    summary: liftedThenFrozen
      ? "Lifted when the full checks approved; frozen since, by Phase 3"
      : lifted
        ? account?.state === "OPEN_ENHANCED"
          ? "Payments and transfers open; a deposit limit still applies"
          : "Fully open: the earlier limits no longer apply"
        : phase2Decision
          ? "Not lifted"
          : opened
            ? "Lifted when the full checks approve"
            : phase2Idle,
    short: liftedThenFrozen
      ? "Lifted · frozen since, by Phase 3"
      : lifted
        ? account?.state === "OPEN_ENHANCED"
          ? "Transfers open · deposit limit stays"
          : "Fully open"
        : phase2Decision
          ? "Not lifted"
          : "Waiting",
    at: secondsSince(phase2Start, status?.fullDecidedAt),
  };

  const notes = trace?.notifications ?? [];
  const guarded = notes.filter((n) => n.replacedByGuard).length;
  const notifyRun = agentRun(trace, "phase2", "notify");
  // Phase 2's own execution: a Notify step that failed is caught by the state machine (the
  // decision stands), so only the step history shows it. An API from before `executions` has it
  // only when Phase 2 wrote the newest decision record.
  const phase2Steps =
    trace?.executions?.phase2?.steps ??
    (trace?.execution?.phase === "phase2" ? trace.execution.steps : []);
  const notifyFailed = phase2Steps.some((s) => s.name.startsWith("Notify") && s.status === "FAILED");
  const notifyStage: Stage = {
    id: "notify",
    title: "Customer told",
    name: "Customer told",
    by: "Notify agent (AgentCore harness, no tools) → tipping-off guard",
    via: "Notify agent · tipping-off guard",
    icon: "bell",
    status: notes.length ? "done" : notifyFailed ? "warn" : "idle",
    summary: notes.length
      ? `${notes.length} message${notes.length === 1 ? "" : "s"} sent${guarded ? `; ${guarded} rewritten by the guard` : ", as written"}`
      : notifyFailed
        ? "Not sent: the Notify agent's call failed. The decision stands"
        : "Plain words about what the account can do, never why",
    short: notes.length
      ? `${notes.length} message${notes.length === 1 ? "" : "s"}${guarded ? ` · ${guarded} rewritten by the guard` : " · as written"}`
      : notifyFailed
        ? "Not sent · the agent's call failed"
        : "Waiting",
    took: took(notifyRun),
  };

  const phase2Decided = Boolean(phase2Decision);
  const phase2Stages: Stage[] = [
    pepStage,
    mediaStage,
    decisionStage("rules-2", phase2Decision, phase2Running, secondsSince(phase2Start, status?.fullDecidedAt), opened ? "Reads the two facts above" : phase2Idle),
  ];
  const review2 = reviewStage("review-2", "phase2", trace, caseId, phase2Start);
  if (review2) phase2Stages.push(review2);
  const sealed2 = sealedStage("sealed-2", "phase2", phase2Decision, latestRecord, phase2Start, phase2Decided);
  if (review2 && review2.status === "warn" && review2.reviewCaseId && sealed2.status === "active") {
    waitForReviewer(sealed2);
  }
  phase2Stages.push(sealed2, liftStage, notifyStage);

  // ---------------------------------------------------------------- phase 3
  const phase3Decision = trace?.decisions?.phase3;
  const frozen = account?.state === "FROZEN" || state === "FROZEN";
  const phase3Outcome = str(phase3Decision?.outcome);
  const watching = !phase3Outcome && !frozen && opened;
  const monitorStage: Stage = {
    id: "monitor",
    title: "Re-screened after a list update",
    name: "Re-screen on every list update",
    by: "EventBridge → Phase 3 state machine → Pre-Screen agent (AgentCore harness) → AgentCore Gateway → sanctions_screen",
    via: "EventBridge · Phase 3 · Pre-Screen agent",
    icon: "eye",
    steady: watching,
    status: phase3Outcome ? OUTCOME_TEXT[phase3Outcome]?.status ?? "done" : frozen ? "fail" : watching ? "active" : "idle",
    summary: phase3Outcome
      ? `${OUTCOME_TEXT[phase3Outcome]?.text ?? phase3Outcome}${str(phase3Decision?.reasonCode) ? ` · ${str(phase3Decision?.reasonCode)}` : ""}`
      : frozen
        ? "Account frozen"
        : opened
          ? "Watching for list updates and risk events"
          : "Starts once the account is open",
    short: phase3Outcome
      ? `${OUTCOME_TEXT[phase3Outcome]?.short ?? phase3Outcome}${str(phase3Decision?.reasonCode) ? ` · ${str(phase3Decision?.reasonCode)}` : ""}`
      : frozen
        ? "Account frozen"
        : opened
          ? "Watching"
          : "Starts once the account is open",
    details: phase3Decision
      ? ([["Rule", str(phase3Decision.rule) ?? "—"]] as Array<[string, string]>)
      : undefined,
  };
  const phase3Stages: Stage[] = [monitorStage];
  const review3 = reviewStage("review-3", "phase3", trace, caseId, ms(trace?.phaseStartedAt?.phase3 ?? null));
  if (review3) phase3Stages.push(review3);

  // ---------------------------------------------------------------- groups
  const groups: StageGroup[] = [
    {
      id: "applicant",
      title: "On the applicant's device",
      subtitle: "Capture: everything the customer does",
      status: groupStatus(
        applicantStages,
        onDevice(["intake", "details", "documents", "liveness", "review", "submitting"]),
      ),
      stages: applicantStages,
    },
    {
      id: "phase1",
      title: "Phase 1 · Open the account",
      subtitle: `Four agents, each an AgentCore harness, gather evidence in parallel; the rules decide. Target ${TARGET_SECONDS} s.`,
      status: !caseExists ? "idle" : phaseGroupStatus(phase1Decision, phase1Stages, phase1Running),
      label: awaitingReview1 ? "With a reviewer" : undefined,
      stages: phase1Stages,
      clock: caseExists && (phase1Running || awaitingReview1) ? nowOffset : null,
      startedAt: phase1StartOffset,
      took: phase1Decided ? secondsSince(attemptStart, phase1DecidedIso) : null,
    },
    {
      id: "phase2",
      title: "Phase 2 · Full checks",
      subtitle: "While the customer already uses the account: PEP, adverse media, then the limits.",
      status: !opened ? "idle" : phaseGroupStatus(phase2Decision, phase2Stages, phase2Running),
      stages: phase2Stages,
      clock: phase2Running && phase2Start !== null ? Math.max(0, (now - phase2Start) / 1000) : null,
      startedAt: opened ? secondsSince(attemptStart, phase2StartIso) : null,
      took: secondsSince(phase2Start, status?.fullDecidedAt),
    },
    {
      id: "phase3",
      title: "Phase 3 · Ongoing monitoring",
      subtitle: "For as long as the account is open: list updates and risk events re-assess it.",
      status: !opened && !phase3Decision ? "idle" : watching && !review3 ? "active" : groupStatus(phase3Stages, false),
      label: frozen ? "Account frozen" : review3?.reviewCaseId ? "With a reviewer" : undefined,
      stages: phase3Stages,
      steady: watching && !review3,
    },
  ];

  // ---------------------------------------------------------------- headline
  // A frozen or restricted account says so first, whatever the case is waiting on.
  const riskState = trace?.applicant.riskState ?? null;
  const riskInfo =
    riskState === "FROZEN" || riskState === "RESTRICTED" ? STATE_LABEL[riskState] : undefined;
  const stateInfo = riskInfo ?? (state ? STATE_LABEL[state] : undefined);
  const declinedLabel =
    state === "DECIDED" && str(phase1Decision?.outcome) === "REJECT"
      ? { label: "Declined", tone: "danger" as Tone }
      : undefined;
  const story = describe({
    caseExists,
    state,
    riskState: trace?.applicant.riskState ?? null,
    deviceStep: deviceStep ?? null,
    status,
    account,
    phase1Decision,
    phase2Running,
    phase3Decision,
    reviewPhase: trace?.review?.phase ?? null,
    reviewReason: trace?.review?.reason ?? null,
  });
  const headline: ProcessHeadline = {
    name: declaredName ?? "New application",
    caseId,
    applicantId,
    jurisdiction: trace?.applicant.jurisdiction ?? input.jurisdiction ?? null,
    stateLabel: declinedLabel?.label ?? stateInfo?.label ?? (caseExists ? "Submitted" : deviceStep ? "Applying on the device" : "Not started"),
    stateTone: declinedLabel?.tone ?? stateInfo?.tone ?? (caseExists ? "info" : "neutral"),
    outcome: story.outcome,
    detail: story.detail,
    timer: (() => {
      if (!caseExists || attemptStart === null) {
        return { mode: "idle" as const, seconds: null, label: "Time to decision", tone: "neutral" as Tone };
      }
      if (phase1Running) {
        return {
          mode: "running" as const,
          seconds: Math.max(0, (now - attemptStart) / 1000),
          label: "Time to decision",
          tone: "info" as Tone,
        };
      }
      const decidedMs = ms(phase1DecidedIso);
      const seconds = decidedMs !== null ? Math.max(0, (decidedMs - attemptStart) / 1000) : null;
      // The 90-second figure is for the automated path. When the rules referred the case, say how
      // long it then waited for a person, so the number is not read as the machine's time.
      const referredMs = ms(phase1Outcome?.at);
      const reviewedMs = review1 && review1.status !== "warn" ? ms(str(trace?.reviews?.phase1?.decidedAt)) : null;
      const waited = referredMs !== null && reviewedMs !== null ? (reviewedMs - referredMs) / 1000 : null;
      const opened = Boolean(status?.provisionalDecidedAt);
      const outcome = str(phase1Decision?.outcome);
      const label = opened
        ? "Account opened in"
        : state === "AWAITING_REVIEW"
          ? "Referred to a person in"
          : state === "AWAITING_APPLICANT"
            ? "Rules answered in"
            : outcome === "REJECT"
              ? "Declined in"
              : "Phase 1 decided in";
      const tone: Tone = opened
        ? seconds !== null && seconds <= TARGET_SECONDS ? "success" : "warning"
        : outcome === "REJECT"
          ? "danger"
          : "warning";
      return {
        mode: "stopped" as const,
        seconds,
        label,
        tone,
        note: waited !== null && waited > 0 ? `incl. ${waited.toFixed(1)} s waiting for a reviewer` : null,
      };
    })(),
  };

  // ---------------------------------------------------------------- the state log
  const events: ProcessEvent[] = transitions.map((t) => ({
    at: t.at ?? "",
    label: t.reason ? sentence(t.reason) : `${t.from ?? "—"} → ${t.to}`,
    actor: actorLabel(t.actor),
    tone: STATE_LABEL[t.to]?.tone ?? "neutral",
  }));

  const backstage = buildBackstage({
    trace,
    status,
    now,
    groups,
    attemptStart,
    available: input.traceAvailable ?? true,
  });

  return { headline, groups, events, backstage };
}

/** A velocity count as words: "1 person", "3 people". */
function people(value: unknown): string {
  const n = value === null || value === undefined || value === "" ? NaN : Number(value);
  if (!Number.isFinite(n)) return "an unknown number of people";
  return n === 1 ? "1 person" : `${n} people`;
}

function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function lowerFirst(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1);
}

interface StoryInput {
  caseExists: boolean;
  state: string | null;
  riskState: string | null;
  deviceStep: FlowStep | null;
  status: CaseStatusResponse | null;
  account: AccountView | null;
  phase1Decision: Record<string, unknown> | null | undefined;
  phase2Running: boolean;
  phase3Decision: Record<string, unknown> | null | undefined;
  reviewPhase: string | null;
  reviewReason: string | null;
}

/**
 * The headline: where the case stands in a few words, and one line on why or what comes next.
 * Read by an audience, so it says what the system is doing rather than naming a state.
 */
function describe(s: StoryInput): { outcome: string; detail: string | null } {
  if (!s.caseExists) {
    switch (s.deviceStep) {
      case "intake":
        return {
          outcome: "Applying on the phone",
          detail: "Consent comes first: the face check needs the applicant's agreement",
        };
      case "details":
        return {
          outcome: "Applying on the phone",
          detail: "The declared details come first: the agents check the document against them",
        };
      case "documents":
        return {
          outcome: "Photographing the ID document",
          detail: "Uploaded straight to the S3 evidence bucket, with an integrity checksum",
        };
      case "liveness":
        return {
          outcome: "Selfie video",
          detail: "Amazon Rekognition Face Liveness: the video streams from the browser to Rekognition",
        };
      case "review":
        return {
          outcome: "Checking before submitting",
          detail: "Nothing reaches the bank until Submit. Then four agents start at once.",
        };
      case "submitting":
        return { outcome: "Submitting", detail: "Four agents start the moment the case exists" };
      default:
        return {
          outcome: "Ready for an application",
          detail: "Start on the phone. Four agents start the moment it is submitted.",
        };
    }
  }

  const p1Explanation = str(s.phase1Decision?.explanation);
  const p3Explanation = str(s.phase3Decision?.explanation);

  if (s.riskState === "FROZEN" || s.account?.state === "FROZEN") {
    return {
      outcome: "Account frozen",
      detail:
        p3Explanation ??
        (s.state === "AWAITING_REVIEW"
          ? "A list update matched this customer: frozen at once, and a reviewer confirms"
          : "Frozen after a list update"),
    };
  }

  switch (s.state) {
    case "RECEIVED":
      return {
        outcome: "Submitted · queued for Phase 1",
        detail: "Amazon SQS FIFO → EventBridge Pipes → AWS Step Functions",
      };
    case "IN_PROGRESS":
      return {
        outcome: "Four agents are gathering the evidence",
        detail: "In parallel, each through AgentCore Gateway to its evidence tool. Each tool records a fact; the rules decide on the facts.",
      };
    case "AWAITING_APPLICANT":
      return {
        outcome: "Waiting for the applicant",
        detail:
          p1Explanation ??
          (s.status?.requiredItem ? `Asked for: ${s.status.requiredItem}` : "The rules asked for one more thing"),
      };
    case "AWAITING_REVIEW": {
      if (s.reviewPhase === "phase3") {
        return { outcome: "With a reviewer", detail: `After a list update: ${reasonText(s.reviewReason)}` };
      }
      if (s.reviewPhase === "phase2") {
        return { outcome: "Open, with a reviewer", detail: `The full checks referred it: ${reasonText(s.reviewReason)}` };
      }
      return {
        outcome: "Referred to a person",
        detail: p1Explanation ? `The rules referred it: ${lowerFirst(p1Explanation)}` : `Referred: ${reasonText(s.reviewReason)}`,
      };
    }
    case "PROVISIONAL":
      return {
        outcome: "Account open, with limits",
        detail: s.phase2Running
          ? "Phase 2 is running the full checks while the customer already uses the account"
          : "The limits stay until the full checks approve",
      };
    case "MONITORING":
    case "RESTRICTED":
      if (s.account?.state === "OPEN_ENHANCED") {
        return {
          outcome: "Account open",
          detail: "Transfers open, a deposit limit stays, enhanced monitoring · watching for list updates",
        };
      }
      if (s.account?.state === "OPEN_RESTRICTED" || s.riskState === "RESTRICTED") {
        return { outcome: "Account restricted", detail: p3Explanation ?? "Restricted after a re-assessment" };
      }
      return {
        outcome: "Account fully open",
        detail: "The limits are lifted · watching for list updates",
      };
    case "DECIDED":
      if (str(s.phase1Decision?.outcome) === "REJECT") {
        return { outcome: "Application declined", detail: p1Explanation };
      }
      return { outcome: "Decided", detail: null };
    default:
      return { outcome: "Submitted", detail: null };
  }
}

const COGNITO_SUB = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function actorLabel(actor: string | null): string {
  if (!actor) return "system";
  if (actor.startsWith("applicant:")) return "applicant";
  // A review is recorded under the reviewer's bare Cognito sub (`review/decisions.py`).
  if (actor.startsWith("reviewer:") || COGNITO_SUB.test(actor)) return "reviewer";
  return actor;
}
