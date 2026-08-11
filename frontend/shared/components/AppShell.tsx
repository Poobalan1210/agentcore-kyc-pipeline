import { ReactNode } from "react";
import "./AppShell.css";

interface AppShellProps {
  appName: string;
  userLabel?: string;
  onSignOut?: () => void;
  children: ReactNode;
}

/**
 * Shared top bar + content frame for all three apps. The KYC90 wordmark and app name are the
 * only branding — no logo mark, no hero imagery. This is an internal/compliance tool and a
 * regulated capture flow, and both benefit from reading as understated infrastructure rather
 * than a product marketing surface.
 */
export function AppShell({ appName, userLabel, onSignOut, children }: AppShellProps) {
  return (
    <div className="kyc-shell">
      <header className="kyc-shell__header">
        <div className="kyc-shell__brand">
          <span className="kyc-shell__wordmark">KYC90</span>
          <span className="kyc-shell__app-name">{appName}</span>
        </div>
        {userLabel ? (
          <div className="kyc-shell__user">
            <span>{userLabel}</span>
            {onSignOut ? (
              <button className="kyc-shell__signout" onClick={onSignOut} type="button">
                Sign out
              </button>
            ) : null}
          </div>
        ) : null}
      </header>
      <main className="kyc-shell__content">{children}</main>
    </div>
  );
}
