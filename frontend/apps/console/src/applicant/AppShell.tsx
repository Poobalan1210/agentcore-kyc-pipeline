import { ReactNode } from "react";
import { Icon } from "../console/icons";
import { BANK_NAME } from "./copy";

/**
 * The bank app's layout pieces, so every screen on the phone has the same bones: a header (the
 * onboarding progress, or the bank's own bar once the application is in), the content, and the
 * main action pinned to the bottom where a thumb reaches it.
 */

/** The four onboarding steps, as the progress header names them. */
export const ONBOARDING_STEPS = ["About you", "ID document", "Selfie", "Review"] as const;

/** The bank's mark: a monogram, so no real brand is implied. */
export function BankMark({ large = false }: { large?: boolean }) {
  return (
    <span className={"app-mark" + (large ? " app-mark--lg" : "")} aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4}
        strokeLinecap="round" strokeLinejoin="round">
        <path d="M6.5 18 12 5.5 17.5 18" />
        <path d="M8.8 13.4h6.4" />
      </svg>
    </span>
  );
}

export function BrandBar({ trailing }: { trailing?: ReactNode }) {
  return (
    <div className="app-brandbar">
      <span className="app-brand">
        <BankMark /> {BANK_NAME}
      </span>
      {trailing}
    </div>
  );
}

/** An onboarding step's header: back, where the applicant is, and a segment per step. */
export function StepHeader({ index, onBack }: { index: number; onBack?: () => void }) {
  const total = ONBOARDING_STEPS.length;
  return (
    <div className="app-step">
      <div className="app-step__row">
        {onBack ? (
          <button type="button" className="app-step__back" onClick={onBack} aria-label="Back">
            <Icon name="chevron" size={18} className="app-step__back-icon" />
          </button>
        ) : (
          <span className="app-step__back app-step__back--empty" aria-hidden="true" />
        )}
        <span className="app-step__label">
          Step {index} of {total} · {ONBOARDING_STEPS[index - 1]}
        </span>
        <span className="app-step__back app-step__back--empty" aria-hidden="true" />
      </div>
      <div
        className="app-step__bar"
        role="progressbar"
        aria-label="Application progress"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={index}
      >
        {ONBOARDING_STEPS.map((label, i) => (
          <span key={label} className={"app-step__seg" + (i < index ? " app-step__seg--done" : "")} />
        ))}
      </div>
    </div>
  );
}

/** The bank's bar once the application is in: the brand, and the messages bell. */
export function HomeHeader({ unread = false }: { unread?: boolean }) {
  return (
    <BrandBar
      trailing={
        <span className={"app-bell" + (unread ? " app-bell--unread" : "")} aria-hidden="true">
          <Icon name="bell" size={19} />
        </span>
      }
    />
  );
}

/** One screen of the app. `centered` is for a screen that is a single message. */
export function AppScreen({
  header,
  title,
  lead,
  children,
  footer,
  centered = false,
}: {
  header?: ReactNode;
  title?: ReactNode;
  lead?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  centered?: boolean;
}) {
  return (
    <div className={"app-screen" + (centered ? " app-screen--centered" : "")}>
      {header ? <div className="app-screen__header">{header}</div> : null}
      <div className="app-screen__body">
        {title ? <h2 className="app-title">{title}</h2> : null}
        {lead ? <p className="app-lead">{lead}</p> : null}
        {children}
      </div>
      {footer ? <div className="app-screen__footer">{footer}</div> : null}
    </div>
  );
}

/** A screen that is waiting on something happening elsewhere: the camera, or the bank. */
export function WaitingArt({
  icon,
  tone = "live",
}: {
  icon: "face" | "document" | "shield" | "check" | "alert" | "clock" | "x";
  tone?: "live" | "ok" | "warn" | "fail" | "idle";
}) {
  return (
    <span className={`app-orb app-orb--${tone}`} aria-hidden="true">
      {tone === "live" ? <span className="app-orb__ring" /> : null}
      <Icon name={icon} size={36} />
    </span>
  );
}
