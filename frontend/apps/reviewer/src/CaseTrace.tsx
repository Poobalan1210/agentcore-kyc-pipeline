import { useEffect, useState } from "react";
import { Badge, BadgeTone } from "@shared/components/Badge";
import { api, CaseTraceResponse } from "@shared/api/client";
import "./CaseTrace.css";

/**
 * The reviewer trace view — task 14.9: which agents ran, what each one found, the document
 * extraction, the decision record, and the Step Functions timeline.
 *
 * Three tabs rather than one long page, because the three things a reviewer asks are genuinely
 * different questions: "who is this?" (Applicant), "what did each check find?" (Agents), "what
 * happened and in what order?" (Timeline). Collapsing them into one scroll makes all three harder
 * to answer, not easier.
 *
 * R18.2's audience is auditors, not applicants — this view exists only in the reviewer console.
 * Every agent finding is rendered under an "Agent finding" label, never presented as if it were
 * the decision itself: the pure decision engine is the only thing that decides (design's whole
 * point), and a reviewer reading model output as a verdict is exactly the failure mode that
 * architecture exists to prevent. No raw confidence score is rendered anywhere here (F-44) — the
 * API never returns one to this route either, so there is nothing to accidentally surface.
 */
export function CaseTrace({ caseId }: { caseId: string }) {
  const [trace, setTrace] = useState<CaseTraceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"timeline" | "agents" | "applicant">("timeline");

  useEffect(() => {
    let cancelled = false;
    setTrace(null);
    setError(null);

    async function load() {
      try {
        const result = await api.getCaseTrace(caseId);
        if (!cancelled) setTrace(result);
      } catch (err) {
        if (!cancelled) setError("Could not load the case trace.");
        // eslint-disable-next-line no-console
        console.error(err);
      }
    }
    load();
    // Polled while the case may still be in progress — Phase 1 usually finishes in seconds, but a
    // degraded path can take longer, and this view should catch up rather than require a refresh.
    const id = setInterval(load, 8000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [caseId]);

  if (error) {
    return <p className="kyc-error-text">{error}</p>;
  }
  if (!trace) {
    return <p className="kyc-hint">Loading trace…</p>;
  }

  return (
    <div className="kyc-trace">
      <div className="kyc-trace__tabs" role="tablist">
        <TabButton id="timeline" active={tab} onSelect={setTab}>
          Timeline
        </TabButton>
        <TabButton id="agents" active={tab} onSelect={setTab}>
          Agent findings ({trace.agents.length})
        </TabButton>
        <TabButton id="applicant" active={tab} onSelect={setTab}>
          Applicant
        </TabButton>
      </div>

      {tab === "timeline" ? <TimelineTab trace={trace} /> : null}
      {tab === "agents" ? <AgentsTab trace={trace} /> : null}
      {tab === "applicant" ? <ApplicantTab trace={trace} /> : null}
    </div>
  );
}

function TabButton({
  id,
  active,
  onSelect,
  children,
}: {
  id: "timeline" | "agents" | "applicant";
  active: string;
  onSelect: (id: "timeline" | "agents" | "applicant") => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active === id}
      className={"kyc-trace__tab" + (active === id ? " kyc-trace__tab--active" : "")}
      onClick={() => onSelect(id)}
    >
      {children}
    </button>
  );
}

function TimelineTab({ trace }: { trace: CaseTraceResponse }) {
  const decision = trace.decision as { outcome?: string; reasonCode?: string } | null;

  return (
    <div className="kyc-trace__section">
      {decision ? (
        <dl className="kyc-trace__row">
          <dt>Decision</dt>
          <dd>
            <Badge tone={outcomeTone(decision.outcome)}>{decision.outcome ?? "—"}</Badge>
            {decision.reasonCode ? ` · ${decision.reasonCode}` : ""}
          </dd>
        </dl>
      ) : (
        <p className="kyc-hint">No decision has been recorded for this case yet.</p>
      )}

      {trace.execution ? (
        <>
          <dl className="kyc-trace__row">
            <dt>Execution status</dt>
            <dd>
              <Badge tone={executionTone(trace.execution.status)}>
                {trace.execution.status ?? "UNKNOWN"}
              </Badge>
            </dd>
          </dl>
          <ol className="kyc-trace__timeline">
            {trace.execution.steps.map((step, index) => (
              <li key={`${step.name}-${index}`} className="kyc-trace__step">
                <Badge tone={executionTone(step.status)}>{step.status}</Badge>
                <span className="kyc-trace__step-name">{step.name}</span>
                <span className="kyc-trace__step-duration">
                  {stepDuration(step.startedAt, step.endedAt)}
                </span>
              </li>
            ))}
          </ol>
        </>
      ) : (
        <p className="kyc-trace__empty">
          No execution timeline is available for this case — it may predate this view, or its
          history has expired from Step Functions' own retention window.
        </p>
      )}
    </div>
  );
}

function AgentsTab({ trace }: { trace: CaseTraceResponse }) {
  const [open, setOpen] = useState<Set<string>>(new Set());

  function toggle(agent: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(agent)) next.delete(agent);
      else next.add(agent);
      return next;
    });
  }

  if (trace.agents.length === 0 && !trace.documentExtraction) {
    return <p className="kyc-trace__empty">No agent has recorded a finding for this case yet.</p>;
  }

  return (
    <div className="kyc-trace__section">
      {trace.documentExtraction ? (
        <div className="kyc-trace__agent">
          <button
            type="button"
            className="kyc-trace__agent-header"
            aria-expanded={open.has("__extraction")}
            onClick={() => toggle("__extraction")}
          >
            <span>Document extraction (verified — MRZ check digits, not model output)</span>
            <span>{open.has("__extraction") ? "▲" : "▼"}</span>
          </button>
          {open.has("__extraction") ? (
            <pre className="kyc-trace__agent-body">
              {JSON.stringify(trace.documentExtraction, null, 2)}
            </pre>
          ) : null}
        </div>
      ) : null}

      {trace.agents.map(({ agent, finding }) => {
        const isRaw = finding?.raw === true;
        return (
          <div key={agent} className="kyc-trace__agent">
            <button
              type="button"
              className="kyc-trace__agent-header"
              aria-expanded={open.has(agent)}
              onClick={() => toggle(agent)}
            >
              <span>
                {agent} — {isRaw ? "unstructured output (no submit_* finding recorded)" : "agent finding"}
              </span>
              <span>{open.has(agent) ? "▲" : "▼"}</span>
            </button>
            {open.has(agent) ? (
              <pre className="kyc-trace__agent-body">
                {isRaw ? String(finding.text ?? "") : JSON.stringify(finding, null, 2)}
              </pre>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function ApplicantTab({ trace }: { trace: CaseTraceResponse }) {
  const { applicant } = trace;
  return (
    <dl className="kyc-trace__section">
      <div className="kyc-trace__row">
        <dt>Case</dt>
        <dd>{applicant.caseId ?? "—"}</dd>
      </div>
      <div className="kyc-trace__row">
        <dt>State</dt>
        <dd>
          <Badge tone="neutral">{applicant.state ?? "—"}</Badge>
        </dd>
      </div>
      <div className="kyc-trace__row">
        <dt>Jurisdiction / product</dt>
        <dd>
          {applicant.jurisdiction?.toUpperCase() ?? "—"} · {applicant.productApplied ?? "—"}
        </dd>
      </div>
      <div className="kyc-trace__row">
        <dt>Declared identity</dt>
        <dd>
          {applicant.declaredIdentity
            ? `${applicant.declaredIdentity.givenNames} ${applicant.declaredIdentity.surname}, `
              + `${applicant.declaredIdentity.dateOfBirth}, `
              + `${applicant.declaredIdentity.nationality}`
            : "not provided"}
        </dd>
      </div>
      <div className="kyc-trace__row">
        <dt>Documents</dt>
        <dd>
          {applicant.documents.length > 0
            ? applicant.documents.map((d) => `${d.type} (${d.issuingCountry})`).join(", ")
            : "none"}
        </dd>
      </div>
      <div className="kyc-trace__row">
        <dt>Liveness</dt>
        <dd>
          {applicant.liveness ? (
            <Badge tone={applicant.liveness.result === "PASS" ? "success" : "danger"}>
              {applicant.liveness.result}
            </Badge>
          ) : (
            "not recorded"
          )}
        </dd>
      </div>
    </dl>
  );
}

function outcomeTone(outcome?: string): BadgeTone {
  switch (outcome) {
    case "PROVISIONAL_APPROVE_RESTRICTED":
      return "success";
    case "STEP_UP":
      return "warning";
    case "MANUAL_REVIEW":
      return "info";
    case "REJECT":
      return "danger";
    default:
      return "neutral";
  }
}

function executionTone(status?: string | null): BadgeTone {
  switch (status) {
    case "SUCCEEDED":
      return "success";
    case "RUNNING":
      return "info";
    case "FAILED":
    case "TIMED_OUT":
    case "ABORTED":
      return "danger";
    default:
      return "neutral";
  }
}

function stepDuration(startedAt?: string, endedAt?: string): string {
  if (!startedAt || !endedAt) return "";
  const ms = new Date(endedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(ms) || ms < 0) return "";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}
