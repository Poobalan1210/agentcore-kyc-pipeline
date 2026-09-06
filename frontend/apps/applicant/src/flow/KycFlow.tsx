import { useState } from "react";
import { Button } from "@shared/components/Button";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { api, CaseStatusResponse } from "@shared/api/client";
import { IntakeStep } from "./IntakeStep";
import { DetailsStep } from "./DetailsStep";
import { LivenessStep } from "./LivenessStep";
import { DocumentsStep } from "./DocumentsStep";
import { ProgressStep } from "./ProgressStep";
import { ResultStep } from "./ResultStep";
import { DeclaredIdentity, DocumentDraft, FlowStep, IntakeSelection } from "./types";

/**
 * The applicant capture flow — task 14.5. One step machine, not a router: the steps are
 * strictly sequential (intake -> liveness -> documents -> submit -> progress -> result) and
 * none of them is a page an applicant should be able to deep-link into independently, since
 * each depends on state (jurisdiction, liveness result, uploaded documents) the previous step
 * produced.
 */
export function KycFlow({ onRestart }: { onRestart: () => void }) {
  const [step, setStep] = useState<FlowStep>("intake");
  const [selection, setSelection] = useState<IntakeSelection | null>(null);
  /**
   * One id per run of the flow, generated here rather than inside a step, because three separate
   * calls have to agree on it: the liveness result (persisted before the case exists, F-40), the
   * presigned upload prefix, and case creation. The server composes the real DynamoDB key as
   * `APP-{cognito sub}-{applicationId}`, so this value cannot address anyone else's case — it
   * only separates this application from the same user's next one.
   *
   * Generated in a `useState` initialiser so it is stable across re-renders. `App.tsx` remounts
   * `KycFlow` with a fresh `key` on restart, so "start a new application" genuinely gets a new
   * id rather than reusing one whose case already exists.
   */
  const [applicationId] = useState(() => crypto.randomUUID());
  const [declaredIdentity, setDeclaredIdentity] = useState<DeclaredIdentity | null>(null);
  // Not read anywhere in this component — kept only because a future step (e.g. showing "you
  // may be asked to verify again" when isLive is false) is a plausible extension, and the value
  // is otherwise discarded the moment onComplete fires. Not wired into the UI today: R30.2's
  // liveness-failure handling is enforced server-side (see LivenessStep's comment), so this
  // client-side value has no decision to make yet.
  const [, setIsLive] = useState<boolean | null>(null);
  const [applicantId, setApplicantId] = useState<string | null>(null);
  const [result, setResult] = useState<CaseStatusResponse | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  async function handleDocumentsComplete(documents: DocumentDraft[]) {
    if (!selection || !declaredIdentity) return;
    setStep("submitting");
    setSubmitError(null);
    try {
      const created = await api.createApplicant({
        jurisdiction: selection.jurisdiction,
        productApplied: selection.productApplied,
        applicationId,
        declaredIdentity,
        documents: documents.map((d) => ({
          bucket: d.uploadedBucket ?? "",
          key: d.uploadedKey ?? "",
          type: d.type,
          issuingCountry: d.issuingCountry,
        })),
      });
      setApplicantId(created.applicantId);
      setStep("progress");
    } catch (err) {
      setSubmitError("We could not submit your application. Please try again.");
      setStep("error");
      // eslint-disable-next-line no-console
      console.error(err);
    }
  }

  if (step === "intake") {
    return (
      <IntakeStep
        onContinue={(sel) => {
          setSelection(sel);
          setStep("details");
        }}
      />
    );
  }

  if (step === "details" && selection) {
    return (
      <DetailsStep
        jurisdiction={selection.jurisdiction}
        onContinue={(identity) => {
          setDeclaredIdentity(identity);
          setStep("liveness");
        }}
      />
    );
  }

  if (step === "liveness" && selection) {
    return (
      <LivenessStep
        jurisdiction={selection.jurisdiction}
        applicationId={applicationId}
        onComplete={(live) => {
          setIsLive(live);
          // R32.5/design's liveness contract: the app moves forward regardless of the boolean
          // here — a failed liveness check does not block document upload. R30.2 (three
          // liveness failures route to MANUAL_REVIEW with an alternative path, never a lockout)
          // is enforced server-side by CheckPreconditions in asl.py, not by gating the UI.
          setStep("documents");
        }}
      />
    );
  }

  if (step === "documents" && selection) {
    return (
      <DocumentsStep
        jurisdiction={selection.jurisdiction}
        applicationId={applicationId}
        onComplete={handleDocumentsComplete}
      />
    );
  }

  if (step === "submitting") {
    return (
      <Card>
        <CardBody>
          <p>Submitting your application…</p>
        </CardBody>
      </Card>
    );
  }

  if (step === "progress" && applicantId) {
    return (
      <ProgressStep
        applicantId={applicantId}
        onSettled={(status) => {
          setResult(status);
          setStep("result");
        }}
      />
    );
  }

  if (step === "result" && result) {
    return (
      <ResultStep
        status={result}
        onRestart={onRestart}
        // Back to the liveness step with the same application: same applicationId, same declared
        // identity, same document. A step-up asks for one thing, so the applicant is sent to do
        // that one thing instead of starting again from the jurisdiction picker.
        onRetryLiveness={() => {
          setResult(null);
          setSubmitError(null);
          setStep("liveness");
        }}
      />
    );
  }

  return (
    <Card>
      <CardHeader>
        <h2>Something went wrong</h2>
      </CardHeader>
      <CardBody>
        <p className="kyc-error-text">{submitError ?? "An unexpected error occurred."}</p>
        <Button variant="secondary" onClick={onRestart}>
          Start over
        </Button>
      </CardBody>
    </Card>
  );
}
