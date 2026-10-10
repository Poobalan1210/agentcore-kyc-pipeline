/**
 * The console's icons: simple 24x24 stroke drawings in `currentColor`, inline so the console
 * needs no icon package and no network request. Decorative unless a `title` is given.
 */

import { ReactNode } from "react";

export type IconName =
  | "logo"
  | "live"
  | "review"
  | "cases"
  | "controls"
  | "signOut"
  | "check"
  | "alert"
  | "x"
  | "clock"
  | "plus"
  | "close"
  | "chevron"
  | "device"
  | "face"
  | "document"
  | "queue"
  | "agent"
  | "rules"
  | "account"
  | "lock"
  | "bell"
  | "eye"
  | "person"
  | "globe"
  | "skip"
  | "shield"
  | "arrow"
  | "info"
  | "text";

const PATHS: Record<IconName, ReactNode> = {
  logo: (
    <>
      <path d="M12 2.8 4.5 5.6v5.9c0 4.6 3.1 8.6 7.5 9.7 4.4-1.1 7.5-5.1 7.5-9.7V5.6L12 2.8Z" />
      <path d="m8.6 12.1 2.4 2.4 4.6-4.8" />
    </>
  ),
  live: (
    <>
      <path d="M3 12h3.5l2.2-5.5 4.6 11 2.2-5.5H21" />
    </>
  ),
  review: (
    <>
      <path d="M4 13.5V18a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4.5" />
      <path d="M4 13.5 6.4 5.6A2 2 0 0 1 8.3 4.2h7.4a2 2 0 0 1 1.9 1.4l2.4 7.9" />
      <path d="M4 13.5h4.2l1.3 2.2h5l1.3-2.2H20" />
    </>
  ),
  cases: (
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M3.5 9.5h17M9 9.5v10" />
    </>
  ),
  controls: (
    <>
      <path d="M5 4v6M5 14v6M12 4v3M12 11v9M19 4v9M19 17v3" />
      <path d="M3 12h4M10 9h4M17 15h4" />
    </>
  ),
  signOut: (
    <>
      <path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" />
      <path d="M10 16.5 5.5 12 10 7.5M5.5 12H16" />
    </>
  ),
  check: <path d="m5 12.5 4.4 4.4L19 7.4" />,
  alert: (
    <>
      <path d="M12 4 2.8 19.5h18.4L12 4Z" />
      <path d="M12 10v4.2M12 17.2v.1" />
    </>
  ),
  x: <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="M7.5 7.5l9 9M16.5 7.5l-9 9" />,
  chevron: <path d="m9.5 6 6 6-6 6" />,
  device: (
    <>
      <rect x="6.5" y="2.5" width="11" height="19" rx="2.5" />
      <path d="M10.5 18.5h3" />
    </>
  ),
  face: (
    <>
      <path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2" />
      <circle cx="12" cy="10.5" r="2.8" />
      <path d="M7.8 17c.8-1.9 2.4-3 4.2-3s3.4 1.1 4.2 3" />
    </>
  ),
  document: (
    <>
      <rect x="3.5" y="5" width="17" height="14" rx="2" />
      <circle cx="9" cy="11" r="2.2" />
      <path d="M6 16c.6-1.3 1.7-2 3-2s2.4.7 3 2M14 10h4M14 13h3" />
    </>
  ),
  queue: (
    <>
      <path d="M4 7h11M4 12h11M4 17h11" />
      <path d="m17 9.5 3 2.5-3 2.5" />
    </>
  ),
  agent: (
    <>
      <rect x="5.5" y="7.5" width="13" height="11" rx="3" />
      <path d="M12 7.5V4.5M9.5 12.5v.1M14.5 12.5v.1M9.5 15.5h5M3 12v2.5M21 12v2.5" />
    </>
  ),
  rules: (
    <>
      <path d="M12 4v16M7 20h10M5 7h14" />
      <path d="M7 7 4.5 13a3 3 0 0 0 5 0L7 7ZM17 7l-2.5 6a3 3 0 0 0 5 0L17 7Z" />
    </>
  ),
  account: (
    <>
      <path d="M3.5 9.5 12 4.5l8.5 5" />
      <path d="M5.5 10v7M10 10v7M14 10v7M18.5 10v7M3.5 19.5h17" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="10.5" width="14" height="10" rx="2" />
      <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5M12 14.5v2" />
    </>
  ),
  bell: (
    <>
      <path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 1.5H5l1.5-1.5Z" />
      <path d="M10 20.5a2.2 2.2 0 0 0 4 0" />
    </>
  ),
  eye: (
    <>
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="2.8" />
    </>
  ),
  person: (
    <>
      <circle cx="12" cy="8.5" r="3.5" />
      <path d="M5 20c1-3.6 3.8-5.5 7-5.5s6 1.9 7 5.5" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17M12 3.5c2.4 2.4 3.5 5.3 3.5 8.5s-1.1 6.1-3.5 8.5c-2.4-2.4-3.5-5.3-3.5-8.5S9.6 5.9 12 3.5Z" />
    </>
  ),
  skip: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8 12h8" />
    </>
  ),
  shield: <path d="M12 2.8 4.5 5.6v5.9c0 4.6 3.1 8.6 7.5 9.7 4.4-1.1 7.5-5.1 7.5-9.7V5.6L12 2.8Z" />,
  arrow: <path d="M4.5 12h15M13.5 6l6 6-6 6" />,
  info: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5.2M12 7.8v.1" />
    </>
  ),
  // A large and a small A: the size of the type.
  text: (
    <>
      <path d="M3 18.5 7.7 5.5h1.1l4.7 13M4.8 14h7" />
      <path d="M14.2 18.5 17.3 10h.9l3.1 8.5M15.3 15.6h5" />
    </>
  ),
};

export function Icon({
  name,
  size = 18,
  title,
  className,
}: {
  name: IconName;
  size?: number;
  title?: string;
  className?: string;
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}
      {PATHS[name]}
    </svg>
  );
}
