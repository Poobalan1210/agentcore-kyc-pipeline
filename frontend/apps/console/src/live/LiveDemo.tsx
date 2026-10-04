import { KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FlowPrefill, FlowProgress, KycFlow } from "../applicant/KycFlow";
import { DOCUMENT_TYPE_COPY } from "../applicant/copy";
import {
  DemoPersona,
  documentTypeOf,
  identityOf,
  personaById,
} from "../applicant/demoPersonas";
import { Icon } from "../console/icons";
import { buildProcess } from "../process/pipeline";
import { ProcessView } from "../process/ProcessView";
import { useCaseProcess } from "../process/useCaseProcess";
import { ApplicationTab, displayName, loadTabs, newTab, saveTabs } from "./applications";
import { DeviceFrame } from "./DeviceFrame";
import "./LiveDemo.css";

/**
 * The presenter's New application menu, in the order the talk uses the personas. Picking one
 * opens the customer's app with that persona's details filled in; the app itself never shows a
 * persona, as a bank's would not.
 */
const PERSONA_GROUPS: Array<{ heading: string; ids: string[] }> = [
  { heading: "Act 1, then Act 3", ids: ["DEMO-US-03"] },
  { heading: "Act 2", ids: ["DEMO-US-02", "DEMO-US-04"] },
  { heading: "Spare clean path", ids: ["DEMO-US-01"] },
  { heading: "Outside the US", ids: ["DEMO-UK-01", "DEMO-AU-01", "DEMO-JP-01"] },
];

/**
 * The live demo: the customer's phone on the left, everything AWS does with the application on
 * the right, side by side and updating together. Each application has its own tab, so the acts can
 * move between customers without losing any of them.
 */
export function LiveDemo({ active, canSeeProcess }: { active: boolean; canSeeProcess: boolean }) {
  const [state, setState] = useState(loadTabs);
  const [progress, setProgress] = useState<Record<string, FlowProgress>>({});
  const progressRef = useRef(progress);
  progressRef.current = progress;

  useEffect(() => {
    saveTabs(state.tabs, state.activeId);
  }, [state]);

  const openNew = useCallback((personaId?: string) => {
    setState((s) => {
      const tab = newTab(personaId);
      // A blank application no one has started (the one the page opens with) gives way to the
      // new one rather than sitting beside it as an empty tab.
      const untouched = (t: ApplicationTab) =>
        !t.resume &&
        !t.personaId &&
        !t.label &&
        (progressRef.current[t.tabId]?.step ?? "intake") === "intake";
      return { tabs: [...s.tabs.filter((t) => !untouched(t)), tab], activeId: tab.tabId };
    });
  }, []);

  const select = useCallback((tabId: string) => {
    setState((s) => (s.activeId === tabId ? s : { ...s, activeId: tabId }));
  }, []);

  const close = useCallback((tabId: string) => {
    setState((s) => {
      const tabs = s.tabs.filter((t) => t.tabId !== tabId);
      if (tabs.length === 0) {
        const tab = newTab();
        return { tabs: [tab], activeId: tab.tabId };
      }
      const activeId = s.activeId === tabId ? tabs[tabs.length - 1].tabId : s.activeId;
      return { tabs, activeId };
    });
  }, []);

  const report = useCallback((tabId: string, p: FlowProgress) => {
    setProgress((prev) => ({ ...prev, [tabId]: p }));
    setState((s) => {
      let changed = false;
      const tabs = s.tabs.map((t) => {
        if (t.tabId !== tabId) return t;
        const label = p.declaredIdentity
          ? displayName(p.declaredIdentity.givenNames, p.declaredIdentity.surname)
          : t.label;
        const jurisdiction = p.selection?.jurisdiction ?? t.jurisdiction;
        const resume =
          p.applicantId && p.selection
            ? t.resume && t.resume.applicantId === p.applicantId
              ? t.resume
              : {
                  applicationId: p.applicationId,
                  applicantId: p.applicantId,
                  selection: p.selection,
                  declaredIdentity: p.declaredIdentity,
                }
            : t.resume;
        if (label === t.label && jurisdiction === t.jurisdiction && resume === t.resume) return t;
        changed = true;
        return { ...t, label, jurisdiction, resume };
      });
      return changed ? { ...s, tabs } : s;
    });
  }, []);

  const activeTab = state.tabs.find((t) => t.tabId === state.activeId) ?? state.tabs[0];
  const activeProgress = progress[activeTab.tabId];
  const applicantId = activeProgress?.applicantId ?? activeTab.resume?.applicantId ?? null;
  const data = useCaseProcess(applicantId, { withTrace: canSeeProcess, visible: active });
  const deviceStep = activeProgress?.step ?? (activeTab.resume ? "status" : "intake");

  const view = useMemo(
    () =>
      buildProcess({
        deviceStep,
        declaredName: activeTab.label,
        jurisdiction: activeProgress?.selection?.jurisdiction ?? activeTab.jurisdiction,
        status: data.status,
        trace: data.trace,
        now: data.now,
        traceAvailable: canSeeProcess && !data.traceDenied,
      }),
    [deviceStep, activeTab.label, activeTab.jurisdiction, activeProgress, data, canSeeProcess],
  );

  const notice = !canSeeProcess || data.traceDenied
    ? "This account is not in the reviewers group, so only the applicant's status is shown. " +
      "Sign in as a reviewer to see each check as it runs."
    : data.error;

  return (
    <div className="live">
      <div className="live-tabs">
        <div className="live-tabs__list" role="tablist" aria-label="Applications on the device">
          {state.tabs.map((tab) => (
            <TabButton
              key={tab.tabId}
              tab={tab}
              selected={tab.tabId === activeTab.tabId}
              step={progress[tab.tabId]?.step ?? (tab.resume ? "status" : "intake")}
              onSelect={() => select(tab.tabId)}
              onClose={state.tabs.length > 1 || tab.resume ? () => close(tab.tabId) : undefined}
            />
          ))}
        </div>
        <NewApplicationMenu onOpen={openNew} />
      </div>

      <div className="live-stage">
        <div className="live-stage__device">
          <DeviceFrame>
            {state.tabs.map((tab) => (
              <div
                key={tab.tabId}
                id={`live-panel-${tab.tabId}`}
                className="device__app"
                hidden={tab.tabId !== activeTab.tabId}
                role="tabpanel"
                aria-labelledby={`live-tab-${tab.tabId}`}
              >
                <KycFlow
                  resume={tab.resume}
                  prefill={prefillFor(tab.personaId)}
                  // The camera steps take the whole window, so only the application on screen
                  // may run them: another tab, or another view, stops its camera.
                  active={active && tab.tabId === activeTab.tabId}
                  onRestart={() => openNew(tab.personaId)}
                  onProgress={(p) => report(tab.tabId, p)}
                />
              </div>
            ))}
          </DeviceFrame>
          <PresenterNote persona={personaById(activeTab.personaId)} />
        </div>
        <div className="live-stage__process">
          <ProcessView view={view} notice={notice} />
        </div>
      </div>
    </div>
  );
}

/** What the persona fills in on the customer's app: read once, when its application opens. */
function prefillFor(personaId: string | undefined): FlowPrefill | undefined {
  const persona = personaById(personaId);
  if (!persona) return undefined;
  return {
    selection: { jurisdiction: persona.jurisdiction, productApplied: "current_account" },
    identity: identityOf(persona),
    documentType: documentTypeOf(persona),
  };
}

function initials(persona: DemoPersona): string {
  return `${persona.givenNames.charAt(0)}${persona.surname.charAt(0)}`.toUpperCase();
}

/**
 * New application, with the persona to open it for. A menu button: arrow keys move between the
 * choices, Escape closes it, and a click anywhere else does too.
 */
function NewApplicationMenu({ onOpen }: { onOpen: (personaId?: string) => void }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  function choose(personaId?: string) {
    setOpen(false);
    buttonRef.current?.focus();
    onOpen(personaId);
  }

  function onMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [],
    );
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    const focus = (i: number) => items[(i + items.length) % items.length]?.focus();
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        focus(index + 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        focus(index - 1);
        break;
      case "Home":
        event.preventDefault();
        focus(0);
        break;
      case "End":
        event.preventDefault();
        focus(items.length - 1);
        break;
      case "Escape":
        event.preventDefault();
        setOpen(false);
        buttonRef.current?.focus();
        break;
      case "Tab":
        setOpen(false);
        break;
      default:
        break;
    }
  }

  return (
    <div className="live-new" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="live-tabs__new"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="plus" size={15} /> New application
        <Icon name="chevron" size={13} className="live-new__caret" />
      </button>
      {open ? (
        <div
          ref={menuRef}
          className="live-menu"
          role="menu"
          aria-label="Open an application for"
          onKeyDown={onMenuKeyDown}
        >
          {PERSONA_GROUPS.map((group) => {
            const personas = group.ids
              .map((id) => personaById(id))
              .filter((p): p is DemoPersona => Boolean(p));
            const headingId = `live-menu-${group.heading.replace(/\W+/g, "-").toLowerCase()}`;
            return (
              <div key={group.heading} className="live-menu__group" role="group" aria-labelledby={headingId}>
                <p className="live-menu__heading" id={headingId}>
                  {group.heading}
                </p>
                {personas.map((persona) => (
                  <button
                    key={persona.id}
                    type="button"
                    role="menuitem"
                    className="live-menu__item"
                    title={persona.scenario}
                    onClick={() => choose(persona.id)}
                  >
                    <span className="live-menu__avatar" aria-hidden="true">
                      {initials(persona)}
                    </span>
                    <span className="live-menu__text">
                      <span className="live-menu__name">
                        {displayName(persona.givenNames, persona.surname)}
                      </span>
                      <span className="live-menu__meta">
                        {persona.tag} · {persona.jurisdiction.toUpperCase()} ·{" "}
                        {DOCUMENT_TYPE_COPY[documentTypeOf(persona)]?.title ?? "ID"}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            );
          })}
          <div className="live-menu__sep" role="separator" />
          <button type="button" role="menuitem" className="live-menu__item" onClick={() => choose()}>
            <span className="live-menu__avatar live-menu__avatar--blank" aria-hidden="true">
              <Icon name="plus" size={15} />
            </span>
            <span className="live-menu__text">
              <span className="live-menu__name">Blank application</span>
              <span className="live-menu__meta">Type every detail in on the phone</span>
            </span>
          </button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * For the presenter, under the phone: which persona this is and the ID that goes with it. Off the
 * customer's screen, where a real app would have nothing of the kind.
 */
function PresenterNote({ persona }: { persona: DemoPersona | undefined }) {
  if (!persona) {
    return (
      <p className="live-note">
        <Icon name="info" size={14} className="live-note__icon" />
        <span>Blank application: type the details in, then photograph an ID that matches them.</span>
      </p>
    );
  }
  return (
    <p className="live-note">
      <Icon name="document" size={14} className="live-note__icon" />
      <span>
        <strong>{persona.id}</strong> · {persona.tag} · ID <code>{persona.documentFile}</code>: hold
        the printout up to the camera, or upload the file.
      </span>
    </p>
  );
}

function TabButton({
  tab,
  selected,
  step,
  onSelect,
  onClose,
}: {
  tab: ApplicationTab;
  selected: boolean;
  step: string;
  onSelect: () => void;
  onClose?: () => void;
}) {
  const submitted = Boolean(tab.resume);
  return (
    <div className={"live-tab" + (selected ? " live-tab--selected" : "")}>
      <button
        type="button"
        role="tab"
        id={`live-tab-${tab.tabId}`}
        aria-selected={selected}
        aria-controls={`live-panel-${tab.tabId}`}
        className="live-tab__main"
        onClick={onSelect}
      >
        <span className={"live-tab__dot" + (submitted ? " live-tab__dot--submitted" : "")} aria-hidden="true" />
        <span className="live-tab__text">
          <span className="live-tab__name">{tab.label ?? "New application"}</span>
          <span className="live-tab__meta">
            {(tab.jurisdiction ?? "").toUpperCase() || "—"} · {submitted ? "submitted" : stepLabel(step)}
          </span>
        </span>
      </button>
      {onClose ? (
        <button
          type="button"
          className="live-tab__close"
          aria-label={`Close ${tab.label ?? "this application"}`}
          title="Close this tab. The case stays in AWS."
          onClick={onClose}
        >
          <Icon name="close" size={14} />
        </button>
      ) : null}
    </div>
  );
}

/** Where an unsubmitted application is up to, in the onboarding's own words. */
function stepLabel(step: string): string {
  switch (step) {
    case "intake":
      return "welcome";
    case "details":
      return "about you";
    case "documents":
      return "ID document";
    case "liveness":
      return "selfie";
    case "review":
      return "review";
    case "submitting":
      return "submitting";
    case "error":
      return "needs attention";
    default:
      return "in progress";
  }
}
