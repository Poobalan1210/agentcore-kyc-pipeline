import { Badge } from "@shared/components/Badge";
import { QueuedCaseSummary } from "@shared/api/client";
import { formatAge, phaseLabel, reasonLabel } from "./labels";
import "./QueueList.css";

/**
 * The AWAITING_REVIEW queue, oldest case first — `review_api.py`'s `list_queue` already returns
 * it in that order (ascending `createdAt` on the `state` GSI), so this component does not
 * re-sort. R34.1.
 *
 * Each row says which phase the review belongs to and why the case is here, because the two
 * decide what the reviewer is being asked: "should this person get an account" in Phase 1,
 * "should this customer keep one" in Phase 3.
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
            aria-current={queuedCase.applicantId === selectedId ? "true" : undefined}
            onClick={() => onSelect(queuedCase)}
          >
            <span className="kyc-queue-list__case-id">{queuedCase.caseId}</span>
            <span className="kyc-queue-list__reason">{reasonLabel(queuedCase.reason)}</span>
            <span className="kyc-queue-list__meta">
              <span className="kyc-queue-list__badges">
                <Badge tone={queuedCase.phase === "phase3" ? "warning" : "neutral"}>
                  {phaseLabel(queuedCase.phase)}
                </Badge>
                <Badge tone="neutral">{queuedCase.jurisdiction}</Badge>
                {queuedCase.hasBriefing ? <Badge tone="info">Briefing ready</Badge> : null}
              </span>
              <span className="kyc-queue-list__age">
                {formatAge(queuedCase.queuedAt ?? queuedCase.createdAt)}
              </span>
            </span>
            {tokenState(queuedCase) === "pending" ? (
              <span className="kyc-hint kyc-queue-list__no-token">
                Handing off to review — this refreshes automatically.
              </span>
            ) : null}
            {tokenState(queuedCase) === "orphaned" ? (
              <span className="kyc-hint kyc-queue-list__orphaned">
                Review window expired — no decision can be submitted from the console.
                Needs an operator re-drive.
              </span>
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  );
}

/**
 * How long the `ManualReview` task waits on its task token before Step Functions times it out —
 * `TimeoutSeconds: 604800` on the `sqs:sendMessage.waitForTaskToken` task in `asl.py`. Kept as a
 * named constant with that trace because the number is the ONLY thing that separates "the token
 * has not arrived yet" from "the token can never arrive".
 */
const REVIEW_TASK_TIMEOUT_MS = 604_800 * 1000;

/**
 * Three states, not two. The token is written to the case record by the token materialiser a
 * moment after the case is queued, so a case can legitimately appear with no token for a moment —
 * that is `pending`, and a refresh fixes it.
 *
 * But once the review task has timed out, the execution has moved on and the token is gone for
 * good. The case still sits in `AWAITING_REVIEW`, so it keeps appearing here forever. Showing
 * "refreshing automatically" for that case tells a reviewer to wait for something that will never
 * happen — which is how four cases sat in this queue for over three weeks. Naming the state is the
 * fix: the reviewer stops waiting and the case gets escalated to whoever can re-drive it.
 *
 * Measured from when the review was queued, not when the case was created: a Phase 3 review is
 * queued months after creation and must not read as expired the moment it arrives.
 */
function tokenState(queuedCase: QueuedCaseSummary): "actionable" | "pending" | "orphaned" {
  if (queuedCase.taskToken) {
    return "actionable";
  }
  const queued = Date.parse(queuedCase.queuedAt ?? queuedCase.createdAt);
  if (Number.isNaN(queued)) {
    return "pending";
  }
  return Date.now() - queued > REVIEW_TASK_TIMEOUT_MS ? "orphaned" : "pending";
}
