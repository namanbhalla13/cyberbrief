/** Officer-to-officer consultation: the question one officer asks the officer who holds an earlier report, the answer, and the inbox that shows both.
 *
 * Sending is always the sender's own approval of the exact words shown. The person asked sees only that question, the two report references and the
 * identifier both reports name; never the sender's case. An answer is an officer's account of their own report and is labelled so.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "./api";
import { Icon, IconButton, PageHead } from "./icons";
import { StatePanel } from "./statepanel";
import { useI18n } from "./ui";
import { consult, investigate, type Consultation, type ConsultationItem, type ConsultationSummary } from "./workflowapi";

const message = (e: unknown, fallback: string) => (e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : e instanceof Error ? e.message : fallback);

export const STATE_WORDS: Record<ConsultationItem["state"], string> = { draft: "Draft: not sent", sent: "Sent, waiting for an answer", replied: "Answered", closed: "Closed", cancelled: "Withdrawn" };
const clock = (iso: string, language: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
const newKey = () => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `k${Date.now()}${Math.random().toString(16).slice(2)}`);

/** How many things wait on this officer: new questions and answers, and questions still to answer. Polled gently; failures leave the last number. */
export function useConsultSummary(): ConsultationSummary | null {
  const [s, setS] = useState<ConsultationSummary | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => consult.summary().then((v) => { if (live) setS(v); }).catch(() => undefined);
    load();
    const timer = window.setInterval(load, 20000);
    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    window.addEventListener("cb-consult-changed", onFocus);
    return () => { live = false; window.clearInterval(timer); window.removeEventListener("focus", onFocus); window.removeEventListener("cb-consult-changed", onFocus); };
  }, []);
  return s;
}
const announce = () => window.dispatchEvent(new Event("cb-consult-changed"));

/** The sidebar entry: where pending questions and answers are, with a count that is also spoken. */
export function InboxLink() {
  const { t } = useI18n();
  const s = useConsultSummary();
  const n = s?.waiting ?? 0;
  return <NavLink to="/officer/inbox"><Icon name="mail"/> {t("Inbox")}{n > 0 && <span className="navcount" aria-label={t("{n} waiting", { n })}>{n}</span>}</NavLink>;
}

/** A notice on the desk for each question waiting for this officer; each one opens that conversation, not a list. */
export function DeskNotices() {
  const { t } = useI18n();
  const [items, setItems] = useState<ConsultationItem[]>([]);
  useEffect(() => {
    let live = true;
    consult.list("all").then((l) => { if (live) setItems(l.items.filter((i) => i.unread || i.awaitingYourReply)); }).catch(() => undefined);
    return () => { live = false; };
  }, []);
  if (items.length === 0) return null;
  return <section className="card" aria-label={t("Questions from other officers")}>
    <h2>{t("Waiting in your inbox")}</h2>
    <ul className="plain-list">{items.map((i) => <li key={i.id}>
      <Link to={`/officer/consultations/${i.id}`}>{i.role === "recipient" ? `${t("A question from")} ${i.otherParty.name ?? t("an officer")} ${t("about your report")} ${i.yourCaseRef}` : `${t("An answer from")} ${i.otherParty.name ?? t("an officer")} ${t("about")} ${i.yourCaseRef}`}</Link>
      {" "}<span className="badge amber">{t(i.role === "recipient" ? "Needs your answer" : "New answer")}</span></li>)}</ul></section>;
}

// ---- one consultation ---------------------------------------------------------------------------------------------------------------

export function ConsultationPanel({ id, onChanged, compact = false }: { id: string; onChanged?: () => void; compact?: boolean }) {
  const { t, language } = useI18n();
  const navigate = useNavigate();
  const [c, setC] = useState<Consultation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [verified, setVerified] = useState("");
  const [helped, setHelped] = useState("");
  const [note, setNote] = useState("");
  const key = useRef(newKey());
  const load = useCallback(async () => {
    try { const v = await consult.get(id); setC(v); setText(v.payload.question ?? ""); setError(null); }
    catch (e) { setError(message(e, "Could not load the consultation")); }
  }, [id]);
  useEffect(() => { setC(null); void load(); }, [load]);
  const run = async (work: () => Promise<Consultation | void>) => {
    setBusy(true); setError(null);
    try { const out = await work(); if (out) { setC(out); setText(out.payload.question ?? ""); } announce(); onChanged?.(); }
    catch (e) { setError(message(e, "That did not work")); void load(); }   // a refused action shows why, and the record as it now stands
    finally { setBusy(false); }
  };
  if (!c) return error ? <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => void load() }}/> : <StatePanel kind="loading"/>;
  const sender = c.role === "sender";
  const who = c.otherParty.name ?? t("an officer");
  const changed = c.actions.canEdit && text.trim() !== (c.payload.question ?? "").trim();
  const updatePlan = () => void run(async () => {
    const runs = await investigate.runs(c.caseId!);
    const base = runs.find((r) => (r.phase ?? 1) === 1) ?? runs[0];
    if (!base) throw new ApiError(409, "review_conflict", "There is no investigation of this report to update.", "Run the investigator on the report first.");
    await investigate.update(c.caseId!, base.id, c.id);
    navigate(`/officer/cases/${c.caseId}?investigate=1`);
  });
  return <div className="consultation" role="region" aria-label={t("Consultation")}>
    <p><span className={`badge ${c.state === "replied" ? "green" : c.state === "draft" ? "amber" : ""}`}>{t(STATE_WORDS[c.state])}</span>
      {c.unread && <span className="badge amber">{t("New")}</span>}
      <span className="small muted"> {sender ? `${t("You asked")} ${who}` : `${t("Asked by")} ${who}`}{c.sentAt ? ` · ${clock(c.sentAt, language)}` : ""}</span></p>
    {error && <div className="note amber" role="alert">{error}</div>}
    {c.needsRefresh && <div className="note amber" role="status"><b>{t("This draft is out of date")}:</b><ul className="plain-list">{c.stale.map((r) => <li key={r}>{r}</li>)}</ul>
      {c.actions.canRefresh && <button type="button" className="secondary" disabled={busy} onClick={() => void run(() => consult.refresh(c.id))}><Icon name="refresh" size={16}/> {t("Refresh this draft")}</button>}
      <span className="small muted block">{t("Read the refreshed wording before you send it.")}</span></div>}

    {c.yourReport && <p className="small" aria-label={t("The report this is about")}>{sender ? t("Your report") : t("Your report that this is about")}: <span className="mono">{c.yourReport.caseRef}</span>
      {c.yourReport.available && c.yourReport.path ? <> · {c.yourReport.title}{c.yourReport.area ? ` · ${c.yourReport.area}` : ""}{" "}<Link to={c.yourReport.path}>{t("Open the report")}</Link></> : <> · {t("You no longer hold this report.")}</>}</p>}

    {c.payload.withheld ? <p className="note">{c.payload.withheld}</p> : <>
      <section aria-label={t("What the recipient sees")}><h3 className="small">{c.state === "draft" ? t("What they will see, exactly") : t("What was sent, exactly")}</h3>
        {c.actions.canEdit ? <>
          <label className="small" htmlFor={`q-${c.id}`}>{t("The question")}</label>
          <textarea id={`q-${c.id}`} rows={compact ? 5 : 6} maxLength={700} value={text} onChange={(e) => setText(e.target.value)}/></>
          : <p className="quote" data-testid="question">{c.payload.question}</p>}
        <dl className="kv small">
          <dt>{t("To")}</dt><dd>{sender ? (c.payload.recipientName ?? who) : t("You")}</dd>
          <dt>{t("Report references")}</dt><dd><span className="mono">{c.payload.senderCaseRef}</span> · <span className="mono">{c.payload.earlierCaseRef}</span></dd>
          <dt>{t("Both reports name")}</dt><dd><span className="mono">{c.payload.sharedIdentifier}</span> ({c.payload.sharedKind})</dd></dl>
        {sender && c.payload.keeps_back && <p className="small muted"><b>{t("Not shared")}:</b> {c.payload.keeps_back.map((k) => t(k)).join(" · ")}.</p>}
      </section></>}

    {c.messages.length > 0 && <section aria-label={t("Conversation")}><h3 className="small">{t("Conversation")}</h3>
      <ol className="threads">{c.messages.map((m) => <li key={m.id} className={`thread ${m.authorRole === "sender" ? "outgoing" : "incoming"}`}>
        <span className="small muted"><time dateTime={m.at}>{clock(m.at, language)}</time> · <strong>{m.kind === "system" ? t("Record") : m.authorName ?? t("An officer")}</strong>{m.kind === "request" ? ` · ${t("question")}` : m.kind === "reply" ? ` · ${t("answer")}` : ""}</span>
        {m.kind === "reply" ? <span className="msg-body"><b>{t("What I verified")}:</b> {m.data.verified}<br/><b>{t("What helped")}:</b> {c.helpedOptions.find((o) => o.id === m.data.helpedAction)?.label ?? m.data.helpedAction}{m.data.note ? <><br/><b>{t("Note")}:</b> {m.data.note}</> : null}</span>
          : <span className="msg-body">{m.body}</span>}</li>)}</ol>
      {c.replyCount > 0 && <p className="small muted">{t(c.attribution)}</p>}</section>}

    {c.actions.canReply && <form className="replyform" aria-label={t("Answer this question")} onSubmit={(e) => { e.preventDefault(); void run(async () => { const out = await consult.reply(c.id, { verified: verified.trim(), helpedAction: helped, note: note.trim() || undefined, key: key.current }); key.current = newKey(); setVerified(""); setHelped(""); setNote(""); return out; }); }}>
      <h3 className="small">{t("Your answer")}</h3>
      <p className="small muted">{t("Say what you checked on your own report and which action helped. This goes only to the officer who asked.")}</p>
      <label htmlFor={`v-${c.id}`} className="small">{t("What I verified")}</label>
      <textarea id={`v-${c.id}`} rows={3} maxLength={600} required minLength={3} value={verified} onChange={(e) => setVerified(e.target.value)}/>
      <label htmlFor={`h-${c.id}`} className="small">{t("What helped")}</label>
      <select id={`h-${c.id}`} required value={helped} onChange={(e) => setHelped(e.target.value)}><option value="">{t("Choose one")}</option>{c.helpedOptions.map((o) => <option key={o.id} value={o.id}>{t(o.label)}</option>)}</select>
      <label htmlFor={`n-${c.id}`} className="small">{t("Note (optional)")}</label>
      <textarea id={`n-${c.id}`} rows={2} maxLength={800} value={note} onChange={(e) => setNote(e.target.value)}/>
      <div className="inline-form"><button type="submit" disabled={busy || verified.trim().length < 3 || !helped}>{t("Send reply")}</button></div></form>}

    <div className="inline-form">
      {c.actions.canEdit && changed && <button type="button" className="secondary" disabled={busy} onClick={() => void run(() => consult.edit(c.id, text))}>{t("Save my edit")}</button>}
      {c.state === "draft" && sender && <button type="button" disabled={busy || changed || !c.actions.canApprove} title={changed ? t("Save your edit first: an approval covers exactly the words shown.") : undefined}
        onClick={() => void run(() => consult.approve(c.id, c.payloadDigest))}>{t("Approve and send")}</button>}
      {c.actions.canUpdatePlan && <button type="button" disabled={busy} onClick={updatePlan}><Icon name="spark" size={16}/> {t("Update investigation plan")}</button>}
      {c.actions.canClose && <button type="button" className="secondary" disabled={busy} onClick={() => void run(() => consult.close(c.id))}>{t("Close this consultation")}</button>}
      {c.actions.canCancel && <button type="button" className="secondary" disabled={busy} onClick={() => void run(() => consult.cancel(c.id))}>{t(c.state === "draft" ? "Discard this draft" : "Withdraw this question")}</button>}
    </div>
    {c.state === "draft" && sender && <p className="small muted">{t("Nothing is sent until you press Approve and send. The officer asked sees only what is shown above.")}</p>}
  </div>;
}

// ---- the inbox and the conversation page --------------------------------------------------------------------------------------------

export function InboxPage() {
  const { t, language } = useI18n();
  const [box, setBox] = useState<"all" | "inbox" | "sent">("all");
  const [list, setList] = useState<{ items: ConsultationItem[]; unread: number; awaitingYourReply: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    setError(null);
    consult.list(box).then((l) => { if (live) setList(l); }).catch((e) => { if (live) setError(message(e, "Could not load your inbox")); });
    return () => { live = false; };
  }, [box, n]);
  const rows = list?.items ?? [];
  const FILTERS: ["all" | "inbox" | "sent", string][] = [["all", "All"], ["inbox", "Asked of me"], ["sent", "Asked by me"]];
  return <>
    <PageHead eyebrow={t("Investigation workspace")} title={t("Inbox")} lead={t("Questions between officers about related reports. Each shows only what its sender approved.")}/>
    <section className="card" aria-label={t("Consultations")}>
      <div className="sectionhead"><h2>{t("Consultations")}</h2><IconButton name="refresh" label={t("Refresh the inbox")} onClick={() => setN((v) => v + 1)}/></div>
      <div className="filters" role="group" aria-label={t("Show")}>{FILTERS.map(([k, label]) => <button key={k} type="button" className={`filter${box === k ? " active" : ""}`} aria-pressed={box === k} onClick={() => setBox(k)}>{t(label)}</button>)}</div>
      {error && <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => setN((v) => v + 1) }}/>}
      {!list && !error && <StatePanel kind="loading"/>}
      {list && rows.length === 0 && !error && <div className="empty"><h3>{t("No consultations yet")}</h3><p className="muted">{t("When you ask the officer who holds an earlier report, or someone asks you, it appears here.")}</p></div>}
      <ul className="plain-list">{rows.map((i) => <li className="reportrow" key={i.id}>
        <span className="tileicon"><Icon name="mail"/></span>
        <div className="grow"><b>{i.role === "recipient" ? `${t("From")} ${i.otherParty.name ?? t("an officer")}` : `${t("To")} ${i.otherParty.name ?? t("an officer")}`}</b>
          <p className="small muted">{t("Your report")}: <span className="mono">{i.yourCaseRef}</span> · {clock(i.lastActivityAt, language)}</p></div>
        {i.awaitingYourReply && <span className="badge amber">{t("Needs your answer")}</span>}
        {i.unread && !i.awaitingYourReply && <span className="badge amber">{t("New answer")}</span>}
        <span className={`badge ${i.state === "replied" ? "green" : ""}`}>{t(STATE_WORDS[i.state])}</span>
        <Link className="button" to={`/officer/consultations/${i.id}`}>{t("Open")} <Icon name="arrow" size={18}/></Link></li>)}</ul>
    </section></>;
}

export function ConsultationPage() {
  const { t } = useI18n();
  const { id } = useParams();
  if (!id) return null;
  return <>
    <PageHead eyebrow={t("Investigation workspace")} title={t("Consultation")} lead={t("A question between two officers about related reports.")} action={<Link className="button secondary" to="/officer/inbox"><Icon name="back" size={16}/> {t("Inbox")}</Link>}/>
    <section className="card"><ConsultationPanel id={id}/></section></>;
}

/** The questions already asked about this report, each opening its conversation. */
export function CaseConsultations({ caseId, reload, onOpen }: { caseId: string; reload: number; onOpen: (id: string) => void }) {
  const { t } = useI18n();
  const [items, setItems] = useState<Consultation[]>([]);
  useEffect(() => { let live = true; consult.forCase(caseId).then((v) => { if (live) setItems(v); }).catch(() => undefined); return () => { live = false; }; }, [caseId, reload]);
  if (items.length === 0) return null;
  return <section aria-label={t("Questions to other officers")}><h3>{t("Questions to other officers")}</h3>
    <ul className="plain-list">{items.map((c) => <li key={c.id}>
      <button type="button" className="quiet" onClick={() => onOpen(c.id)}>{t("To")} {c.otherParty.name ?? t("an officer")} · {c.aboutCaseRef ?? ""} →</button>{" "}
      <span className={`badge ${c.state === "replied" ? "green" : ""}`}>{t(STATE_WORDS[c.state])}</span>{c.unread ? <span className="badge amber">{t("New answer")}</span> : null}</li>)}</ul></section>;
}
