import { Authenticator, ThemeProvider, useAuthenticator } from "@aws-amplify/ui-react";
import { Console } from "./console/Console";
import { SessionProvider } from "./console/session";
import { Icon } from "./console/icons";
import "./console/SignIn.css";

/**
 * One sign-in for the whole console. Sign-up is hidden: the presenter and reviewer accounts are
 * created by hand (`scripts/prepare_demo.py users`), because the presenter controls can put any
 * open account on hold.
 */
export function App() {
  return (
    <ThemeProvider>
      <Authenticator.Provider>
        <Gate />
      </Authenticator.Provider>
    </ThemeProvider>
  );
}

function Gate() {
  const { route } = useAuthenticator((context) => [context.route]);
  if (route === "authenticated") {
    return (
      <SessionProvider>
        <Console />
      </SessionProvider>
    );
  }
  return <SignInPage />;
}

function SignInPage() {
  return (
    <div className="signin">
      <section className="signin__brand" aria-label="About this console">
        <div className="signin__logo">
          <span className="signin__mark">
            <Icon name="logo" size={22} />
          </span>
          <span className="signin__wordmark">KYC90</span>
        </div>
        <div className="signin__pitch">
          <h1>Onboarding, decided in ninety seconds.</h1>
          <p>
            One console for the live demo: the customer's journey, every check AWS runs on it as
            it happens, the review queue and the presenter controls.
          </p>
          <ul className="signin__points">
            <li>
              <Icon name="agent" size={18} />
              Seven agents on Amazon Bedrock AgentCore gather the evidence through tools
            </li>
            <li>
              <Icon name="rules" size={18} />
              Every tool records a fact, and deterministic rules decide on the facts
            </li>
            <li>
              <Icon name="lock" size={18} />
              Every decision is sealed in an audit record under S3 Object Lock
            </li>
          </ul>
        </div>
        <p className="signin__foot">Synthetic identities only · AWS re:Invent 2026 demo</p>
      </section>
      <section className="signin__form">
        <div className="signin__form-inner">
          <h2>Sign in to the console</h2>
          <p className="kyc-hint">Use the presenter or reviewer account you were given.</p>
          <Authenticator hideSignUp />
        </div>
      </section>
    </div>
  );
}
