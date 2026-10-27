import type { FlowResume } from "../applicant/KycFlow";
import { personaById } from "../applicant/demoPersonas";

/**
 * The applications open on the device, one tab each. A presenter runs several in one session
 * (Nadia in Act 1, Avery and Dmitri in Act 2, back to Nadia's frozen account in Act 3), so each
 * keeps its own screen, and a reload must not lose them mid-talk.
 *
 * Only submitted applications are saved, in this browser's local storage, with what reopening one
 * needs: its ids, the country and product, and the declared identity a step-up resubmits. In this
 * demo every identity is a synthetic persona; nothing here is a credential.
 */
export interface ApplicationTab {
  tabId: string;
  /** The demo persona the presenter opened it for; absent for a blank application. */
  personaId?: string;
  /** The persona's name, then the declared one once the details step is done. */
  label: string | null;
  jurisdiction: string | null;
  openedAt: string;
  /** Set once submitted: what a reload reopens. */
  resume?: FlowResume;
}

const STORAGE_KEY = "kyc90.console.applications.v1";
const MAX_SAVED = 12;

export function newTab(personaId?: string): ApplicationTab {
  const persona = personaById(personaId);
  return {
    tabId: crypto.randomUUID(),
    personaId: persona?.id,
    label: persona ? displayName(persona.givenNames, persona.surname) : null,
    jurisdiction: persona?.jurisdiction ?? null,
    openedAt: new Date().toISOString(),
  };
}

function isResume(value: unknown): value is FlowResume {
  const r = value as FlowResume | null;
  return Boolean(
    r &&
      typeof r.applicationId === "string" &&
      typeof r.applicantId === "string" &&
      r.selection &&
      typeof r.selection.jurisdiction === "string" &&
      typeof r.selection.productApplied === "string",
  );
}

export function loadTabs(): { tabs: ApplicationTab[]; activeId: string } {
  let saved: ApplicationTab[] = [];
  let activeId: string | null = null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { tabs?: unknown[]; activeId?: unknown };
      saved = (parsed.tabs ?? [])
        .filter((t): t is ApplicationTab => {
          const tab = t as ApplicationTab;
          return Boolean(tab && typeof tab.tabId === "string" && isResume(tab.resume));
        })
        .map((t) => ({
          tabId: t.tabId,
          personaId: personaById(t.personaId)?.id,
          label: typeof t.label === "string" ? t.label : null,
          jurisdiction: typeof t.jurisdiction === "string" ? t.jurisdiction : null,
          openedAt: typeof t.openedAt === "string" ? t.openedAt : new Date().toISOString(),
          resume: t.resume,
        }));
      activeId = typeof parsed.activeId === "string" ? parsed.activeId : null;
    }
  } catch {
    saved = [];
  }
  if (saved.length === 0) {
    const fresh = newTab();
    return { tabs: [fresh], activeId: fresh.tabId };
  }
  const active = saved.find((t) => t.tabId === activeId) ?? saved[saved.length - 1];
  return { tabs: saved, activeId: active.tabId };
}

export function saveTabs(tabs: ApplicationTab[], activeId: string): void {
  // The SSN or ITIN never reaches local storage. A step-up resubmitted from a reloaded tab goes
  // without it, and the API keeps the one already on file.
  const submitted = tabs
    .filter((t) => t.resume)
    .slice(-MAX_SAVED)
    .map((t) => {
      const declared = t.resume?.declaredIdentity;
      if (!t.resume || !declared?.taxId) return t;
      return { ...t, resume: { ...t.resume, declaredIdentity: { ...declared, taxId: undefined } } };
    });
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ tabs: submitted, activeId }),
    );
  } catch {
    // Storage full or disabled: the session still works, it just will not survive a reload.
  }
}

export function displayName(givenNames?: string | null, surname?: string | null): string | null {
  const full = `${givenNames ?? ""} ${surname ?? ""}`.trim();
  if (!full) return null;
  return full
    .toLowerCase()
    .replace(/(^|[\s-])([a-z])/g, (_, sep: string, ch: string) => sep + ch.toUpperCase());
}
