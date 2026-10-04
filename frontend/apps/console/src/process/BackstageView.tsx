import { MouseEvent, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "../console/icons";
import { navigate } from "../console/route";
import { STATUS_LABEL, StatusMark } from "./marks";
import type { StageGroup } from "./pipeline";
import type { Backstage, FeedItem, Lane, LaneTone, Mark, PhaseTimeline, Segment } from "./timeline";
import "./BackstageView.css";

/**
 * The live process, as one screen: what the customer did on the phone, then each phase on its own
 * clock, lane by lane, then everything the system wrote down, as it happens. The lanes' results
 * read as the checklist did; a click on a lane opens what did the work and what it recorded.
 */
export function BackstageView({
  backstage,
  applicant,
  now,
}: {
  backstage: Backstage;
  /** The phone's steps, when the view is beside the phone. */
  applicant?: StageGroup | null;
  now: number;
}) {
  return (
    <div className="bs">
      {applicant ? <PhoneStrip group={applicant} /> : null}
      {backstage.phases.map((phase) => (
        <PhaseCard key={phase.id} phase={phase} />
      ))}
      <Feed items={backstage.feed} now={now} />
    </div>
  );
}

// ------------------------------------------------------------------------------- the phone

function PhoneStrip({ group }: { group: StageGroup }) {
  return (
    <section className={`tl-phone tl-phone--${group.status}`} aria-label="On the applicant's phone">
      <span className="tl-phone__title">
        <Icon name="device" size={15} /> On the phone
      </span>
      <ol className="tl-phone__steps">
        {group.stages.map((stage) => (
          <li key={stage.id} className={`tl-phone__step tl-phone__step--${stage.status}`}>
            <StatusMark status={stage.status} icon={stage.icon} />
            <span className="tl-phone__text">
              <b>{stage.title}</b>
              <small>{stage.short ?? stage.summary}</small>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

// ------------------------------------------------------------------------------- a phase

function PhaseCard({ phase }: { phase: PhaseTimeline }) {
  const [open, setOpen] = useState<string | null>(null);
  const label = phase.steady ? "Watching" : phase.label ?? STATUS_LABEL[phase.status];
  return (
    <section
      className={`tl-card tl-card--${phase.status}${phase.steady ? " tl-card--steady" : ""}`}
      aria-labelledby={`tl-title-${phase.id}`}
    >
      <header className="tl-card__head">
        <StatusMark status={phase.status} icon={phase.steady ? "eye" : undefined} steady={phase.steady} />
        <div className="tl-card__titles">
          <h3 id={`tl-title-${phase.id}`}>{phase.title}</h3>
          <p>{phase.subtitle}</p>
        </div>
        <div className="tl-card__meta">
          {phase.clock !== null ? (
            <span className="tl-card__clock kyc-num" title={phase.running ? "Running for" : "Ran for"}>
              {clockText(phase.clock)}
            </span>
          ) : null}
          <span className={`tl-card__state tl-card__state--${phase.steady ? "steady" : phase.status}`}>
            {label}
          </span>
          {phase.consoleUrl ? (
            <a
              className="tl-card__console"
              href={phase.consoleUrl}
              target="_blank"
              rel="noreferrer"
              title={`Open ${phase.executionName ?? "this execution"} in the AWS Step Functions console`}
            >
              Step Functions console <Icon name="arrow" size={12} className="tl-card__console-icon" />
            </a>
          ) : null}
        </div>
      </header>

      {phase.lanes.length ? (
        <div className={`tl${phase.note ? " tl--waiting" : ""}`}>
          <AxisRow phase={phase} />
          {phase.lanes.map((lane, i) => (
            <LaneRow
              key={lane.id}
              lane={lane}
              phase={phase}
              first={Boolean(lane.parallel && !phase.lanes[i - 1]?.parallel)}
              last={Boolean(lane.parallel && !phase.lanes[i + 1]?.parallel)}
              open={open === lane.id}
              onToggle={() => setOpen((current) => (current === lane.id ? null : lane.id))}
            />
          ))}
          {phase.note ? <p className="tl__note">{phase.note}</p> : null}
        </div>
      ) : (
        <p className={`tl-card__empty${phase.steady ? " tl-card__empty--steady" : ""}`}>
          {phase.steady ? <span className="tl-card__pulse" aria-hidden="true" /> : null}
          {phase.note}
        </p>
      )}
    </section>
  );
}

function AxisRow({ phase }: { phase: PhaseTimeline }) {
  const last = phase.ticks.length - 1;
  return (
    <div className="tl-row tl-row--axis" aria-hidden="true">
      <span className="tl-row__label tl-axis__caption">
        {phase.id === "phase1" ? "Seconds after Submit" : "Since it started"}
      </span>
      <span className="tl-row__track">
        {phase.ticks.map((t, i) => (
          <span
            key={t}
            className={`tl-tick${i === 0 ? " tl-tick--first" : ""}${i === last ? " tl-tick--last" : ""}`}
            style={{ left: pct(t, phase.span) }}
          >
            {tickText(t, phase.span, i === last)}
          </span>
        ))}
        {phase.target !== null ? (
          <Flag at={phase.target} span={phase.span} tone="target" text={`${phase.target} s target`} />
        ) : null}
        {phase.decided ? (
          <Flag at={phase.decided.at} span={phase.span} tone="decided" text={phase.decided.label} />
        ) : phase.now !== null ? (
          <Flag at={phase.now} span={phase.span} tone="now" text={`now · ${clockText(phase.now)}`} />
        ) : null}
      </span>
      <span className="tl-row__result tl-axis__caption">Result</span>
    </div>
  );
}

/** A label on the axis for a line drawn down the lanes; kept inside the track at either end. */
function Flag({ at, span, tone, text }: { at: number; span: number; tone: string; text: string }) {
  const where = at / span;
  const align = where > 0.72 ? " tl-axis-flag--end" : where < 0.08 ? " tl-axis-flag--start" : "";
  return (
    <span className={`tl-axis-flag tl-axis-flag--${tone}${align}`} style={{ left: pct(at, span) }}>
      {text}
    </span>
  );
}

function LaneRow({
  lane,
  phase,
  first,
  last,
  open,
  onToggle,
}: {
  lane: Lane;
  phase: PhaseTimeline;
  first: boolean;
  last: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  // The whole row opens the detail; the name is the keyboard's way in, and links and the review
  // button keep their own clicks.
  const onRowClick = (event: MouseEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest("button, a")) return;
    onToggle();
  };
  const classes = [
    "tl-row",
    "tl-lane",
    `tl-lane--${lane.tone}`,
    lane.parallel ? "tl-lane--parallel" : "",
    first ? "tl-lane--first" : "",
    last ? "tl-lane--last" : "",
    open ? "tl-lane--open" : "",
    lane.segments.length || lane.marks.length ? "" : "tl-lane--empty",
  ].filter(Boolean).join(" ");
  return (
    <>
      <div className={classes} onClick={onRowClick}>
        <span className="tl-row__label">
          <button
            type="button"
            className="tl-lane__name"
            aria-expanded={open}
            aria-controls={`tl-detail-${phase.id}-${lane.id}`}
            onClick={onToggle}
          >
            {lane.name}
          </button>
          <span className="tl-lane__sub">{lane.sub}</span>
        </span>
        <span className="tl-row__track">
          {phase.ticks.map((t) => (
            <span key={t} className="tl-grid" style={{ left: pct(t, phase.span) }} aria-hidden="true" />
          ))}
          {phase.target !== null ? (
            <span className="tl-line tl-line--target" style={{ left: pct(phase.target, phase.span) }} aria-hidden="true" />
          ) : null}
          {lane.segments.map((segment, i) => (
            <SegmentBar key={i} segment={segment} phase={phase} tone={lane.tone} />
          ))}
          {lane.marks.map((mark, i) => (
            <MarkDot key={i} mark={mark} span={phase.span} />
          ))}
          {phase.decided ? (
            <span className="tl-line tl-line--decided" style={{ left: pct(phase.decided.at, phase.span) }} aria-hidden="true" />
          ) : null}
          {phase.now !== null ? (
            <span className="tl-line tl-line--now" style={{ left: pct(phase.now, phase.span) }} aria-hidden="true" />
          ) : null}
        </span>
        <span className={`tl-row__result tl-result tl-result--${lane.status}`}>
          {lane.result ? (
            <span className="tl-result__text">{lane.result}</span>
          ) : (
            <span className="tl-result__none">—</span>
          )}
          {lane.reviewCaseId ? (
            <button
              type="button"
              className="tl-result__action"
              onClick={() => navigate("review", lane.reviewCaseId ?? undefined)}
            >
              Review now
            </button>
          ) : null}
        </span>
      </div>
      {open ? <LaneDetail id={`tl-detail-${phase.id}-${lane.id}`} lane={lane} /> : null}
    </>
  );
}

function SegmentBar({ segment, phase, tone }: { segment: Segment; phase: PhaseTimeline; tone: LaneTone }) {
  const end = segment.end ?? phase.now ?? segment.start;
  const width = (Math.max(0, end - segment.start) / phase.span) * 100;
  return (
    <span
      className={`tl-seg tl-seg--${tone} tl-seg--${segment.status}`}
      style={{ left: pct(segment.start, phase.span), width: `max(${Math.min(100, width)}%, 3px)` }}
      title={segment.title}
    >
      {segment.label && width >= 16 ? <span className="tl-seg__label">{segment.label}</span> : null}
    </span>
  );
}

function MarkDot({ mark, span }: { mark: Mark; span: number }) {
  return (
    <span className={`tl-mark tl-mark--${mark.kind}`} style={{ left: pct(mark.at, span) }} title={mark.title}>
      {mark.label ? <span className="tl-mark__label">{mark.label}</span> : null}
    </span>
  );
}

function LaneDetail({ id, lane }: { id: string; lane: Lane }) {
  const stage = lane.stage;
  const rows: Array<[string, string]> = [...(lane.facts ?? []), ...(stage?.details ?? [])];
  const states = lane.segments.slice(0, 30);
  return (
    <div className="tl-detail" id={id}>
      <p className="tl-detail__by">{stage?.by ?? lane.sub}</p>
      {stage ? <p className="tl-detail__summary">{stage.summary}</p> : null}
      {rows.length ? (
        <dl className="tl-detail__rows">
          {rows.map(([label, value], i) => (
            <div key={`${label}-${i}`}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {states.length ? (
        <ul className="tl-detail__states" aria-label="States, as Step Functions recorded them">
          {states.map((segment, i) => (
            <li key={i} className={`tl-detail__state tl-detail__state--${segment.status}`}>
              {segment.title}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------- the feed

function Feed({ items, now }: { items: FeedItem[]; now: number }) {
  const listRef = useRef<HTMLOListElement>(null);
  // Follows the newest line, unless the presenter has scrolled up to point at an older one.
  const follow = useRef(true);
  const lastAt = items.length ? items[items.length - 1].t : 0;
  useLayoutEffect(() => {
    const list = listRef.current;
    if (list && follow.current) list.scrollTop = list.scrollHeight;
  }, [items.length, lastAt]);

  return (
    <section className="tl-feed" aria-labelledby="tl-feed-title">
      <header className="tl-feed__head">
        <div>
          <h3 id="tl-feed-title">
            <Icon name="live" size={15} /> Live feed
          </h3>
          <p>Every line is something the system wrote down, in the order it happened.</p>
        </div>
        <span className="tl-feed__count kyc-num">{items.length}</span>
      </header>
      {items.length ? (
        <ol
          ref={listRef}
          className="tl-feed__list"
          onScroll={(event) => {
            const list = event.currentTarget;
            follow.current = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
          }}
        >
          {items.map((item, i) => (
            <li
              key={`${item.t}-${i}`}
              className={`tl-feed__item tl-feed__item--${item.tone}${now - item.t < 6000 ? " tl-feed__item--new" : ""}`}
            >
              <time className="kyc-num" dateTime={item.at}>
                {clock(item.at)}
              </time>
              <span className={`tl-src tl-src--${item.source}`}>{item.service}</span>
              <span className="tl-feed__text">{item.text}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="tl-feed__empty">Nothing yet. The first line appears when the applicant presses Submit.</p>
      )}
    </section>
  );
}

// ------------------------------------------------------------------------------- formatting

function pct(t: number, span: number): string {
  return `${Math.min(100, Math.max(0, (t / span) * 100))}%`;
}

function tickText(t: number, span: number, last: boolean): string {
  if (span <= 120) return last ? `${t} s` : `${t}`;
  const m = Math.floor(t / 60);
  const s = Math.round(t % 60);
  return `${m}:${String(s).padStart(2, "0")}${last ? " min" : ""}`;
}

function clockText(value: number): string {
  if (value < 90) return `${value.toFixed(1)} s`;
  const m = Math.floor(value / 60);
  const s = Math.floor(value % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function clock(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const d = new Date(t);
  const hms = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return `${hms}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}
