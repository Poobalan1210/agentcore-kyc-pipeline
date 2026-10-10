import { useEffect, useState } from "react";
import { Badge } from "@shared/components/Badge";
import { Button } from "@shared/components/Button";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { api, ApiError, CaseTraceResponse, QueuedCaseSummary, ScreeningHit } from "@shared/api/client";
import { readBriefing, readReply } from "../process/agentReply";
import { CaseTrace } from "./CaseTrace";
import {
  actionsFor,
  classificationTone,
  formatAge,
  INVALID_REASONS,
  phaseLabel,
  reasonLabel,
} from "./labels";
import "./DecisionForm.css";

/** The trace is re-read while the reviewer works: a briefing written seconds after the case was
 * queued, or a fact recorded late, appears without a refresh. */
const TRACE_POLL_MS = 8000;

/**
 * The decision form for one selected case — task 14.6, R6.3.
 *
 * What a reviewer needs to decide, in the order they need it: the briefing (what happened, in a
 * paragraph), the screening alerts and what an approval would clear, then the actions this phase
 * accepts. Underneath, the trace: the facts the rules decided on, each phase's decision, what the
 * agents said, the timeline, the applicant and the account.
 *
 * The console mirrors two rules the review handler enforces, so the reviewer is told before they
 * submit rather than refused after: a CONFIRMED match cannot be approved, and Phase 3 has no
 * step-up. The handler still enforces both; the console is not the control.
 */
export function DecisionForm({
  queuedCase,
  onDecided,
}: {
  queuedCase: QueuedCaseSummary;
  onDecided: () => void;
}) {
  const [trace, setTrace] = useState<CaseTraceResponse | null>(null);
  const [traceError, setTraceError] = useState<string | null>(null);
  const [action, setAction] = useState<string | null>(null);
  const [rationale, setRationale] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const result = await api.getCaseTrace(queuedCase.caseId);
        if (!cancelled) {
          setTrace(result);
          setTraceError(null);
        }
      } catch (err) {
        if (!cancelled) setTraceError("Could not load the case trace. Retrying.");
        // eslint-disable-next-line no-console
        console.error(err);
      }
    }
    load();
    const id = setInterval(load, TRACE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [queuedCase.caseId]);

  const phase = queuedCase.phase || "phase1";
  const hits: ScreeningHit[] = trace?.facts.screening?.hits ?? [];
  const confirmed = hits.filter((h) => h.classification === "CONFIRMED");
  const clearable = hits.filter(
    (h) => h.classification === "PROBABLE" || h.classification === "POSSIBLE",
  );
  const actions = actionsFor(phase);

  const blocked = action === "APPROVE" && confirmed.length > 0;
  const canSubmit =
    Boolean(action) && rationale.trim().length > 0 && Boolean(queuedCase.taskToken) && !blocked;

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
        phase,
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
        const reasons = (body?.reasons ?? []).map((r) => INVALID_REASONS[r] ?? r);
        setError(`Not submitted: ${reasons.join(" ") || body?.detail || "invalid submission"}`);
      } else if (err instanceof ApiError && err.status === 410) {
        setError("This case has moved on and is no longer awaiting review.");
      } else {
        setError("Could not submit the decision. Try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  const briefing = trace?.review.briefing;

  return (
    <Card>
      <CardHeader>
        <h2>{queuedCase.caseId}</h2>
      </CardHeader>
      <CardBody>
        <dl className="kyc-decision-summary">
          <dt>Review</dt>
          <dd>
            <Badge tone={phase === "phase3" ? "warning" : "neutral"}>{phaseLabel(phase)}</Badge>{" "}
            {reasonLabel(queuedCase.reason)}
          </dd>
          <dt>Queued</dt>
          <dd>
            {queuedCase.queuedAt ?? "—"}{" "}
            {queuedCase.queuedAt ? `(${formatAge(queuedCase.queuedAt)})` : ""}
          </dd>
          <dt>Applicant</dt>
          <dd>{queuedCase.applicantId}</dd>
        </dl>

        <section className="kyc-briefing" aria-labelledby="briefing-heading">
          <h3 id="briefing-heading">Briefing</h3>
          {briefing?.text ? (
            <>
              <BriefingBody text={briefing.text} />
              <p className="kyc-briefing__label">
                Written by the Briefing agent, an AgentCore harness with no tools, from the
                recorded facts. It explains the case; it decided nothing. Check it against the
                facts below.
              </p>
            </>
          ) : (
            <p className="kyc-hint">
              {trace
                ? "No briefing was written for this case. The facts below are complete without it."
                : "Loading…"}
            </p>
          )}
        </section>

        {hits.length > 0 ? (
          <section className="kyc-alerts" aria-labelledby="alerts-heading">
            <h3 id="alerts-heading">Screening alerts</h3>
            <ul className="kyc-alerts__list">
              {hits.map((hit) => (
                <li key={hit.entryId}>
                  <Badge tone={classificationTone(hit.classification)}>
                    {hit.classification}
                  </Badge>
                  <span className="kyc-alerts__id">{hit.entryId}</span>
                  <span>{hit.matchedName}</span>
                  <span className="kyc-alerts__reason">{hit.reason}</span>
                </li>
              ))}
            </ul>
            {confirmed.length > 0 ? (
              <p className="kyc-error-text">
                A confirmed match cannot be approved: {confirmed.map((h) => h.entryId).join(", ")}.
                The review handler refuses it too.
              </p>
            ) : clearable.length > 0 ? (
              <p className="kyc-hint">
                Approving clears {clearable.map((h) => `${h.entryId} (${h.classification})`)
                  .join(", ")}. The clearance is recorded with your reviewer ID and the list version
                ({trace?.facts.screening?.listVersion ?? "—"}), and from then on the rules treat
                that listing as cleared for this customer. A confirmed match is never cleared.
              </p>
            ) : null}
          </section>
        ) : null}

        {!queuedCase.taskToken ? (
          <p className="kyc-hint">
            Waiting for the review hand-off to finish. The task token is written to the case
            record a moment after the case is queued; this refreshes automatically.
          </p>
        ) : null}

        <div className="kyc-decision-actions" role="group" aria-label="Review action">
          {actions.map((option) => (
            <Button
              key={option.value}
              type="button"
              variant={option.variant}
              className={action === option.value ? "kyc-decision-actions__selected" : undefined}
              aria-pressed={action === option.value}
              onClick={() => setAction(option.value)}
              disabled={submitting || (option.value === "APPROVE" && confirmed.length > 0)}
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
            placeholder="Why you decided this. Sealed in the decision record with your reviewer ID."
          />
        </div>

        {error ? <p className="kyc-error-text" role="alert">{error}</p> : null}
        {conflict ? <pre className="kyc-hint">{JSON.stringify(conflict, null, 2)}</pre> : null}

        <Button variant="primary" onClick={submit} loading={submitting} disabled={!canSubmit}>
          Submit decision
        </Button>

        {traceError ? <p className="kyc-error-text">{traceError}</p> : null}
        {trace ? <CaseTrace trace={trace} /> : null}
      </CardBody>
    </Card>
  );
}

/**
 * The briefing as a reviewer reads it: the summary, each check and its finding, and what is left
 * to establish. The agent answers in those fields; printed as it arrives, a reviewer was handed a
 * JSON object. A briefing written as prose is shown as the prose it is.
 */
function BriefingBody({ text }: { text: string }) {
  const parts = readBriefing(text);
  if (!parts) {
    return <p className="kyc-briefing__text">{readReply(text)?.text ?? text}</p>;
  }
  return (
    <div className="kyc-briefing__body">
      {parts.summary ? <p className="kyc-briefing__summary">{parts.summary}</p> : null}
      {parts.checks.length ? (
        <ul className="kyc-briefing__checks">
          {parts.checks.map((row, i) => (
            <li key={`${row.check}-${i}`}>
              {row.check ? <span className="kyc-briefing__check">{row.check}</span> : null}
              <span>{row.finding}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {parts.openQuestions.length ? (
        <>
          <h4 className="kyc-briefing__subhead">To establish before deciding</h4>
          <ul className="kyc-briefing__questions">
            {parts.openQuestions.map((question, i) => (
              <li key={i}>{question}</li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
