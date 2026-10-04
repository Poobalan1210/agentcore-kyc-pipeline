import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@shared/components/Button";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import {
  api,
  ApiError,
  DemoControls,
  DemoFaults,
  ListEntry,
  ListUpdateResponse,
} from "@shared/api/client";
import { Icon } from "../console/icons";
import { hrefFor } from "../console/route";
import { TARGET_SECONDS } from "../process/pipeline";
import "./Controls.css";

/** `api/demo.py` `TOOLS` — the six evidence tools a fault may mark unavailable. */
const TOOLS: Array<{ name: string; label: string }> = [
  { name: "s3_get_document", label: "Document store" },
  { name: "rekognition_compare_faces", label: "Face compare" },
  { name: "velocity_check", label: "Velocity" },
  { name: "sanctions_screen", label: "Sanctions screening" },
  { name: "pep_lookup", label: "PEP list" },
  { name: "adverse_media", label: "Adverse media" },
];

/** `api/demo.py` `AGENTS` — the Phase 1 evidence agents whose time a fault may shorten. */
const AGENTS = ["documents", "identity", "fraud", "prescreen"];
const MAX_AGENT_TIMEOUT = 90;

/**
 * Act 3: the list update published on stage, arriving the way a real one does, as a batch of new
 * listings. The first is DEMO-US-03's own identity, so the re-screen of her open account finds a
 * CONFIRMED match; the other three share no name or date of birth with any persona, so they
 * change no one's screening. `demo-assets/gen_documents.py` `ACT3_LIST_UPDATE` and
 * `ACT3_OTHER_LISTINGS` hold the same values, and a test keeps the two files equal.
 */
const ACT3_ENTRY: ListEntry = {
  entryId: "SYN-0901",
  name: "Nadia Rose Korval",
  dob: "1987-02-11",
  country: "US",
};

const ACT3_OTHER_ENTRIES: ListEntry[] = [
  { entryId: "SYN-0902", name: "Orrin Castellane", dob: "1975-09-14", country: "MT" },
  { entryId: "SYN-0903", name: "Lucian Varga", dob: "1979-06-02", country: "RO" },
  { entryId: "SYN-0904", name: "Ignatius Moreau", dob: "1968-12-01", country: "FR" },
];

/**
 * The presenter's controls. Every one writes the same runtime parameters production code reads,
 * so the demo exercises the real paths rather than a demo-only branch.
 */
export function ControlsView() {
  const [controls, setControls] = useState<DemoControls | null>(null);
  const [controlsError, setControlsError] = useState<string | null>(null);

  const loadControls = useCallback(async () => {
    try {
      setControls(await api.getDemoControls());
      setControlsError(null);
    } catch (err) {
      setControlsError(
        err instanceof ApiError && err.status === 404
          ? "The demo routes are not deployed (stacks synthesised with -c demoControls=false)."
          : "Could not read the demo controls.",
      );
    }
  }, []);

  useEffect(() => {
    loadControls();
  }, [loadControls]);

  const faultsOn = Boolean(
    controls &&
      ((controls.faults.unavailableTools ?? []).length ||
        Object.keys(controls.faults.agentTimeouts ?? {}).length),
  );
  const listOn = Boolean(controls && controls.listOverlay.entries.length);

  return (
    <div className="controls">
      {controlsError ? <p className="kyc-error-text">{controlsError}</p> : null}
      <div className={"controls-status" + (faultsOn || listOn ? " controls-status--set" : "")}>
        <Icon name={faultsOn || listOn ? "alert" : "check"} size={18} />
        <span>
          {faultsOn || listOn
            ? `Set now: ${[faultsOn ? "faults" : null, listOn ? "a published list update" : null]
                .filter(Boolean)
                .join(" and ")}. Reset before the next run.`
            : "Controls clear: no faults, no published list update. Ready for a run."}
        </span>
      </div>
      <div className="controls-grid">
        <div className="controls-grid__main">
          <ListUpdateCard current={controls?.listOverlay ?? null} onChanged={loadControls} />
          <FaultsCard current={controls?.faults ?? null} onChanged={loadControls} />
        </div>
        <div className="controls-grid__side">
          <Stopwatch />
          <ResetCard onReset={loadControls} />
        </div>
      </div>
    </div>
  );
}

// -------------------------------------------------------------------------- the stopwatch

/** For timing from the moment the presenter presses Submit, beside the system's own figure. */
function Stopwatch() {
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [stoppedMs, setStoppedMs] = useState<number | null>(null);
  const [, setTick] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => {
    if (timer.current) clearInterval(timer.current);
  }, []);

  function start() {
    setStartedAt(Date.now());
    setStoppedMs(null);
    if (timer.current) clearInterval(timer.current);
    timer.current = setInterval(() => setTick((t) => t + 1), 100);
  }

  function stop() {
    if (startedAt === null) return;
    setStoppedMs(Date.now() - startedAt);
    if (timer.current) clearInterval(timer.current);
  }

  function reset() {
    if (timer.current) clearInterval(timer.current);
    setStartedAt(null);
    setStoppedMs(null);
  }

  const elapsed = stoppedMs ?? (startedAt !== null ? Date.now() - startedAt : 0);
  const seconds = elapsed / 1000;

  return (
    <Card>
      <CardHeader>
        <h2>Stopwatch</h2>
        <span className="controls-muted">target {TARGET_SECONDS} s</span>
      </CardHeader>
      <CardBody>
        <p
          className={
            "controls-stopwatch kyc-num " +
            (seconds > TARGET_SECONDS ? "controls-stopwatch--slow" : startedAt !== null ? "controls-stopwatch--fast" : "")
          }
          aria-live="off"
        >
          {seconds.toFixed(1)}
          <span>s</span>
        </p>
        <p className="kyc-hint">
          Optional, beside the system's own figure: start it when the applicant presses Submit.
        </p>
        <div className="controls-buttons">
          <Button variant="primary" onClick={start}>
            {startedAt === null ? "Start" : "Restart"}
          </Button>
          <Button onClick={stop} disabled={startedAt === null || stoppedMs !== null}>
            Stop
          </Button>
          <Button variant="ghost" onClick={reset}>
            Reset
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

// ------------------------------------------------------------------------------- reset

function ResetCard({ onReset }: { onReset: () => void }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function reset() {
    setBusy(true);
    setNote(null);
    try {
      const result = await api.resetDemo();
      setNote(`Faults and published list updates cleared. ${result.note}.`);
      onReset();
    } catch {
      setNote("Reset failed. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <h2>Reset</h2>
      </CardHeader>
      <CardBody>
        <p className="kyc-hint">
          Before every run. Clears every fault and every published list update. It does not
          unfreeze anything: only a reviewer does that.
        </p>
        <Button variant="secondary" onClick={reset} loading={busy}>
          Reset demo controls
        </Button>
        {note ? (
          <p className="kyc-hint" role="status">
            {note}
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}

// ------------------------------------------------------------------------------ faults

function FaultsCard({
  current,
  onChanged,
}: {
  current: DemoFaults | null;
  onChanged: () => void;
}) {
  const [tools, setTools] = useState<Set<string>>(new Set());
  const [timeouts, setTimeouts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!current) return;
    setTools(new Set(current.unavailableTools ?? []));
    setTimeouts(
      Object.fromEntries(
        Object.entries(current.agentTimeouts ?? {}).map(([agent, s]) => [agent, String(s)]),
      ),
    );
  }, [current]);

  function toggle(tool: string) {
    setTools((prev) => {
      const next = new Set(prev);
      if (next.has(tool)) next.delete(tool);
      else next.add(tool);
      return next;
    });
  }

  async function apply(faults: DemoFaults) {
    setBusy(true);
    setMessage(null);
    try {
      await api.putDemoFaults(faults);
      setMessage(
        "Applied. The tools pick it up within a few seconds; agent time limits apply from the " +
          "next case.",
      );
      onChanged();
    } catch (err) {
      const detail = err instanceof ApiError ? (err.body as { detail?: string })?.detail : null;
      setMessage(`Not applied${detail ? `: ${detail}` : "."}`);
    } finally {
      setBusy(false);
    }
  }

  function applyCurrent() {
    const agentTimeouts: Record<string, number> = {};
    for (const [agent, value] of Object.entries(timeouts)) {
      if (value.trim() !== "") agentTimeouts[agent] = Number(value);
    }
    apply({ unavailableTools: [...tools].sort(), agentTimeouts });
  }

  return (
    <Card>
      <CardHeader>
        <h2>Faults</h2>
        <span className="controls-muted">optional act</span>
      </CardHeader>
      <CardBody>
        <p className="kyc-hint">
          A fault can only send a case to a person. An unavailable tool records that the check did
          not happen; a shorter agent time ends a run with nothing to report. Neither can approve
          anyone.
        </p>
        <fieldset className="controls-fieldset">
          <legend>Tools reported unavailable</legend>
          <div className="controls-checks">
            {TOOLS.map((tool) => (
              <label key={tool.name} className={"controls-check" + (tools.has(tool.name) ? " controls-check--on" : "")}>
                <input
                  type="checkbox"
                  checked={tools.has(tool.name)}
                  onChange={() => toggle(tool.name)}
                />
                <span>
                  {tool.label}
                  <code>{tool.name}</code>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset className="controls-fieldset">
          <legend>Agent time limits (seconds, blank for the default)</legend>
          <div className="controls-timeouts">
            {AGENTS.map((agent) => (
              <div key={agent} className="kyc-form-row">
                <label htmlFor={`timeout-${agent}`}>{agent}</label>
                <input
                  id={`timeout-${agent}`}
                  type="number"
                  min={1}
                  max={MAX_AGENT_TIMEOUT}
                  value={timeouts[agent] ?? ""}
                  onChange={(e) => setTimeouts((prev) => ({ ...prev, [agent]: e.target.value }))}
                />
              </div>
            ))}
          </div>
        </fieldset>
        <div className="controls-buttons">
          <Button variant="primary" onClick={applyCurrent} loading={busy}>
            Apply faults
          </Button>
          <Button
            variant="secondary"
            onClick={() => apply({ unavailableTools: [], agentTimeouts: {} })}
            disabled={busy}
          >
            Clear faults
          </Button>
        </div>
        {message ? (
          <p className="kyc-hint" role="status">
            {message}
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}

// ------------------------------------------------------------------------- list update

function ListUpdateCard({
  current,
  onChanged,
}: {
  current: DemoControls["listOverlay"] | null;
  onChanged: () => void;
}) {
  const [entry, setEntry] = useState<ListEntry>(ACT3_ENTRY);
  const [version, setVersion] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ListUpdateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  function update<K extends keyof ListEntry>(field: K, value: ListEntry[K]) {
    setEntry((prev) => ({ ...prev, [field]: value }));
  }

  // The batch as it will be sent: the editable first listing, then the others. One the edit
  // renamed onto is dropped rather than sent twice, which the route would refuse as a duplicate.
  const batch: ListEntry[] = [
    entry,
    ...ACT3_OTHER_ENTRIES.filter((e) => e.entryId !== entry.entryId.trim()),
  ];

  async function publish() {
    setBusy(true);
    setError(null);
    setResult(null);
    setConfirming(false);
    try {
      const entries: ListEntry[] = batch.map((e) => ({
        entryId: e.entryId.trim(),
        name: e.name.trim(),
        ...(e.dob ? { dob: e.dob } : {}),
        ...(e.country ? { country: e.country.trim().toUpperCase() } : {}),
      }));
      const published = await api.publishListUpdate({
        ...(version.trim() ? { version: version.trim() } : {}),
        entries,
      });
      setResult(published);
      onChanged();
    } catch (err) {
      const detail = err instanceof ApiError ? (err.body as { detail?: string })?.detail : null;
      setError(`Not published${detail ? `: ${detail}` : "."}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="controls-act3">
      <CardHeader>
        <h2>Act 3 · Sanctions list update</h2>
        <span className="controls-act-tag">on stage</span>
      </CardHeader>
      <CardBody>
        <p className="kyc-hint">
          Publishes a synthetic list update of {batch.length} new listings, then sends one event
          per open account. Phase 3 re-screens each account against the updated list, and the
          screening decides: a confirmed match freezes that account and queues it for a reviewer.
          Ids are reserved to SYN-0900 to SYN-0999, so an update can never shadow an existing
          listing.
        </p>
        <table className="controls-listings">
          <caption className="controls-listings__caption">The update</caption>
          <thead>
            <tr>
              <th scope="col">Listing</th>
              <th scope="col">Name</th>
              <th scope="col">Born</th>
              <th scope="col">Country</th>
            </tr>
          </thead>
          <tbody>
            {batch.map((e) => (
              <tr key={e.entryId}>
                <td>
                  <code>{e.entryId || "—"}</code>
                </td>
                <td>{e.name || "—"}</td>
                <td className="kyc-num">{e.dob || "—"}</td>
                <td>{e.country || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <details className="controls-edit">
          <summary>Edit the first listing or the version</summary>
          <div className="controls-entry">
            <div className="kyc-form-row">
              <label htmlFor="entry-id">Listing id</label>
              <input
                id="entry-id"
                value={entry.entryId}
                onChange={(e) => update("entryId", e.target.value.toUpperCase())}
              />
            </div>
            <div className="kyc-form-row controls-entry__wide">
              <label htmlFor="entry-name">Listed name</label>
              <input id="entry-name" value={entry.name} onChange={(e) => update("name", e.target.value)} />
            </div>
            <div className="kyc-form-row">
              <label htmlFor="entry-dob">Date of birth</label>
              <input
                id="entry-dob"
                type="date"
                value={entry.dob ?? ""}
                onChange={(e) => update("dob", e.target.value)}
              />
            </div>
            <div className="kyc-form-row">
              <label htmlFor="entry-country">Country</label>
              <input
                id="entry-country"
                maxLength={2}
                value={entry.country ?? ""}
                onChange={(e) => update("country", e.target.value.toUpperCase())}
              />
            </div>
            <div className="kyc-form-row">
              <label htmlFor="entry-version">Version (blank for a timestamp)</label>
              <input id="entry-version" value={version} onChange={(e) => setVersion(e.target.value)} />
            </div>
          </div>
        </details>

        {confirming ? (
          <div className="controls-confirm" role="alertdialog" aria-label="Confirm publish">
            <p>
              This re-screens every open account against {batch.length} new listings
              {version.trim() ? (
                <>
                  {" "}
                  (version <strong>{version.trim()}</strong>)
                </>
              ) : null}
              . Publish now?
            </p>
            <div className="controls-buttons">
              <Button variant="danger" onClick={publish} loading={busy}>
                Publish now
              </Button>
              <Button variant="ghost" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <Button variant="primary" onClick={() => setConfirming(true)} loading={busy}>
            Publish list update
          </Button>
        )}

        {result ? (
          <p className="controls-result" role="status">
            <Icon name="check" size={16} /> Published {result.version}: {result.eventsSent} of{" "}
            {result.accounts} open accounts sent for re-screening
            {result.eventsFailed > 0 ? `, ${result.eventsFailed} failed to send` : ""}. Watch it on{" "}
            <a href={hrefFor("cases")}>All cases</a>.
          </p>
        ) : null}
        {error ? (
          <p className="kyc-error-text" role="alert">
            {error}
          </p>
        ) : null}

        {current && current.entries.length > 0 ? (
          <div className="controls-published">
            <h3>Published now{current.version ? ` · ${current.version}` : ""}</h3>
            <ul>
              {current.entries.map((e) => (
                <li key={e.entryId}>
                  <code>{e.entryId}</code> {e.name}
                  {e.dob ? ` · ${e.dob}` : ""}
                  {e.country ? ` · ${e.country}` : ""}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
