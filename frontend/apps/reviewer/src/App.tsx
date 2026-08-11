import { useEffect, useState } from "react";
import { Authenticator, ThemeProvider, useAuthenticator } from "@aws-amplify/ui-react";
import { fetchAuthSession } from "aws-amplify/auth";
import { AppShell } from "@shared/components/AppShell";
import { ReviewConsole } from "./ReviewConsole";

/**
 * Reviewer console shell. Uses the same Amplify `Authenticator` as the applicant app (task
 * 14.6) — same user pool, so the same login form works for both, but the server-side gate
 * (`review_api.py`'s `_is_reviewer`, R34.6) is what actually enforces `reviewers` group
 * membership. This client-side check is purely so a non-reviewer signed-in user sees a clear
 * message instead of a queue that always 403s.
 */
export function App() {
  return (
    <ThemeProvider>
      <Authenticator>
        {({ signOut, user }) => (
          <AppShell
            appName="Reviewer Console"
            userLabel={user?.signInDetails?.loginId}
            onSignOut={signOut}
          >
            <Gate />
          </AppShell>
        )}
      </Authenticator>
    </ThemeProvider>
  );
}

function Gate() {
  const { user } = useAuthenticator((context) => [context.user]);
  const [groups, setGroups] = useState<string[] | null>(null);

  useEffect(() => {
    if (!user) {
      return;
    }
    fetchAuthSession().then((session) => {
      const claim = session.tokens?.idToken?.payload?.["cognito:groups"];
      setGroups(Array.isArray(claim) ? (claim as string[]) : []);
    });
  }, [user]);

  if (!user || groups === null) {
    return <p>Loading…</p>;
  }

  if (!groups.includes("reviewers")) {
    return (
      <p className="kyc-hint">
        This account is not a member of the <strong>reviewers</strong> group. Contact an
        administrator to be added to Cognito user pool group `reviewers` before using this
        console.
      </p>
    );
  }

  return <ReviewConsole />;
}
