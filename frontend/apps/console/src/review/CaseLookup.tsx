import { useEffect, useState } from "react";
import { Card, CardBody, CardHeader } from "@shared/components/Card";
import { api, ApiError, CaseTraceResponse } from "@shared/api/client";
import { CaseTrace } from "./CaseTrace";

/**
 * Any case's trace, read-only, by id — for the cases that are not waiting for anyone: the namesake
 * the rules excluded and approved on their own, a customer whose account a list update froze
 * after the review was decided. The same reviewers-only route as the queue's trace; there is no
 * decision form here because there is no review to decide.
 */
export function CaseLookup({ caseId }: { caseId: string }) {
  const [trace, setTrace] = useState<CaseTraceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTrace(null);
    setError(null);
    async function load() {
      try {
        const result = await api.getCaseTrace(caseId);
        if (!cancelled) {
          setTrace(result);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError && err.status === 404
            ? "No case with that id."
            : "Could not load the case. Retrying.");
        }
      }
    }
    load();
    const id = setInterval(load, 8000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [caseId]);

  return (
    <Card>
      <CardHeader>
        <h2>{trace?.applicant.caseId ?? caseId}</h2>
      </CardHeader>
      <CardBody>
        <p className="kyc-hint">
          Read-only. This case is not waiting for a review, so there is nothing to decide here.
        </p>
        {error ? <p className="kyc-error-text">{error}</p> : null}
        {trace ? <CaseTrace trace={trace} /> : error ? null : <p className="kyc-hint">Loading…</p>}
      </CardBody>
    </Card>
  );
}

