import { useMemo } from "react";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { Icon } from "../console/icons";
import { hrefFor, navigate } from "../console/route";
import { buildProcess } from "../process/pipeline";
import { ProcessView } from "../process/ProcessView";
import { useCaseProcess } from "../process/useCaseProcess";
import { CaseTrace } from "../review/CaseTrace";
import "./Cases.css";

/**
 * One case from the board: its whole process, then everything the reviewer console would show
 * about it (facts, decisions, agent narrative, timeline, applicant and account), read-only.
 */
export function CaseDetail({ caseId, canReview }: { caseId: string; canReview: boolean }) {
  // `/cases/{id}` takes the applicant id; the trace route takes either form.
  const applicantId = caseId.startsWith("CASE-") ? caseId.slice("CASE-".length) : caseId;
  const data = useCaseProcess(applicantId, { withTrace: canReview, visible: true });
  const view = useMemo(
    () =>
      buildProcess({
        status: data.status,
        trace: data.trace,
        now: data.now,
        traceAvailable: canReview && !data.traceDenied,
      }),
    [data, canReview],
  );
  const awaitingReview = data.trace?.applicant.state === "AWAITING_REVIEW";

  return (
    <div className="case-detail">
      <div className="case-detail__bar">
        <a href={hrefFor("cases")} className="case-detail__back">
          <Icon name="chevron" size={14} className="case-detail__back-icon" /> All cases
        </a>
        {awaitingReview && canReview ? (
          <button
            type="button"
            className="case-detail__review"
            onClick={() => navigate("review", data.trace?.applicant.caseId ?? caseId)}
          >
            <Icon name="review" size={15} /> Open in the review queue
          </button>
        ) : null}
      </div>

      <div className="case-detail__grid">
        <ProcessView
          view={view}
          notice={
            !canReview || data.traceDenied
              ? "Only reviewers can read a case's facts and decisions; showing its status only."
              : data.error
          }
        />
        <Card>
          <CardHeader>
            <h2>Case record</h2>
          </CardHeader>
          <CardBody>
            {data.trace ? (
              <CaseTrace trace={data.trace} />
            ) : (
              <p className="kyc-hint">
                {data.traceDenied || !canReview ? "Reviewers only." : "Loading…"}
              </p>
            )}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
