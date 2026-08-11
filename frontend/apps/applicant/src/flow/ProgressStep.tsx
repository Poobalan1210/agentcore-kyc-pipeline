import { useEffect, useRef, useState } from "react";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { api, ApiError, CaseStatusResponse } from "@shared/api/client";

const POLL_INTERVAL_MS = 2000;
// R37: the 90-second claim is T1 (Submit -> decision rendered). Polling stops well past that so
// a genuinely slow run (cold start, retried agent) still resolves rather than the UI giving up
// on a case that is actually still progressing.
const MAX_POLL_MS = 3 * 60 * 1000;

/**
 * Live progress while Phase 1 runs — task 14.5, R32.7.
 *
 * "Display live progress across Phase 1 stages and show an elapsed-time counter." The Public
 * API does not expose a stage-by-stage feed (task 7's design intentionally keeps Phase 1's
 * internal state machine states off the applicant-facing surface — `_project_case` in
 * `api/applicants.py` returns only `state`/`outcome`, not which agent is currently running), so
 * this polls case status and shows an indeterminate "in progress" state plus the one thing it
 * can measure precisely: elapsed time.
 */
export function ProgressStep({
  applicantId,
  onSettled,
}: {
  applicantId: string;
  onSettled: (status: CaseStatusResponse) => void;
}) {
  const [elapsedMs, setElapsedMs] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [caseState, setCaseState] = useState<string | null>(null);
  const startRef = useRef(Date.now());

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      if (cancelled) return;
      const elapsed = Date.now() - startRef.current;
      setElapsedMs(elapsed);

      if (elapsed > MAX_POLL_MS) {
        setError(
          "This is taking longer than expected. Your application has been received and you " +
            "will be notified once it is complete.",
        );
        return;
      }

      try {
        const status = await api.getCase(applicantId);
        // A case is settled only once `outcome` is present. `RECEIVED` was treated as the only
        // non-terminal state on the assumption that any case that had produced a Phase 1 outcome
        // always carried `outcome` — true once decided, but not while a case is genuinely still
        // waiting on a human. A case in `AWAITING_REVIEW` has no `phase1` attribute yet (it is
        // only written by `PersistOutcome`, after the reviewer decides), so this used to fire
        // "settled" with no outcome at all, and the applicant saw a generic "status has changed"
        // message instead of either a real result or a clear "still waiting" state.
        if (status.outcome) {
          if (!cancelled) onSettled(status);
          return;
        }
        if (!cancelled) setCaseState(status.state);
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 404)) {
          // A 404 this early just means the case record has not been written yet — keep
          // polling. Anything else is worth surfacing.
          // eslint-disable-next-line no-console
          console.error(err);
        }
      }

      timer = setTimeout(poll, POLL_INTERVAL_MS);
    }

    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [applicantId, onSettled]);

  const seconds = Math.floor(elapsedMs / 1000);

  return (
    <Card>
      <CardHeader>
        <h2>Reviewing your application</h2>
      </CardHeader>
      <CardBody>
        <div className="kyc-progress">
          <div className="kyc-progress__spinner" aria-hidden="true" />
          <p className="kyc-progress__elapsed">
            {seconds}s elapsed
          </p>
        </div>
        <p className="kyc-hint">
          {caseState === "AWAITING_REVIEW"
            ? "Your application requires a closer look from our compliance team. This can take " +
              "longer than 90 seconds — we will keep checking and notify you once it is complete."
            : "We're verifying your identity and documents. This usually takes under 90 seconds."}
        </p>
        {error ? <p className="kyc-error-text">{error}</p> : null}
      </CardBody>
    </Card>
  );
}
