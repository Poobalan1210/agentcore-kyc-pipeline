import { ReactNode, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { Button } from "@shared/components/Button";
import type { LivenessChallenge } from "@shared/api/client";
import { Icon } from "../console/icons";
import { AppScreen, StepHeader, WaitingArt } from "./AppShell";
import { SELFIE_COPY } from "./copy";
import "./liveness.css";

/** One Face Liveness session, as the backend issued it. */
export interface LivenessSession {
  sessionId: string;
  region: string;
  challenge: LivenessChallenge;
}

/** Where the face check is up to. `LivenessStep` owns it; everything here only renders it. */
export type LivenessPhase =
  | { kind: "starting" }
  | { kind: "checking"; session: LivenessSession }
  | { kind: "verifying"; session: LivenessSession }
  | { kind: "done"; isLive: boolean }
  | { kind: "stopped" }
  | { kind: "error"; message: string; closed: boolean };

/**
 * A camera step's own stage: the whole window, with the camera view at the top centre. The selfie
 * video uses it, and so does the photo of the ID.
 *
 * A laptop's webcam sits above the middle of the display. A camera view in the phone mock-up on
 * the left made the person look left, so Rekognition saw a face in three-quarter profile and kept
 * asking to centre it. Here the view sits right under the lens, as large as the window allows, so
 * looking at it is looking at the camera.
 *
 * Rendered into `document.body`, outside the console's layout, so nothing on the page can clip or
 * cover it. No ancestor of the detector carries a transform, a filter or a backdrop filter: the
 * light challenge paints its colours on a `position: fixed` canvas, and any of those would confine
 * that canvas to the ancestor instead of the whole window.
 */
export function LivenessStage({
  label,
  live,
  lens = live,
  lensText = "Look up here, at the camera",
  children,
  footer,
  onEscape,
}: {
  label: string;
  /** The camera is running: the frame glows. */
  live: boolean;
  /** Point at the webcam: while the camera is starting or running. */
  lens?: boolean;
  lensText?: string;
  /** What goes in the frame: the camera, or a panel in its place. */
  children: ReactNode;
  /** Instructions, actions and the privacy note, under the frame. */
  footer: ReactNode;
  onEscape?: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);

  // The page underneath must not scroll while the camera runs, and focus starts in the dialog.
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  useEffect(() => {
    if (!onEscape) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onEscape();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onEscape]);

  return createPortal(
    <div
      className="lv-stage"
      role="dialog"
      aria-modal="true"
      aria-label={label}
      ref={dialogRef}
      tabIndex={-1}
    >
      <div className="lv-stage__backdrop" aria-hidden="true" />
      <div className="lv-stage__column">
        {/* Kept in the layout either way, so the frame does not jump when it goes. */}
        <div className={"lv-stage__lens" + (lens ? "" : " lv-stage__lens--hidden")} aria-hidden="true">
          <Icon name="arrow" size={14} className="lv-stage__lens-arrow" />
          {lensText}
        </div>
        <div className={"lv-stage__frame kyc-light" + (live ? " lv-stage__frame--live" : "")}>
          {children}
        </div>
        {footer}
      </div>
    </div>,
    document.body,
  );
}

/** What fills the frame when the camera is not running. */
export function StagePanel({ phase }: { phase: LivenessPhase }) {
  if (phase.kind === "done") {
    return (
      <div className="lv-panel" role="status">
        <span className={`lv-panel__icon ${phase.isLive ? "lv-panel__icon--ok" : "lv-panel__icon--warn"}`}>
          <Icon name={phase.isLive ? "check" : "alert"} size={48} />
        </span>
        <p className="lv-panel__text">
          {phase.isLive ? "Live person confirmed" : "We couldn't confirm a live person"}
        </p>
        <p className="lv-panel__sub">
          {phase.isLive
            ? "Next: check your details and submit"
            : "You can carry on; we may ask you to repeat the selfie video."}
        </p>
      </div>
    );
  }
  if (phase.kind === "stopped" || phase.kind === "error") {
    return (
      <div className="lv-panel" role={phase.kind === "error" ? "alert" : "status"}>
        <span className={`lv-panel__icon ${phase.kind === "error" ? "lv-panel__icon--warn" : "lv-panel__icon--idle"}`}>
          <Icon name={phase.kind === "error" ? "alert" : "face"} size={44} />
        </span>
        <p className="lv-panel__text">
          {phase.kind === "error" ? "The face check didn't finish" : "Face check stopped"}
        </p>
        <p className="lv-panel__sub">
          {phase.kind === "error" ? phase.message : "The camera is off. Start again when you're ready."}
        </p>
      </div>
    );
  }
  return (
    <div className="lv-panel" role="status">
      <span className="lv-panel__spinner" aria-hidden="true" />
      <p className="lv-panel__sub">Starting the camera…</p>
    </div>
  );
}

export function StageFooter({
  phase,
  movement,
  onRetry,
  onBack,
  onBackToApplication,
}: {
  phase: LivenessPhase;
  movement: boolean;
  onRetry: () => void;
  onBack?: () => void;
  onBackToApplication?: () => void;
}) {
  const running = phase.kind === "starting" || phase.kind === "checking";
  // Once the frame itself says what happened (a result, a stop, an error), the footer does not
  // say it again: it keeps only the actions.
  const title = phase.kind === "verifying" ? "Checking…" : running ? "Look straight at the camera" : null;
  const lead =
    phase.kind === "verifying"
      ? "Amazon Rekognition is confirming that a real person is in front of the camera."
      : running
        ? movement
          ? "Move closer until your face fills the oval, then hold still for a moment. It starts by itself."
          : "Move closer until your face fills the oval, then hold still while a few colours flash."
        : null;

  return (
    <>
      <div className="lv-stage__copy">
        <span className="lv-stage__eyebrow">
          <Icon name="face" size={14} /> Selfie video · Amazon Rekognition Face Liveness
        </span>
        {title ? <h2 className="lv-stage__title">{title}</h2> : null}
        {lead ? <p className="lv-stage__lead">{lead}</p> : null}
      </div>

      <div className="lv-stage__actions">
        {phase.kind === "error" && phase.closed ? (
          onBackToApplication ? (
            <Button variant="primary" onClick={onBackToApplication}>
              Back to my application
            </Button>
          ) : null
        ) : phase.kind === "error" || phase.kind === "stopped" ? (
          // F-41: sessions are single-use; a retry always asks for a fresh SessionId.
          <Button variant="primary" onClick={onRetry}>
            {phase.kind === "error" ? "Start a new attempt" : "Try again"}
          </Button>
        ) : null}
        {onBack && (running || phase.kind === "stopped" || (phase.kind === "error" && !phase.closed)) ? (
          <Button variant="ghost" onClick={onBack}>
            {running ? "Cancel" : "Back"}
          </Button>
        ) : null}
      </div>

      {/*
        D-14: every deletion claim is scoped to our copy. Rekognition keeps its own reference
        images, and this demo account has not opted out of their service-improvement use. The
        full notice is on the welcome screen, where the applicant agreed to it.
      */}
      <p className="lv-stage__privacy">
        The video goes straight to Amazon Rekognition. Our copy of one still is deleted after about
        a day; AWS may keep its own, as this demo has not opted out.
      </p>
    </>
  );
}

/** The phone, while the face check runs on its own stage. Exactly what the applicant sees. */
export function PhoneScreen({ phase, onBack }: { phase: LivenessPhase; onBack?: () => void }) {
  const done = phase.kind === "done";
  const stopped = phase.kind === "error" || phase.kind === "stopped";
  return (
    <AppScreen header={<StepHeader index={3} onBack={onBack} />} centered>
      <div role="status">
        {done ? (
          <WaitingArt icon={phase.isLive ? "check" : "alert"} tone={phase.isLive ? "ok" : "warn"} />
        ) : stopped ? (
          <WaitingArt icon="face" tone="idle" />
        ) : (
          <WaitingArt icon="face" tone="live" />
        )}
      </div>
      <h2 className="app-title">
        {done
          ? phase.isLive
            ? SELFIE_COPY.done
            : SELFIE_COPY.finished
          : stopped
            ? SELFIE_COPY.stopped
            : SELFIE_COPY.title}
      </h2>
      <p className="app-lead">
        {done ? SELFIE_COPY.doneNext : phase.kind === "verifying" ? SELFIE_COPY.verifying : SELFIE_COPY.lead}
      </p>
    </AppScreen>
  );
}
