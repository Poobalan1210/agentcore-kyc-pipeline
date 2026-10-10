import { ReactNode, useCallback, useEffect, useState } from "react";
import { api } from "@shared/api/client";
import { LiveDemo } from "../live/LiveDemo";
import { ReviewConsole } from "../review/ReviewConsole";
import { CasesView } from "../cases/CasesView";
import { CaseDetail } from "../cases/CaseDetail";
import { ControlsView } from "../controls/ControlsView";
import { Icon, IconName } from "./icons";
import { hrefFor, useRoute, View } from "./route";
import { useSession } from "./session";
import "./Console.css";

interface ViewInfo {
  view: View;
  label: string;
  icon: IconName;
  title: string;
  subtitle: string;
  /** The Cognito group the server requires for this view's routes. */
  needs: "reviewers" | "presenters" | null;
}

const VIEWS: ViewInfo[] = [
  {
    view: "live",
    label: "Live demo",
    icon: "live",
    title: "Live demo",
    subtitle: "The customer's phone, and everything AWS does with the application, side by side.",
    needs: null,
  },
  {
    view: "review",
    label: "Review queue",
    icon: "review",
    title: "Review queue",
    subtitle:
      "Cases the rules sent to a person. Decide with a rationale; the decision is sealed in the audit record.",
    needs: "reviewers",
  },
  {
    view: "cases",
    label: "All cases",
    icon: "cases",
    title: "All cases",
    subtitle: "Every application, where it stands, and its time to decision from the case record.",
    needs: "presenters",
  },
  {
    view: "controls",
    label: "Demo controls",
    icon: "controls",
    title: "Demo controls",
    subtitle: "The Act 3 sanctions list update, faults, the stopwatch and reset.",
    needs: "presenters",
  },
];

/**
 * The console shell: one bar across the top (the product, the four views, the Region and who is
 * signed in) and the current view under it, full width, so the live demo has the whole screen.
 *
 * The live demo stays mounted when another view is open, hidden rather than removed, because it
 * holds the applications in progress on the device: a presenter who opens the review queue in
 * Act 2 comes back to the same screen, mid-application, with nothing lost.
 */
export function Console() {
  const session = useSession();
  const route = useRoute();
  const queueDepth = useQueueDepth(session.isReviewer);
  const [largeText, setLargeText] = useLargeText();
  const current = VIEWS.find((v) => v.view === route.view) ?? VIEWS[0];

  const allowed = (info: ViewInfo) =>
    info.needs === null ||
    (info.needs === "reviewers" ? session.isReviewer : session.isPresenter);

  const roles = [
    session.isPresenter ? "Presenter" : null,
    session.isReviewer ? "Reviewer" : null,
  ].filter(Boolean);

  return (
    <div className={`console${largeText ? " console--large" : ""}`}>
      <header className="topbar">
        <a href={hrefFor("live")} className="brand" aria-label="KYC in 90 seconds: live demo">
          <span className="brand__mark" aria-hidden="true">
            <Icon name="logo" size={19} />
          </span>
          <span className="brand__text">
            <span className="brand__title">
              KYC in <span className="brand__ninety">90</span> seconds
            </span>
            <span className="brand__sub">Amazon Bedrock AgentCore · live demo</span>
          </span>
        </a>

        <nav className="topnav" aria-label="Console views">
          {VIEWS.map((info) => {
            const active = info.view === route.view;
            const enabled = allowed(info);
            return (
              <a
                key={info.view}
                href={hrefFor(info.view)}
                className={
                  "topnav__item" +
                  (active ? " topnav__item--active" : "") +
                  (enabled ? "" : " topnav__item--locked")
                }
                aria-current={active ? "page" : undefined}
                title={enabled ? undefined : `Needs the ${info.needs} group`}
              >
                <Icon name={info.icon} size={17} />
                <span className="topnav__label">{info.label}</span>
                {info.view === "review" && queueDepth ? (
                  <span className="topnav__count" aria-label={`${queueDepth} waiting`}>
                    {queueDepth}
                  </span>
                ) : null}
              </a>
            );
          })}
        </nav>

        <div className="topbar__end">
          <button
            type="button"
            className={`text-toggle${largeText ? " text-toggle--on" : ""}`}
            aria-pressed={largeText}
            onClick={() => setLargeText(!largeText)}
            title="Larger text for the room: the process view and the review console grow, the phone keeps its size"
          >
            <Icon name="text" size={16} />
            <span className="text-toggle__label">Large text</span>
          </button>
          <span className="env-chip" title="The Region of the API this console calls">
            <span className="env-chip__dot" aria-hidden="true" />
            Live AWS · <strong>{apiRegion() ?? "region unknown"}</strong>
          </span>
          <span className="user-chip" title={session.email}>
            <span className="user-chip__avatar" aria-hidden="true">
              {(session.email || "?").charAt(0).toUpperCase()}
            </span>
            <span className="user-chip__text">
              <span className="user-chip__email">{session.email}</span>
              <span className="user-chip__roles">{roles.length ? roles.join(" · ") : "Applicant"}</span>
            </span>
          </span>
          <button
            type="button"
            className="icon-button"
            onClick={session.signOut}
            aria-label="Sign out"
            title="Sign out"
          >
            <Icon name="signOut" size={17} />
          </button>
        </div>
      </header>

      <main className={`console-content console-content--${route.view}`}>
        {route.view !== "live" ? (
          <header className="page-head">
            <span className="page-head__eyebrow">
              <Icon name={current.icon} size={14} /> {current.label}
            </span>
            <h1>{current.title}</h1>
            <p>{current.subtitle}</p>
          </header>
        ) : null}

        {/* Always mounted: it holds the applications in progress on the device. */}
        <div hidden={route.view !== "live"}>
          <LiveDemo active={route.view === "live"} canSeeProcess={session.isReviewer} />
        </div>
        {route.view === "review" ? (
          <Gated info={current} allowed={allowed(current)}>
            <ReviewConsole key={route.id ?? "queue"} openCaseId={route.id} />
          </Gated>
        ) : null}
        {route.view === "cases" ? (
          <Gated info={current} allowed={allowed(current)}>
            {route.id ? (
              <CaseDetail key={route.id} caseId={route.id} canReview={session.isReviewer} />
            ) : (
              <CasesView />
            )}
          </Gated>
        ) : null}
        {route.view === "controls" ? (
          <Gated info={current} allowed={allowed(current)}>
            <ControlsView />
          </Gated>
        ) : null}
      </main>
    </div>
  );
}

function Gated({ info, allowed, children }: { info: ViewInfo; allowed: boolean; children: ReactNode }) {
  if (allowed) return <>{children}</>;
  return (
    <div className="console-empty">
      <span className="console-empty__icon">
        <Icon name="lock" size={26} />
      </span>
      <h2>{info.title} needs the {info.needs} group</h2>
      <p>
        Add this account to the Cognito group <code>{info.needs}</code> with{" "}
        <code>scripts/prepare_demo.py users … --apply</code>, then sign out and in again so the new
        group is in your token.
      </p>
    </div>
  );
}

const LARGE_TEXT_KEY = "kyc90.console.largeText";

/**
 * Larger text for a room (D-40): the process side and the review console grow; the phone keeps
 * its size, so it still fits beside them. Browser zoom is not the same thing: it narrows the
 * window, which hides the AgentCore line in the bar and squeezes the timeline. Kept across
 * reloads, like the Story or Backstage choice.
 */
function useLargeText(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(() => {
    try {
      return window.localStorage.getItem(LARGE_TEXT_KEY) === "1";
    } catch {
      return false;
    }
  });
  const choose = useCallback((next: boolean) => {
    setOn(next);
    try {
      window.localStorage.setItem(LARGE_TEXT_KEY, next ? "1" : "0");
    } catch {
      // Storage disabled: the choice lasts until the page reloads.
    }
  }, []);
  return [on, choose];
}

/** How many cases wait for a reviewer, for the badge in the bar. Reviewers only. */
function useQueueDepth(enabled: boolean): number {
  const [depth, setDepth] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    async function load() {
      try {
        const queue = await api.getReviewQueue();
        if (!cancelled) setDepth(queue.depth);
      } catch {
        // The badge is a convenience; the queue view reports its own errors.
      }
    }
    load();
    const id = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled]);
  return depth;
}

/** The Region, read from the API's own URL (`<id>.execute-api.<region>.amazonaws.com`). */
function apiRegion(): string | null {
  const url = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
  const match = url.match(/execute-api\.([a-z0-9-]+)\.amazonaws\.com/);
  return match ? match[1] : null;
}
