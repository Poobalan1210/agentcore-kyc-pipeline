import { useState } from "react";
import { Button, ButtonVariant } from "@shared/components/Button";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { api, ApiError, QueuedCaseSummary } from "@shared/api/client";
import { CaseTrace } from "./CaseTrace";
import "./DecisionForm.css";

/** `decisions.py`'s `ReviewAction` — exactly these three (R6.3, design §18.4). Keeping this list
 * a literal copy rather than deriving it from a shared source: the frontend and the Python
 * decision layer are separate packages, and a fourth action added to one without the other
 * should fail loudly (a request the backend rejects with UNKNOWN_ACTION) rather than silently
 * pass through a stale list. */
const ACTIONS: Array<{ value: string; label: string; variant: ButtonVariant }> = [
  { value: "APPROVE", label: "Approve", variant: "primary" },
  { value: "REQUEST_STEP_UP", label: "Request step-up", variant: "secondary" },
  { value: "REJECT", label: "Reject", variant: "danger" },
];

/**
 * The decision form for one selected case — task 14.6, R6.3.
 *
 * `phase` defaults to `phase1`: `review_handler.py`'s own `parse_event` defaults to `phase1` when
 * absent, and Phase 2 has no manual-review wait state in this POC (design §7 only wires
 * `ManualReview` into Phase 1's flow), so there is currently no case this console would ever show
 * with a different phase.
 */
export function DecisionForm({
  queuedCase,
  onDecided,
}: {
  queuedCase: QueuedCaseSummary;
  onDecided: () => void;
}) {
  const [action, setAction] = useState<string | null>(null);
  const [rationale, setRationale] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<unknown>(null);

  const canSubmit = Boolean(action) && rationale.trim().length > 0 && Boolean(queuedCase.taskToken);

  async function submit() {
    if (!action || !queuedCase.taskToken) {
      return;
    }
    setSubmitting(true);
    setError(null);
    setConflict(null);
    try {
      await api.submitReviewDecision(queuedCase.caseId, {
        applicantId: queuedCase.applicantId,
        phase: queuedCase.phase ?? "phase1",
        action,
        rationale,
        taskToken: queuedCase.taskToken,
      });
      onDecided();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError("Another reviewer already decided this case.");
        const body = err.body as { existingDecision?: unknown };
        setConflict(body?.existingDecision ?? null);
      } else if (err instanceof ApiError && err.status === 422) {
        const body = err.body as { reasons?: string[]; detail?: string };
        setError(`Submission rejected: ${body?.detail ?? body?.reasons?.join(", ") ?? "invalid"}`);
      } else if (err instanceof ApiError && err.status === 410) {
        setError("This case has moved on and is no longer awaiting review.");
      } else {
        setError("Could not submit the decision. Try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <h2>{queuedCase.caseId}</h2>
      </CardHeader>
      <CardBody>
        <dl className="kyc-decision-summary">
          <dt>Applicant</dt>
          <dd>{queuedCase.applicantId}</dd>
          <dt>Jurisdiction</dt>
          <dd>{queuedCase.jurisdiction}</dd>
          <dt>Enqueued</dt>
          <dd>{queuedCase.createdAt}</dd>
        </dl>

        {!queuedCase.taskToken ? (
          <p className="kyc-hint">
            Waiting for the task token to become visible in the queue (it is peeked from SQS
            without deleting the message). This refreshes automatically every few seconds.
          </p>
        ) : null}

        <div className="kyc-decision-actions" role="group" aria-label="Review action">
          {ACTIONS.map((option) => (
            <Button
              key={option.value}
              type="button"
              variant={option.variant}
              className={action === option.value ? "kyc-decision-actions__selected" : undefined}
              aria-pressed={action === option.value}
              onClick={() => setAction(option.value)}
              disabled={submitting}
            >
              {option.label}
            </Button>
          ))}
        </div>

        <div className="kyc-form-row">
          <label htmlFor="rationale">Rationale (required, R6.3)</label>
          <textarea
            id="rationale"
            className="kyc-decision-rationale"
            value={rationale}
            onChange={(event) => setRationale(event.target.value)}
            disabled={submitting}
            placeholder="Free-text rationale for this decision — recorded in the audit trail."
          />
        </div>

        {error ? <p className="kyc-error-text">{error}</p> : null}
        {conflict ? (
          <pre className="kyc-hint">{JSON.stringify(conflict, null, 2)}</pre>
        ) : null}

        <Button variant="primary" onClick={submit} loading={submitting} disabled={!canSubmit}>
          Submit decision
        </Button>

        <CaseTrace caseId={queuedCase.caseId} />
      </CardBody>
    </Card>
  );
}
