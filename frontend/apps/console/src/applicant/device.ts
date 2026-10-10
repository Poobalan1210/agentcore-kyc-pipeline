/**
 * A random id for this browser, sent with every application (D-41).
 *
 * The Fraud agent's velocity tool counts how many different people applied from one device in a
 * day; this is the device. It is not a fingerprint: it is a random UUID kept in local storage,
 * so clearing the browser's storage starts a new one, and the API stores only a keyed hash of it.
 *
 * Every application tab in the console shares it, as tabs on one laptop share a device.
 */
const DEVICE_KEY = "kyc90.deviceId";

/** What the API accepts (`api/velocity.py`). */
const DEVICE_ID = /^[A-Za-z0-9-]{16,64}$/;

/**
 * The id, created on first use. Null when storage is unavailable: a fresh id for every
 * application would read as a new device each time, so none is sent and velocity is recorded as
 * not established rather than as a clean result.
 */
export function deviceId(): string | null {
  try {
    const existing = window.localStorage.getItem(DEVICE_KEY);
    if (existing && DEVICE_ID.test(existing)) return existing;
    const fresh = crypto.randomUUID();
    window.localStorage.setItem(DEVICE_KEY, fresh);
    return window.localStorage.getItem(DEVICE_KEY) === fresh ? fresh : null;
  } catch {
    return null;
  }
}
