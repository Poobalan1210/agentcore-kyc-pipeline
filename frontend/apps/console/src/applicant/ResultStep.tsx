import { Button } from "@shared/components/Button";
import type { CaseStatusResponse } from "@shared/api/client";
import { AccountPanel } from "./AccountPanel";
import { AppScreen, HomeHeader, WaitingArt } from "./AppShell";
import { STATUS_COPY, STEP_UP_COPY } from "./copy";

export interface StepUpActions {
  /** Repeat the face check, keeping the rest of the application. */
  onRedoLiveness: () => void;
  /** Upload a different document, keeping the rest of the application. */
  onNewDocument: () => void;
  onRestart: () => void;
}

/** Step-up reasons about the face check, and about the document (`notifications.py`'s
 * `APPLICANT_SAFE_REASON_CODES` — the only reasons the API ever sends an applicant). */
const LIVENESS_REASONS = new Set(["LIVENESS_REQUIRED", "LIVENESS_FAILED"]);
const DOCUMENT_REASONS = new Set([
  "DOCUMENTS_REQUIRED",
  "FACE_IMAGE_UNREADABLE",
  "NO_FACE_ON_DOCUMENT",
  "DOCUMENT_UNUSABLE",
  "DOCUMENT_NOT_FOUND",
  "DOCUMENT_TOO_LARGE",
  "UNSUPPORTED_FORMAT",
  "NO_DOCUMENT_ON_CASE",
  "DOCUMENT_UNREADABLE",
]);

/**
 * The decision, and from then on the account — task 14.5, R31.9, R32.8.
 *
 * Before an account exists the applicant is told the application's status, the way a bank's app
 * says it: one thing needed and the button that does it, a review under way, or a decline. Once
 * the account exists the account is the subject, live from the platform that enforces it. A case
 * that goes back to a reviewer after opening is not shown as such — that is not news a bank
 * shares with its customer, and the account's own limits already say what changed for them.
 */
export function ResultStep({
  applicantId,
  status,
  actions,
  firstName,
  productApplied,
}: {
  applicantId: string;
  status: CaseStatusResponse;
  actions: StepUpActions;
  firstName?: string | null;
  productApplied?: string | null;
}) {
  if (status.provisionalDecidedAt) {
    return (
      <AccountPanel
        applicantId={applicantId}
        notifications={status.notifications}
        firstName={firstName}
        productApplied={productApplied}
      />
    );
  }

  const copy = STATUS_COPY[status.status] ?? STATUS_COPY.CHECKING;

  if (status.status === "MORE_INFORMATION_NEEDED") {
    const reason = status.reasonCode ?? "";
    const livenessFirst = LIVENESS_REASONS.has(reason);
    const documentFirst = DOCUMENT_REASONS.has(reason);
    return (
      <AppScreen
        header={<HomeHeader />}
        centered
        footer={
          <>
            {/*
              A step-up asks for one thing, so the screen offers that one thing first. It used
              to offer only "start a new application", which threw away the declared identity
              and the uploaded document to redo a ten-second camera step.
            */}
            {!documentFirst ? (
              <Button variant="primary" className="app-cta" onClick={actions.onRedoLiveness}>
                {STEP_UP_COPY.redoLiveness}
              </Button>
            ) : null}
            {!livenessFirst ? (
              <Button
                variant={documentFirst ? "primary" : "secondary"}
                className="app-cta"
                onClick={actions.onNewDocument}
              >
                {STEP_UP_COPY.newDocument}
              </Button>
            ) : null}
            <button type="button" className="app-link app-link--center" onClick={actions.onRestart}>
              {STEP_UP_COPY.restart}
            </button>
          </>
        }
      >
        <WaitingArt icon={documentFirst ? "document" : "face"} tone="warn" />
        <h2 className="app-title">{copy.title}</h2>
        <p className="app-lead">
          {livenessFirst ? STEP_UP_COPY.livenessHint : documentFirst ? STEP_UP_COPY.documentHint : copy.body}
        </p>
        {status.requiredItem ? (
          <p className="app-need">
            <strong>{STEP_UP_COPY.needed}</strong>
            {status.requiredItem}
          </p>
        ) : null}
      </AppScreen>
    );
  }

  const declined = status.status === "DECLINED";
  return (
    <AppScreen
      header={<HomeHeader />}
      centered
      footer={
        declined ? (
          <Button variant="secondary" className="app-cta" onClick={actions.onRestart}>
            {STEP_UP_COPY.restart}
          </Button>
        ) : undefined
      }
    >
      <WaitingArt
        icon={declined ? "x" : status.status === "IN_REVIEW" ? "clock" : "shield"}
        tone={declined ? "fail" : "idle"}
      />
      <h2 className="app-title">{copy.title}</h2>
      <p className="app-lead">{copy.body}</p>
    </AppScreen>
  );
}
