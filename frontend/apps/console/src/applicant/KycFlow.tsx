import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@shared/components/Button";
import { api, ApiError } from "@shared/api/client";
import { AppScreen, HomeHeader, WaitingArt } from "./AppShell";
import { IntakeStep } from "./IntakeStep";
import { DetailsStep } from "./DetailsStep";
import { DocumentsStep } from "./DocumentsStep";
import { LivenessStep } from "./LivenessStep";
import { ReviewStep } from "./ReviewStep";
import { StatusStep } from "./StatusStep";
import { StepUpActions } from "./ResultStep";
import { ERROR_COPY, PROGRESS_COPY } from "./copy";
import { deviceId } from "./device";
import { DeclaredIdentity, DocumentDraft, FlowStep, IntakeSelection } from "./types";
import "./app.css";

/** Where one application stands, reported to the console so it can show the process beside it. */
export interface FlowProgress {
  step: FlowStep;
  applicationId: string;
  applicantId: string | null;
  selection: IntakeSelection | null;
  declaredIdentity: DeclaredIdentity | null;
}

/** What it takes to reopen a submitted application after a reload: straight to its status, with
 * enough kept that a step-up (redo the face check, send another document) still works. */
export interface FlowResume {
  applicationId: string;
  applicantId: string;
  selection: IntakeSelection;
  declaredIdentity: DeclaredIdentity | null;
}

/** What the presenter's persona fills in, so nothing needs typing on stage. Read at mount. */
export interface FlowPrefill {
  selection: IntakeSelection;
  identity: DeclaredIdentity | null;
  documentType?: string;
}

/**
 * The applicant onboarding flow — task 14.5, D-37. One step machine, not a router, in the order
 * a bank's digital onboarding uses: welcome and consent, the declared details, the ID document,
 * the selfie video, a review of all of it, then submit and the outcome.
 *
 * None of the steps is a page an applicant should deep-link into, since each depends on state
 * the one before produced (the jurisdiction, the uploaded document, the face check on record).
 * The ID is uploaded before the selfie and nothing is sent to the bank until the review's Submit,
 * which is when the four agents start.
 *
 * A step-up loops back into the same application rather than starting a new one: the applicant
 * redoes the selfie or sends a new photo of the ID, sees the review again, and the next
 * submission is recorded as the case's next attempt (`applicants._continue_case`), with the
 * previous one kept.
 */
export function KycFlow({
  onRestart,
  onProgress,
  resume,
  prefill,
  active = true,
}: {
  onRestart: () => void;
  onProgress?: (progress: FlowProgress) => void;
  /** Read once, at mount. */
  resume?: FlowResume;
  /** Read once, at mount. */
  prefill?: FlowPrefill;
  /** This application is the one on screen; only then may a camera step use the camera. */
  active?: boolean;
}) {
  const [step, setStep] = useState<FlowStep>(resume ? "status" : "intake");
  const [selection, setSelection] = useState<IntakeSelection | null>(resume?.selection ?? null);
  /**
   * One id per run of the flow, generated here rather than inside a step, because three separate
   * calls have to agree on it: the liveness session and result (persisted before the case exists,
   * F-40), the presigned upload prefix, and case creation. The server composes the real DynamoDB
   * key as `APP-{cognito sub}-{applicationId}`, so this value cannot address anyone else's case —
   * it only separates this application from the same user's next one.
   *
   * Generated in a `useState` initialiser so it is stable across re-renders. The console opens a
   * new `KycFlow` (a new application tab) on restart, so "start a new application" genuinely gets
   * a new id rather than reusing one whose case already exists. A resumed application keeps the
   * id it was submitted under.
   */
  const [applicationId] = useState(() => resume?.applicationId ?? crypto.randomUUID());
  const [declaredIdentity, setDeclaredIdentity] = useState<DeclaredIdentity | null>(
    resume?.declaredIdentity ?? null,
  );
  const [applicantId, setApplicantId] = useState<string | null>(resume?.applicantId ?? null);
  /** The ID document this attempt carries, uploaded and waiting for the review's Submit. Kept
   * after submitting, so a selfie step-up can send the same document again. */
  const [documents, setDocuments] = useState<DocumentDraft[] | null>(null);
  /** This attempt has a face check on record: done here, or already on the submitted case. */
  const [livenessDone, setLivenessDone] = useState(Boolean(resume));
  /** What that face check found, when it was done here: the review shows a failed one as
   * failed. Null for a check already on the case when the application was reopened. */
  const [livenessLive, setLivenessLive] = useState<boolean | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [prefilled] = useState(prefill);

  // The callback is read through a ref so a parent that passes a new function each render does
  // not re-report; only a change in the application itself does.
  const progressRef = useRef(onProgress);
  progressRef.current = onProgress;
  useEffect(() => {
    progressRef.current?.({ step, applicationId, applicantId, selection, declaredIdentity });
  }, [step, applicationId, applicantId, selection, declaredIdentity]);

  async function handleSubmit(docs: DocumentDraft[]) {
    if (!selection || !declaredIdentity) return;
    setStep("submitting");
    setSubmitError(null);
    setNotice(null);
    try {
      const created = await api.createApplicant({
        jurisdiction: selection.jurisdiction,
        productApplied: selection.productApplied,
        applicationId,
        declaredIdentity,
        deviceId: deviceId(),
        documents: docs.map((d) => ({
          bucket: d.uploadedBucket ?? "",
          key: d.uploadedKey ?? "",
          type: d.type,
          issuingCountry: d.issuingCountry,
        })),
      });
      setApplicantId(created.applicantId);
      setStep("status");
    } catch (err) {
      // A double submit, or a retry after a timeout that actually landed: the case exists and is
      // being decided, so show it rather than an error.
      const existing = (err instanceof ApiError && err.status === 409
        ? (err.body as { applicantId?: string } | null)?.applicantId
        : undefined);
      if (existing) {
        setApplicantId(existing);
        setNotice(ERROR_COPY.alreadySubmitted);
        setStep("status");
        return;
      }
      setSubmitError(ERROR_COPY.submit);
      setStep("error");
      // eslint-disable-next-line no-console
      console.error(err);
    }
  }

  const stepUpActions: StepUpActions = useMemo(
    () => ({
      // The document is still in hand unless the page was reloaded; then it is asked for again.
      onRedoLiveness: () => {
        setLivenessDone(false);
        setLivenessLive(null);
        setStep("liveness");
      },
      onNewDocument: () => {
        setDocuments(null);
        setStep("documents");
      },
      onRestart,
    }),
    [onRestart],
  );

  // Where each step goes next: the first thing this attempt still lacks, else the review.
  const nextAfterDetails: FlowStep = !documents ? "documents" : !livenessDone ? "liveness" : "review";

  if (step === "intake") {
    return (
      <IntakeStep
        initial={selection ?? prefilled?.selection}
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
        initial={declaredIdentity ?? prefilled?.identity ?? null}
        onBack={() => setStep("intake")}
        onContinue={(identity) => {
          setDeclaredIdentity(identity);
          setStep(nextAfterDetails);
        }}
      />
    );
  }

  if (step === "documents" && selection) {
    return (
      <DocumentsStep
        jurisdiction={selection.jurisdiction}
        applicationId={applicationId}
        initialType={documents?.[0]?.type ?? prefilled?.documentType}
        active={active}
        onBack={() => setStep(applicantId ? "status" : "details")}
        onComplete={(docs) => {
          setDocuments(docs);
          setStep(livenessDone ? "review" : "liveness");
        }}
      />
    );
  }

  if (step === "liveness" && selection) {
    return (
      <LivenessStep
        jurisdiction={selection.jurisdiction}
        applicationId={applicationId}
        active={active}
        onComplete={(isLive) => {
          // The app moves forward whatever the result: a failed check does not block the
          // application, and the review says it failed and offers it again. R30.2 (three
          // failures route to a person, never a lockout) is enforced server-side, where the
          // failures are counted.
          setLivenessDone(true);
          setLivenessLive(isLive);
          setStep(documents ? "review" : "documents");
        }}
        // Before submission, back to the document; after a step-up, back to the application.
        onBack={() => setStep(applicantId ? "status" : "documents")}
        onBackToApplication={applicantId ? () => setStep("status") : undefined}
      />
    );
  }

  if (step === "review" && selection && declaredIdentity && documents) {
    return (
      <ReviewStep
        identity={declaredIdentity}
        selection={selection}
        documents={documents}
        liveness={livenessLive}
        onEditDetails={() => setStep("details")}
        onRetakeDocument={() => setStep("documents")}
        onRedoSelfie={() => {
          setLivenessDone(false);
          setLivenessLive(null);
          setStep("liveness");
        }}
        onSubmit={() => handleSubmit(documents)}
      />
    );
  }

  if (step === "submitting") {
    return (
      <AppScreen header={<HomeHeader />} centered>
        <div role="status">
          <WaitingArt icon="shield" tone="live" />
        </div>
        <h2 className="app-title">{PROGRESS_COPY.submitting}</h2>
      </AppScreen>
    );
  }

  if (step === "status" && applicantId) {
    return (
      <>
        {notice ? <p className="app-warning" role="status">{notice}</p> : null}
        <StatusStep
          applicantId={applicantId}
          actions={stepUpActions}
          firstName={firstNameOf(declaredIdentity)}
          productApplied={selection?.productApplied}
        />
      </>
    );
  }

  return (
    <AppScreen
      header={<HomeHeader />}
      centered
      footer={
        <Button variant="secondary" className="app-cta" onClick={onRestart}>
          {ERROR_COPY.startOver}
        </Button>
      }
    >
      <WaitingArt icon="alert" tone="warn" />
      <h2 className="app-title">{ERROR_COPY.generic}</h2>
      <p className="app-lead">{submitError ?? ERROR_COPY.generic}</p>
    </AppScreen>
  );
}

/** "TAYLOR JORDAN" -> "Taylor", for the greeting on the account. */
function firstNameOf(identity: DeclaredIdentity | null): string | null {
  const first = identity?.givenNames.trim().split(/\s+/)[0];
  if (!first) return null;
  return first.charAt(0).toUpperCase() + first.slice(1).toLowerCase();
}
