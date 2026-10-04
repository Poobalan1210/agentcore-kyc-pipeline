import { useEffect, useRef, useState } from "react";
import { api, ApiError, CaseStatusResponse, CaseTraceResponse } from "@shared/api/client";

/** Fast while something is happening, slow once the case has settled, slower when not on screen. */
const POLL_BUSY_MS = 2000;
const POLL_SETTLED_MS = 6000;
const POLL_HIDDEN_MS = 15000;

const BUSY_STATES = new Set(["RECEIVED", "IN_PROGRESS"]);

/** Any phase's execution still running, as the trace's execution history says. */
function anyRunning(trace: CaseTraceResponse | null): boolean {
  return Object.values(trace?.executions ?? {}).some((e) => e?.status === "RUNNING");
}

export interface CaseProcessData {
  status: CaseStatusResponse | null;
  trace: CaseTraceResponse | null;
  /** The trace route is reviewers-only; without it the view shows the applicant-level status. */
  traceDenied: boolean;
  error: string | null;
  /** The browser clock, ticking while a phase runs, for the live elapsed figure only. */
  now: number;
}

/**
 * Reads one case for the process view: the applicant-level status and, for reviewers, the full
 * trace (facts, decisions, agents, the state log). Polls until it is unmounted; a 404 just after
 * submission means the record is not readable yet, so it keeps asking.
 */
export function useCaseProcess(
  applicantId: string | null,
  { withTrace, visible }: { withTrace: boolean; visible: boolean },
): CaseProcessData {
  const [status, setStatus] = useState<CaseStatusResponse | null>(null);
  const [trace, setTrace] = useState<CaseTraceResponse | null>(null);
  const [traceDenied, setTraceDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const visibleRef = useRef(visible);
  visibleRef.current = visible;

  useEffect(() => {
    setStatus(null);
    setTrace(null);
    setTraceDenied(false);
    setError(null);
    if (!applicantId) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let denied = false;

    async function poll() {
      if (cancelled || !applicantId) return;
      let busy = true;
      try {
        const next = await api.getCase(applicantId);
        if (cancelled) return;
        setStatus(next);
        setError(null);
        busy = BUSY_STATES.has(next.state) || (Boolean(next.provisionalDecidedAt) && !next.fullDecidedAt && next.phase2Pending === "PENDING");
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 404) && !cancelled) {
          setError("Could not read the case. Retrying.");
        }
      }
      if (withTrace && !denied) {
        try {
          const nextTrace = await api.getCaseTrace(applicantId);
          if (!cancelled) setTrace(nextTrace);
          // A phase still running keeps the poll fast, whatever the case's state says: a Phase 3
          // re-assessment runs while the case reads as open and settled.
          if (anyRunning(nextTrace)) busy = true;
        } catch (err) {
          if (err instanceof ApiError && err.status === 403) {
            denied = true;
            if (!cancelled) setTraceDenied(true);
          }
          // A 404 before the record exists is expected; anything else retries next round.
        }
      }
      if (cancelled) return;
      const delay = !visibleRef.current ? POLL_HIDDEN_MS : busy ? POLL_BUSY_MS : POLL_SETTLED_MS;
      timer = setTimeout(poll, delay);
    }

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [applicantId, withTrace]);

  // The clock ticks between polls, so nothing that is still running looks frozen: fast while
  // Phase 1 runs (the headline timer shows tenths), once a second while the case waits for a
  // reviewer or Phase 2 runs (the timeline's wait and the phase clock grow).
  const running = Boolean(status && BUSY_STATES.has(status.state));
  const waiting = Boolean(
    (status &&
      (status.state === "AWAITING_REVIEW" ||
        (status.provisionalDecidedAt && !status.fullDecidedAt && status.phase2Pending === "PENDING"))) ||
      anyRunning(trace),
  );
  const tickMs = running ? 250 : waiting ? 1000 : 0;
  useEffect(() => {
    if (!tickMs) {
      setNow(Date.now());
      return;
    }
    const id = setInterval(() => setNow(Date.now()), tickMs);
    return () => clearInterval(id);
  }, [tickMs]);

  return { status, trace, traceDenied, error, now };
}
