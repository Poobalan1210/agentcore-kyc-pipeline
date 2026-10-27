import { useState } from "react";
import { Badge } from "@shared/components/Badge";
import type {
  AgentNarrative,
  CaseTraceResponse,
  FactBase,
  ScreeningFact,
} from "@shared/api/client";
import { AgentReply, fieldLabel, formatValue, readReply, SAID_FIELDS } from "../process/agentReply";
import { agentName } from "../process/agents";
import { classificationTone, executionTone, guardReason, outcomeTone, phaseLabel } from "./labels";
import "./CaseTrace.css";

type Tab = "facts" | "decisions" | "agents" | "timeline" | "applicant";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "facts", label: "Recorded facts" },
  { id: "decisions", label: "Decisions" },
  { id: "agents", label: "Agent narrative" },
  { id: "timeline", label: "Timeline" },
  { id: "applicant", label: "Applicant & account" },
];

/**
 * The reviewer trace view — task 14.9, R18.2.
 *
 * Facts first, because the facts are what the rules decided on: each evidence tool recorded its
 * result on the case, and the decision engine read those records and nothing else. The agents'
 * replies are a separate tab, labelled as narrative, because a reviewer reading model output as a
 * verdict is exactly the failure this architecture exists to prevent — measured in this build, an
 * agent told a tool was unavailable replied "no match was found".
 *
 * Never shown here, because the API never sends it: S3 locations, biometric image keys, a
 * document's printed text, and any raw confidence score (F-44).
 */
export function CaseTrace({ trace }: { trace: CaseTraceResponse }) {
  const [tab, setTab] = useState<Tab>("facts");

  return (
    <div className="kyc-trace">
      <div className="kyc-trace__tabs" role="tablist" aria-label="Case trace">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`trace-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`trace-panel-${t.id}`}
            className={"kyc-trace__tab" + (tab === t.id ? " kyc-trace__tab--active" : "")}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.id === "agents" ? ` (${trace.agents.length})` : ""}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`trace-panel-${tab}`} aria-labelledby={`trace-tab-${tab}`}>
        {tab === "facts" ? <FactsTab trace={trace} /> : null}
        {tab === "decisions" ? <DecisionsTab trace={trace} /> : null}
        {tab === "agents" ? <AgentsTab agents={trace.agents} /> : null}
        {tab === "timeline" ? <TimelineTab trace={trace} /> : null}
        {tab === "applicant" ? <ApplicantTab trace={trace} /> : null}
      </div>
    </div>
  );
}

// -------------------------------------------------------------------------------- facts

function FactsTab({ trace }: { trace: CaseTraceResponse }) {
  const { facts } = trace;
  return (
    <div className="kyc-trace__section">
      <p className="kyc-hint">
        What each tool recorded on the case. The rules read these records and nothing else. A
        fact that is missing, or marked not performed, is a check that did not happen, and a
        missing check can only send a case to a person.
      </p>
      <ScreeningCard fact={facts.screening} />
      <FactCard
        title="Face match"
        fact={facts.faceMatch}
        fields={[
          ["match", "Match"],
          ["similarityBand", "Similarity band"],
          ["faceDetected", "Face detected"],
          ["unmatchedFaces", "Unmatched faces"],
          ["threshold", "Threshold applied"],
        ]}
      />
      <FactCard
        title="Document"
        fact={facts.documentExtraction}
        fields={[
          ["documentReadable", "Readable"],
          ["mrzPresent", "Machine-readable zone"],
          ["unreadableReason", "Why unreadable"],
        ]}
        extra={documentDetail(facts.documentExtraction)}
      />
      <FactCard
        title="Politically exposed persons"
        fact={facts.pep}
        fields={[["pepStatus", "Status"]]}
        extra={listCount(facts.pep, "hits", "listing")}
      />
      <FactCard
        title="Adverse media"
        fact={facts.adverseMedia}
        fields={[["maxSeverity", "Highest severity"]]}
        extra={listCount(facts.adverseMedia, "attributed", "attributed article")}
      />
      <FactCard
        title="Device and network velocity"
        fact={facts.velocity}
        fields={[
          ["deviceApplications", "People who applied from this device"],
          ["ipApplications", "People who applied from this network"],
          ["windowHours", "Window (hours)"],
        ]}
        extra={velocitySignals(facts.velocity)}
      />
    </div>
  );
}

/** D-41: which velocity signal the tool set, if any. Either critical one sends the case to a
 * person; the counts are of different people, so one person applying again counts once. */
function velocitySignals(fact: FactBase | null): React.ReactNode {
  if (!fact || fact.performed === false) return null;
  const signals = (fact.signals ?? {}) as Record<string, unknown>;
  const set = [
    signals.criticalDeviceReuse === true ? "critical reuse of this device" : null,
    signals.criticalIpReuse === true ? "critical reuse of this network" : null,
    signals.elevatedIpReuse === true ? "elevated reuse of this network" : null,
  ].filter(Boolean);
  return (
    <p className={set.some((s) => s?.startsWith("critical")) ? "kyc-error-text" : "kyc-hint"}>
      {set.length ? `Signals: ${set.join(", ")}.` : "No velocity signal."}
    </p>
  );
}

function ScreeningCard({ fact }: { fact: ScreeningFact | null }) {
  return (
    <FactShell title="Sanctions screening" fact={fact}>
      {fact && fact.performed !== false ? (
        <>
          <dl className="kyc-trace__facts">
            <dt>Strongest alert</dt>
            <dd>
              <Badge tone={classificationTone(fact.strongest)}>{fact.strongest ?? "—"}</Badge>
            </dd>
            <dt>List version</dt>
            <dd>{fact.listVersion ?? "—"}</dd>
            <dt>Screened</dt>
            <dd>
              {(fact.screened?.names ?? []).join(" / ") || "—"}
              {fact.screened?.dob ? ` · ${fact.screened.dob}` : ""}
              {fact.screened?.country ? ` · ${fact.screened.country}` : ""}
            </dd>
          </dl>
          {fact.agentNameMatchesRecord === false ? (
            <p className="kyc-error-text">
              The agent named "{fact.agentSuppliedName}", who is not the applicant on the record.
              The tool screened the applicant on the record anyway. This is what a manipulated
              agent looks like from here.
            </p>
          ) : null}
          {(fact.hits ?? []).length > 0 ? (
            <ul className="kyc-trace__hits">
              {(fact.hits ?? []).map((hit) => (
                <li key={hit.entryId}>
                  <Badge tone={classificationTone(hit.classification)}>
                    {hit.classification}
                  </Badge>
                  <span className="kyc-trace__mono">{hit.entryId}</span>
                  <span>{hit.matchedName}</span>
                  <span className="kyc-trace__hit-reason">{hit.reason}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="kyc-hint">No alerts.</p>
          )}
          {(fact.excluded ?? []).length > 0 ? (
            <details className="kyc-trace__details">
              <summary>
                {(fact.excluded ?? []).length} name match
                {(fact.excluded ?? []).length === 1 ? "" : "es"} excluded, with reasons
              </summary>
              <ul className="kyc-trace__hits">
                {(fact.excluded ?? []).map((ex) => (
                  <li key={ex.entryId}>
                    <Badge tone="success">EXCLUDED</Badge>
                    <span className="kyc-trace__mono">{ex.entryId}</span>
                    <span>{ex.matchedName}</span>
                    <span className="kyc-trace__hit-reason">{ex.reason}</span>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </>
      ) : null}
    </FactShell>
  );
}

function FactCard({
  title,
  fact,
  fields,
  extra,
}: {
  title: string;
  fact: FactBase | null;
  fields: Array<[string, string]>;
  extra?: React.ReactNode;
}) {
  return (
    <FactShell title={title} fact={fact}>
      {fact && fact.performed !== false ? (
        <>
          {fields.length > 0 ? (
            <dl className="kyc-trace__facts">
              {fields
                .filter(([key]) => fact[key] !== undefined && fact[key] !== null)
                .map(([key, label]) => (
                  <FactRow key={key} label={label} value={fact[key]} />
                ))}
            </dl>
          ) : null}
          {extra}
        </>
      ) : null}
    </FactShell>
  );
}

function FactShell({
  title,
  fact,
  children,
}: {
  title: string;
  fact: FactBase | null;
  children: React.ReactNode;
}) {
  const state = fact === null ? "missing" : fact.performed === false ? "not-performed" : "recorded";
  return (
    <section className="kyc-trace__fact">
      <header className="kyc-trace__fact-header">
        <h4>{title}</h4>
        {state === "recorded" ? <Badge tone="success">Recorded</Badge> : null}
        {state === "missing" ? <Badge tone="warning">Not recorded</Badge> : null}
        {state === "not-performed" ? (
          <Badge tone="danger">{fact?.status ?? "NOT PERFORMED"}</Badge>
        ) : null}
        {fact?.recordedAt ? (
          <span className="kyc-trace__fact-time">{String(fact.recordedAt)}</span>
        ) : null}
      </header>
      {state === "not-performed" ? (
        <p className="kyc-hint">
          Not performed{fact?.reasonCode ? ` (${fact.reasonCode})` : ""}
          {fact?.reason ? `: ${fact.reason}` : ""}.
        </p>
      ) : null}
      {children}
      {fact ? (
        <details className="kyc-trace__details">
          <summary>As recorded</summary>
          <pre className="kyc-trace__agent-body">{JSON.stringify(fact, null, 2)}</pre>
        </details>
      ) : null}
    </section>
  );
}

function FactRow({ label, value }: { label: string; value: unknown }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{typeof value === "boolean" ? (value ? "yes" : "no") : String(value)}</dd>
    </>
  );
}

function documentDetail(fact: FactBase | null): React.ReactNode {
  if (!fact || fact.performed === false) return null;
  const doc = (fact.document ?? {}) as Record<string, unknown>;
  const mrz = (fact.mrz ?? null) as Record<string, unknown> | null;
  return (
    <dl className="kyc-trace__facts">
      <dt>Type</dt>
      <dd>
        {String(doc.documentType ?? "—")} · {String(doc.issuingCountry ?? "—")}
        {doc.synthetic ? " · synthetic" : ""}
      </dd>
      {mrz ? (
        <>
          <dt>Check digits</dt>
          <dd>{mrz.checkDigitsValid ? "valid" : "not valid"}</dd>
          {mrz.expiryDate ? (
            <>
              <dt>Expiry (verified)</dt>
              <dd>{String(mrz.expiryDate)}</dd>
            </>
          ) : null}
        </>
      ) : null}
    </dl>
  );
}

function listCount(fact: FactBase | null, field: string, noun: string): React.ReactNode {
  if (!fact || fact.performed === false) return null;
  const items = fact[field];
  if (!Array.isArray(items)) return null;
  return (
    <p className="kyc-hint">
      {items.length} {noun}
      {items.length === 1 ? "" : "s"}
    </p>
  );
}

// ---------------------------------------------------------------------------- decisions

function DecisionsTab({ trace }: { trace: CaseTraceResponse }) {
  const phases = ["phase1", "phase2", "phase3"].filter(
    (p) => trace.decisions[p] || trace.reviews[p],
  );
  const sealed = trace.decision;
  return (
    <div className="kyc-trace__section">
      {phases.length === 0 ? (
        <p className="kyc-trace__empty">No phase has decided this case yet.</p>
      ) : null}
      {phases.map((phase) => {
        const decision = (trace.decisions[phase] ?? {}) as Record<string, unknown>;
        const review = trace.reviews[phase] as Record<string, unknown> | null | undefined;
        return (
          <section key={phase} className="kyc-trace__fact">
            <header className="kyc-trace__fact-header">
              <h4>{phaseLabel(phase)}</h4>
              {decision.outcome ? (
                <Badge tone={outcomeTone(String(decision.outcome))}>
                  {String(decision.outcome)}
                </Badge>
              ) : null}
            </header>
            <dl className="kyc-trace__facts">
              {(["rule", "reasonCode", "tier", "decidedBy", "requiredItem", "explanation",
                "engineOutcome", "reviewReason"] as const)
                .filter((k) => decision[k] !== undefined && decision[k] !== null)
                .map((k) => (
                  <FactRow key={k} label={k} value={decision[k]} />
                ))}
            </dl>
            {review ? (
              <div className="kyc-trace__review">
                <strong>Reviewed:</strong> {String(review.action ?? "—")} by{" "}
                {String(review.reviewerId ?? "—")}
                {review.decidedAt ? ` at ${String(review.decidedAt)}` : ""}
                {review.rationale ? <p>“{String(review.rationale)}”</p> : null}
                {Array.isArray(review.clearedAlerts) && review.clearedAlerts.length > 0 ? (
                  <p>Cleared: {review.clearedAlerts.join(", ")}</p>
                ) : null}
              </div>
            ) : null}
          </section>
        );
      })}

      {trace.screeningDispositions.length > 0 ? (
        <section className="kyc-trace__fact">
          <header className="kyc-trace__fact-header">
            <h4>Alerts cleared by reviewers</h4>
          </header>
          <ul className="kyc-trace__hits">
            {trace.screeningDispositions.map((d) => (
              <li key={`${d.entryId}-${d.decidedAt}`}>
                <Badge tone={classificationTone(d.classification)}>{d.classification}</Badge>
                <span className="kyc-trace__mono">{d.entryId}</span>
                <span>
                  {d.reviewerId} · {phaseLabel(d.phase)}
                </span>
                <span className="kyc-trace__hit-reason">
                  list {d.listVersion} · {d.decidedAt}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="kyc-trace__fact">
        <header className="kyc-trace__fact-header">
          <h4>Latest sealed audit record</h4>
          {sealed ? (
            <Badge tone={outcomeTone(sealed.decision?.outcome)}>
              {sealed.decision?.outcome ?? "—"}
            </Badge>
          ) : null}
        </header>
        {sealed ? (
          <dl className="kyc-trace__facts">
            <FactRow label="Decision id" value={sealed.decisionId} />
            <FactRow label="Phase" value={sealed.phase} />
            <FactRow label="Decided by" value={sealed.actor} />
            <FactRow label="Decided at" value={sealed.decidedAt} />
            <FactRow label="Execution" value={sealed.executionName ?? "—"} />
            {sealed.decision?.engineOutcome ? (
              <FactRow label="The rules had concluded" value={sealed.decision.engineOutcome} />
            ) : null}
            {sealed.decision?.clearedAlerts && sealed.decision.clearedAlerts.length > 0 ? (
              <FactRow label="Alerts cleared" value={sealed.decision.clearedAlerts.join(", ")} />
            ) : null}
            {sealed.decision?.rationale ? (
              <FactRow label="Reviewer's rationale, sealed" value={sealed.decision.rationale} />
            ) : null}
            {sealed.supersedesDecisionId ? (
              <FactRow label="Supersedes" value={sealed.supersedesDecisionId} />
            ) : null}
          </dl>
        ) : (
          <p className="kyc-hint">No decision record has been sealed for this case yet.</p>
        )}
      </section>
    </div>
  );
}

// ------------------------------------------------------------------------------- agents

function AgentsTab({ agents }: { agents: AgentNarrative[] }) {
  if (agents.length === 0) {
    return <p className="kyc-trace__empty">No agent has run on this case yet.</p>;
  }
  return (
    <div className="kyc-trace__section">
      <p className="kyc-hint">
        What each agent, an AgentCore harness, said. Narrative only: the rules decided on the
        recorded facts, never on this text, and a reply here can be wrong without changing any
        outcome.
      </p>
      {agents.map((run, i) => {
        const reply = readReply(run.narrative);
        return (
          <details key={`${run.phase}-${run.agent}-${i}`} className="kyc-trace__agent">
            <summary className="kyc-trace__agent-header">
              <span>
                {agentName(run.agent)} · {phaseLabel(run.phase)}
              </span>
              <span className="kyc-trace__agent-meta">
                {/* Whether the harness answered, not whether the answer is right. */}
                <Badge tone={run.status === "OK" ? "success" : "danger"}>
                  {run.status === "OK" ? "Answered" : run.status === "DEGRADED" ? "No answer" : run.status ?? "—"}
                </Badge>
                {run.latencyMs ? ` ${(run.latencyMs / 1000).toFixed(1)}s` : ""}
              </span>
            </summary>
            {reply && run.narrative ? (
              <ReplyBody reply={reply} raw={run.narrative} />
            ) : (
              <pre className="kyc-trace__agent-body">
                {`No reply${run.reason ? ` (${run.reason})` : ""}${
                  run.stopReason ? ` — stop reason ${run.stopReason}` : ""
                }.`}
              </pre>
            )}
          </details>
        );
      })}
    </div>
  );
}

/** A reply in fields: the agent's sentence, then its fields; the reply as written, on request. */
function ReplyBody({ reply, raw }: { reply: AgentReply; raw: string }) {
  if (!reply.fields) return <pre className="kyc-trace__agent-body">{reply.text}</pre>;
  const fields = Object.entries(reply.fields).filter(([key]) => !SAID_FIELDS.includes(key));
  return (
    <div className="kyc-trace__agent-reply">
      {reply.said ? <p className="kyc-trace__agent-said">“{reply.said}”</p> : null}
      {fields.length ? (
        <dl className="kyc-trace__facts">
          {fields.map(([key, value]) => (
            <FactRow key={key} label={fieldLabel(key)} value={formatValue(value)} />
          ))}
        </dl>
      ) : null}
      <details className="kyc-trace__details">
        <summary>The reply, as the model wrote it</summary>
        <pre className="kyc-trace__agent-body">{raw}</pre>
      </details>
    </div>
  );
}

// ----------------------------------------------------------------------------- timeline

function TimelineTab({ trace }: { trace: CaseTraceResponse }) {
  const execution = trace.execution;
  if (!execution) {
    return (
      <p className="kyc-trace__empty">
        No execution timeline is available for this case — no decision record names its
        execution yet, or the history has aged out of Step Functions' retention window.
      </p>
    );
  }
  return (
    <div className="kyc-trace__section">
      <dl className="kyc-trace__facts">
        <dt>Execution</dt>
        <dd>
          <Badge tone={executionTone(execution.status)}>{execution.status ?? "UNKNOWN"}</Badge>{" "}
          {phaseLabel(execution.phase)}
        </dd>
        <dt>Duration</dt>
        <dd>{duration(execution.startDate, execution.stopDate ?? undefined) || "running"}</dd>
      </dl>
      <ol className="kyc-trace__timeline">
        {execution.steps.map((step, index) => (
          <li key={`${step.name}-${index}`} className="kyc-trace__step">
            <Badge tone={executionTone(step.status)}>{step.status}</Badge>
            <span className="kyc-trace__step-name">{step.name}</span>
            <span className="kyc-trace__step-duration">
              {duration(step.startedAt, step.endedAt)}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

// ---------------------------------------------------------------------------- applicant

function ApplicantTab({ trace }: { trace: CaseTraceResponse }) {
  const { applicant, account, notifications } = trace;
  const identity = applicant.declaredIdentity;
  return (
    <div className="kyc-trace__section">
      <dl className="kyc-trace__facts">
        <FactRow label="Case" value={applicant.caseId ?? "—"} />
        <FactRow label="State" value={applicant.state ?? "—"} />
        {applicant.riskState ? <FactRow label="Risk state" value={applicant.riskState} /> : null}
        <FactRow
          label="Jurisdiction / product"
          value={`${applicant.jurisdiction?.toUpperCase() ?? "—"} · ${
            applicant.productApplied ?? "—"
          }`}
        />
        <FactRow
          label="Declared identity"
          value={
            identity
              ? `${identity.givenNames} ${identity.surname}, ${identity.dateOfBirth}, ` +
                `${identity.nationality}`
              : "not provided"
          }
        />
        <FactRow
          label="SSN or ITIN"
          value={applicant.taxIdLast4 ? `on file, ending ${applicant.taxIdLast4}` : "not provided"}
        />
        <FactRow
          label="Documents"
          value={
            applicant.documents.length > 0
              ? applicant.documents.map((d) => `${d.type} (${d.issuingCountry})`).join(", ")
              : "none"
          }
        />
        <FactRow label="Liveness" value={applicant.liveness?.result ?? "not recorded"} />
        <FactRow label="Liveness failures" value={applicant.livenessFailures ?? 0} />
        <FactRow label="Earlier attempts" value={applicant.previousAttempts} />
      </dl>

      <section className="kyc-trace__fact">
        <header className="kyc-trace__fact-header">
          <h4>Account, as the platform holds it</h4>
          {account ? <Badge tone="neutral">{account.state}</Badge> : null}
        </header>
        {account ? (
          <dl className="kyc-trace__facts">
            <FactRow label="Posture" value={account.posture ?? "—"} />
            <FactRow
              label="Restrictions"
              value={account.restrictions.length > 0 ? account.restrictions.join(", ") : "none"}
            />
            <FactRow
              label="Holds"
              value={
                account.holds && account.holds.length > 0
                  ? account.holds.map((h) => `${h.holdId} (${h.posture})`).join(", ")
                  : "none"
              }
            />
            <FactRow label="Balance" value={`${account.balance} ${account.limits.currency}`} />
          </dl>
        ) : (
          <p className="kyc-hint">No account has been opened.</p>
        )}
      </section>

      <section className="kyc-trace__fact">
        <header className="kyc-trace__fact-header">
          <h4>Messages to the applicant</h4>
        </header>
        {notifications.length === 0 ? (
          <p className="kyc-hint">None sent.</p>
        ) : (
          <ul className="kyc-trace__messages">
            {notifications.map((n, i) => {
              const why = guardReason(n.replacedBecause);
              return (
                <li key={`${n.at}-${i}`}>
                  <div className="kyc-trace__message-head">
                    <Badge tone={n.replacedByGuard ? "warning" : "success"}>
                      {n.replacedByGuard ? "Replaced by the guard" : "Sent as written"}
                    </Badge>
                    <span className="kyc-trace__fact-time">
                      {n.kind} · {n.at}
                    </span>
                  </div>
                  {n.replacedByGuard ? (
                    <>
                      <p className="kyc-trace__message-written">Written: {n.written}</p>
                      <p className="kyc-trace__message-text">Shown: {n.shown}</p>
                      {why ? <p className="kyc-trace__message-why">Replaced because {why}.</p> : null}
                    </>
                  ) : (
                    <p className="kyc-trace__message-text">{n.shown}</p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}

function duration(startedAt?: string, endedAt?: string): string {
  if (!startedAt || !endedAt) return "";
  const ms = new Date(endedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(ms) || ms < 0) return "";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}
