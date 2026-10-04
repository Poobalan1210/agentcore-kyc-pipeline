import { useCallback, useEffect, useRef, useState } from "react";
import {
  AccountState,
  AccountView,
  api,
  ApiError,
  ApplicantNotification,
  TransactionRecord,
  TransactionType,
} from "@shared/api/client";
import { Icon, IconName } from "../console/icons";
import { AppScreen, HomeHeader } from "./AppShell";
import {
  ACCOUNT_COPY,
  ACTION_COPY,
  BANK_NAME,
  HOME_COPY,
  PRODUCT_COPY,
  TRANSACTION_LABELS,
} from "./copy";

const POLL_MS = 4000;

/**
 * The one-tap actions, each with a fixed amount, as a banking app's home offers them. Each sends a
 * real transaction to the account platform, which allows or refuses it; the app never decides.
 */
const ACTIONS: Array<{ type: TransactionType; amount: number; icon: IconName; blockedBy: string }> = [
  { type: "DEPOSIT", amount: 100, icon: "plus", blockedBy: "DEPOSITS_BLOCKED" },
  { type: "INTERNATIONAL_TRANSFER", amount: 250, icon: "globe", blockedBy: "INTERNATIONAL_TRANSFERS_BLOCKED" },
  { type: "THIRD_PARTY_PAYMENT", amount: 50, icon: "person", blockedBy: "THIRD_PARTY_PAYMENTS_BLOCKED" },
  { type: "CASH_WITHDRAWAL", amount: 40, icon: "account", blockedBy: "CASH_WITHDRAWALS_BLOCKED" },
];

const STATUS_CHIP: Record<AccountState, string> = {
  OPEN_RESTRICTED: HOME_COPY.statusLimited,
  OPEN_ENHANCED: HOME_COPY.statusActive,
  OPEN_STANDARD: HOME_COPY.statusActive,
  FROZEN: HOME_COPY.statusFrozen,
};

interface Sheet {
  tone: "ok" | "no";
  title: string;
  text: string;
}

/**
 * The account, as the platform that enforces it holds it — R31.2, R31.9.
 *
 * This is where the demo shows a restriction is real: the customer taps *Send abroad* on a
 * provisional account and the account platform itself refuses, naming what is limited. The page
 * then keeps reading the account, so the moment Phase 2 lifts the limits — or a list update puts
 * the account on hold — the customer sees it without refreshing.
 *
 * Everything shown comes from `GET /accounts/{id}`, which gives the applicant what they can do
 * and never why: no holds, no platform events, no decision ids.
 */
export function AccountPanel({
  applicantId,
  notifications,
  firstName,
  productApplied,
}: {
  applicantId: string;
  notifications: ApplicantNotification[];
  firstName?: string | null;
  productApplied?: string | null;
}) {
  const [account, setAccount] = useState<AccountView | null>(null);
  const [transactions, setTransactions] = useState<TransactionRecord[]>([]);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState<TransactionType | null>(null);
  const [sheet, setSheet] = useState<Sheet | null>(null);

  const refresh = useCallback(async () => {
    try {
      const listed = await api.listTransactions(applicantId);
      setAccount(listed.account);
      setTransactions(listed.transactions);
      setLoadError(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // The account opens right after the decision is recorded, so a 404 here is a moment:
        // the "being set up" line stays until the next poll finds it.
        setLoadError(false);
      } else {
        setLoadError(true);
        // eslint-disable-next-line no-console
        console.error(err);
      }
    }
  }, [applicantId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  // A confirmation goes by itself; a refusal stays until it is dismissed, so it can be read out.
  useEffect(() => {
    if (sheet?.tone !== "ok") return;
    const id = setTimeout(() => setSheet(null), 4000);
    return () => clearTimeout(id);
  }, [sheet]);

  // A message that arrives while the account is on screen is marked new, as an inbox would.
  const seenRef = useRef<Set<string> | null>(null);
  if (seenRef.current === null) {
    seenRef.current = new Set(notifications.map(messageKey));
  }

  async function act(type: TransactionType, amount: number) {
    setBusy(type);
    setSheet(null);
    try {
      const result = await api.createTransaction(applicantId, { type, amount });
      const copy = ACTION_COPY[type];
      if (result.allowed) {
        setSheet({ tone: "ok", title: `${copy?.done ?? "Done"} ${money(String(amount))}`, text: "" });
      } else {
        setSheet({
          tone: "no",
          title: HOME_COPY.refusedTitle,
          text:
            result.reasonCode === "INSUFFICIENT_FUNDS"
              ? HOME_COPY.insufficient
              : result.reasonCode === "DEPOSIT_CEILING_EXCEEDED"
                ? HOME_COPY.ceiling
                : sentence(result.reason ?? ""),
        });
      }
      refresh();
    } catch (err) {
      setSheet({ tone: "no", title: HOME_COPY.refusedTitle, text: HOME_COPY.unavailable });
      // eslint-disable-next-line no-console
      console.error(err);
    } finally {
      setBusy(null);
    }
  }

  const copy = account ? ACCOUNT_COPY[account.state] : null;
  const frozen = account?.state === "FROZEN";
  const restrictions = new Set(account?.restrictions ?? []);
  const unread = notifications.some((n) => !seenRef.current?.has(messageKey(n)));
  const product = PRODUCT_COPY[productApplied ?? "current_account"]?.title ?? "Account";

  // What an action came back with sits at the bottom of the screen, over the content, as a banking
  // app's confirmation does: in view wherever the page is scrolled. The live region is always
  // there, empty in between, so a screen reader announces each answer as it arrives.
  const snack = account ? (
    <div className="app-snack-region" aria-live="polite">
      {sheet ? (
        <div className={`app-sheet app-sheet--${sheet.tone} app-sheet--snack`}>
          <Icon name={sheet.tone === "ok" ? "check" : "lock"} size={18} />
          <span className="app-sheet__text">
            <strong>{sheet.title}</strong>
            {sheet.text}
          </span>
          <button
            type="button"
            className="app-sheet__close"
            aria-label={HOME_COPY.dismiss}
            onClick={() => setSheet(null)}
          >
            <Icon name="close" size={15} />
          </button>
        </div>
      ) : null}
    </div>
  ) : undefined;

  return (
    <AppScreen header={<HomeHeader unread={unread} />} footer={snack}>
      <p className="app-greeting__hello">
        {firstName ? `${HOME_COPY.hello} ${firstName}` : BANK_NAME}
      </p>
      <h2 className="app-title">{copy?.title ?? HOME_COPY.settingUp}</h2>
      <p className="app-lead">
        {copy ? copy.body : loadError ? HOME_COPY.unavailable : HOME_COPY.settingUp}
      </p>

      {account ? (
        <>
          <div className={"app-account" + (frozen ? " app-account--frozen" : "")}>
            <div className="app-account__top">
              <span>{product}</span>
              <span className="app-account__chip">{STATUS_CHIP[account.state]}</span>
            </div>
            <p className="app-account__label">{HOME_COPY.balance}</p>
            <p className="app-account__balance">{money(account.balance, account.limits.currency)}</p>
            {account.limits.depositsRemaining !== null && !frozen ? (
              <p className="app-account__sub">
                {HOME_COPY.depositsRemaining}{" "}
                {money(account.limits.depositsRemaining, account.limits.currency)}
              </p>
            ) : null}
          </div>

          <section className="app-section">
            <h3>{HOME_COPY.actionsHeading}</h3>
            <div className="app-actions">
              {ACTIONS.map((a) => {
                const locked = frozen || restrictions.has(a.blockedBy);
                return (
                  <button
                    key={a.type}
                    type="button"
                    className="app-action"
                    disabled={busy !== null}
                    onClick={() => act(a.type, a.amount)}
                  >
                    <span className="app-action__icon">
                      <Icon name={a.icon} size={22} />
                      {locked ? (
                        <span className="app-action__lock" aria-label="Limited">
                          <Icon name="lock" size={11} />
                        </span>
                      ) : null}
                    </span>
                    {ACTION_COPY[a.type]?.label ?? a.type}
                  </button>
                );
              })}
            </div>
          </section>

          {account.restrictionDetails.length > 0 && !frozen ? (
            <section className="app-section">
              <h3>{HOME_COPY.limitsHeading}</h3>
              <ul className="app-limits">
                {account.restrictionDetails.map((r) => (
                  <li key={r.code}>
                    <Icon name="lock" size={15} /> {r.description}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <section className="app-section">
            <h3>{HOME_COPY.activityHeading}</h3>
            {transactions.length > 0 ? (
              <ul className="app-activity">
                {transactions.map((t) => (
                  <li key={t.transactionId}>
                    <span className="app-activity__icon">
                      <Icon name={iconFor(t.type)} size={17} />
                    </span>
                    <span className="app-activity__text">
                      {TRANSACTION_LABELS[t.type as TransactionType] ?? t.type}
                      <small>
                        {t.allowed ? HOME_COPY.completed : HOME_COPY.notAvailable} · {formatTime(t.at)}
                      </small>
                    </span>
                    <span
                      className={
                        "app-activity__amount" +
                        (!t.allowed
                          ? " app-activity__amount--refused"
                          : t.type === "DEPOSIT"
                            ? " app-activity__amount--in"
                            : "")
                      }
                    >
                      {t.type === "DEPOSIT" ? "+" : "−"}
                      {money(t.amount, t.currency)}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="app-empty">{HOME_COPY.noActivity}</p>
            )}
          </section>
        </>
      ) : null}

      {notifications.length > 0 ? (
        <section className="app-section">
          <h3>{HOME_COPY.messagesHeading}</h3>
          <ul className="app-inbox">
            {[...notifications].reverse().map((n) => {
              const fresh = !seenRef.current?.has(messageKey(n));
              return (
                <li key={messageKey(n)} className={fresh ? "app-inbox--new" : undefined}>
                  <span className="app-inbox__icon">
                    <Icon name="bell" size={17} />
                  </span>
                  <span>
                    <span className="app-inbox__text">{n.text}</span>
                    {fresh ? <span className="app-inbox__badge">{HOME_COPY.newBadge}</span> : null}
                    <time dateTime={n.at}>{formatTime(n.at)}</time>
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
    </AppScreen>
  );
}

function messageKey(n: ApplicantNotification): string {
  return `${n.at}|${n.kind}`;
}

function iconFor(type: string): IconName {
  switch (type) {
    case "DEPOSIT":
      return "plus";
    case "INTERNATIONAL_TRANSFER":
      return "globe";
    case "THIRD_PARTY_PAYMENT":
      return "person";
    default:
      return "account";
  }
}

function sentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  const capital = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

function money(value: string, currency = "USD"): string {
  const n = Number(value);
  if (Number.isNaN(n)) return value;
  return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(n);
}

function formatTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
