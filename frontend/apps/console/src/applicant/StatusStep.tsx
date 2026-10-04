import { useEffect, useRef, useState } from "react";
import { api, ApiError, CaseStatusResponse } from "@shared/api/client";
import { ProgressStep } from "./ProgressStep";
import { ResultStep, StepUpActions } from "./ResultStep";

/** Faster while the 90-second decision is being made, slower once there is one to show. */
const POLL_WAITING_MS = 2000;
const POLL_SETTLED_MS = 4000;

/** R37: the 90-second claim is T1 (Submit -> decision). Past this the page says it is slow, and
 * keeps checking: a cold start or a retried agent still resolves, it just takes longer. */
const SLOW_AFTER_MS = 3 * 60 * 1000;

/** Statuses with nothing to show yet but the waiting screen. */
const WAITING = new Set(["SUBMITTED", "CHECKING"]);

/**
 * One application's status, from submission onward — task 14.5, R32.7, R31.9.
 *
 * It never stops polling while it is on screen. The decision is not the end of the story: Phase
 * 2 lifts the provisional limits a minute or two later, and a sanctions list update can put the
 * account on hold at any time. Both reach this page by themselves, because the page keeps asking.
 *
 * A 404 right after submission means the case record is not readable yet, so it keeps polling.
 */
export function StatusStep({
  applicantId,
  actions,
  firstName,
  productApplied,
}: {
  applicantId: string;
  actions: StepUpActions;
  /** For the greeting on the account. */
  firstName?: string | null;
  productApplied?: string | null;
}) {
  const [status, setStatus] = useState<CaseStatusResponse | null>(null);
  const [slow, setSlow] = useState(false);
  const startRef = useRef(Date.now());

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      if (cancelled) return;
      let next: CaseStatusResponse | null = null;
      try {
        next = await api.getCase(applicantId);
        if (!cancelled) setStatus(next);
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 404)) {
          // eslint-disable-next-line no-console
          console.error(err);
        }
      }
      if (cancelled) return;
      const waiting = !next || WAITING.has(next.status);
      setSlow(waiting && Date.now() - startRef.current > SLOW_AFTER_MS);
      timer = setTimeout(poll, waiting ? POLL_WAITING_MS : POLL_SETTLED_MS);
    }

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [applicantId]);

  if (!status || WAITING.has(status.status)) {
    return <ProgressStep status={status?.status ?? "SUBMITTED"} slow={slow} />;
  }

  return (
    <ResultStep
      applicantId={applicantId}
      status={status}
      actions={actions}
      firstName={firstName}
      productApplied={productApplied}
    />
  );
}
