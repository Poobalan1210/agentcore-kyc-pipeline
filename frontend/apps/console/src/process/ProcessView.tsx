import { useCallback, useState } from "react";
import { Icon } from "../console/icons";
import { navigate } from "../console/route";
import { BackstageView } from "./BackstageView";
import { STATUS_LABEL, StatusMark } from "./marks";
import { ProcessView as ProcessModel, Stage, StageGroup, TARGET_SECONDS } from "./pipeline";
import "./ProcessView.css";

type Mode = "story" | "backstage";

const MODE_KEY = "kyc90.console.processMode";

/** The presenter's choice of view, kept across tabs, cases and reloads. Backstage by default. */
function useMode(): [Mode, (mode: Mode) => void] {
  const [mode, setMode] = useState<Mode>(() => {
    try {
      return window.localStorage.getItem(MODE_KEY) === "story" ? "story" : "backstage";
    } catch {
      return "backstage";
    }
  });
  const choose = useCallback((next: Mode) => {
    setMode(next);
    try {
      window.localStorage.setItem(MODE_KEY, next);
    } catch {
      // Storage disabled: the choice lasts until the page reloads.
    }
  }, []);
  return [mode, choose];
}

/**
 * The whole process for one case, in two views of the same records.
 *
 * Story is the checklist: what the applicant did, then each phase's checks, decision and effect,
 * in plain words, with the time each one landed. Backstage is the same case as it ran: every state
 * Step Functions entered, the four agents side by side, each tool's fact where it landed, and a
 * feed of everything the services wrote down. Backstage reads the execution history, which only
 * reviewers can, so anyone else gets the checklist. Read-only either way; a step that needs a
 * person offers to open the review queue at that case.
 */
export function ProcessView({
  view,
  notice,
  showApplicant = true,
}: {
  view: ProcessModel;
  notice?: string | null;
  showApplicant?: boolean;
}) {
  const [chosen, setMode] = useMode();
  const mode: Mode = view.backstage.available ? chosen : "story";
  const groups = view.groups.filter((g) => showApplicant || g.id !== "applicant");
  const applicant = showApplicant ? view.groups.find((g) => g.id === "applicant") ?? null : null;
  return (
    <div className="process">
      <ProcessHeader view={view} mode={mode} onMode={setMode} />
      {notice ? (
        <p className="process__notice" role="status">
          {notice}
        </p>
      ) : null}
      {mode === "backstage" ? (
        <BackstageView backstage={view.backstage} applicant={applicant} now={view.backstage.now} />
      ) : (
        <>
          <div className="process__groups">
            {groups.map((group) => (
              <GroupCard key={group.id} group={group} />
            ))}
          </div>
          {view.events.length > 0 ? <EventLog view={view} /> : null}
        </>
      )}
      <p className="process__foot">
        Every tool call goes through Amazon Bedrock AgentCore Gateway, where Cedar policy evaluates
        it. The agents gather evidence; only the recorded facts reach the rules.
      </p>
    </div>
  );
}

function ModeSwitch({
  mode,
  available,
  onMode,
}: {
  mode: Mode;
  available: boolean;
  onMode: (mode: Mode) => void;
}) {
  return (
    <div className="process-mode" role="group" aria-label="How to show the process">
      <button
        type="button"
        className={"process-mode__option" + (mode === "story" ? " process-mode__option--on" : "")}
        aria-pressed={mode === "story"}
        onClick={() => onMode("story")}
      >
        <Icon name="cases" size={14} /> Story
      </button>
      <button
        type="button"
        className={"process-mode__option" + (mode === "backstage" ? " process-mode__option--on" : "")}
        aria-pressed={mode === "backstage"}
        disabled={!available}
        title={
          available
            ? "Every step as it runs: the execution history, the agents side by side, the live feed"
            : "Reviewers only: the timeline reads the Step Functions execution history"
        }
        onClick={() => onMode("backstage")}
      >
        <Icon name="live" size={14} /> Backstage
      </button>
    </div>
  );
}

function ProcessHeader({
  view,
  mode,
  onMode,
}: {
  view: ProcessModel;
  mode: Mode;
  onMode: (mode: Mode) => void;
}) {
  const { headline } = view;
  const seconds = headline.timer.seconds;
  const over = seconds !== null && seconds > TARGET_SECONDS;
  const fill = seconds === null ? 0 : Math.min(100, (seconds / TARGET_SECONDS) * 100);
  return (
    <header className="process-head">
      <div className="process-head__who">
        <div className="process-head__name">
          <h2>{headline.name}</h2>
          <span className={`process-pill process-pill--${headline.stateTone}`}>
            {headline.stateLabel}
          </span>
        </div>
        <div className="process-head__ids">
          {headline.jurisdiction ? (
            <span className="process-head__chip">{headline.jurisdiction.toUpperCase()}</span>
          ) : null}
          {headline.caseId ? <code>{headline.caseId}</code> : <span>Not submitted yet</span>}
        </div>
      </div>
      <ModeSwitch mode={mode} available={view.backstage.available} onMode={onMode} />
      <div
        className={
          "process-timer" +
          (headline.timer.mode === "running"
            ? " process-timer--running"
            : over || headline.timer.tone === "warning" || headline.timer.tone === "danger"
              ? " process-timer--over"
              : seconds !== null
                ? " process-timer--ok"
                : "")
        }
        aria-live="off"
      >
        {/* What the number measures, from the model: "Account opened in", "Referred to a person
            in". A referral is not the 90-second figure, and the label says so. */}
        <span className="process-timer__label">{headline.timer.label}</span>
        <span className="process-timer__value kyc-num">
          {seconds === null ? "—" : `${seconds.toFixed(1)} s`}
        </span>
        <span className="process-timer__bar" aria-hidden="true">
          <span style={{ width: `${fill}%` }} />
        </span>
        <span className="process-timer__target">target {TARGET_SECONDS} s</span>
        {headline.timer.note ? (
          <span className="process-timer__note">{headline.timer.note}</span>
        ) : null}
      </div>
    </header>
  );
}

function GroupCard({ group }: { group: StageGroup }) {
  return (
    <section className={`process-group process-group--${group.status}`}>
      <header className="process-group__head">
        <StatusMark status={group.status} icon={group.steady ? "eye" : undefined} steady={group.steady} />
        <div>
          <h3>{group.title}</h3>
          <p>{group.subtitle}</p>
        </div>
        <span className={`process-group__state process-group__state--${group.status}`}>
          {group.steady ? "Watching" : group.label ?? STATUS_LABEL[group.status]}
        </span>
      </header>
      <ol className="process-stages">
        {group.stages.map((stage) => (
          <StageRow key={stage.id} stage={stage} />
        ))}
      </ol>
    </section>
  );
}

function StageRow({ stage }: { stage: Stage }) {
  const [open, setOpen] = useState(false);
  const hasDetails = Boolean(stage.details && stage.details.length);
  return (
    <li className={`process-stage process-stage--${stage.status}${stage.steady ? " process-stage--steady" : ""}`}>
      <span className="process-stage__marker">
        <StatusMark status={stage.status} icon={stage.icon} steady={stage.steady} />
      </span>
      <div className="process-stage__body">
        <div className="process-stage__top">
          <span className="process-stage__title">{stage.title}</span>
          <span className="process-stage__times">
            {stage.at !== undefined && stage.at !== null ? (
              <span className="process-chip kyc-num" title="Seconds after submission">
                +{stage.at.toFixed(1)} s
              </span>
            ) : null}
            {stage.took ? (
              <span className="process-chip process-chip--agent kyc-num" title="Agent run time">
                <Icon name="agent" size={12} /> {stage.took.toFixed(1)} s
              </span>
            ) : null}
          </span>
        </div>
        <p className="process-stage__by">{stage.by}</p>
        <p className="process-stage__summary">{stage.summary}</p>
        {stage.reviewCaseId ? (
          <button
            type="button"
            className="process-stage__action"
            onClick={() => navigate("review", stage.reviewCaseId)}
          >
            <Icon name="review" size={15} /> Review now
          </button>
        ) : null}
        {hasDetails ? (
          <>
            <button
              type="button"
              className="process-stage__toggle"
              aria-expanded={open}
              onClick={() => setOpen((v) => !v)}
            >
              {open ? "Hide detail" : "Show detail"}
            </button>
            {open ? (
              <dl className="process-stage__details">
                {stage.details?.map(([label, value], i) => (
                  <div key={`${label}-${i}`}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </>
        ) : null}
      </div>
    </li>
  );
}

function EventLog({ view }: { view: ProcessModel }) {
  const [open, setOpen] = useState(true);
  return (
    <section className="process-log">
      <button
        type="button"
        className="process-log__head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span>
          <Icon name="clock" size={15} /> State changes on the case record
        </span>
        <span className="process-log__count">{view.events.length}</span>
      </button>
      {open ? (
        <ol className="process-log__list">
          {view.events.map((event, i) => (
            <li key={`${event.at}-${i}`}>
              <time className="kyc-num" dateTime={event.at}>
                {clock(event.at)}
              </time>
              <span className={`process-log__dot process-log__dot--${event.tone}`} aria-hidden="true" />
              <span className="process-log__label">{event.label}</span>
              <span className="process-log__actor">{event.actor}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

function clock(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const d = new Date(t);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
