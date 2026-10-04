import { useEffect, useMemo, useState } from "react";
import { Badge, BadgeTone } from "@shared/components/Badge";
import { api, BoardCase } from "@shared/api/client";
import { Icon } from "../console/icons";
import { navigate } from "../console/route";
import { TARGET_SECONDS } from "../process/pipeline";
import "./Cases.css";

const POLL_MS = 3000;

const STATE_TONE: Record<string, BadgeTone> = {
  RECEIVED: "neutral",
  IN_PROGRESS: "info",
  AWAITING_APPLICANT: "warning",
  AWAITING_REVIEW: "warning",
  PROVISIONAL: "success",
  MONITORING: "success",
  RESTRICTED: "warning",
  FROZEN: "danger",
  DECIDED: "neutral",
};

const STATE_LABEL: Record<string, string> = {
  RECEIVED: "Queued",
  IN_PROGRESS: "Checking",
  AWAITING_APPLICANT: "Waiting for applicant",
  AWAITING_REVIEW: "With a reviewer",
  PROVISIONAL: "Open with limits",
  MONITORING: "Fully open",
  RESTRICTED: "Restricted",
  FROZEN: "Frozen",
  DECIDED: "Decided",
};

/**
 * Every case, newest first, with where it stands and its time to decision — measured server-side
 * from the case record's own timestamps (submission and the provisional decision). A row opens
 * that case's whole process.
 */
export function CasesView() {
  const [cases, setCases] = useState<BoardCase[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const result = await api.getDemoCases();
        if (!cancelled) {
          setCases(result.cases);
          setError(null);
        }
      } catch {
        if (!cancelled) setError("Could not load the cases. Retrying.");
      }
    }
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const stats = useMemo(() => summarise(cases ?? []), [cases]);

  return (
    <div className="cases">
      <div className="cases-stats">
        <Stat label="Applications" value={String(stats.total)} />
        <Stat label="Accounts open" value={String(stats.open)} tone="success" />
        <Stat label="With a reviewer" value={String(stats.review)} tone={stats.review ? "warning" : undefined} />
        <Stat label="Frozen" value={String(stats.frozen)} tone={stats.frozen ? "danger" : undefined} />
        <Stat
          label="Median time to decision"
          value={stats.median === null ? "—" : `${stats.median.toFixed(1)} s`}
          tone={stats.median === null ? undefined : stats.median <= TARGET_SECONDS ? "success" : "warning"}
          hint={`target ${TARGET_SECONDS} s · ${stats.decided} decided`}
        />
      </div>

      <section className="cases-table-card">
        <header className="cases-table-card__head">
          <h2>Cases</h2>
          <span className="kyc-hint">
            Refreshes every few seconds. Time to decision comes from the case record, not this
            browser.
          </span>
        </header>
        {error ? <p className="kyc-error-text cases-pad">{error}</p> : null}
        {cases === null ? (
          <p className="kyc-hint cases-pad">Loading…</p>
        ) : cases.length === 0 ? (
          <div className="cases-empty">
            <Icon name="cases" size={26} />
            <p>No applications yet. Start one in the live demo.</p>
          </div>
        ) : (
          <table className="cases-table">
            <thead>
              <tr>
                <th scope="col">Applicant</th>
                <th scope="col">State</th>
                <th scope="col">Submitted</th>
                <th scope="col" className="cases-table__num">Time to decision</th>
                <th scope="col">Note</th>
                <th scope="col" aria-label="Open" />
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr
                  key={c.applicantId}
                  className="cases-table__row"
                  tabIndex={0}
                  onClick={() => navigate("cases", c.caseId ?? c.applicantId)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      navigate("cases", c.caseId ?? c.applicantId);
                    }
                  }}
                >
                  <td>
                    <div className="cases-table__name">{c.name ?? "—"}</div>
                    <div className="cases-table__id">{c.caseId ?? c.applicantId}</div>
                  </td>
                  <td>
                    <Badge tone={STATE_TONE[c.state ?? ""] ?? "neutral"}>
                      {STATE_LABEL[c.state ?? ""] ?? c.state ?? "—"}
                    </Badge>
                    {c.riskState && c.riskState !== c.state ? (
                      <div className="cases-table__id">risk: {c.riskState}</div>
                    ) : null}
                  </td>
                  <td className="cases-table__mono">{clock(c.submittedAt)}</td>
                  <td className="cases-table__num">
                    {c.secondsToProvisional !== null ? (
                      <span
                        className={
                          "cases-table__time " +
                          (c.secondsToProvisional <= TARGET_SECONDS
                            ? "cases-table__time--fast"
                            : "cases-table__time--slow")
                        }
                      >
                        {c.secondsToProvisional.toFixed(1)} s
                      </span>
                    ) : (
                      <span className="cases-table__muted">—</span>
                    )}
                  </td>
                  <td className="cases-table__note">
                    {c.state === "AWAITING_REVIEW"
                      ? `${phaseText(c.reviewPhase)}${c.reviewReason ? ` · ${reasonText(c.reviewReason)}` : ""}`
                      : ""}
                  </td>
                  <td className="cases-table__go">
                    <Icon name="chevron" size={16} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: "success" | "warning" | "danger";
  hint?: string;
}) {
  return (
    <div className={"cases-stat" + (tone ? ` cases-stat--${tone}` : "")}>
      <span className="cases-stat__label">{label}</span>
      <span className="cases-stat__value kyc-num">{value}</span>
      {hint ? <span className="cases-stat__hint">{hint}</span> : null}
    </div>
  );
}

function summarise(cases: BoardCase[]) {
  const decided = cases
    .map((c) => c.secondsToProvisional)
    .filter((s): s is number => s !== null)
    .sort((a, b) => a - b);
  const median = decided.length
    ? decided.length % 2
      ? decided[(decided.length - 1) / 2]
      : (decided[decided.length / 2 - 1] + decided[decided.length / 2]) / 2
    : null;
  return {
    total: cases.length,
    open: cases.filter((c) => c.state === "PROVISIONAL" || c.state === "MONITORING").length,
    review: cases.filter((c) => c.state === "AWAITING_REVIEW").length,
    frozen: cases.filter((c) => c.state === "FROZEN").length,
    decided: decided.length,
    median,
  };
}

function phaseText(phase?: string | null): string {
  return phase === "phase2" ? "Phase 2" : phase === "phase3" ? "Phase 3" : "Phase 1";
}

function reasonText(reason: string): string {
  return reason.toLowerCase().replace(/_/g, " ");
}

function clock(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleTimeString();
}
