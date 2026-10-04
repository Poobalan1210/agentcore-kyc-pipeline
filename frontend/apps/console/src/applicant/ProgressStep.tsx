import type { ApplicantStatus } from "@shared/api/client";
import { AppScreen, HomeHeader, WaitingArt } from "./AppShell";
import { PROGRESS_COPY, STATUS_COPY } from "./copy";

/**
 * While the decision is being made — task 14.5, R32.7.
 *
 * The applicant API deliberately exposes a status, not the state machine's stages (which agent is
 * running is not the applicant's business), so this says what that status means and nothing it
 * cannot know: no invented progress steps, no ticking counter. How long it took is the bank's
 * figure, shown on the console beside the phone. Polling lives in `StatusStep`; this only renders.
 */
export function ProgressStep({ status, slow }: { status: ApplicantStatus; slow: boolean }) {
  const copy = STATUS_COPY[status] ?? STATUS_COPY.CHECKING;
  return (
    <AppScreen header={<HomeHeader />} centered>
      <div role="status" aria-live="polite">
        <WaitingArt icon="shield" tone="live" />
      </div>
      <h2 className="app-title">{copy.title}</h2>
      <p className="app-lead">{copy.body}</p>
      <p className="app-wait-note">{slow ? PROGRESS_COPY.slow : PROGRESS_COPY.keepOpen}</p>
    </AppScreen>
  );
}
