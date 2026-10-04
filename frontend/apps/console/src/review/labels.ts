import type { BadgeTone } from "@shared/components/Badge";

/**
 * Why a case is in the queue, in the reviewer's words. The keys are the state machines' own
 * `reviewReason` values (`infra/kyc/asl.py`); an unknown one is shown as it is rather than
 * guessed at.
 */
export const REVIEW_REASONS: Record<string, string> = {
  ENGINE_MANUAL_REVIEW: "The rules referred it to a person",
  UNRECOGNISED_OUTCOME: "The rules returned an outcome this workflow does not know",
  DEGRADED_PIPELINE: "A check could not run, so the rules could not decide",
  AUDIT_RECORD_MISSING: "Decided, but the audit record could not be written",
  PROVISIONAL_NOT_RECORDED: "Approved, but the approval could not be recorded",
  JURISDICTION_NOT_DECIDED: "This jurisdiction is not decided automatically",
  REASSESSMENT_FAILED: "A re-assessment could not complete",
  HISTORY_NOT_RECORDED: "A re-assessment could not be recorded",
  // A confirmed match, or a CRITICAL risk event: either way the rules froze first.
  ACCOUNT_FROZEN: "Frozen by the rules: confirm the freeze or exit",
  ACCOUNT_FREEZE_FAILED: "A freeze was required but could not be applied",
  RESTRICTION_NOT_APPLIED: "A restriction was required but could not be applied",
  RISK_INCREASED: "Risk went up after a new event",
  RESCREEN_INCOMPLETE: "A re-screen could not complete",
  UNSPECIFIED: "No reason was recorded",
};

export function reasonLabel(reason?: string | null): string {
  if (!reason) return "No reason recorded";
  return REVIEW_REASONS[reason] ?? reason;
}

/**
 * Why the guard replaced a Notify message with the fixed wording (`api/notifications.py`
 * `check`), worded to follow "because". An unknown code is shown as it is.
 */
export const GUARD_REASONS: Record<string, string> = {
  TIPPING_OFF: "it said why the account is limited",
  CONTRADICTS_ACCOUNT: "it said something the account's state rules out",
  UNVERIFIABLE_DETAIL: "it quoted amounts or listed limits, which the guard cannot check",
  NO_MESSAGE: "the agent's reply had no message in it",
};

/** Null when there is no reason: sent as written, or an API that predates the field. */
export function guardReason(reason?: string | null): string | null {
  if (!reason) return null;
  return GUARD_REASONS[reason] ?? reason;
}

export const PHASE_LABELS: Record<string, string> = {
  phase1: "Phase 1 · opening",
  phase2: "Phase 2 · full checks",
  phase3: "Phase 3 · ongoing",
};

export function phaseLabel(phase?: string | null): string {
  return (phase && PHASE_LABELS[phase]) || phase || "phase1";
}

export interface ActionOption {
  value: "APPROVE" | "REQUEST_STEP_UP" | "REJECT";
  label: string;
  variant: "primary" | "secondary" | "danger";
}

/**
 * The actions each phase's review accepts (`review/decisions.py` `TARGET_STATES`), labelled with
 * what they do to the customer. Phase 3 has no step-up: a review of an existing customer clears
 * the alert or exits the relationship.
 */
export function actionsFor(phase: string): ActionOption[] {
  switch (phase) {
    case "phase2":
      return [
        { value: "APPROVE", label: "Approve · lift limits", variant: "primary" },
        { value: "REQUEST_STEP_UP", label: "Step up · keep limits", variant: "secondary" },
        { value: "REJECT", label: "Reject · freeze", variant: "danger" },
      ];
    case "phase3":
      return [
        { value: "APPROVE", label: "Clear · restore account", variant: "primary" },
        { value: "REJECT", label: "Exit · freeze", variant: "danger" },
      ];
    default:
      return [
        { value: "APPROVE", label: "Approve · open with limits", variant: "primary" },
        { value: "REQUEST_STEP_UP", label: "Request step-up", variant: "secondary" },
        { value: "REJECT", label: "Reject", variant: "danger" },
      ];
  }
}

/** `review/decisions.py` `InvalidReason`, as the reviewer should read it. */
export const INVALID_REASONS: Record<string, string> = {
  PROHIBITED_TIER_NOT_OVERRIDABLE: "A confirmed sanctions match cannot be approved.",
  ACTION_NOT_AVAILABLE: "That action is not available for this phase.",
  MISSING_RATIONALE: "A rationale is required.",
  MISSING_TASK_TOKEN: "The review is not ready yet: no task token.",
  MISSING_REVIEWER: "Your identity could not be read from the session.",
  UNKNOWN_ACTION: "Unknown action.",
  UNKNOWN_PHASE: "Unknown phase.",
  CASE_MISMATCH: "The case does not match this review.",
  MISSING_CASE: "The case could not be found.",
};

export function outcomeTone(outcome?: string | null): BadgeTone {
  switch (outcome) {
    case "PROVISIONAL_APPROVE_RESTRICTED":
    case "FULL_APPROVE_STANDARD":
    case "FULL_APPROVE_ENHANCED_MONITORING":
    case "RISK_UNCHANGED":
      return "success";
    case "STEP_UP":
    case "RESTRICT_FURTHER":
    case "RISK_INCREASED_RESTRICT":
      return "warning";
    case "MANUAL_REVIEW":
    case "RISK_INCREASED_REVIEW":
      return "info";
    case "REJECT":
    case "FREEZE":
    case "SYSTEM_ERROR":
      return "danger";
    default:
      return "neutral";
  }
}

export function classificationTone(classification?: string | null): BadgeTone {
  switch (classification) {
    case "CONFIRMED":
      return "danger";
    case "PROBABLE":
      return "warning";
    case "POSSIBLE":
      return "info";
    case "NO_MATCH":
      return "success";
    default:
      return "neutral";
  }
}

export function executionTone(status?: string | null): BadgeTone {
  switch (status) {
    case "SUCCEEDED":
      return "success";
    case "RUNNING":
      return "info";
    case "FAILED":
    case "TIMED_OUT":
    case "ABORTED":
      return "danger";
    default:
      return "neutral";
  }
}

export function formatAge(iso?: string | null): string {
  if (!iso) return "";
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  const seconds = Math.max(0, Math.floor((Date.now() - at) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
