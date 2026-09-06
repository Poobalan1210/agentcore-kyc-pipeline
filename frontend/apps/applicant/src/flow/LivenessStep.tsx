import { useCallback, useEffect, useState } from "react";
import { FaceLivenessDetector } from "@aws-amplify/ui-react-liveness";
import { Loader } from "@aws-amplify/ui-react";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { Button } from "@shared/components/Button";
import { api } from "@shared/api/client";

/**
 * Face Liveness capture — task 14.5, R32.4, F-39, F-40, F-41.
 *
 * F-39: this cannot be a server-side check. The flow is: this component asks the backend for a
 * SessionId (`POST /liveness/session`), renders `FaceLivenessDetector` with it, the component
 * streams video directly to the Rekognition streaming service (bypassing the Public API), and
 * only the boolean result comes back through the backend afterward.
 *
 * F-40/F-41: a SessionId expires 3 minutes after issue and can be used exactly once — reused on
 * any retry. `fetchSession` is called again on every mount and on every `onError`, never cached
 * across a retry, so a failed attempt always gets a genuinely fresh session rather than retrying
 * a session that is now guaranteed to fail.
 */
/**
 * How long the detector may sit in its own connect/init state before we treat it as wedged rather
 * than slow. Streaming normally begins in a few seconds; this is deliberately several times that
 * so a slow network or a cold model load is never mistaken for the permanent stall described on
 * the watchdog effect below.
 */
const LIVENESS_CONNECT_TIMEOUT_MS = 25_000;

export function LivenessStep({
  jurisdiction,
  applicationId,
  onComplete,
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
  onComplete: (isLive: boolean) => void;
}) {
  const [session, setSession] = useState<{ sessionId: string; region: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSession = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const created = await api.createLivenessSession(jurisdiction);
      setSession(created);
    } catch (err) {
      setError("We could not start the liveness check. Please try again.");
      // eslint-disable-next-line no-console
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, [jurisdiction]);

  useEffect(() => {
    fetchSession();
  }, [fetchSession]);

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
  useEffect(() => {
    if (!session || error) return;
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
        setError(
          "The camera check could not start. This can happen with some cameras or if another " +
            "app is using the camera. Close any other app using it, then start a new attempt.",
        );
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [session, error]);

  const handleAnalysisComplete = useCallback(async () => {
    if (!session) return;
    try {
      const result = await api.getLivenessResult(
        session.sessionId,
        jurisdiction,
        applicationId,
      );
      // R33.2/F-44: `result.isLive` is the only field this call ever returns — there is no raw
      // confidence score anywhere in this component's data, because the backend never sent one.
      onComplete(result.isLive);
    } catch (err) {
      setError("We could not retrieve your liveness result. Please try again.");
      // eslint-disable-next-line no-console
      console.error(err);
      await fetchSession();
    }
  }, [session, jurisdiction, applicationId, onComplete, fetchSession]);

  const handleError = useCallback(
    async (livenessError: unknown) => {
      // eslint-disable-next-line no-console
      console.error("liveness error", livenessError);
      setError("The liveness check was interrupted. Starting a new attempt.");
      // F-41: sessions are single-use; any error requires a fresh SessionId, never a retry of
      // the one that just failed.
      await fetchSession();
    },
    [fetchSession],
  );

  return (
    <Card>
      <CardHeader>
        <h2>Confirm you're really here</h2>
      </CardHeader>
      <CardBody>
        <p className="kyc-hint">
          Position your face in the frame and follow the on-screen prompts. This check happens
          entirely in your browser and is not recorded.
        </p>
        {error ? (
          <div className="kyc-liveness-error">
            <p>{error}</p>
            <Button variant="secondary" onClick={fetchSession}>
              Try again
            </Button>
          </div>
        ) : loading || !session ? (
          <Loader />
        ) : (
          <div className="kyc-liveness-frame">
            <FaceLivenessDetector
              sessionId={session.sessionId}
              region={session.region}
              onAnalysisComplete={handleAnalysisComplete}
              onError={handleError}
            />
          </div>
        )}
      </CardBody>
    </Card>
  );
}
