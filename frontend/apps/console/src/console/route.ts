import { useEffect, useState } from "react";

/**
 * The console's four views, addressed by the URL hash so a view survives a reload and a link can
 * open one directly: `#/live`, `#/review/<caseId>`, `#/cases/<caseId>`, `#/controls`.
 */
export type View = "live" | "review" | "cases" | "controls";

export interface Route {
  view: View;
  /** A case id (`CASE-…` or `APP-…`) for the review and cases views. */
  id: string | null;
}

const VIEWS: readonly View[] = ["live", "review", "cases", "controls"];

export function parseHash(hash: string): Route {
  const [head, ...rest] = hash.replace(/^#\/?/, "").split("/");
  const view = (VIEWS as readonly string[]).includes(head) ? (head as View) : "live";
  const raw = rest.join("/");
  let id: string | null = null;
  if (raw) {
    try {
      id = decodeURIComponent(raw);
    } catch {
      id = raw;
    }
  }
  return { view, id: id && id.trim() ? id.trim() : null };
}

export function hrefFor(view: View, id?: string | null): string {
  return `#/${view}${id ? `/${encodeURIComponent(id)}` : ""}`;
}

export function navigate(view: View, id?: string | null): void {
  window.location.hash = hrefFor(view, id);
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
