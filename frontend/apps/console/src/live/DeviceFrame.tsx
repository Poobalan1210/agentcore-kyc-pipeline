import { ReactNode, useEffect, useState } from "react";

/**
 * The customer's phone, as hardware only: the bezel, the status bar and the home indicator. What
 * shows on the display is the bank's app and nothing else, so the console never draws its own
 * chrome (steps, labels, a "secure" badge) onto the customer's screen. The app brings its own
 * header and progress, as a real one would.
 *
 * The display is a light scope (`kyc-light`): the console around it is dark, the bank app is not.
 */
export function DeviceFrame({ children }: { children: ReactNode }) {
  const now = useClock();
  return (
    <div className="device">
      <div className="device__bezel">
        <div className="device__display kyc-light">
          <div className="device__status" aria-hidden="true">
            <span className="device__time kyc-num">{now}</span>
            <span className="device__island" />
            <span className="device__icons">
              <SignalIcon />
              <WifiIcon />
              <BatteryIcon />
            </span>
          </div>
          <div className="device__screen">{children}</div>
          <div className="device__home" aria-hidden="true" />
        </div>
      </div>
    </div>
  );
}

/** "9:41", as a phone shows it: hours and minutes, no AM/PM. */
function useClock(): string {
  const format = () =>
    new Date()
      .toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
      .replace(/\s?[AP]M$/i, "");
  const [now, setNow] = useState(format);
  useEffect(() => {
    const id = setInterval(() => setNow(format()), 15_000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function SignalIcon() {
  return (
    <svg width="17" height="11" viewBox="0 0 17 11" fill="currentColor">
      <rect x="0" y="7" width="3" height="4" rx="0.8" />
      <rect x="4.6" y="5" width="3" height="6" rx="0.8" />
      <rect x="9.2" y="2.6" width="3" height="8.4" rx="0.8" />
      <rect x="13.8" y="0" width="3" height="11" rx="0.8" />
    </svg>
  );
}

function WifiIcon() {
  return (
    <svg width="15" height="11" viewBox="0 0 15 11" fill="none" stroke="currentColor"
      strokeWidth="1.7" strokeLinecap="round">
      <path d="M1.2 3.9a9 9 0 0 1 12.6 0" />
      <path d="M3.5 6.3a5.7 5.7 0 0 1 8 0" />
      <path d="M5.8 8.6a2.4 2.4 0 0 1 3.4 0" />
    </svg>
  );
}

function BatteryIcon() {
  return (
    <svg width="25" height="12" viewBox="0 0 25 12" fill="none">
      <rect x="0.6" y="0.6" width="21" height="10.8" rx="3.2" stroke="currentColor" strokeOpacity="0.4"
        strokeWidth="1.2" />
      <rect x="2.4" y="2.4" width="15.6" height="7.2" rx="1.8" fill="currentColor" />
      <path d="M23.2 4.2v3.6a1.9 1.9 0 0 0 0-3.6Z" fill="currentColor" fillOpacity="0.45" />
    </svg>
  );
}
