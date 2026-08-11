import { Badge } from "@shared/components/Badge";
import { QueuedCaseSummary } from "@shared/api/client";
import "./QueueList.css";

/**
 * The AWAITING_REVIEW queue, oldest case first — `review_api.py`'s `list_queue` already returns
 * it in that order (ascending `createdAt` on the `state` GSI), so this component does not
 * re-sort. R34.1.
 */
export function QueueList({
  cases,
  selectedId,
  onSelect,
}: {
  cases: QueuedCaseSummary[];
  selectedId?: string;
  onSelect: (queuedCase: QueuedCaseSummary) => void;
}) {
  if (cases.length === 0) {
    return <p className="kyc-hint">No cases are currently awaiting review.</p>;
  }

  return (
    <ul className="kyc-queue-list">
      {cases.map((queuedCase) => (
        <li key={queuedCase.applicantId}>
          <button
            type="button"
            className={
              "kyc-queue-list__item" +
              (queuedCase.applicantId === selectedId ? " kyc-queue-list__item--selected" : "")
            }
            onClick={() => onSelect(queuedCase)}
          >
            <span className="kyc-queue-list__case-id">{queuedCase.caseId}</span>
            <span className="kyc-queue-list__meta">
              <Badge tone="neutral">{queuedCase.jurisdiction}</Badge>
              <span className="kyc-queue-list__age">{formatAge(queuedCase.createdAt)}</span>
            </span>
            {!queuedCase.taskToken ? (
              <span className="kyc-hint kyc-queue-list__no-token">
                Task token not yet visible — refreshing automatically.
              </span>
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  );
}

function formatAge(createdAt: string): string {
  const created = Date.parse(createdAt);
  if (Number.isNaN(created)) {
    return createdAt;
  }
  const seconds = Math.max(0, Math.floor((Date.now() - created) / 1000));
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours}h ago`;
}
