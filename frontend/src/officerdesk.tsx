/** The officer's review desk: a simulated bank check, what supports each payment, the timeline with the evidence
 *  behind it, suggested next steps, and the audit trail.
 *
 * Everything here reads the record or writes to the audit trail. Nothing changes the case, its totals or any
 * official status: a bank check is a labelled simulation beside the citizen's own confirmation, and a suggested
 * step does nothing until the officer presses the existing button it points at.
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ApiError, api, type AuditEvent, type CaseSnapshot, type OfficerDesk, type Proposal, type SupportLevel } from "./api";
import { ErrorNotice, useI18n } from "./ui";
import { wf } from "./workflowapi";


/** A mark and a word for every level, never colour alone. */
const TONE: Record<SupportLevel, { tone: string; mark: string }> = {
  bank_matched: { tone: "ok", mark: "✓" },
  bank_differs: { tone: "error", mark: "✕" },
  bank_not_found: { tone: "pending", mark: "!" },
  cannot_verify: { tone: "muted", mark: "?" },
  citizen_confirmed: { tone: "info", mark: "●" },
  ai_proposed: { tone: "pending", mark: "!" },
};
const LEVEL_WORDS: Record<SupportLevel, string> = {
  bank_matched: "Matches the simulated bank record", bank_differs: "Differs from the simulated bank record",
  bank_not_found: "Not in the simulated bank record", cannot_verify: "Can't verify",
  citizen_confirmed: "Confirmed by the citizen, not bank-checked", ai_proposed: "Read by the system, not yet confirmed",
};
const FIELD_WORDS: Record<string, string> = { amount: "Amount", currency: "Currency", direction: "Direction", status: "Status", date: "Date", "paid to": "Paid to" };
const RESULT_WORDS: Record<string, string> = { match: "Agrees", differs: "Differs", not_stated: "Not stated" };
const BANK_RESULT_WORDS: Record<string, string> = { matched: "matched", differs: "differs", not_found: "not found", cannot_verify: "could not be checked" };

function SupportBadge({ level, label }: { level: SupportLevel; label: string }) {
  const { t } = useI18n();
  const { tone, mark } = TONE[level];
  return <span className={`badge support-${tone}`}><span aria-hidden="true">{mark}</span> {t(label)}</span>;
}

export interface DeskParts { payments: ReactNode; steps: ReactNode; timeline: ReactNode }

export function OfficerDeskPanels({ caseId, snapshot, refresh, onDrafted, children }: {
  caseId: string; snapshot: CaseSnapshot; refresh: number; onDrafted: () => void; children: (parts: DeskParts) => ReactNode;
}) {
  const { t, language } = useI18n();
  const [desk, setDesk] = useState<OfficerDesk | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sourceName = new Map(snapshot.sources.map((s) => [s.id, s.displayFilename]));
  const clock = (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));

  useEffect(() => {
    api.officerDesk(caseId).then(setDesk).catch((e) => setError(e instanceof Error ? e.message : "Could not load the review desk"));
  }, [caseId, refresh]);

  const run = useCallback(async (key: string, work: () => Promise<OfficerDesk>) => {
    setBusy(key);
    setError(null);
    setMessage(null);
    try {
      setDesk(await work());
    } catch (e) {
      setError(e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : "That did not work");
    } finally {
      setBusy(null);
    }
  }, []);

  const bankCheck = () => run("bank", () => api.officerBankCheck(caseId));
  const suggest = () => run("suggest", () => api.officerNextActions(caseId));

  async function decide(set: string, p: Proposal, decision: "accepted" | "dismissed") {
    // Accepting is the officer's own choice, recorded; what it points at is still done with the button it names.
    if (decision === "accepted" && p.kind === "request_more_information") {
      // One way to ask the citizen: the same draft the Evidence tab makes, to be edited and approved in Messages.
      try {
        await wf.draftRequest(caseId, p.paymentIds ?? [], p.noteDraft ?? undefined);
        onDrafted();
        setMessage(t("A draft question is waiting in Messages. Edit it there and approve it to send it."));
      } catch (e) {
        setError(e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : "Could not prepare the question");
        return;
      }
    } else if (decision === "accepted" && p.kind !== "run_bank_check" && p.kind !== "wait") {
      setMessage(t("Recorded. Use the matching button in the resolution summary below to act on it."));
    }
    await run(`decide-${p.key}`, async () => {
      if (decision === "accepted" && p.kind === "run_bank_check") await api.officerBankCheck(caseId);
      return api.officerActionDecision(caseId, { proposalSetId: set, key: p.key, decision });
    });
  }

  function auditLine(e: AuditEvent): string {
    const d = e.detail as Record<string, string | undefined>;
    if (e.kind === "bank_check") return `${t("Simulated bank check")} — ${d.name ?? ""}: ${t(BANK_RESULT_WORDS[d.result ?? ""] ?? d.result ?? "")}`;
    if (e.kind === "next_actions_proposed") return `${t("Suggested next steps")} (${((e.detail as { proposals?: unknown[] }).proposals ?? []).length})`;
    if (e.kind === "action_decision") return `${t(d.decision === "accepted" ? "Accepted" : "Dismissed")}: ${d.title ?? ""}`;
    return `${t("Review recorded")}: ${(d.action ?? "").replace(/_/g, " ")}`;
  }

  if (!desk) return error ? <ErrorNotice message={error}/> : <p className="muted"> {t("Loading…")} </p>;
  const actions = desk.actions;
  const payments = <section className="card" aria-label={t("Payments and what supports them")}>
      <h2> {t("Payments and what supports them")} </h2>
      <p className="small muted"> {desk.notice} </p>
      <p className="small muted"> {t("A bank check is recorded in the audit log and changes nothing in the case: not the totals, not the citizen's confirmations, not any official status.")} </p>
      <div className="inline-form">
        <button type="button" disabled={busy !== null || desk.payments.length === 0} onClick={() => void bankCheck()}>
          {t(busy === "bank" ? "Checking…" : "Check against the simulated bank record")}
        </button>
      </div>
      {desk.payments.length === 0 && <p className="muted"> {t("No payments have been read from this case.")} </p>}
      <ul className="record-list">
        {desk.payments.map((p) => <li key={p.paymentId}>
          <span className="record-main">{p.name}</span>
          <SupportBadge level={p.level} label={p.label}/>
          <ul className="plain-list small">{p.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          {p.bank && p.bank.fields.length > 0 && <details>
            <summary> {t("Compare with the simulated bank record")} </summary>
            <table className="compare">
              <thead><tr><th scope="col"> {t("Detail")} </th><th scope="col"> {t("In the case")} </th><th scope="col"> {t("Simulated bank record")} </th><th scope="col"> {t("Result")} </th></tr></thead>
              <tbody>{p.bank.fields.map((f) => <tr key={f.field}>
                <th scope="row">{t(FIELD_WORDS[f.field] ?? f.field)}</th>
                <td>{f.claimed ?? t("not stated")}</td><td>{f.bank}</td>
                <td className={f.result === "differs" ? "differs" : undefined}>{t(RESULT_WORDS[f.result])}</td>
              </tr>)}</tbody>
            </table>
          </details>}
        </li>)}
      </ul>
    </section>;

  const steps = <section className="card" aria-label={t("Suggested next steps")}>
      <h2> {t("Suggested next steps")} </h2>
      <p className="small muted"> {t("Suggestions only. You decide, and what you decide is recorded. A suggestion never acts on the case by itself.")} </p>
      <div className="inline-form">
        <button type="button" className="secondary" disabled={busy !== null} onClick={() => void suggest()}>
          {t(busy === "suggest" ? "Working out next steps…" : actions ? "Suggest again" : "Suggest next steps")}
        </button>
      </div>
      {message && <p className="small" role="status"> {message} </p>}
      {actions && <>
        <p className="small muted">
          <span className={`badge ${actions.aiMode === "fixture" ? "badge-fixture" : "badge-live"}`}>
            {t(actions.aiMode === "fixture" ? "Replayed demo (not live)" : "Live Meta model")}</span>
          {" "}{clock(actions.generatedAt)}{actions.notice ? ` · ${actions.notice}` : ""}
        </p>
        {actions.stale && <p className="small"> {t("The case has changed since these were written. Suggest again to include the change.")} </p>}
        <ul className="record-list">
          {actions.proposals.map((p) => <li key={p.key}>
            <span className="record-main">{p.title}</span>
            <span>{p.reason}</span>
            {p.noteDraft && <blockquote className="note-draft">{p.noteDraft}</blockquote>}
            <span className="small muted">{t(p.origin === "model" ? "Written by the model, checked by code" : "From fixed rules")}</span>
            {p.decision ? <span className="badge badge-user_confirmed"> {t(p.decision === "accepted" ? "You accepted this" : "You set this aside")} </span>
              : !actions.stale && <span className="inline-form">
                <button type="button" disabled={busy !== null} onClick={() => void decide(actions.id, p, "accepted")}> {t(p.kind === "run_bank_check" ? "Check now" : "Use this")} </button>
                <button type="button" className="secondary" disabled={busy !== null} onClick={() => void decide(actions.id, p, "dismissed")}> {t("Not now")} </button>
              </span>}
          </li>)}
        </ul>
      </>}
    </section>;

  const timeline = <><section className="card" aria-label={t("Timeline")}>
      <h2> {t("Timeline")} </h2>
      <p className="small muted"> {t("What happened to this case, oldest first, with the evidence behind each payment.")} </p>
      <ol className="timeline">
        {desk.timeline.map((item, i) => <li key={`${item.kind}-${i}`} className={item.level ? `tl-${TONE[item.level].tone}` : undefined}>
          <time dateTime={item.at}>{clock(item.at)}</time>
          <span className="record-main">{item.title}</span>
          {item.level && <SupportBadge level={item.level} label={LEVEL_WORDS[item.level]}/>}
          {item.detail && (!item.level || item.kind === "bank_check") && <span className="small muted">{item.detail}</span>}
          {item.sourceIds.length > 0 && <span className="small">
            {item.sourceIds.map((id) => <a key={id} className="evidence-link" href={`/api/officer/cases/${caseId}/sources/${id}`} target="_blank" rel="noreferrer">
              {t("View")} {sourceName.get(id) ?? t("evidence")} ↗</a>)}
          </span>}
        </li>)}
      </ol>
    </section>

    <details className="card">
      <summary><strong> {t("Audit log")} </strong> ({desk.audit.length})</summary>
      <p className="small muted"> {t("Every bank check, set of suggestions, decision on one, and review recorded on this case. Entries are never changed or removed.")} </p>
      <ul className="plain-list">
        {desk.audit.map((e) => <li key={e.id}><time dateTime={e.createdAt}>{clock(e.createdAt)}</time> · {e.actorRole} · {auditLine(e)}</li>)}
      </ul>
    </details></>;
  return <>{error && <ErrorNotice message={error}/>}{children({ payments, steps, timeline })}</>;
}
