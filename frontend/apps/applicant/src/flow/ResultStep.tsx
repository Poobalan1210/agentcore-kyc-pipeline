import { Badge, BadgeTone } from "@shared/components/Badge";
import { Button } from "@shared/components/Button";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { CaseStatusResponse } from "@shared/api/client";

/** design-engine/models.py's `Phase1Outcome` — exactly these four (R3.1), never a
 * `FULL_APPROVE_*` in Phase 1 (R3.3). Matching them here by literal string rather than a
 * partial/fuzzy match, so an outcome this UI does not recognise falls through to the `default`
 * case and is shown honestly as unrecognised rather than silently mis-rendered as one of the
 * four. */
const OUTCOME_COPY: Record<
  string,
  { title: string; tone: BadgeTone; description: string }
> = {
  PROVISIONAL_APPROVE_RESTRICTED: {
    title: "Provisionally approved",
    tone: "success",
    description:
      "Your account has been opened with temporary restrictions while we complete full " +
      "verification. These restrictions will be reviewed and may be lifted or adjusted.",
  },
  STEP_UP: {
    title: "One more step needed",
    tone: "warning",
    description: "We need additional information before we can continue.",
  },
  MANUAL_REVIEW: {
    title: "Under review",
    tone: "info",
    description:
      "Your application requires a closer look from our compliance team. We will contact you " +
      "once this review is complete.",
  },
  REJECT: {
    title: "Application not approved",
    tone: "danger",
    description: "We were unable to approve your application at this time.",
  },
};

/**
 * The provisional decision — task 14.5, R31.9, R32.8.
 *
 * "Display the outcome, the specific restrictions applied, and that they are temporary" (R31.9)
 * — `requiredItem` (STEP_UP) and `reasonCode` are shown verbatim rather than paraphrased,
 * because a paraphrase risks drifting from what the decision engine actually determined.
 */
export function ResultStep({ status, onRestart }: { status: CaseStatusResponse; onRestart: () => void }) {
  const copy = status.outcome ? OUTCOME_COPY[status.outcome] : undefined;

  return (
    <Card>
      <CardHeader>
        <h2>{copy?.title ?? "Application update"}</h2>
      </CardHeader>
      <CardBody>
        {status.outcome ? <Badge tone={copy?.tone ?? "neutral"}>{status.outcome}</Badge> : null}
        <p className="kyc-result-description">
          {copy?.description ?? "Your application status has changed."}
        </p>

        {status.outcome === "PROVISIONAL_APPROVE_RESTRICTED" ? (
          <p className="kyc-hint">
            <strong>This approval is provisional and temporary.</strong> Full verification is
            still in progress; restrictions on your account will be updated once it completes.
          </p>
        ) : null}

        {status.requiredItem ? (
          <p className="kyc-hint">
            <strong>Needed from you:</strong> {status.requiredItem}
          </p>
        ) : null}

        {status.reasonCode ? (
          <p className="kyc-hint">
            <strong>Reason:</strong> {status.reasonCode}
          </p>
        ) : null}

        <Button variant="secondary" onClick={onRestart}>
          Start a new application
        </Button>
      </CardBody>
    </Card>
  );
}
