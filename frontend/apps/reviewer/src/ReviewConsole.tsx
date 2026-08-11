import { useCallback, useEffect, useState } from "react";
import { api, ApiError, QueuedCaseSummary } from "@shared/api/client";
import { QueueList } from "./QueueList";
import { DecisionForm } from "./DecisionForm";
import "./ReviewConsole.css";

/**
 * Reviewer console root — task 14.6, R34.1/R34.5/R34.6.
 *
 * Owns the queue poll and the selected-case state; `QueueList` and `DecisionForm` are both
 * presentational. Polling (rather than a push channel) matches the applicant app's
 * `ProgressStep` pattern (task 14.5) — R34.5 asks for "near-real-time" depth/age, not a
 * websocket, and every other surface in this POC already reads state via plain GET polling.
 */
const POLL_INTERVAL_MS = 8000;

export function ReviewConsole() {
  const [cases, setCases] = useState<QueuedCaseSummary[]>([]);
  const [depth, setDepth] = useState(0);
  const [selected, setSelected] = useState<QueuedCaseSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const result = await api.getReviewQueue();
      setCases(result.cases);
      setDepth(result.depth);
      setError(null);
      // Keep the selected case's data current (e.g. a refreshed taskToken) without losing the
      // reviewer's place in the form if they are mid-decision.
      setSelected((current) => {
        if (!current) {
          return current;
        }
        return result.cases.find((c) => c.applicantId === current.applicantId) ?? current;
      });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setError("This account is not authorized to view the review queue (reviewers group only).");
      } else {
        setError("Could not load the review queue. Retrying automatically.");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const handleDecided = useCallback(() => {
    setSelected(null);
    refresh();
  }, [refresh]);

  return (
    <div className="kyc-review-layout">
      <section className="kyc-review-layout__queue">
        <div className="kyc-review-layout__queue-header">
          <h2>Review queue</h2>
          <span className="kyc-hint">{depth} case{depth === 1 ? "" : "s"} awaiting review</span>
        </div>
        {error ? <p className="kyc-error-text">{error}</p> : null}
        {loading ? (
          <p className="kyc-hint">Loading queue…</p>
        ) : (
          <QueueList cases={cases} selectedId={selected?.applicantId} onSelect={setSelected} />
        )}
      </section>
      <section className="kyc-review-layout__detail">
        {selected ? (
          <DecisionForm key={selected.applicantId} queuedCase={selected} onDecided={handleDecided} />
        ) : (
          <p className="kyc-hint">Select a case from the queue to review it.</p>
        )}
      </section>
    </div>
  );
}
