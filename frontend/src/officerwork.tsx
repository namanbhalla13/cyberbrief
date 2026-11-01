/** The officer's working case file: overview and next steps, claims against evidence, messages and requests, related reports.
 *
 * The officer assistant drafts; the officer edits and approves. A request reaches the citizen, or the simulated bank, only after
 * the officer approves exactly the words shown, and a step is ticked only when the record shows it happened.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, type CaseSnapshot } from "./api";
import { ErrorNotice, MoneyText, useI18n } from "./ui";
import { Dialog } from "./dialog";
import { AgentCard, IconButton } from "./icons";
import { ConsultationPanel } from "./officerconsult";
import { type DeskParts } from "./officerdesk";
import { ConnectionsPanel, DEFAULT_GOAL, InvestigatorDrawer, type Command } from "./officerintelligence";
import { copyText, downloadText, requestText, TransactionVerification } from "./officerverify";
import { StatePanel } from "./statepanel";
import { Tabs } from "./tabs";
import { investigate, wf, type CaseFile, type CaseMessage, type Claim, type Proposal, type RunAction } from "./workflowapi";

const ASSESSMENTS: [string, string][] = [["supported", "Supported by the evidence"], ["needs_evidence", "Needs more evidence"], ["contradicted", "Contradicted"], ["possible_duplicate", "Possible duplicate"]];

function DraftEditor({ caseId, draft, onDone, busy, run, onNotice }: { caseId: string; draft: Proposal; onDone: () => void; busy: boolean; run: (work: () => Promise<unknown>) => Promise<void>; onNotice: (text: string) => void }) {
  const { t } = useI18n();
  const bankDraft = draft.kind === "bank_request";
  const field = bankDraft ? "body" : "text";
  const [text, setText] = useState(String(draft.payload[field] ?? ""));
  const [current, setCurrent] = useState(draft);
  const [chosen, setChosen] = useState<string[]>(draft.payload.attachments ?? []);   // the evidence files the officer selects for a bank request
  const [copied, setCopied] = useState<string | null>(null);
  const savedAttachments = current.payload.attachments ?? [];
  const attachmentsChanged = bankDraft && [...chosen].sort().join() !== [...savedAttachments].sort().join();
  const changed = text.trim() !== String(current.payload[field] ?? "").trim() || attachmentsChanged;
  const mode = current.payload.delivery ?? "simulated";
  const approveLabel = !bankDraft ? "Approve and send to the citizen" : mode === "email" ? "Approve and send by email" : mode === "manual" ? "Approve (nothing will be sent by CyberBrief)" : "Approve and place in the simulated outbox";
  const names = current.payload.attachmentNames ?? {};
  const asText = () => requestText({ to: String(current.payload.to ?? ""), subject: String(current.payload.subject ?? ""), body: text, attachments: chosen.map((id) => ({ filename: names[id] ?? "File" })) });
  return <div className="draft">
    {bankDraft
      ? <dl className="kv small"><dt>{t("Recipient")}</dt><dd>{String(current.payload.to ?? "")} <span className="muted">({current.payload.bankName}; {t("from the bank directory")})</span></dd>
          <dt>{t("Subject")}</dt><dd>{current.payload.subject}</dd>
          <dt>{t("Response reference")}</dt><dd className="mono">{current.payload.responseRef}</dd></dl>
      : <p className="small muted">{t("To the citizen")}</p>}
    <p className="small muted">{t(draft.origin === "model" ? "Drafted by the model, checked by code" : "Drafted from fixed rules")}</p>
    <textarea aria-label={t("Draft wording")} value={text} rows={bankDraft ? 14 : 4} maxLength={bankDraft ? 6000 : 2000} onChange={(e) => setText(e.target.value)}/>
    {bankDraft && <fieldset className="attachments"><legend className="small">{t("Evidence to attach")}</legend>
      {Object.keys(names).length === 0 && <p className="small muted">{t("No evidence files are available to attach.")}</p>}
      {Object.entries(names).map(([id, name]) => <label className="check small" key={id}><input type="checkbox" checked={chosen.includes(id)}
        onChange={(e) => setChosen((cur) => e.target.checked ? [...cur, id] : cur.filter((x) => x !== id))}/> {name}</label>)}</fieldset>}
    <p className="small muted">{!bankDraft ? t("Goes to the citizen inside this report.")
      : mode === "simulated" ? t("Simulated: no bank is contacted and no email is sent.")
      : mode === "email" ? t("Sent by email to the address above only after you approve. Accepted by the email service is not proof of delivery.")
      : `${t("Email is not available for this request, so CyberBrief will not send it. Copy or download it and send it yourself.")} ${current.payload.deliveryNote ?? ""}`}</p>
    {bankDraft && current.payload.contactMissing && <p className="small differs" role="status">{t("Your official contact details are not configured, so the request names none.")}</p>}
    <div className="inline-form">
      {changed && <button type="button" className="secondary" disabled={busy} onClick={() => void run(async () => setCurrent(await wf.editDraft(caseId, current.id, text, bankDraft ? chosen : undefined)))}> {t("Save my edit")} </button>}
      <button type="button" disabled={busy || changed} title={changed ? t("Save your edit first: an approval covers exactly the words shown.") : undefined}
              onClick={() => void run(async () => {
                const done = await wf.approveDraft(caseId, current.id, current.digest);
                if (done.bankRequest?.state === "failed") onNotice(`${t("Your approval is recorded, but sending failed")}: ${done.bankRequest.lastError ?? ""} ${t("The request is kept in the verification section, where you can try again.")}`);
                onDone();
              })}>{t(approveLabel)}</button>
      {bankDraft && <IconButton name="clip" label={t("Copy the draft")} onClick={() => void copyText(asText()).then((ok) => setCopied(ok ? t("Copied to the clipboard.") : t("Could not copy. Select the text and copy it.")))}/>}
      {bankDraft && <IconButton name="download" label={t("Download the draft")} onClick={() => downloadText(`bank-request-draft-${current.payload.responseRef ?? "draft"}.txt`, asText())}/>}
      <button type="button" className="secondary" disabled={busy} onClick={() => void run(async () => { await wf.rejectDraft(caseId, current.id); onDone(); })}> {t("Discard")} </button>
      {copied && <span className="small muted" role="status">{copied}</span>}
    </div>
  </div>;
}

function Thread({ m, caseId, run, busy, onReview }: { m: CaseMessage; caseId: string; run: (work: () => Promise<unknown>) => Promise<void>; busy: boolean; onReview: (m: CaseMessage) => void }) {
  const { t, language } = useI18n();
  const when = new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(m.createdAt));
  const bank = m.channel === "bank";
  const who = bank ? (m.direction === "outgoing" ? t("Request to the bank") : t("Reply from the bank")) : (m.direction === "outgoing" ? t("You asked the citizen") : t("The citizen replied"));
  return <li className={`thread ${m.direction}`}>
    <span className="small muted"><time dateTime={m.createdAt}>{when}</time> · <strong>{who}</strong>{m.simulated && <span className="badge badge-fixture"> {t("Simulated")} </span>}</span>
    <span className="msg-body">{m.body || t("(files only)")}</span>
    {m.direction === "outgoing" && m.state === "sent" && bank && m.simulated && <span className="inline-form">
      <span className="small muted">{t("Waiting for the simulated bank's reply.")}</span>
      <button type="button" className="secondary" disabled={busy} onClick={() => void run(() => wf.simulateReply(caseId, m.id))}> {t("Simulate the bank's reply")} </button></span>}
    {m.direction === "outgoing" && bank && !m.simulated && <span className="small muted">{t(m.state === "sent" ? "Sent. Waiting for the bank's reply: attach it under Transaction verification." : m.state === "failed" ? "Sending failed. Open Transaction verification to try again." : "Approved, not sent. Open Transaction verification.")}</span>}
    {m.direction === "incoming" && bank && !m.simulated && <span className="badge amber">{t("Officer-provided evidence: not an authenticated bank response")}</span>}
    {m.direction === "incoming" && m.state === "received" && <span className="inline-form">
      <span className="small muted">{t("Received, not yet reviewed.")}</span>
      <button type="button" className="secondary" disabled={busy} onClick={() => onReview(m)}> {t("Review the reply")} </button></span>}
    {m.direction === "incoming" && m.state === "reviewed" && <span className="small muted">{t("Reviewed by you.")}</span>}
    {m.direction === "outgoing" && m.state === "sent" && !bank && <span className="small muted">{t("Waiting for the citizen.")}</span>}
    {m.comparison && <table className="compare"><thead><tr><th scope="col">{t("Reference")}</th><th scope="col">{t("Against the case")}</th></tr></thead>
      <tbody>{m.comparison.map((c) => <tr key={c.reference}><th scope="row">{c.reference}</th><td className={c.verdict.startsWith("differs") || c.verdict.startsWith("no record") ? "differs" : undefined}>{c.verdict}</td></tr>)}</tbody></table>}
    {m.comparison && <span className="small muted">{t("Recorded as what the simulated bank said. It does not change the case's own figures.")}</span>}
  </li>;
}

export function OfficerWork({ caseId, snapshot, parts, tab, onTab, reload, onChanged, children }: {
  caseId: string; snapshot: CaseSnapshot; parts: DeskParts; tab?: string; onTab: (id: string) => void; reload: number; onChanged: () => void; children?: ReactNode;
}) {
  const { t, language } = useI18n();
  const [file, setFile] = useState<CaseFile | null>(null);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [leads, setLeads] = useState(0);   // strong connections still undecided, from the index, for the tab badge
  const [selected, setSelected] = useState<string[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [drawer, setDrawer] = useState<{ goal: string } | null>(() => (new URLSearchParams(window.location.search).get("investigate") === "1" ? { goal: DEFAULT_GOAL } : null));
  const [command, setCommand] = useState<Command>(null);
  const opener = useRef<HTMLElement | null>(null);   // where focus returns when the investigator closes
  const [draftOpen, setDraftOpen] = useState<string | null>(null);   // the id of the draft being reviewed in the dialog
  const [consultOpen, setConsultOpen] = useState<string | null>(null);   // the consultation being reviewed or read in its dialog
  const [consultN, setConsultN] = useState(0);
  const [reviewing, setReviewing] = useState<CaseMessage | null>(null);
  const [checked, setChecked] = useState<Record<string, boolean>>({});   // "I opened the source and checked it", per claim
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);   // an outcome that must stay visible after a dialog closes, such as a send that failed

  const load = useCallback(async () => {
    try {
      const [f, c, r] = await Promise.all([wf.file(caseId), wf.claims(caseId), investigate.summary(caseId)]);
      setFile(f); setClaims(c); setLeads(r.toReview);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not load the case file"); }
  }, [caseId]);
  useEffect(() => { void load(); }, [load, reload]);

  const onRunSettled = useCallback(() => { void load(); }, [load]);   // a drafted question shows up in Messages once the run has finished

  const run = useCallback(async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try { await work(); await load(); onChanged(); }
    catch (e) { setError(e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : "That did not work"); }
    finally { setBusy(false); }
  }, [load, onChanged]);

  if (!file) return error ? <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => { setError(null); void load(); } }}/> : <StatePanel kind="loading"/>;
  const paymentIdOf = (key: string) => key.replace("payment:", "");
  const toggle = (key: string) => setSelected((cur) => (cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]));
  const ids = selected.map(paymentIdOf);
  const clock = (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  const overview = <section className="card" id="sec-overview" aria-label={t("Overview")}>
      <h2> {t("Overview")} </h2>
      <p>{t("Assigned to")}: <strong>{file.assignedTo}</strong> · {file.area ?? t("No area given")}
        {file.acknowledged && <span className="badge badge-user_confirmed"> {t("Acknowledged")} </span>}</p>
      <h3> {t("Checklist")} </h3>
      <ul className="checklist">{file.checklist.map((c) => <li key={c.key} className={c.done ? "done" : ""}>
        <span aria-hidden="true">{c.done ? "✓" : "○"}</span> {t(c.title)} <span className="visually-hidden">{t(c.done ? "done" : "not done")}</span></li>)}</ul>
      <p className="small muted"> {t("A step is ticked only when the record shows it happened.")} </p>
    </section>;

  const agentLog = <section className="card" aria-label={t("What the assistant did")}>
      <h2> {t("What the assistant did")} </h2>
        <ul className="plain-list small">{file.activity.map((a, i) => <li key={i}><time dateTime={a.at}>{clock(a.at)}</time> · {a.steps.join(" → ")}</li>)}</ul>
    </section>;

  const claimsCard = <section className="card" id="sec-claims" aria-label={t("Evidence and claims")}>
      <h2> {t("Evidence and claims")} </h2>
      <p className="small muted"> {t("What the citizen said, what the evidence supports, and your assessment are kept apart. Your assessment never overwrites the citizen's words.")} </p>
      <table className="compare claims"><thead><tr><th scope="col"/><th scope="col">{t("Claim")}</th><th scope="col">{t("What supports it")}</th><th scope="col">{t("Your assessment")}</th></tr></thead>
        <tbody>{claims.map((c) => <tr key={c.key}>
          <td><input type="checkbox" aria-label={`${t("Select")} ${c.claim}`} checked={selected.includes(c.key)} onChange={() => toggle(c.key)}/></td>
          <th scope="row">{c.claim}<br/><span className="small muted">{t(c.citizenSaid)}</span></th>
          <td data-label={t("What supports it")}>{c.freshness === "needs_review" && <div className="badge badge-conflicting" role="status"> {t("New evidence since your assessment: review it again")} </div>}
            {c.changes.map((x) => <div key={`${x.kind}-${x.revision}`} className={`small${x.conflict ? " differs" : ""}`}>
              {t("Changed")}: {x.kind} {x.was ? `${x.was} → ` : ""}<strong>{x.now}</strong>{x.confirmedByCitizen ? ` (${t("you were told the citizen confirmed an earlier value")})` : ""}
              {x.conflict ? ` · ${t("conflicts with a confirmed value")}` : ""}</div>)}
            {c.newSources.map((x) => <div key={x.id} className="small muted">{t("New source")}: {x.filename}</div>)}
            {t(c.support.label)}{c.contradictions.map((x) => <div key={x} className="small differs">{x}</div>)}{c.supportedBy.length > 0 && <div className="small muted">{c.supportedBy.length} {t("source(s)")}</div>}</td>
          <td data-label={t("Your assessment")}>
            <select aria-label={`${t("Assessment of")} ${c.claim}`} value={c.assessment?.value ?? ""} disabled={busy}
                    onChange={(e) => e.target.value && void run(async () => { setClaims(await wf.assess(caseId, c.key, e.target.value, notes[c.key])); })}>
              <option value="">{t("Not assessed")}</option>{ASSESSMENTS.map(([v, l]) => <option key={v} value={v}>{t(l)}</option>)}
            </select>
            {c.assessment && <span className="small muted"> v{c.assessment.version}</span>}
            <input aria-label={`${t("Note on")} ${c.claim}`} placeholder={t("Note (optional)")} value={notes[c.key] ?? c.assessment?.note ?? ""} onChange={(e) => setNotes({ ...notes, [c.key]: e.target.value })}/>
          </td></tr>)}</tbody></table>
      <div className="inline-form">
        <button type="button" disabled={busy} onClick={() => void run(async () => { await wf.draftRequest(caseId, ids); })}> {t("Draft a question for the citizen")} </button>
        <button type="button" className="secondary" disabled={busy || ids.length === 0} onClick={() => void run(async () => { await wf.draftBank(caseId, ids); })}> {t("Draft a bank verification request")} </button>
      </div>
      <p className="small muted"> {t("Select payments to focus a question, or choose them for a bank request. Nothing is sent until you approve it.")} </p>
    </section>;

  const messagesCard = <section className="card" id="sec-messages" aria-label={t("Messages and requests")}>
      <h2> {t("Messages and requests")} </h2>
      {file.drafts.map((d) => <div className="message" key={d.id}>
        <div className="sectionhead"><b>{d.kind === "bank_request" ? `${t("To")} ${d.payload.bankName}` : t("To the citizen")}</b><span className={`badge ${d.kind === "bank_request" ? "amber" : ""}`}>{t(d.kind === "bank_request" ? "Simulated draft" : "Draft")}</span></div>
        <p className="small">{String(d.payload[d.kind === "bank_request" ? "body" : "text"] ?? "").slice(0, 220)}</p>
        <button type="button" onClick={() => setDraftOpen(d.id)}>{t(d.kind === "bank_request" ? "Review bank request" : "Review citizen request")}</button></div>)}
      {file.messages.length === 0 && file.drafts.length === 0 && <p className="muted"> {t("No messages yet.")} </p>}
      <ol className="threads">{file.messages.map((m) => <Thread key={m.id} m={m} caseId={caseId} run={run} busy={busy} onReview={setReviewing}/>)}</ol>
      <p className="small muted"> {t("The bank channel is a fictional outbox and inbox. A reply is recorded only if it answers a request this case sent.")} </p>
    </section>;

  const openInvestigator = (goal: string, from?: HTMLElement | null) => {
    opener.current = from ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    setDrawer({ goal });
  };
  const closeInvestigator = () => { setDrawer(null); window.setTimeout(() => opener.current?.focus?.(), 0); };
  const narrow = () => window.matchMedia("(max-width: 900px)").matches;
  const investigatorAction = (a: RunAction) => {
    if (a.type === "review_draft") { onTab("messages"); setDraftOpen(a.proposalId ?? null); }
    else if (a.type === "review_consultation") setConsultOpen(a.consultationId ?? null);
    else if (a.handle) { onTab("related"); setCommand({ type: a.type === "open_comparison" ? "compare" : "handling", handle: a.handle, n: Date.now() }); }
    if (narrow()) closeInvestigator();   // on a phone the panel covers the page, so it steps aside for what was asked for
  };
  const relatedCard = <ConnectionsPanel caseId={caseId} reload={reload + consultN} command={command} onChanged={() => { void load(); onChanged(); }} onAsk={(goal) => openInvestigator(goal)} onConsultation={setConsultOpen}/>;

  const unread = file.messages.filter((m) => m.direction === "incoming" && m.state === "received").length;
  // what needs attention, in the order to take it: each line is true of the record now, and says where to act on it
  const attention: { title: string; text: string; label: string; go: () => void }[] = [
    ...(!file.acknowledged ? [{ title: t("Acknowledge the assignment"), text: t("The admin is waiting to see that you have it."), label: t("Acknowledge this assignment"), go: () => void run(async () => { await wf.acknowledge(caseId); }) }] : []),
    ...(unread > 0 ? [{ title: t("Review the new reply"), text: t("A reply has arrived and has not been reviewed."), label: t("Open Messages"), go: () => onTab("messages") }] : []),
    ...(leads > 0 ? [{ title: t("Review the related lead"), text: t("An earlier report shares an exact recipient identifier."), label: t("Inspect the match"), go: () => onTab("related") }] : []),
    { title: t("Ask for one missing detail"), text: t("Draft a focused question; nothing is sent until you approve it."), label: t("Draft a clarification"), go: () => void run(async () => { await wf.draftRequest(caseId, []); onTab("messages"); }) },
  ];
  const changedClaims = claims.filter((c) => c.freshness === "needs_review" && c.review);
  const sourceHref = (id: string) => `/api/officer/cases/${caseId}/sources/${id}`;
  const openTasks = file.tasks.filter((x) => x.state === "open");
  /** One card for new evidence: what changed, where it was read, what you said before, why it matters and the one next step, which you still approve word for word. */
  const changeCard = changedClaims.length === 0 ? null : <section className="card" aria-label={t("New evidence needs review")}>
    <h2>{t("New evidence needs review")}</h2>
    {changedClaims.map((c) => {
      const review = c.review!;
      const sources = [...new Map([...c.changes.flatMap((x) => x.sources ?? []), ...c.newSources].map((s) => [s.id, s])).values()];
      const quote = c.changes.find((x) => x.quote)?.quote;
      const overall = c.changes.some((x) => x.state === "conflicting") ? "conflicting" : c.changes[0]?.state ?? "proposed";
      const ok = Boolean(checked[c.key]);
      return <article className="changecard" key={c.key} aria-label={c.claim}>
        <div className="sectionhead"><b>{c.claim}</b><span className={`badge ${overall === "conflicting" ? "red" : overall === "reviewed" ? "green" : "amber"}`}>{t(overall)}</span></div>
        <p className="small">{review.why}</p>
        {c.changes.length > 0 && <div className="tablewrap"><table className="compare"><thead><tr><th scope="col">{t("Detail")}</th><th scope="col">{t("Before")}</th><th scope="col">{t("Proposed after")}</th></tr></thead>
          <tbody>{c.changes.map((x) => <tr key={`${x.kind}-${x.revision}`}><th scope="row">{x.kind.replace(/_/g, " ")}</th><td>{x.was ?? t("Not stated")}</td>
            <td><b>{x.now}</b>{x.conflict ? <small className="differs">{t("conflicts with a confirmed value")}</small> : null}</td></tr>)}</tbody></table></div>}
        <p className="small"><b>{t("Read from")}:</b> {sources.length === 0 ? t("No source recorded") : sources.map((s, i) => <span key={s.id}>{i > 0 ? ", " : ""}<a href={sourceHref(s.id)} target="_blank" rel="noreferrer noopener">{s.filename}</a></span>)}
          {quote ? <q className="quote"> {quote}</q> : null}</p>
        <p className="small"><b>{t("Your earlier assessment")}:</b> {c.assessment ? `${t(ASSESSMENTS.find(([v]) => v === c.assessment!.value)?.[1] ?? c.assessment.value)} (v${c.assessment.version})${c.assessment.note ? ` · ${c.assessment.note}` : ""}` : t("None")}</p>
        <label className="check"><input type="checkbox" checked={ok} onChange={(e) => setChecked({ ...checked, [c.key]: e.target.checked })}/> {t("I opened the source and checked it")}</label>
        <p className="small muted">{t("Before the next step")}: {review.next.requires.map((r) => t(r)).join(" · ")}</p>
        <div className="inline-form">
          {review.next.type !== "reassess" && <button type="button" disabled={busy || !ok} onClick={() => void run(async () => {
            if (review.next.type === "bank_request") await wf.draftBank(caseId, [c.paymentId!]); else await wf.draftRequest(caseId, [c.paymentId!], review.next.topic ?? undefined);
            onTab("messages");
          })}>{t(review.next.label)}</button>}
          <button type="button" className="secondary" disabled={busy || !ok || !c.assessment} onClick={() => void run(async () => { setClaims(await wf.assess(caseId, c.key, c.assessment!.value, c.assessment!.note ?? undefined)); })}>{t("Keep my assessment")}</button>
          <select aria-label={`${t("Change my assessment")}: ${c.claim}`} value="" disabled={busy || !ok} onChange={(e) => e.target.value && void run(async () => { setClaims(await wf.assess(caseId, c.key, e.target.value)); })}>
            <option value="">{t("Change my assessment")}</option>{ASSESSMENTS.map(([v, l]) => <option key={v} value={v}>{t(l)}</option>)}</select>
        </div>
        <p className="small muted">{t("The step is only prepared: you read and approve its exact wording before anything is sent.")}</p>
      </article>;
    })}
  </section>;
  const openItems = file.tasks.filter((x) => x.state === "open").length;
  const context = <section className="casecontext" aria-label={t("This report at a glance")}>
    <dl>
      <div><dt>{t("Report")}</dt><dd className="mono">{file.caseRef}</dd></div>
      <div><dt>{t("Owner")}</dt><dd>{file.assignedTo ?? t("Not yet assigned")}</dd></div>
      <div><dt>{t("Reported amount")}</dt><dd><MoneyText minor={snapshot.totals.citizenReportedMinor}/></dd></div>
      <div><dt>{t("Recorded status")}</dt><dd>{snapshot.case.reviewState}{file.workflowState ? ` · ${file.workflowState.replace(/_/g, " ")}` : ""}</dd></div>
      <div><dt>{t("Outstanding")}</dt><dd>{openItems > 0 ? t("{n} open item(s)", { n: openItems }) : t("Nothing open")}</dd></div>
    </dl></section>;
  return <>
    {error && <ErrorNotice message={error}/>}
    {notice && <div className="note amber" role="alert"><IconButton name="close" label={t("Dismiss this message")} onClick={() => setNotice(null)}/> {notice}</div>}
    {context}
    <Dialog title={t("Question to another officer")} open={consultOpen !== null} onClose={() => setConsultOpen(null)}>
      {consultOpen && <ConsultationPanel id={consultOpen} compact onChanged={() => { setConsultN((v) => v + 1); void load(); onChanged(); }}/>}
    </Dialog>
    <Dialog title={draftOpen ? t(file.drafts.find((d) => d.id === draftOpen)?.kind === "bank_request" ? "Review bank request" : "Review citizen request") : ""} open={draftOpen !== null} onClose={() => setDraftOpen(null)}>
      {file.drafts.filter((d) => d.id === draftOpen).map((d) => <DraftEditor key={`${d.id}-${d.digest}`} caseId={caseId} draft={d} busy={busy} run={run} onNotice={setNotice} onDone={() => setDraftOpen(null)}/>)}
    </Dialog>
    <Dialog title={t("Review the new reply")} open={reviewing !== null} onClose={() => setReviewing(null)}
      footer={<button type="button" disabled={busy} onClick={() => { const m = reviewing!; setReviewing(null); void run(() => wf.markReviewed(caseId, m.id)); }}>{t("Mark this reply as reviewed")}</button>}>
      {reviewing && <>
        <div className="note">{t(reviewing.channel === "bank" ? "This is a simulated response, not a real bank verification. Reviewing it does not change any amount or close the record." : "This is the citizen's answer. It is now part of the report's evidence.")}</div>
        <p className="quote">{reviewing.body || t("(files only)")}</p>
        {reviewing.comparison && <table className="compare"><thead><tr><th scope="col">{t("Reference")}</th><th scope="col">{t("Against the case")}</th></tr></thead>
          <tbody>{reviewing.comparison.map((c) => <tr key={c.reference}><th scope="row">{c.reference}</th><td>{c.verdict}</td></tr>)}</tbody></table>}
        {claims.filter((c) => c.freshness === "needs_review").map((c) => <div className="note amber" key={c.key}><b>{c.claim}</b>: {c.changes.map((x) => `${x.kind} ${x.was ? `${x.was} → ` : ""}${x.now}`).join("; ") || t("New evidence since your assessment: review it again")}</div>)}
      </>}
    </Dialog>
    <div className="investigate-bar"><button type="button" className="secondary" aria-expanded={drawer !== null} onClick={(e) => (drawer ? closeInvestigator() : openInvestigator(DEFAULT_GOAL, e.currentTarget))}>{t("Investigate connections")}</button>
      <span className="small muted">{t("Search other reports, compare them and see how they were handled.")}</span></div>
    <div className={`withdrawer${drawer ? " open" : ""}`}><div className="drawermain">
    <Tabs label={t("Sections of this case")} selected={tab} onSelect={onTab} tabs={[
      { id: "brief", label: t("Brief"), content: <div className="withaside"><div>
        {changeCard}
        <section className="card" aria-label={t("What needs attention")}><h2>{t("What needs attention")}</h2>
          {attention.map((a, i) => <div className="task" key={a.title}><span className="number">{i + 1}</span><div><b>{a.title}</b><p className="small muted">{a.text}</p>
            <button type="button" className="quiet" disabled={busy} onClick={a.go}>{a.label} →</button></div></div>)}
          {openTasks.length > 0 && <p className="small muted"><b>{t("Waiting on")}:</b> {openTasks.map((x) => `${x.title}${x.dueAt ? ` (${t("by")} ${clock(x.dueAt)})` : ""}`).join(" · ")}</p>}</section>
        <TransactionVerification caseId={caseId} reload={reload} onChanged={() => { void load(); onChanged(); }} onTab={onTab} onDraft={(d) => { setDraftOpen(d.id); void load(); }}/>
        {overview}{children}{parts.steps}</div>
        <AgentCard title={t("Case assistant")} subtitle={t("Suggestions only. You decide.")}
          steps={[{ title: t("Sources stay attached"), text: t("Every claim can be traced to its evidence.") }, { title: t("You control the decision"), text: t("Messages and changes wait for your approval.") },
                  { title: t("One useful next step"), text: t("Ask only for what is missing.") }]}
          focus={`${t("Current focus")}|${file.tasks.find((x) => x.state === "open")?.title ?? t("Nothing is waiting on you right now.")}`}/></div> },
      { id: "evidence", label: t("Evidence"), content: <>{claimsCard}{parts.payments}</> },
      { id: "messages", label: t("Messages"), badge: unread > 0 ? String(unread) : null, content: messagesCard },
      { id: "related", label: t("Connections"), badge: leads > 0 ? String(leads) : null, content: relatedCard },
      { id: "activity", label: t("Activity"), content: <>{agentLog}{parts.timeline}</> },
    ]}/>
    </div>{drawer && <InvestigatorDrawer caseId={caseId} initialGoal={drawer.goal} onClose={closeInvestigator} onAction={investigatorAction} onRunSettled={onRunSettled}/>}
    </div>
  </>;
}
