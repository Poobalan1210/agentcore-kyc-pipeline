import type {
  AgentNarrative,
  CaseStatusResponse,
  CaseTraceResponse,
  ExecutionStep,
  ExecutionTrace,
  FactBase,
} from "@shared/api/client";
import { guardReason } from "../review/labels";
import type { Stage, StageGroup, StageStatus } from "./pipeline";

/**
 * The live process as timelines, for the Backstage view: each phase on its own clock, lane by
 * lane, then one feed of everything the services wrote down. Pure, like `pipeline.ts`.
 *
 * Nothing here is simulated. A bar is a state Step Functions entered and left (its execution
 * history, read by the trace route); a dot is the moment a tool's fact landed on the case record;
 * a feed line is a state change, a step, a fact or a message, at the time it was recorded. The
 * results down the right are the checklist's own words (`pipeline.ts`), so the two views agree.
 *
 * What the system does not record, this does not draw: the agents' turns inside a harness (each
 * model call, each tool call's arguments) are not emitted as spans by the harness surface
 * (`audit/spans.py`), so an agent's lane is its task, from invoke to answer, with its tool's fact
 * marked where it landed.
 */

export type LaneTone = "queue" | "sfn" | "agent" | "rules" | "seal" | "account" | "review" | "notify";
export type SegmentStatus = "done" | "running" | "failed";
export type FeedSource =
  | "api"
  | "ddb"
  | "pipes"
  | "sfn"
  | "events"
  | "agent"
  | "tool"
  | "rules"
  | "seal"
  | "account"
  | "notify"
  | "review";
export type FeedTone = "neutral" | "success" | "warning" | "danger";

/** One state, entered and left: seconds from the phase's zero. */
export interface Segment {
  start: number;
  /** Null while it runs: drawn to now. */
  end: number | null;
  status: SegmentStatus;
  /** Inside the bar, when the bar is wide enough. */
  label?: string;
  /** The state as Step Functions names it, and how long it took. */
  title: string;
}

export interface Mark {
  at: number;
  /** A tool's fact landing, a moment (Submit, an event), a message to the customer. */
  kind: "fact" | "flag" | "message";
  label?: string;
  title: string;
}

export interface Lane {
  id: string;
  name: string;
  /** What does the work, in a few words: the model and tokens, or the service. */
  sub: string;
  tone: LaneTone;
  /** One branch of a Parallel state: the four evidence agents, bracketed together. */
  parallel?: boolean;
  segments: Segment[];
  marks: Mark[];
  status: StageStatus;
  result: string | null;
  /** A person is needed: the case to open in the review queue. */
  reviewCaseId?: string | null;
  /** The checklist row this lane does the work of: its detail opens under the lane. */
  stage?: Stage;
  facts?: Array<[string, string]>;
}

export interface PhaseTimeline {
  id: "phase1" | "phase2" | "phase3";
  title: string;
  subtitle: string;
  status: StageStatus;
  /** The checklist's name for the state, when the status alone would mislead ("Account frozen"). */
  label?: string;
  /** Active with no end (monitoring). */
  steady?: boolean;
  running: boolean;
  /** Seconds it has run, or ran. */
  clock: number | null;
  /** The axis: seconds from zero, and where its ticks go. */
  span: number;
  ticks: number[];
  /** The 90-second line, when the axis reaches it. */
  target: number | null;
  decided: { at: number; label: string } | null;
  /** Seconds from zero now, while the phase runs. */
  now: number | null;
  lanes: Lane[];
  /** Said over the lanes while nothing has run, or in their place. */
  note: string | null;
  executionName: string | null;
  consoleUrl: string | null;
}

export interface FeedItem {
  /** Milliseconds since the epoch: the order, and how new it is. */
  t: number;
  at: string;
  service: string;
  source: FeedSource;
  text: string;
  tone: FeedTone;
}

export interface Backstage {
  /** False when the execution history cannot be read (not a reviewer): the checklist instead. */
  available: boolean;
  phases: PhaseTimeline[];
  feed: FeedItem[];
  /** The clock the view was built at, for what counts as new in the feed. */
  now: number;
}

export interface BackstageInput {
  trace: CaseTraceResponse | null;
  status: CaseStatusResponse | null;
  now: number;
  /** The checklist, whose stages give each lane its result. */
  groups: StageGroup[];
  /** When this attempt was submitted (ms): Phase 1's zero. */
  attemptStart: number | null;
  available: boolean;
}

const TARGET_SECONDS = 90;

// --------------------------------------------------------------------------------- helpers

function ms(iso?: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

function str(value: unknown): string | null {
  return value === undefined || value === null || value === "" ? null : String(value);
}

function offset(iso: string | null | undefined, zero: number | null): number | null {
  const t = ms(iso);
  if (t === null || zero === null) return null;
  return Math.max(0, (t - zero) / 1000);
}

/** "8.1 s", "640 ms", "2:05". */
function duration(seconds: number): string {
  if (seconds < 1) return `${Math.max(1, Math.round(seconds * 1000))} ms`;
  if (seconds < 90) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function stageById(groups: StageGroup[], id: string): Stage | undefined {
  for (const group of groups) {
    const found = group.stages.find((s) => s.id === id);
    if (found) return found;
  }
  return undefined;
}

function worst(...statuses: Array<StageStatus | undefined>): StageStatus {
  const order: StageStatus[] = ["fail", "active", "warn", "done", "skip", "idle"];
  for (const status of order) if (statuses.includes(status)) return status;
  return "idle";
}

function machineName(exec: ExecutionTrace | null): string | null {
  // arn:aws:states:<region>:<account>:execution:<machine>:<name>
  const parts = exec?.executionArn.split(":") ?? [];
  return parts.length >= 8 ? parts[6] : null;
}

function executionName(exec: ExecutionTrace | null): string | null {
  const parts = exec?.executionArn.split(":") ?? [];
  return parts.length >= 8 ? parts.slice(7).join(":") : null;
}

/** The execution's page in the Step Functions console: its graph, its events, its state inputs. */
function consoleUrl(exec: ExecutionTrace | null): string | null {
  const region = exec?.executionArn.split(":")[3];
  if (!exec || !region) return null;
  return (
    `https://${region}.console.aws.amazon.com/states/home?region=${region}` +
    `#/v2/executions/details/${exec.executionArn}`
  );
}

/** `us.amazon.nova-lite-v1:0` -> "Nova Lite"; `us.amazon.nova-2-lite-v1:0` -> "Nova 2 Lite". */
function modelName(id?: string | null): string | null {
  if (!id) return null;
  const nova = /nova-(2-)?(lite|micro|pro|premier|sonic)/i.exec(id);
  if (nova) {
    const size = nova[2].toLowerCase();
    return `Nova ${nova[1] ? "2 " : ""}${size.charAt(0).toUpperCase()}${size.slice(1)}`;
  }
  return id.split(".").pop()?.replace(/-v\d+(:\d+)?$/, "") ?? id;
}

function tokens(n?: number | null): string | null {
  return n ? `${n.toLocaleString("en-US")} tokens` : null;
}

function agentRun(trace: CaseTraceResponse | null, phase: string, agent: string): AgentNarrative | undefined {
  const runs = (trace?.agents ?? []).filter((a) => a.phase === phase && a.agent === agent);
  return runs.length ? runs[runs.length - 1] : undefined;
}

/** A fact recorded in this window, or nothing: an older one belongs to an earlier run. */
function factIn(fact: FactBase | null | undefined, since: number | null): FactBase | null {
  if (!fact) return null;
  const at = ms(str(fact.recordedAt));
  if (at === null) return null;
  if (since !== null && at < since - 2000) return null;
  return fact;
}

const SEGMENT_STATUS: Record<string, SegmentStatus> = {
  SUCCEEDED: "done",
  RUNNING: "running",
  FAILED: "failed",
};

function segmentOf(step: ExecutionStep, zero: number | null, label?: string): Segment | null {
  const start = offset(step.startedAt, zero);
  if (start === null) return null;
  const end = step.endedAt ? offset(step.endedAt, zero) : null;
  const status = SEGMENT_STATUS[step.status] ?? (step.endedAt ? "done" : "running");
  const took = end !== null ? duration(Math.max(0, end - start)) : "running";
  return {
    start,
    end,
    status,
    label: label ?? step.name,
    title: `${step.name} · ${took}${status === "failed" ? " · an attempt failed" : ""}`,
  };
}

function segmentsOf(steps: ExecutionStep[], zero: number | null, label?: (s: ExecutionStep) => string): Segment[] {
  return steps
    .map((s) => segmentOf(s, zero, label?.(s)))
    .filter((s): s is Segment => s !== null);
}

function laneStatusOf(steps: ExecutionStep[]): StageStatus {
  if (steps.some((s) => s.status === "FAILED")) return "fail";
  if (steps.some((s) => s.status === "RUNNING")) return "active";
  return steps.length ? "done" : "idle";
}

// ------------------------------------------------------------------- what each step is

type StepKind = "agent" | "notify" | "briefing" | "rules" | "seal" | "account" | "review" | "history" | "other";

const AGENT_KEYS = ["documents", "identity", "fraud", "prescreen", "compliance", "notify", "briefing"];

/** Each agent task's state name, for a step recorded before the history said what it called. */
const AGENT_BY_STATE: Record<string, string> = {
  Documents: "documents",
  Identity: "identity",
  Fraud: "fraud",
  PreScreen: "prescreen",
  ReScreen: "prescreen",
  ComplianceRisk: "compliance",
  NotifyProvisional: "notify",
  NotifyOutcome: "notify",
  Briefing: "briefing",
};

const RULES_STATES = new Set(["PreliminaryTier", "FullTier", "Reassess"]);
const SEAL_STATES = new Set([
  "WriteDecisionRecord",
  "RecordOutcome",
  "ReviewApproved",
  "ReviewRejectedRecord",
  "ReviewStepUpRecord",
  "RecordStandardApproval",
  "RecordEnhancedApproval",
  "WriteTightenedRecord",
  "WriteFrozenRecord",
  "RecordReviewedApproval",
  "WriteReviewedFreezeRecord",
  "WriteReviewedTightenRecord",
  "RecordAudit",
  "RecordReviewedClearance",
  "RecordReviewedExit",
]);
const HISTORY_STATES = new Set(["AppendDecision", "AppendReviewClearance", "AppendReviewExit"]);

/** What the account platform was asked to do, as the feed says it. */
const ACCOUNT_STATES: Record<string, string> = {
  OpenAccount: "Account opened, with limits",
  ReadAccount: "Read the account's limits",
  LiftAllRestrictions: "Limits lifted",
  LiftTransfersKeepCeiling: "Transfers allowed; the deposit limit stays",
  Tighten: "Account tightened",
  FreezeAccount: "Account frozen",
  ReviewLiftTransfersKeepCeiling: "Transfers allowed, after the review",
  ReviewFreezeAccount: "Account frozen by the reviewer",
  ReviewTighten: "Account tightened by the reviewer",
  RestrictForReview: "Account restricted until a person decides",
  ReapplyRestrictions: "Restrictions put back",
  RestoreStandard: "Posture restored after the review",
  RestoreEnhanced: "Posture restored after the review",
  RestoreTightened: "Posture restored after the review",
  RestoreProvisional: "Posture restored after the review",
};

/** What a running top-level state is doing, in a few words. */
const RUNNING_PHRASE: Record<string, string> = {
  Phase1Parallel: "four agents at work",
  AwaitReviewDecision: "waiting for a reviewer",
  WaitForOpenReview: "waiting for the open review",
  NotifyProvisional: "writing the customer's message",
  NotifyOutcome: "writing the customer's message",
  ComplianceRisk: "compliance checks",
  ReScreen: "re-screening",
  Briefing: "briefing the reviewer",
};

function agentKeyOf(step: ExecutionStep): string | null {
  // A harness is `kyc90_<agent>-<id>`.
  const target = step.target ?? "";
  for (const key of AGENT_KEYS) if (target.includes(`_${key}-`)) return key;
  if (step.resource && !step.resource.includes("invokeHarness")) return null;
  return AGENT_BY_STATE[step.name] ?? null;
}

function kindOf(step: ExecutionStep): StepKind {
  const resource = step.resource ?? "";
  const target = step.target ?? "";
  if (resource.includes("waitForTaskToken") || step.name === "AwaitReviewDecision") return "review";
  const agent = agentKeyOf(step);
  if (agent === "notify") return "notify";
  if (agent === "briefing") return "briefing";
  if (agent) return "agent";
  if (resource.startsWith("lambda")) {
    if (target.includes("decision")) return "rules";
    if (target.includes("audit")) return "seal";
    if (target.includes("account")) return "account";
  }
  if (RULES_STATES.has(step.name)) return "rules";
  if (SEAL_STATES.has(step.name)) return "seal";
  if (step.name in ACCOUNT_STATES) return "account";
  if (resource.includes("dynamodb:putItem") || HISTORY_STATES.has(step.name)) return "history";
  return "other";
}

// --------------------------------------------------------------------------------- the axis

const TICK_STEPS = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600];

function axis(extent: number, min: number): { span: number; ticks: number[] } {
  const want = Math.max(min, extent * 1.06);
  const step = TICK_STEPS.find((s) => want / s <= 6) ?? TICK_STEPS[TICK_STEPS.length - 1];
  const span = Math.ceil(want / step) * step;
  const ticks: number[] = [];
  for (let t = 0; t <= span + 1e-6; t += step) ticks.push(t);
  return { span, ticks };
}

function extentOf(lanes: Lane[], extra: Array<number | null>): number {
  let extent = 0;
  for (const lane of lanes) {
    for (const s of lane.segments) extent = Math.max(extent, s.end ?? s.start);
    for (const m of lane.marks) extent = Math.max(extent, m.at);
  }
  for (const value of extra) if (value !== null) extent = Math.max(extent, value);
  return extent;
}

// --------------------------------------------------------------------------------- lanes

function sfnLane(exec: ExecutionTrace | null, zero: number | null, fallbackMachine: string): Lane {
  const top = (exec?.steps ?? []).filter((s) => !s.depth);
  const failed = top.find((s) => s.status === "FAILED");
  const runningStep = [...top].reverse().find((s) => s.status === "RUNNING");
  const status = exec?.status ?? null;
  const ran = exec ? offset(exec.stopDate, ms(exec.startDate)) : null;
  const ended = (status ?? "STOPPED").toLowerCase().replace(/_/g, " ");
  const result = !exec
    ? null
    : status === "RUNNING"
      ? `Running · ${runningStep ? RUNNING_PHRASE[runningStep.name] ?? runningStep.name : "starting"}`
      : status === "SUCCEEDED"
        ? `Succeeded · ${top.length} states${ran !== null ? ` in ${duration(ran)}` : ""}`
        : `${ended.charAt(0).toUpperCase()}${ended.slice(1)}${failed ? ` at ${failed.name}` : ""}`;
  const facts: Array<[string, string]> = [];
  if (exec) {
    facts.push(["Execution", executionName(exec) ?? exec.executionArn]);
    facts.push(["Status", status ?? "—"]);
    facts.push(["States entered", String(exec.steps.length)]);
  }
  return {
    id: "sfn",
    name: "Step Functions",
    sub: machineName(exec) ?? fallbackMachine,
    tone: "sfn",
    segments: segmentsOf(top, zero),
    marks: [],
    status: !exec
      ? "idle"
      : status === "RUNNING"
        ? "active"
        : status === "SUCCEEDED"
          ? "done"
          : "fail",
    result,
    facts,
  };
}

interface AgentSpec {
  key: string;
  states: string[];
  name: string;
  tools: string;
  facts: Array<keyof NonNullable<CaseTraceResponse["facts"]>>;
}

function agentLane(
  spec: AgentSpec,
  phase: "phase1" | "phase2" | "phase3",
  ctx: { trace: CaseTraceResponse | null; steps: ExecutionStep[]; zero: number | null; since: number | null },
  result: { stage?: Stage; status: StageStatus; text: string | null },
  options: { parallel?: boolean; tone?: LaneTone } = {},
): Lane {
  const steps = ctx.steps.filter((s) => spec.states.includes(s.name) && kindOf(s) !== "other");
  const run = agentRun(ctx.trace, phase, spec.key);
  const model = modelName(run?.model);
  const used = tokens(run?.totalTokens);
  const marks: Mark[] = [];
  for (const name of spec.facts) {
    const fact = factIn(ctx.trace?.facts?.[name] ?? null, ctx.since);
    const at = offset(str(fact?.recordedAt), ctx.zero);
    if (fact && at !== null) {
      marks.push({ at, kind: "fact", title: `${FACT_TOOL[name]} recorded its fact · +${duration(at)}` });
    }
  }
  const facts: Array<[string, string]> = [["Tools", spec.tools]];
  if (run?.model) facts.push(["Model", run.model]);
  if (run?.totalTokens) facts.push(["Tokens", run.totalTokens.toLocaleString("en-US")]);
  if (run?.latencyMs) facts.push(["Harness time", duration(run.latencyMs / 1000)]);
  if (run?.stopReason) facts.push(["Stop reason", run.stopReason]);
  return {
    id: spec.key,
    name: spec.name,
    // The model and what it used once the run is on the record; until then, the tool it calls.
    sub: model || used ? [model, used].filter(Boolean).join(" · ") : spec.tools,
    tone: options.tone ?? "agent",
    parallel: options.parallel,
    segments: segmentsOf(steps, ctx.zero, () => spec.tools),
    marks,
    status: steps.length || marks.length ? result.status : result.stage?.status ?? "idle",
    result: result.text,
    stage: result.stage,
    facts,
  };
}

/** The tool that records each fact, for the dot's title and the feed. */
const FACT_TOOL: Record<keyof NonNullable<CaseTraceResponse["facts"]>, string> = {
  documentExtraction: "s3_get_document",
  faceMatch: "rekognition_compare_faces",
  velocity: "velocity_check",
  screening: "sanctions_screen",
  pep: "pep_lookup",
  adverseMedia: "adverse_media",
};

/** The checklist stage whose words describe each fact. */
const FACT_STAGE: Record<keyof NonNullable<CaseTraceResponse["facts"]>, string> = {
  documentExtraction: "doc-read",
  faceMatch: "face-match",
  velocity: "fraud",
  screening: "screening",
  pep: "pep",
  adverseMedia: "media",
};

function kindLane(
  id: string,
  name: string,
  sub: string,
  tone: LaneTone,
  steps: ExecutionStep[],
  zero: number | null,
  stage: Stage | undefined,
  label?: (s: ExecutionStep) => string,
): Lane {
  return {
    id,
    name,
    sub,
    tone,
    segments: segmentsOf(steps, zero, label),
    marks: [],
    status: stage?.status ?? laneStatusOf(steps),
    result: stage?.short ?? stage?.summary ?? (steps.length ? "Done" : null),
    reviewCaseId: stage?.reviewCaseId ?? null,
    stage,
  };
}

function reviewLanes(
  steps: ExecutionStep[],
  zero: number | null,
  stage: Stage | undefined,
  run: AgentNarrative | undefined,
): Lane[] {
  const lanes: Lane[] = [];
  const briefing = steps.filter((s) => kindOf(s) === "briefing");
  if (briefing.length) {
    lanes.push({
      id: "briefing",
      name: "Briefing agent",
      sub: [modelName(run?.model), tokens(run?.totalTokens)].filter(Boolean).join(" · ") ||
        "Explains, never decides",
      tone: "agent",
      segments: segmentsOf(briefing, zero, () => "briefing"),
      marks: [],
      status: laneStatusOf(briefing),
      result: briefing.some((s) => s.status === "RUNNING") ? "Writing the briefing" : "Briefing written, never decides",
    });
  }
  const waits = steps.filter((s) => kindOf(s) === "review");
  if (waits.length || stage) {
    lanes.push({
      id: "review",
      name: "A person decides",
      sub: "SQS · task token wait",
      tone: "review",
      segments: segmentsOf(waits, zero, (s) => (s.status === "RUNNING" ? "waiting for a reviewer" : "reviewed")),
      marks: [],
      status: stage?.status ?? laneStatusOf(waits),
      result: stage?.short ?? stage?.summary ?? null,
      reviewCaseId: stage?.reviewCaseId ?? null,
      stage,
    });
  }
  return lanes;
}

// --------------------------------------------------------------------------------- phase 1

function phase1(input: BackstageInput): PhaseTimeline {
  const { trace, status, now, groups, attemptStart } = input;
  const group = groups.find((g) => g.id === "phase1");
  const caseExists = Boolean(status || trace);
  const state = trace?.applicant.state ?? status?.state ?? null;
  let exec = trace?.executions?.phase1 ?? (trace?.execution?.phase === "phase1" ? trace.execution : null) ?? null;
  // A resubmission is queued and its run not claimed yet: the case still names the last attempt's.
  if (exec && attemptStart !== null && (ms(exec.startDate) ?? 0) < attemptStart - 2000) exec = null;
  const zero = attemptStart ?? ms(exec?.startDate) ?? null;
  const running = exec?.status === "RUNNING" || (caseExists && !exec && (state === "RECEIVED" || state === "IN_PROGRESS"));
  const steps = exec?.steps ?? [];
  const nowOffset = zero !== null && caseExists ? Math.max(0, (now - zero) / 1000) : null;
  const ctx = { trace, steps, zero, since: attemptStart };

  const lanes: Lane[] = [];
  const queued = stageById(groups, "queued");
  const handedOver = offset(exec?.startDate, zero);
  lanes.push({
    id: "ingress",
    name: "Submit → queue",
    sub: "SQS FIFO → EventBridge Pipes",
    tone: "queue",
    segments:
      caseExists && zero !== null
        ? [{
            start: 0,
            end: handedOver ?? (state === "RECEIVED" ? null : 0),
            status: handedOver === null && state === "RECEIVED" ? "running" : "done",
            title: handedOver !== null ? `Queued · ${duration(handedOver)}` : "Queued",
          }]
        : [],
    marks: caseExists && zero !== null ? [{ at: 0, kind: "flag", label: "Submit", title: "The applicant pressed Submit" }] : [],
    status: queued?.status ?? "idle",
    result: queued?.short ?? null,
    stage: queued,
  });
  lanes.push(sfnLane(exec, zero, "kyc90-phase1"));

  const specs: Array<AgentSpec & { stage: string }> = [
    { key: "documents", states: ["Documents"], name: "Documents agent", tools: "s3_get_document → Textract", facts: ["documentExtraction"], stage: "doc-read" },
    { key: "identity", states: ["Identity"], name: "Identity agent", tools: "rekognition_compare_faces", facts: ["faceMatch"], stage: "face-match" },
    { key: "fraud", states: ["Fraud"], name: "Fraud agent", tools: "velocity_check", facts: ["velocity"], stage: "fraud" },
    { key: "prescreen", states: ["PreScreen"], name: "Pre-Screen agent", tools: "sanctions_screen", facts: ["screening"], stage: "screening" },
  ];
  for (const spec of specs) {
    const stage = stageById(groups, spec.stage);
    lanes.push(
      agentLane(spec, "phase1", ctx, { stage, status: stage?.status ?? "idle", text: stage?.short ?? null }, { parallel: true }),
    );
  }

  const rules = stageById(groups, "rules-1");
  lanes.push(kindLane("rules", "Decision engine", "Lambda · rules, no model", "rules",
    steps.filter((s) => kindOf(s) === "rules"), zero, rules, () => "rules"));
  lanes.push(...reviewLanes(steps, zero, stageById(groups, "review-1"), agentRun(trace, "phase1", "briefing")));
  lanes.push(kindLane("seal", "Decision record", "Lambda → S3 Object Lock", "seal",
    steps.filter((s) => kindOf(s) === "seal"), zero, stageById(groups, "sealed-1"), () => "sealed"));
  lanes.push(kindLane("account", "Account platform", "Lambda · enforces the limits", "account",
    steps.filter((s) => kindOf(s) === "account"), zero, stageById(groups, "account"), () => "open"));

  // The rules' answer: the first state change out of IN_PROGRESS in this attempt.
  const answer = (trace?.transitions ?? []).find((t) => {
    const at = ms(t.at);
    return t.from === "IN_PROGRESS" && at !== null && (attemptStart === null || at >= attemptStart - 1000);
  });
  const answeredAt = offset(answer?.at, zero);
  const decided =
    answeredAt !== null
      ? {
          at: answeredAt,
          label: `${
            answer?.to === "PROVISIONAL"
              ? "account open"
              : answer?.to === "AWAITING_REVIEW"
                ? "referred to a person"
                : answer?.to === "AWAITING_APPLICANT"
                  ? "asked for more"
                  : "decided"
          } · ${duration(answeredAt)}`,
        }
      : null;

  const ran = exec ? offset(exec.stopDate, zero) : null;
  const { span, ticks } = axis(extentOf(lanes, [running ? nowOffset : null, decided?.at ?? null, ran]), 30);
  const anything = lanes.some((l) => l.segments.length || l.marks.length);
  return {
    id: "phase1",
    title: group?.title ?? "Phase 1 · Open the account",
    subtitle: group?.subtitle ?? "",
    status: group?.status ?? "idle",
    label: group?.label,
    running,
    clock: running ? nowOffset : ran ?? decided?.at ?? null,
    span,
    ticks,
    target: span >= TARGET_SECONDS ? TARGET_SECONDS : null,
    decided,
    now: running ? nowOffset : null,
    lanes,
    note: !caseExists
      ? "Starts the moment the applicant presses Submit"
      : !anything
        ? "This run's history is not available: it ran before the live view recorded runs, or it has aged out"
        : null,
    executionName: executionName(exec),
    consoleUrl: consoleUrl(exec),
  };
}

// --------------------------------------------------------------------------------- phase 2

function phase2(input: BackstageInput): PhaseTimeline {
  const { trace, status, now, groups } = input;
  const group = groups.find((g) => g.id === "phase2");
  const exec = trace?.executions?.phase2 ?? (trace?.execution?.phase === "phase2" ? trace.execution : null) ?? null;
  const zero = ms(exec?.startDate);
  const opened = Boolean(status?.provisionalDecidedAt || trace?.account);
  const running = exec?.status === "RUNNING";
  const steps = exec?.steps ?? [];
  const nowOffset = zero !== null ? Math.max(0, (now - zero) / 1000) : null;
  const ctx = { trace, steps, zero, since: zero };

  const lanes: Lane[] = [sfnLane(exec, zero, "kyc90-phase2")];

  const notifyStage = stageById(groups, "notify");
  const notifyRun = agentRun(trace, "phase2", "notify");
  const notifySteps = steps.filter((s) => kindOf(s) === "notify");
  const messages: Mark[] = [];
  for (const note of trace?.notifications ?? []) {
    const at = offset(note.at, zero);
    if (at === null || (zero !== null && (ms(note.at) ?? 0) < zero - 2000)) continue;
    const why = guardReason(note.replacedBecause);
    messages.push({
      at,
      kind: "message",
      title: note.replacedByGuard
        ? `The guard replaced the agent's message with the fixed wording${why ? `: ${why}` : ""}`
        : "The message passed the guard as written",
    });
  }
  lanes.push({
    id: "notify",
    name: "Notify agent",
    sub: [modelName(notifyRun?.model), tokens(notifyRun?.totalTokens)].filter(Boolean).join(" · ") ||
      "Message → tipping-off guard",
    tone: "notify",
    segments: segmentsOf(notifySteps, zero, (s) => (s.name === "NotifyOutcome" ? "outcome message" : "first message")),
    marks: messages,
    status: notifyStage?.status ?? laneStatusOf(notifySteps),
    result: notifyStage?.short ?? null,
    stage: notifyStage,
  });

  if (steps.some((s) => s.name === "ReScreen")) {
    const screening = stageById(groups, "screening");
    lanes.push(agentLane(
      { key: "prescreen", states: ["ReScreen"], name: "Pre-Screen agent", tools: "sanctions_screen", facts: ["screening"] },
      "phase2", ctx, { stage: screening, status: laneStatusOf(steps.filter((s) => s.name === "ReScreen")), text: "Re-screened: the list changed since Phase 1" },
    ));
  }

  const pep = stageById(groups, "pep");
  const media = stageById(groups, "media");
  lanes.push(agentLane(
    { key: "compliance", states: ["ComplianceRisk"], name: "Compliance agent", tools: "pep_lookup, adverse_media", facts: ["pep", "adverseMedia"] },
    "phase2", ctx,
    {
      stage: pep,
      status: worst(pep?.status, media?.status),
      // Both checks say "Working…" at once while the agent runs: say it once.
      text: [...new Set([pep?.short, media?.short].filter(Boolean))].join(" · ") || null,
    },
  ));

  lanes.push(kindLane("rules", "Decision engine", "Lambda · rules, no model", "rules",
    steps.filter((s) => kindOf(s) === "rules"), zero, stageById(groups, "rules-2"), () => "rules"));
  lanes.push(...reviewLanes(steps, zero, stageById(groups, "review-2"), agentRun(trace, "phase2", "briefing")));
  lanes.push(kindLane("seal", "Decision record", "Lambda → S3 Object Lock", "seal",
    steps.filter((s) => kindOf(s) === "seal"), zero, stageById(groups, "sealed-2"), () => "sealed"));
  lanes.push(kindLane("account", "Account platform", "Lambda · reads, then lifts", "account",
    steps.filter((s) => kindOf(s) === "account"), zero, stageById(groups, "lift"),
    (s) => (s.name === "ReadAccount" ? "read" : "limits")));

  const decidedIso = status?.fullDecidedAt ?? null;
  const decidedAt = offset(decidedIso, zero);
  const ran = exec ? offset(exec.stopDate, zero) : null;
  const { span, ticks } = axis(extentOf(lanes, [running ? nowOffset : null, decidedAt, ran]), 60);
  return {
    id: "phase2",
    title: group?.title ?? "Phase 2 · Full checks",
    subtitle: group?.subtitle ?? "",
    status: group?.status ?? "idle",
    label: group?.label,
    running,
    clock: running ? nowOffset : ran,
    span,
    ticks,
    target: null,
    decided: decidedAt !== null ? { at: decidedAt, label: `decided · ${duration(decidedAt)}` } : null,
    now: running ? nowOffset : null,
    lanes: exec ? lanes : lanes.map((l) => ({ ...l, segments: [], marks: [] })),
    note: !opened
      ? "Starts once the account is open"
      : !exec
        ? status?.phase2Pending === "PENDING" || !status?.fullDecidedAt
          ? "Starting: Phase 1 hands over to Phase 2 as the account opens"
          : "This run's history is not available: it ran before the live view recorded runs, or it has aged out"
        : null,
    executionName: executionName(exec),
    consoleUrl: consoleUrl(exec),
  };
}

// --------------------------------------------------------------------------------- phase 3

function phase3(input: BackstageInput): PhaseTimeline {
  const { trace, status, now, groups } = input;
  const group = groups.find((g) => g.id === "phase3");
  const exec = trace?.executions?.phase3 ?? (trace?.execution?.phase === "phase3" ? trace.execution : null) ?? null;
  const zero = ms(exec?.startDate);
  const opened = Boolean(status?.provisionalDecidedAt || trace?.account);
  const running = exec?.status === "RUNNING";
  const steps = exec?.steps ?? [];
  const nowOffset = zero !== null ? Math.max(0, (now - zero) / 1000) : null;
  const base = {
    id: "phase3" as const,
    title: group?.title ?? "Phase 3 · Ongoing monitoring",
    subtitle: group?.subtitle ?? "",
    label: group?.label,
    executionName: executionName(exec),
    consoleUrl: consoleUrl(exec),
  };

  if (!exec) {
    const { span, ticks } = axis(0, 60);
    return {
      ...base,
      status: group?.status ?? (opened ? "active" : "idle"),
      steady: Boolean(group?.steady ?? opened),
      running: false,
      clock: null,
      span,
      ticks,
      target: null,
      decided: null,
      now: null,
      lanes: [],
      note: opened
        ? "Listening on the lifecycle bus: a sanctions list update or a risk event re-assesses this account, the moment it is published"
        : "Starts once the account is open",
    };
  }

  const ctx = { trace, steps, zero, since: zero };
  const monitor = stageById(groups, "monitor");
  const lanes: Lane[] = [
    {
      id: "event",
      name: "Risk event",
      sub: "EventBridge lifecycle bus",
      tone: "queue",
      segments: [],
      marks: [{ at: 0, kind: "flag", label: "Event", title: "The event reached this account" }],
      status: "done",
      result: "A list update or a risk event, routed to this account",
    },
    sfnLane(exec, zero, "kyc90-phase3-lifecycle"),
    agentLane(
      { key: "prescreen", states: ["ReScreen"], name: "Pre-Screen agent", tools: "sanctions_screen", facts: ["screening"] },
      "phase3", ctx, { stage: monitor, status: monitor?.status ?? "idle", text: stageById(groups, "screening")?.short ?? null },
    ),
    kindLane("rules", "Decision engine", "Lambda · rules, no model", "rules",
      steps.filter((s) => kindOf(s) === "rules"), zero, monitor, () => "rules"),
    kindLane("history", "Decision history", "DynamoDB · insert-only", "seal",
      steps.filter((s) => kindOf(s) === "history"), zero, undefined, () => "row"),
    ...reviewLanes(steps, zero, stageById(groups, "review-3"), agentRun(trace, "phase3", "briefing")),
    kindLane("seal", "Decision record", "Lambda → S3 Object Lock", "seal",
      steps.filter((s) => kindOf(s) === "seal"), zero, undefined, () => "sealed"),
    kindLane("account", "Account platform", "Lambda · freezes at once", "account",
      steps.filter((s) => kindOf(s) === "account"), zero, undefined, (s) => (s.name.includes("Freeze") ? "freeze" : "limits")),
  ];
  // Lanes with nothing of their own say what their steps did.
  for (const lane of lanes) {
    if (lane.result || !lane.segments.length) continue;
    lane.result = lane.status === "fail" ? "Failed" : lane.status === "active" ? "Running…" : "Done";
  }
  const account = lanes.find((l) => l.id === "account");
  if (account && trace?.account) {
    account.result = trace.account.state === "FROZEN"
      ? "Frozen"
      : trace.account.state === "OPEN_RESTRICTED"
        ? "Open with limits"
        : "Open";
    if (trace.account.state === "FROZEN") account.status = "fail";
  }

  const decidedStep = steps.find((s) => kindOf(s) === "rules" && s.endedAt);
  const decidedAt = offset(decidedStep?.endedAt, zero);
  const ran = offset(exec.stopDate, zero);
  const { span, ticks } = axis(extentOf(lanes, [running ? nowOffset : null, decidedAt, ran]), 30);
  return {
    ...base,
    status: group?.status ?? laneStatusOf(steps),
    running,
    clock: running ? nowOffset : ran,
    span,
    ticks,
    target: null,
    decided: decidedAt !== null ? { at: decidedAt, label: `re-assessed · ${duration(decidedAt)}` } : null,
    now: running ? nowOffset : null,
    lanes,
    note: null,
  };
}

// --------------------------------------------------------------------------------- the feed

const STATE_TONE: Record<string, FeedTone> = {
  PROVISIONAL: "success",
  MONITORING: "success",
  AWAITING_REVIEW: "warning",
  AWAITING_APPLICANT: "warning",
  RESTRICTED: "warning",
  FROZEN: "danger",
};

const AGENT_NAME: Record<string, string> = {
  documents: "Documents agent",
  identity: "Identity agent",
  fraud: "Fraud agent",
  prescreen: "Pre-Screen agent",
  compliance: "Compliance agent",
  notify: "Notify agent",
  briefing: "Briefing agent",
};

function buildFeed(input: BackstageInput): FeedItem[] {
  const { trace, status, groups, attemptStart } = input;
  const items: FeedItem[] = [];
  const add = (at: string | null | undefined, source: FeedSource, service: string, text: string, tone: FeedTone = "neutral") => {
    const t = ms(at);
    if (t === null || !at) return;
    items.push({ t, at, service, source, text, tone });
  };

  const createdAt = trace?.applicant.createdAt ?? status?.createdAt ?? null;
  add(createdAt, "api", "API Gateway", "POST /applicants: the application is in");
  if (status?.resubmittedAt) add(status.resubmittedAt, "api", "API Gateway", "POST /applicants: the next attempt is in");

  for (const t of trace?.transitions ?? []) {
    add(t.at, "ddb", "Case record", `${t.from ?? "new"} → ${t.to}${t.reason ? ` · ${t.reason}` : ""}`, STATE_TONE[t.to] ?? "neutral");
  }

  const executions: Array<["phase1" | "phase2" | "phase3", ExecutionTrace | null | undefined]> = [
    ["phase1", trace?.executions?.phase1 ?? (trace?.execution?.phase === "phase1" ? trace.execution : null)],
    ["phase2", trace?.executions?.phase2 ?? (trace?.execution?.phase === "phase2" ? trace.execution : null)],
    ["phase3", trace?.executions?.phase3 ?? (trace?.execution?.phase === "phase3" ? trace.execution : null)],
  ];
  for (const [phase, exec] of executions) {
    if (!exec) continue;
    const machine = machineName(exec) ?? phase;
    if (phase === "phase1") add(exec.startDate, "pipes", "EventBridge Pipes", `Started ${machine} from the queue`);
    if (phase === "phase2") add(exec.startDate, "sfn", "Step Functions", `${machine} started: the full checks run while the customer uses the account`);
    if (phase === "phase3") add(exec.startDate, "events", "EventBridge", `A risk event reached this account: ${machine} started`);

    for (const step of exec.steps) {
      const kind = kindOf(step);
      const ended = step.endedAt;
      const took = step.startedAt && ended ? duration(Math.max(0, ((ms(ended) ?? 0) - (ms(step.startedAt) ?? 0)) / 1000)) : null;
      if (step.name === "Phase1Parallel") add(step.startedAt, "sfn", "Step Functions", "Four agents invoked at once, in one Parallel state");
      if (kind === "agent" || kind === "notify" || kind === "briefing") {
        const key = agentKeyOf(step) ?? "";
        const run = agentRun(trace, phase, key);
        if (ended) {
          // What a failure means depends on the agent: evidence that did not arrive sends the case
          // to a person; a message that was not written leaves the decision as it is.
          const failure =
            kind === "notify"
              ? "no message was sent, and the decision stands"
              : kind === "briefing"
                ? "the reviewer decides without a briefing"
                : "recorded as unavailable, so a person decides";
          add(ended, kind === "notify" ? "notify" : "agent", AGENT_NAME[key] ?? step.name,
            step.status === "FAILED"
              ? `Failed after ${took}: ${failure}`
              : `Answered in ${took}${run?.totalTokens ? ` · ${tokens(run.totalTokens)}` : ""}${run?.model ? ` · ${modelName(run.model)}` : ""}`,
            step.status === "FAILED" ? "danger" : "neutral");
        }
      } else if (kind === "rules" && ended) {
        const stage = stageById(groups, phase === "phase1" ? "rules-1" : phase === "phase2" ? "rules-2" : "monitor");
        add(ended, "rules", "Decision engine", stage?.short ? `Decided: ${stage.short}` : "Decided on the recorded facts",
          stage?.status === "fail" ? "danger" : stage?.status === "warn" ? "warning" : "success");
      } else if (kind === "seal" && ended) {
        add(ended, "seal", "S3 Object Lock", "Decision record sealed", step.status === "FAILED" ? "danger" : "success");
      } else if (kind === "account" && ended) {
        add(ended, "account", "Account platform", ACCOUNT_STATES[step.name] ?? step.name,
          step.name.includes("Freeze") ? "danger" : step.name.startsWith("Lift") ? "success" : "neutral");
      } else if (kind === "review") {
        add(step.startedAt, "review", "Review queue", "Waiting for a reviewer: Step Functions holds a task token", "warning");
        if (ended) add(ended, "review", "Reviewer", "Decided; Step Functions carries the decision out");
      } else if (kind === "history" && ended) {
        add(ended, "seal", "Decision history", "Row inserted, never updated");
      } else if (step.status === "FAILED") {
        add(ended ?? step.startedAt, "sfn", "Step Functions", `${step.name}: an attempt failed${ended ? " and was handled" : ""}`, "warning");
      }
    }
    if (exec.stopDate) {
      const ran = offset(exec.stopDate, ms(exec.startDate));
      add(exec.stopDate, "sfn", "Step Functions",
        `${machine} ${(exec.status ?? "stopped").toLowerCase().replace(/_/g, " ")}${ran !== null ? ` in ${duration(ran)}` : ""}`,
        exec.status === "SUCCEEDED" ? "success" : "danger");
    }
  }

  const facts = trace?.facts;
  if (facts) {
    for (const name of Object.keys(FACT_TOOL) as Array<keyof typeof FACT_TOOL>) {
      const fact = facts[name];
      const at = str(fact?.recordedAt);
      // Phase 1's facts are this attempt's; a later phase's land after it.
      if (!fact || !at || (attemptStart !== null && (ms(at) ?? 0) < attemptStart - 2000)) continue;
      const stage = stageById(groups, FACT_STAGE[name]);
      add(at, "tool", FACT_TOOL[name], stage?.short ? `Recorded: ${stage.short}` : "Recorded its fact on the case",
        fact.performed === false ? "warning" : "neutral");
    }
  }

  for (const note of trace?.notifications ?? []) {
    const shown = note.shown.length > 90 ? `${note.shown.slice(0, 88)}…` : note.shown;
    const why = guardReason(note.replacedBecause);
    add(note.at, "notify", "Tipping-off guard",
      note.replacedByGuard
        ? `Replaced the agent's message with the fixed wording${why ? `: ${why}` : ""}`
        : `Passed as written: “${shown}”`,
      note.replacedByGuard ? "warning" : "neutral");
  }

  return items.sort((a, b) => a.t - b.t);
}

// --------------------------------------------------------------------------------- the model

export function buildBackstage(input: BackstageInput): Backstage {
  return {
    available: input.available,
    phases: [phase1(input), phase2(input), phase3(input)],
    feed: buildFeed(input),
    now: input.now,
  };
}
