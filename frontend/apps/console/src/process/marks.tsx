import { Icon, IconName } from "../console/icons";
import type { StageStatus } from "./pipeline";

/** What each status is called, on screen and to a screen reader. */
export const STATUS_LABEL: Record<StageStatus, string> = {
  idle: "Waiting",
  active: "Running",
  done: "Done",
  warn: "Needs attention",
  fail: "Failed",
  skip: "Not applicable",
};

/**
 * A stage's or a phase's status as one round mark: a spinner while it runs, a glyph once it has
 * an answer. Shared by the checklist and the timeline, so both read the same way. Styled by
 * `ProcessView.css` (`.process-mark`).
 */
export function StatusMark({
  status,
  icon,
  steady,
}: {
  status: StageStatus;
  icon?: IconName;
  /** Active with no end, like monitoring: calm, no spinner. */
  steady?: boolean;
}) {
  if (status === "active" && !steady) {
    return (
      <span className="process-mark process-mark--active" aria-label="Running">
        <span className="process-mark__spin" />
      </span>
    );
  }
  const glyph: IconName =
    status === "done"
      ? "check"
      : status === "warn"
        ? "alert"
        : status === "fail"
          ? "x"
          : status === "skip"
            ? "skip"
            : icon ?? "clock";
  return (
    <span
      className={`process-mark process-mark--${status}`}
      aria-label={status === "active" && steady ? "Watching" : STATUS_LABEL[status]}
    >
      <Icon name={status === "active" ? icon ?? "eye" : glyph} size={14} />
    </span>
  );
}
