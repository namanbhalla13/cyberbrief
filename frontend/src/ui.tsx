/** Shared across screens: presentation, language, the auth context and case loading. */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, Navigate, Outlet, useLocation } from "react-router-dom";
import { ApiError, api, type CaseSnapshot, type CaseSummary, type FactOut, type QuestionOut, type ReviewAction, type ReviewState, type Role, type SessionOut } from "./api";
import { adoptOwner, clearLocalDrafts, scopeOf } from "./draftstore";
import { hindi } from "./hi";


// ---- from components/money --------------------------------------------------------------------

/** Convert a typed rupee amount ("5,000", "5000.50") to an integer-paise string without floating point. */
export function rupeesToMinor(input: string): string | null {
  const cleaned = input.replace(/[₹,\s]/g, "").replace(/^(INR|Rs\.?)/i, "");
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(cleaned);
  if (!match) return null;
  const rupees = match[1].replace(/^0+(?=\d)/, "");
  const paise = (match[2] ?? "").padEnd(2, "0");
  const combined = `${rupees}${paise}`.replace(/^0+(?=\d)/, "");
  return combined === "" ? "0" : combined;
}

export const RECOVERY_LABELS: Record<string, string> = {
  not_assessed: "Not assessed (reviewed baseline incomplete)",
  no_refund_reported: "No refund reported",
  refund_reported_awaiting_review: "Refund reported, awaiting review",
  partial_recovery_reviewed: "Partial recovery reviewed",
  full_recovery_reported_awaiting_review: "Full recovery reported, review outstanding",
  reviewed_baseline_fully_offset: "Reviewed baseline fully offset by matched refunds",
  discrepancy_needs_review: "Discrepancy needs review",
};

export const STATUS_CATEGORY_LABELS: Record<string, string> = {
  acknowledged: "Acknowledged",
  information_requested: "Information requested",
  forwarded: "Forwarded",
  under_review_or_investigation: "Under review or investigation",
  closure_reported: "Closure reported",
  other: "Other (see wording)",
  unknown: "Unknown",
};

export function effectiveDateText(effectiveAt: Record<string, unknown> | null): string {
  if (!effectiveAt) return "unknown";
  if (effectiveAt.kind === "unresolved") return `unresolved (${String(effectiveAt.issue ?? "")}: "${String(effectiveAt.rawText ?? "")}")`;
  return String(effectiveAt.date ?? effectiveAt.localDatetime ?? effectiveAt.instantUtc ?? effectiveAt.rawText ?? "unknown");
}


// ---- from components/MoneyText ----------------------------------------------------------------

/** Formats integer paise (as a decimal string) with Indian digit grouping. No floating point. */
export function formatInr(minor: string | null | undefined, fallback = "not stated"): string {
  if (minor === null || minor === undefined || minor === "") return fallback;
  if (!/^-?\d+$/.test(minor)) return "unreadable";
  const negative = minor.startsWith("-");
  const digits = negative ? minor.slice(1) : minor;
  const padded = digits.padStart(3, "0");
  const rupees = padded.slice(0, -2);
  const paise = padded.slice(-2);
  let grouped = rupees;
  if (rupees.length > 3) {
    const tail = rupees.slice(-3);
    let head = rupees.slice(0, -3);
    const groups: string[] = [];
    while (head.length > 2) {
      groups.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) groups.unshift(head);
    grouped = [...groups, tail].join(",");
  }
  return `${negative ? "-" : ""}₹${grouped}.${paise}`;
}

export function MoneyText({ minor, fallback }: { minor: string | null | undefined; fallback?: string }) {
  return <span className="money">{formatInr(minor, fallback)}</span>;
}


// ---- from components/Badges -------------------------------------------------------------------

const REVIEW_LABELS: Record<ReviewState, string> = {
    unreviewed: "Needs review",
    user_confirmed: "Confirmed by you",
    reviewer_checked: "Checked by reviewer",
    unresolved: "Left unresolved",
    excluded: "Excluded",
};
const ISSUE_LABELS: Record<string, string> = {
    missing: "Not visible in source",
    unreadable: "Unreadable",
    ambiguous: "Ambiguous",
    conflicting: "Conflicting sources",
};
export function ReviewStateBadge({ state }: {
    state: ReviewState;
}) {
    const { t } = useI18n();
    return <span className={`badge badge-${state}`}> {t(REVIEW_LABELS[state])} </span>;
}
export function IssueBadge({ issue }: {
    issue: string | null;
}) {
    const { t } = useI18n();
    if (!issue)
        return null;
    return <span className="badge badge-issue"> {t(ISSUE_LABELS[issue] ?? issue)} </span>;
}
export function AiModeBadge({ mode }: {
    mode: string;
}) {
    const { t } = useI18n();
    const fixture = mode === "fixture";
    return (<span className={`badge ${fixture ? "badge-fixture" : "badge-live"}`} title={t(fixture ? "Replayed labelled fixture; not a live model call" : "Live Meta model call")}>
      {t(fixture ? "Fixture mode (not live)" : "Live extraction")}
    </span>);
}
export function CategoryBadge({ category, reason }: {
    category: string;
    reason: string | null;
}) {
    const { t } = useI18n();
    return (<span className={`badge badge-cat-${category}`} title={reason ?? undefined}>
      {t(category.replace(/_/g, " "))}
    </span>);
}
export function DemoBanner() {
    const { t } = useI18n();
    return (<aside className="notice official-help" role="note">
      <span><strong> {t("Lost money to a real scam?")} </strong> {t("Report it now. Do not wait to finish this demo.")} </span>
      <div className="help-links"><a href="tel:1930"> {t("Call 1930")} </a><a href="https://cybercrime.gov.in/" target="_blank" rel="noreferrer noopener"> {t("Official reporting portal \u2197")} </a></div>
    </aside>);
}


// ---- from components/JourneySteps -------------------------------------------------------------

export function JourneySteps({ current }: { current: 1 | 2 | 3 }) {
  const { t } = useI18n();
  const labels = ["Tell us", "Add photos or audio", "Check and save"];
  return <nav aria-label={t("Your progress")} className="journey-progress">
    <p className="small muted"> {t("Step {step} of 3", { step: current })} </p>
    <ol>{labels.map((label, index) => <li key={label} aria-current={current === index + 1 ? "step" : undefined} className={index + 1 < current ? "complete" : ""}>
      <span aria-hidden="true">{index + 1 < current ? "✓" : index + 1}</span> {t(label)}
    </li>)}</ol>
  </nav>;
}


// ---- from components/ErrorNotice --------------------------------------------------------------

export function ErrorNotice({ message }: { message: string }) {
  const { language, t } = useI18n();
  const translated = t(message);
  if (language === "en" || translated !== message) return <p role="alert" className="error">{translated}</p>;
  let key = "Please check your details and try again.";
  if (/code.*(?:valid|expired)|otp/i.test(message)) key = "The code is not valid or has expired. Request a new code.";
  else if (/stale|revision|conflict/i.test(message)) key = "This report changed. Refresh it before saving again.";
  else if (/session|sign.in|unauthori/i.test(message)) key = "Please sign in again to continue.";
  else if (/file|image|wav|audio|upload/i.test(message)) key = "The file could not be accepted. Check its type and size.";
  else if (/rate|too many/i.test(message)) key = "Too many attempts. Please wait and try again.";
  else if (/fetch|network|connect/i.test(message)) key = "Could not connect. Check your connection and try again.";
  return <div role="alert" className="error"><p>{t(key)}</p><details><summary>{t("Error details")}</summary><p lang="en">{message}</p></details></div>;
}


// ---- from i18n/I18n ---------------------------------------------------------------------------

export type UiLanguage = "en" | "hi";
const STORAGE_KEY = "cyberbrief.language";
type Values = Record<string, string | number>;
type Translator = (text: string, values?: Values) => string;
const I18nContext = createContext<{ language: UiLanguage; setLanguage: (language: UiLanguage) => void; t: Translator } | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguage] = useState<UiLanguage>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "en" || saved === "hi") return saved;
    } catch { /* Private browsing may disable storage. */ }
    return navigator.language.toLowerCase().startsWith("hi") ? "hi" : "en";
  });
  useEffect(() => {
    document.documentElement.lang = language;
    try { localStorage.setItem(STORAGE_KEY, language); } catch { /* Keep the in-memory choice. */ }
  }, [language]);
  const t: Translator = (text, values) => {
    const translated = language === "hi" ? hindi[text] ?? text : text;
    return translated.replace(/\{(\w+)\}/g, (match, key: string) => values?.[key] === undefined ? match : String(values[key]));
  };
  return <I18nContext.Provider value={{ language, setLanguage, t }}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const context = useContext(I18nContext);
  if (!context) throw new Error("I18nProvider is missing");
  return context;
}

export function LanguageSwitch() {
  const { language, setLanguage } = useI18n();
  return <div className="language-switch" role="group" aria-label="Language / भाषा">
    <button type="button" lang="en" aria-pressed={language === "en"} onClick={() => setLanguage("en")}>English</button>
    <button type="button" lang="hi" aria-pressed={language === "hi"} onClick={() => setLanguage("hi")}>हिन्दी</button>
  </div>;
}


// ---- from i18n/questions ----------------------------------------------------------------------

/** Translate the app's deterministic questions, preserving references and quoted source text. */
export function questionText(question: Pick<QuestionOut, "kind" | "text">, language: UiLanguage): string {
  if (language === "en") return question.text;
  const text = question.text;
  let match: RegExpMatchArray | null;
  if ((match = text.match(/^(.+) and (.+) may show the same payment \(reference (.+)\)\. Can you confirm\?(.*)$/))) {
    return `${match[1]} और ${match[2]} शायद एक ही भुगतान हैं (रेफ़रेंस ${match[3]})। क्या आप पुष्टि कर सकते हैं?${match[4] ? " एक फ़ाइल में भुगतान करने वाले का खाता नहीं दिखता।" : ""}`;
  }
  if ((match = text.match(/^(.+) and (.+) share reference (.+) but show different amounts \((.+) and (.+)\)\./))) {
    return `${match[1]} और ${match[2]} में रेफ़रेंस ${match[3]} एक है, लेकिन रकम ${match[4]} और ${match[5]} अलग हैं। कौन सी सही है, या ये अलग भुगतान हैं?`;
  }
  if ((match = text.match(/^(.+) and (.+) share reference (.+) but show different statuses \((.+) and (.+)\)\./))) {
    const status = (s: string) => ({ successful: "सफल", pending: "लंबित", failed: "असफल" }[s] ?? s);
    return `${match[1]} और ${match[2]} में रेफ़रेंस ${match[3]} एक है, लेकिन स्थिति ${status(match[4])} और ${status(match[5])} अलग है। क्या यह बाद का अपडेट है या कोई गलती?`;
  }
  if ((match = text.match(/^On (.+), the (.+) (?:is|are) not readable\./))) {
    const fields: Record<string, string> = { amount: "रकम", currency: "मुद्रा", "transaction reference": "भुगतान रेफ़रेंस", "payment status": "भुगतान की स्थिति", direction: "पैसे भेजे या मिले", date: "तारीख़" };
    const names = match[2].split(/, | and /).map(s => fields[s] ?? s).join(", ");
    return `${match[1]} में ${names} साफ़ नहीं है। क्या आप यह जानकारी या साफ़ फ़ोटो दे सकते हैं?`;
  }
  if ((match = text.match(/^(.+) \((.+)\) is marked pending\./))) return `${match[1]} (${match[2]}) लंबित दिखता है। क्या यह सफल हुआ, असफल हुआ या अभी लंबित है?`;
  if ((match = text.match(/^(.+) is an incoming credit\./))) return `${match[1]} में पैसे मिले हैं। क्या ये इस रिपोर्ट के किसी भुगतान से जुड़े हैं?`;
  if ((match = text.match(/^You reported (.+), while the reviewed successful receipts total (.+)\. Is there another payment(.*)\?$/))) return `आपने ${match[1]} बताया, लेकिन जांचे गए सफल भुगतान ${match[2]} हैं। क्या कोई और भुगतान${match[3] ? " या लंबित भुगतान" : ""} है?`;
  if ((match = text.match(/^What date does “(.+)” refer to in your account\?$/))) return `आपकी बात में “${match[1]}” का मतलब कौन सी तारीख़ है?`;
  // Unknown future/provider wording remains visible as the original; never invent a translation.
  return text;
}


// ---- from features/auth/AuthProvider ----------------------------------------------------------

type Status = "loading" | "anonymous" | "authenticated";

interface AuthValue {
  status: Status;
  session: SessionOut | null;
  error: string | null;
  /** Ensures a session cookie exists, so the CSRF double-submit check can work on sign-in. */
  ensureSession: () => Promise<SessionOut>;
  setSession: (next: SessionOut) => void;
  logout: () => Promise<void>;
  /** Asks the server who is signed in now (another tab may have changed it). False when it is no longer the person this tab was showing. */
  revalidate: () => Promise<boolean>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSessionState] = useState<SessionOut | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const [error, setError] = useState<string | null>(null);
  const starting = useRef<Promise<SessionOut> | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Read the existing session only. A visitor who has never signed in should not have a
    // session minted just for opening the landing page.
    api
      .getSession()
      .then((s) => {
        if (cancelled) return;
        adoptOwner(scopeOf(s));
        setSessionState(s);
        setStatus(s.authenticated ? "authenticated" : "anonymous");
      })
      .catch((e) => {
        if (cancelled) return;
        if (!(e instanceof ApiError && e.status === 401)) setError(e instanceof Error ? e.message : "Session problem");
        setStatus("anonymous");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const ensureSession = useCallback(async () => {
    if (session) return session;
    // guard against two concurrent callers minting two sessions
    if (!starting.current) starting.current = api.startSession();
    try {
      const fresh = await starting.current;
      setSessionState(fresh);
      setStatus(fresh.authenticated ? "authenticated" : "anonymous");
      return fresh;
    } finally {
      starting.current = null;
    }
  }, [session]);

  const setSession = useCallback((next: SessionOut) => {
    adoptOwner(scopeOf(next));
    setSessionState(next);
    setStatus(next.authenticated ? "authenticated" : "anonymous");
    setError(null);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      clearLocalDrafts();   // the next person at this tab must not find this person's unsent words or report pointer
      setSessionState(null);
      setStatus("anonymous");
    }
  }, []);

  const revalidate = useCallback(async () => {
    const shown = scopeOf(session);
    try {
      const now = await api.getSession();
      const same = scopeOf(now) === shown;
      if (!same) { adoptOwner(scopeOf(now)); setSessionState(now); setStatus(now.authenticated ? "authenticated" : "anonymous"); }
      return same;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { clearLocalDrafts(); setSessionState(null); setStatus("anonymous"); return false; }   // the session ended
      return true;   // a network problem is not proof that the person changed
    }
  }, [session]);

  // another tab may have signed someone else in (the cookie is shared); check again whenever this one is looked at
  useEffect(() => {
    const check = () => { if (document.visibilityState === "visible" && scopeOf(session)) void revalidate(); };
    window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", check);
    return () => { window.removeEventListener("focus", check); document.removeEventListener("visibilitychange", check); };
  }, [session, revalidate]);

  const value = useMemo(
    () => ({ status, session, error, ensureSession, setSession, logout, revalidate }),
    [status, session, error, ensureSession, setSession, logout, revalidate],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}


// ---- from features/auth/RequireAuth -----------------------------------------------------------

/**
 * Gate for signed-in routes. Supplies the outlet context the case pages read, so pages below
 * can treat `session` as present.
 *
 * `kind` is omitted for the whole citizen area on purpose: an officer opening a case link
 * should see the wrong-role notice from the page, not be bounced out of the app.
 */
export function RequireAuth({ kind }: {
    kind?: Role;
}) {
    const { t } = useI18n();
    const { status, session } = useAuth();
    const location = useLocation();
    if (status === "loading")
        return <p className="muted"> {t("Checking your sign-in\u2026")} </p>;
    if (status !== "authenticated" || !session) {
        return <Navigate to={`/login?next=${encodeURIComponent(location.pathname)}`} replace/>;
    }
    if (kind && session.accountKind !== kind) {
        const home = session.accountKind === "officer" ? "/officer" : session.accountKind === "admin" ? "/admin" : "/home";
        return (<section className="card" role="alert">
        <h1> {t("This screen is for")} </h1>
        <p> {t("You are signed in as")}
          .
        </p>
        <div className="actions">
          <Link className="button" to={home}> {t("Go to your home screen")} </Link>
        </div>
      </section>);
    }
    return <Outlet context={{ session }}/>;
}


// ---- from features/case/caseReference ---------------------------------------------------------

/**
 * A short reference people can quote on the phone or write on a form. Derived from the case id,
 * so it needs no column of its own and cannot drift out of step with the record.
 *
 * It is deliberately not an official complaint number, and every screen that shows it says so.
 */
export function caseReference(caseId: string): string {
  return `CB-${caseId.replace(/-/g, "").slice(0, 6).toUpperCase()}`;
}


// ---- from features/complaint/nextStep ---------------------------------------------------------

const REVIEW_STATE_LABELS: Record<string, string> = {
  private: "Not submitted",
  submitted: "With an officer",
  returned: "More information requested",
  reviewed: "Packet reviewed",
  closed: "Closed",
};

/**
 * What this complaint is waiting on, in the words a person would use, and the colour that goes
 * with it. The two are derived together: a badge coloured by `reviewState` while its text came
 * from the draft ladder used to show a green "reviewed" tint behind "Add your receipts".
 *
 * Shared by the home dashboard and the tracking list so a phrase cannot drift between them.
 */
export function nextStep(c: CaseSummary): { text: string; tone: string } {
  if (c.reviewState === "private") {
    if (c.sourceCount === 0) return { text: "Add your evidence", tone: "draft" };
    if (c.activeRunId || c.packetStatus === "processing") return { text: "Reading your evidence…", tone: "draft" };
    if (c.packetStatus !== "ready_for_export") return { text: "A few questions to answer", tone: "submitted" };
    return { text: "Ready to submit", tone: "draft" };
  }
  return { text: REVIEW_STATE_LABELS[c.reviewState] ?? c.reviewState, tone: c.reviewState };
}

/** A complaint still in the citizen's hands, needing something from them before it can be sent. */
export function needsYou(c: CaseSummary): boolean {
  return c.reviewState === "private" ? c.sourceCount > 0 && !c.activeRunId && c.packetStatus !== "ready_for_export"
       : c.reviewState === "returned";
}

/** Sum a money field across cases, in integer paise, without floating point. */
export function sumMinor(cases: CaseSummary[], pick: (c: CaseSummary) => string | null): string | null {
  const present = cases.map(pick).filter((v): v is string => v !== null && /^-?\d+$/.test(v));
  if (present.length === 0) return null;
  return present.reduce((total, v) => total + BigInt(v), 0n).toString();
}


// ---- from features/case/useCase ---------------------------------------------------------------

/** Loads a case snapshot and exposes reload + single-action review helpers with stale-revision handling. */
export function useCase(caseId: string | undefined) {
  const [snapshot, setSnapshot] = useState<CaseSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!caseId) return;
    try {
      setSnapshot(await api.getCase(caseId));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404 ? "Case not found in this session." : e instanceof Error ? e.message : "Could not load the case");
    }
  }, [caseId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const review = useCallback(
    async (actions: ReviewAction[]) => {
      if (!caseId || !snapshot) return;
      setBusy(true);
      try {
        setSnapshot(await api.review(caseId, snapshot.case.currentRevision, actions));
        setError(null);
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          setError(`${e.message} ${e.nextStep ?? ""}`.trim());
          await reload();
        } else {
          setError(e instanceof Error ? e.message : "Review failed");
        }
      } finally {
        setBusy(false);
      }
    },
    [caseId, snapshot, reload],
  );

  return { snapshot, error, busy, reload, review, setSnapshot, setError };
}


// ---- from features/review/factDisplay ---------------------------------------------------------

/** One extracted field as a display string. Shared by the review, case, recovery and reviewer screens. */
export function factDisplay(fact: FactOut): string {
  const v = fact.value;
  if (!v) return fact.rawText ? `“${fact.rawText}” (not interpreted)` : "—";
  if (fact.kind === "amount" || fact.kind === "citizen_reported_amount") return formatInr(v.amountMinor as string);
  if (fact.kind === "citizen_outstanding_statement")
    return v.outstandingMinor === "0" ? "Nothing outstanding" : v.outstandingMinor ? formatInr(v.outstandingMinor as string) : "Unsure";
  if (fact.kind === "currency") return String(v.code ?? "—");
  if (fact.kind === "occurred_at") return String(v.instantUtc ?? v.localDatetime ?? v.date ?? fact.rawText ?? "—");
  if (fact.kind === "reference") return `${v.value}${v.truncated ? " (truncated)" : ""}`;
  return String(v.value ?? fact.rawText ?? "—");
}
