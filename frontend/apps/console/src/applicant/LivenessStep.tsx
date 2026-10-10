import { useCallback, useEffect, useRef, useState } from "react";
import { FaceLivenessDetector } from "@aws-amplify/ui-react-liveness";
import type { FaceLivenessDetectorProps } from "@aws-amplify/ui-react-liveness";
import { api, ApiError, LivenessChallenge } from "@shared/api/client";
import {
  LivenessPhase as Phase,
  LivenessSession as Session,
  LivenessStage,
  PhoneScreen,
  StageFooter,
  StagePanel,
} from "./LivenessStage";

/**
 * Face Liveness capture — task 14.5, R32.4, F-39, F-40, F-41, D-36.
 *
 * F-39: this cannot be a server-side check. The flow is: this component asks the backend for a
 * SessionId (`POST /liveness/session`), renders `FaceLivenessDetector` with it, the component
 * streams video directly to the Rekognition streaming service (bypassing the Public API), and
 * only the boolean result comes back through the backend afterward.
 *
 * F-40/F-41: a SessionId expires 3 minutes after issue and can be used exactly once. Every start
 * and every retry asks for a new one, never reusing one across a retry, so a failed attempt always
 * gets a genuinely fresh session rather than a session that is now guaranteed to fail.
 *
 * The session is bound to this application's case when it is created, and the result is
 * accepted only for the session the case is waiting on. Once the application is submitted the
 * case stops accepting liveness results (409 `CASE_NOT_ACCEPTING_LIVENESS`) until it asks for
 * one again, so the evidence a decision is made on cannot change underneath it.
 *
 * The check itself runs on its own full-window stage (`LivenessStage`), under the webcam; the
 * phone shows where it is up to. With the movement challenge (D-36) it starts as soon as the
 * camera is ready: nothing to press, nothing flashes. The light challenge keeps the component's
 * start screen, because that is where the photosensitivity warning is shown before the colours.
 */

/**
 * How long the detector may sit in its own connect/init state before we treat it as wedged rather
 * than slow. Streaming normally begins in a few seconds; this is deliberately several times that
 * so a slow network or a cold model load is never mistaken for the permanent stall described on
 * the watchdog effect below.
 */
const LIVENESS_CONNECT_TIMEOUT_MS = 25_000;

/** How long the result stays on the stage before the flow moves on to the review. */
const RESULT_HOLD_MS = 1600;

const MOVEMENT: LivenessChallenge = "FaceMovementChallenge";

/** Short, direct prompts, read from across a room. Only wording: geometry and timing are not
 * touched (AWS documents them as part of the check's security and accuracy). */
const DISPLAY_TEXT: FaceLivenessDetectorProps["displayText"] = {
  hintMoveFaceFrontOfCameraText: "Look straight at the camera",
  hintCanNotIdentifyText: "Look straight at the camera",
  hintTooManyFacesText: "Only one face in view, please",
  hintFaceDetectedText: "Face found",
  hintTooCloseText: "A little further back",
  hintTooFarText: "Move closer",
  hintFaceOffCenterText: "Center your face in the oval",
  hintHoldFaceForFreshnessText: "Hold still",
  hintConnectingText: "Connecting to Amazon Rekognition…",
  hintVerifyingText: "Checking…",
  hintCheckCompleteText: "Done",
  hintIlluminationTooBrightText: "Too bright: try softer light",
  hintIlluminationTooDarkText: "Too dark: find more light",
  waitingCameraPermissionText: "Allow camera access to start",
  startScreenBeginCheckText: "Start the face check",
};

export function LivenessStep({
  jurisdiction,
  applicationId,
  active = true,
  onComplete,
  onBack,
  onBackToApplication,
}: {
  jurisdiction: string;
  /**
   * The flow's application id, owned by `KycFlow`. This component must **not** generate its own:
   * it previously created a `crypto.randomUUID()` here and sent that as the applicant id, so
   * every liveness result was written to an orphan DynamoDB row unrelated to the case, and every
   * case then reached Phase 1 with no liveness evidence and was correctly stepped up with
   * LIVENESS_REQUIRED. The id has to be the one the case is created under, which only the flow
   * root knows.
   */
  applicationId: string;
  /**
   * This application is the one on screen. The camera runs only while it is: switching to
   * another application or view stops it, and coming back starts a fresh session.
   */
  active?: boolean;
  onComplete: (isLive: boolean) => void;
  /** Leave the face check: back to the ID document, or to the application after a step-up. */
  onBack?: () => void;
  /** Offered when the case is no longer accepting a liveness result, if there is a case. */
  onBackToApplication?: () => void;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: "starting" });
  /** Bumped by every start and every stop, so a response for an abandoned attempt is ignored. */
  const attemptRef = useRef(0);

  const fetchSession = useCallback(async () => {
    const attempt = ++attemptRef.current;
    setPhase({ kind: "starting" });
    try {
      const created = await api.createLivenessSession(jurisdiction, applicationId);
      if (attempt !== attemptRef.current) return;
      setPhase({
        kind: "checking",
        session: {
          sessionId: created.sessionId,
          region: created.region,
          // An API from before D-36 does not say, and its sessions run the light challenge.
          challenge: created.challenge ?? "FaceMovementAndLightChallenge",
        },
      });
    } catch (err) {
      if (attempt !== attemptRef.current) return;
      if (err instanceof ApiError && err.code === "CASE_NOT_ACCEPTING_LIVENESS") {
        setPhase({
          kind: "error",
          closed: true,
          message: "This application has already been submitted, so its face check can't be redone now.",
        });
      } else {
        setPhase({ kind: "error", closed: false, message: "We could not start the face check. Please try again." });
      }
      // eslint-disable-next-line no-console
      console.error(err);
    }
  }, [jurisdiction, applicationId]);

  // One session each time the step comes on screen. The ref, not the effect, decides whether to
  // start: StrictMode runs a new effect twice, and two sessions would race to be the case's bound
  // one, so the result could later be refused as SESSION_NOT_BOUND.
  const startedRef = useRef(false);
  useEffect(() => {
    if (!active) {
      startedRef.current = false;
      attemptRef.current += 1;
      setPhase((current) => (current.kind === "done" ? current : { kind: "starting" }));
      return;
    }
    if (startedRef.current) return;
    startedRef.current = true;
    fetchSession();
  }, [active, fetchSession]);

  const stop = useCallback(() => {
    attemptRef.current += 1;
    setPhase((current) =>
      current.kind === "starting" || current.kind === "checking" ? { kind: "stopped" } : current,
    );
  }, []);

  /**
   * Watchdog for a wedged connect, observed on a real webcam (MacBook Air Camera, Chrome 141).
   *
   * `FaceLivenessDetector`'s internal machine goes `initCamera.cameraCheck` -> (on success)
   * `initCamera.waitForDOMAndCameraDetails`, and that state is declared `{}` — no `after`, no
   * timeout, no error transition. It leaves it only on a `SET_DOM_AND_CAMERA_DETAILS` event. When
   * that event never arrives the machine parks there permanently: the WebSocket to
   * streaming-rekognition is never opened, `onError` is never invoked, and the applicant is left
   * on "Connecting..." forever with no way forward. Measured: still spinning after 150s.
   *
   * getUserMedia itself is NOT the failure — the library's exact constraint shape
   * ({width:{min:320,ideal:640}, height:{min:240,ideal:480}, frameRate:{min:15,ideal:30,max:60},
   * facingMode:'user'}) resolves fine against this camera. The stall is downstream of acquisition.
   *
   * Because the component exposes no "connected" callback, progress is read from the library's own
   * rendered state: it shows `data-testid="centered-loader"` for exactly `isInitCamera ||
   * isInitWebsocket`, so that node disappearing IS the signal that streaming began. The separate
   * `waiting-camera-permission` node is excluded, otherwise an applicant who takes their time over
   * the browser permission prompt would be failed for being slow rather than for being stuck.
   *
   * A liveness SessionId also expires 3 minutes after issue (F-40), so waiting longer than this
   * cannot succeed anyway — the session would be dead before the applicant ever saw the oval.
   */
  const checkingId = phase.kind === "checking" ? phase.session.sessionId : null;
  useEffect(() => {
    if (!checkingId) return;
    const startedAt = Date.now();
    const timer = setInterval(() => {
      const connecting = document.querySelector('[data-testid="centered-loader"]');
      const awaitingPermission = document.querySelector(
        '[data-testid="waiting-camera-permission"]',
      );
      if (!connecting) {
        clearInterval(timer);
        return;
      }
      if (!awaitingPermission && Date.now() - startedAt >= LIVENESS_CONNECT_TIMEOUT_MS) {
        clearInterval(timer);
        attemptRef.current += 1;
        setPhase({
          kind: "error",
          closed: false,
          message:
            "The camera check could not start. This can happen with some cameras or if another " +
            "app is using the camera. Close any other app using it, then start a new attempt.",
        });
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [checkingId]);

  // The detector reads its callbacks once, when it mounts, so they take the session as an
  // argument rather than reading it from state that may have moved on.
  const finish = useCallback(
    async (session: Session) => {
      const attempt = attemptRef.current;
      setPhase({ kind: "verifying", session });
      try {
        const result = await api.getLivenessResult(session.sessionId, jurisdiction, applicationId);
        if (attempt !== attemptRef.current) return;
        // R33.2/F-44: `result.isLive` is the only field this call ever returns — there is no raw
        // confidence score anywhere in this component's data, because the backend never sent one.
        setPhase({ kind: "done", isLive: result.isLive });
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error(err);
        if (attempt !== attemptRef.current) return;
        if (err instanceof ApiError && err.code === "CASE_NOT_ACCEPTING_LIVENESS") {
          setPhase({
            kind: "error",
            closed: true,
            message: "This application has already been submitted, so its face check can't be redone now.",
          });
          return;
        }
        // SESSION_EXPIRED (410): over three minutes. SESSION_NOT_BOUND (409): a newer session
        // replaced this one, for example in a second tab. Either way only a fresh session can work.
        setPhase({
          kind: "error",
          closed: false,
          message:
            err instanceof ApiError && err.status === 410
              ? "That check took longer than three minutes and expired. Please start a new one."
              : "We could not retrieve your face check result. Please start a new one.",
        });
      }
    },
    [jurisdiction, applicationId],
  );

  const handleError = useCallback((livenessError: unknown) => {
    // eslint-disable-next-line no-console
    console.error("liveness error", livenessError);
    // Not retried automatically: a camera that is denied or busy fails again at once, and every
    // automatic retry would open another Rekognition session.
    attemptRef.current += 1;
    setPhase({ kind: "error", closed: false, message: "The face check was interrupted. Please start a new attempt." });
  }, []);

  // The result stays up for a moment, then the flow moves on: a failed check does not block the
  // application, and the review says it failed and offers it again. R30.2 (three failures route
  // to a person, never a lockout) is enforced server-side, where the failures are counted.
  const completeRef = useRef(onComplete);
  completeRef.current = onComplete;
  const doneLive = phase.kind === "done" ? phase.isLive : null;
  useEffect(() => {
    if (doneLive === null) return;
    const id = setTimeout(() => completeRef.current(doneLive), RESULT_HOLD_MS);
    return () => clearTimeout(id);
  }, [doneLive]);

  const session = phase.kind === "checking" || phase.kind === "verifying" ? phase.session : null;
  const movement = session ? session.challenge === MOVEMENT : true;

  return (
    <>
      <PhoneScreen phase={phase} onBack={onBack} />
      {active ? (
        <LivenessStage
          label="Live face check"
          live={phase.kind === "checking"}
          lens={phase.kind === "starting" || phase.kind === "checking"}
          onEscape={phase.kind === "starting" || phase.kind === "checking" ? stop : undefined}
          footer={
            <StageFooter
              phase={phase}
              movement={movement}
              onRetry={fetchSession}
              onBack={onBack}
              onBackToApplication={onBackToApplication}
            />
          }
        >
          {session ? (
            <FaceLivenessDetector
              key={session.sessionId}
              sessionId={session.sessionId}
              region={session.region}
              // The light challenge keeps the start screen: it carries the photosensitivity
              // warning, which must be read before the colours flash.
              disableStartScreen={session.challenge === MOVEMENT}
              displayText={DISPLAY_TEXT}
              onAnalysisComplete={() => finish(session)}
              onUserCancel={stop}
              onError={handleError}
            />
          ) : (
            <StagePanel phase={phase} />
          )}
        </LivenessStage>
      ) : null}
    </>
  );
}

