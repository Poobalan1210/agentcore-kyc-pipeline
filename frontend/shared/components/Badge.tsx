import "./Badge.css";

export type BadgeTone = "neutral" | "success" | "warning" | "danger" | "info";

/**
 * A single outcome/status label used across the console's views — the decision engine's outcome
 * vocabulary (PROVISIONAL_APPROVE_RESTRICTED, MANUAL_REVIEW, STEP_UP, REJECT) is rendered through
 * this component everywhere rather than each view inventing its own colour-coding, so the same
 * outcome always reads the same way.
 */
export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: React.ReactNode }) {
  return <span className={`kyc-badge kyc-badge--${tone}`}>{children}</span>;
}
