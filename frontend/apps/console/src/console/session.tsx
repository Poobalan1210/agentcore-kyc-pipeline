import { createContext, ReactNode, useContext, useEffect, useState } from "react";
import { useAuthenticator } from "@aws-amplify/ui-react";
import { fetchAuthSession } from "aws-amplify/auth";

/**
 * Who is signed in and which of the console's roles they hold.
 *
 * The groups come from the ID token's `cognito:groups` claim, the same token the API's authorizer
 * reads. They only decide what the console offers: every route checks the same group again,
 * exactly, on the server. A group added while signed in appears after the next sign-in, when a
 * new token carries it.
 */
export interface Session {
  email: string;
  groups: string[];
  isPresenter: boolean;
  isReviewer: boolean;
  signOut: () => void;
}

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const { user, signOut } = useAuthenticator((context) => [context.user]);
  const [groups, setGroups] = useState<string[] | null>(null);
  const [email, setEmail] = useState("");

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    fetchAuthSession()
      .then((session) => {
        if (cancelled) return;
        const payload = session.tokens?.idToken?.payload ?? {};
        const claim = payload["cognito:groups"];
        setGroups(Array.isArray(claim) ? claim.map(String) : []);
        setEmail(String(payload.email ?? user.signInDetails?.loginId ?? user.username ?? ""));
      })
      .catch(() => {
        if (!cancelled) setGroups([]);
      });
    return () => {
      cancelled = true;
    };
  }, [user]);

  if (!user || groups === null) {
    return (
      <div className="console-splash" role="status">
        <span className="kyc-progress__spinner" aria-hidden="true" />
        <span>Signing in…</span>
      </div>
    );
  }

  const value: Session = {
    email,
    groups,
    isPresenter: groups.includes("presenters"),
    isReviewer: groups.includes("reviewers"),
    signOut,
  };
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession must be used inside SessionProvider");
  return session;
}
