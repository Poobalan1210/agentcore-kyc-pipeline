import { useState } from "react";
import { Authenticator, ThemeProvider, useAuthenticator } from "@aws-amplify/ui-react";
import { AppShell } from "@shared/components/AppShell";
import { KycFlow } from "./flow/KycFlow";
import "./amplify-theme.css";

/**
 * Wraps the applicant flow in Amplify's `Authenticator` — the same user pool the Public API's
 * JWT authorizer validates against (R32.2: an applicant authenticates before any liveness
 * session). `Authenticator` is Amplify UI's own sign-up/sign-in component; using it here rather
 * than a hand-built form avoids re-implementing password-reset, verification-code, and error
 * states Cognito's own UI library already gets right, and keeps this app's only login surface
 * consistent with the same library `FaceLivenessDetector` (task 14.5) is drawn from.
 */
export function App() {
  return (
    <ThemeProvider>
      <Authenticator>
        {({ signOut, user }) => (
          <AppShell
            appName="Identity Verification"
            userLabel={user?.signInDetails?.loginId}
            onSignOut={signOut}
          >
            <AuthenticatedFlow />
          </AppShell>
        )}
      </Authenticator>
    </ThemeProvider>
  );
}

function AuthenticatedFlow() {
  const { user } = useAuthenticator((context) => [context.user]);
  const [key, setKey] = useState(0);

  // Amplify's Authenticator can briefly render children with a stale/undefined user object
  // right after sign-in completes; forcing a remount once `user` becomes defined avoids the
  // flow starting before the JWT the API client reads is actually available.
  if (!user) {
    return <p>Loading…</p>;
  }

  return <KycFlow key={key} onRestart={() => setKey((k) => k + 1)} />;
}
