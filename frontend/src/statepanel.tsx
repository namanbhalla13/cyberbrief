/** One place for the states a page can be in besides "showing its content": loading, empty, error, stale, unavailable, denied.
 *
 * Each says in words what happened and offers the one useful next step. Loading and errors are announced; nothing relies on colour alone.
 */
import type { ReactNode } from "react";
import { useI18n } from "./ui";

export type PanelKind = "loading" | "empty" | "error" | "stale" | "unavailable" | "denied";

const WORDS: Record<PanelKind, string> = {
  loading: "Loading…", empty: "Nothing here yet.", error: "That did not work.", stale: "This may be out of date.",
  unavailable: "This is not available right now.", denied: "You do not have access to this.",
};

export function StatePanel({ kind, message, action, children }: { kind: PanelKind; message?: string; action?: { label: string; run: () => void }; children?: ReactNode }) {
  const { t } = useI18n();
  const alert = kind === "error" || kind === "denied";
  return <div className={`state-panel state-${kind}`} role={alert ? "alert" : "status"} aria-live={alert ? "assertive" : "polite"}>
    <p><strong>{t(WORDS[kind])}</strong>{message ? <> {message}</> : null}</p>
    {children}
    {action && <button type="button" className="secondary" onClick={action.run}> {t(action.label)} </button>}
  </div>;
}
