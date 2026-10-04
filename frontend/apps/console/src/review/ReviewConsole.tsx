import { FormEvent, useCallback, useEffect, useState } from "react";
import { Button } from "@shared/components/Button";
import { Card, CardBody } from "@shared/components/Card";
import { api, ApiError, QueuedCaseSummary } from "@shared/api/client";
import { navigate } from "../console/route";
import { QueueList } from "./QueueList";
import { DecisionForm } from "./DecisionForm";
import { CaseLookup } from "./CaseLookup";
import "./ReviewConsole.css";

/**
 * Reviewer console root — task 14.6, R34.1/R34.5/R34.6.
 *
 * Owns the queue poll and the selected-case state; `QueueList` and `DecisionForm` are both
 * presentational. Polling (rather than a push channel) matches the applicant app — R34.5 asks for
 * "near-real-time" depth/age, not a websocket, and every surface in this build reads state by
 * plain GET polling.
 *
 * A case can also be opened by id, from the lookup below or from anywhere in the console
 * (`#/review/<caseId>`, the live view's "Review now"). If it is in the queue, it opens with its
 * decision form; otherwise its trace opens read-only.
 */
const POLL_INTERVAL_MS = 8000;

export function ReviewConsole({ openCaseId }: { openCaseId?: string | null }) {
  const [cases, setCases] = useState<QueuedCaseSummary[]>([]);
  const [depth, setDepth] = useState(0);
  const [selected, setSelected] = useState<QueuedCaseSummary | null>(null);
  const [lookup, setLookup] = useState<string | null>(() => openCaseId ?? null);
  const [lookupDraft, setLookupDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const result = await api.getReviewQueue();
      setCases(result.cases);
      setDepth(result.depth);
      setError(null);
      // Keep the selected case's data current (e.g. a newly written taskToken) without losing
      // the reviewer's place in the form if they are mid-decision.
      setSelected((current) => {
        if (!current) {
          return current;
        }
        return result.cases.find((c) => c.applicantId === current.applicantId) ?? current;
      });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setError("This account is not authorized to view the review queue (reviewers group only).");
      } else {
        setError("Could not load the review queue. Retrying automatically.");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  // A looked-up case that is waiting for a review opens with its decision form.
  useEffect(() => {
    if (!lookup) return;
    const queued = cases.find((c) => c.caseId === lookup || c.applicantId === lookup);
    if (queued) {
      setSelected(queued);
      setLookup(null);
    }
  }, [cases, lookup]);

  const handleDecided = useCallback(() => {
    setSelected(null);
    setLookup(null);
    refresh();
    // Back to the plain queue URL, so a reload does not reopen a decided case.
    if (openCaseId) navigate("review");
  }, [refresh, openCaseId]);

  function openLookup(event: FormEvent) {
    event.preventDefault();
    const id = lookupDraft.trim();
    if (!id) return;
    setSelected(null);
    setLookup(id);
  }

  return (
    <div className="kyc-review-layout">
      <section className="kyc-review-layout__queue">
        <Card>
          <CardBody>
            <div className="kyc-review-layout__queue-header">
              <h2>Waiting for a decision</h2>
              <span className="kyc-review-layout__depth">{depth}</span>
            </div>
            {error ? <p className="kyc-error-text">{error}</p> : null}
            {loading ? (
              <p className="kyc-hint">Loading queue…</p>
            ) : (
              <QueueList
                cases={cases}
                selectedId={selected?.applicantId}
                onSelect={(c) => {
                  setLookup(null);
                  setSelected(c);
                }}
              />
            )}
            <form className="kyc-review-layout__lookup" onSubmit={openLookup}>
              <label htmlFor="case-lookup">Open any case</label>
              <div className="kyc-review-layout__lookup-row">
                <input
                  id="case-lookup"
                  placeholder="CASE-… or APP-…"
                  value={lookupDraft}
                  onChange={(e) => setLookupDraft(e.target.value)}
                />
                <Button type="submit" variant="secondary">
                  Open
                </Button>
              </div>
            </form>
          </CardBody>
        </Card>
      </section>
      <section className="kyc-review-layout__detail">
        {selected ? (
          <DecisionForm key={selected.applicantId} queuedCase={selected} onDecided={handleDecided} />
        ) : lookup ? (
          <CaseLookup key={lookup} caseId={lookup} />
        ) : (
          <div className="kyc-review-layout__placeholder">
            <h2>Select a case to review it</h2>
            <p className="kyc-hint">
              The briefing, the screening alerts and what an approval would clear come first; the
              facts the rules decided on are underneath.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
