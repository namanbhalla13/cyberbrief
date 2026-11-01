/** Transaction verification on the officer's case page.
 *
 * Shows what was reported and where it was read, asks the bank integration when the officer clicks, compares only what the bank returned,
 * and keeps the request to the bank and its reply beside the result. A receipt, a screenshot or a matching field is never shown as bank
 * confirmation; a simulated answer carries its banner on every view of it; a reply attached by an officer is labelled as theirs.
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./api";
import { Icon, IconButton, type IconName } from "./icons";
import { StatePanel } from "./statepanel";
import { ErrorNotice, useI18n } from "./ui";
import { bank, wf, type BankRequestOut, type BankResponseOut, type Proposal, type VerificationOut, type VerificationPayment, type VerificationSection } from "./workflowapi";

/** Every outcome has a word and a shape, so it never depends on colour alone. */
const STATUS_ICON: Record<string, IconName> = {
  not_checked: "help", checking: "clock", bank_confirmed: "check", discrepancy: "alert", no_match: "search", more_info_required: "edit", awaiting_bank: "mail", unavailable: "refresh",
};
const STATUS_TONE: Record<string, string> = {
  bank_confirmed: "green", discrepancy: "red", no_match: "amber", more_info_required: "amber", awaiting_bank: "amber", unavailable: "red", checking: "", not_checked: "",
};
const RESULT_WORDS: Record<string, [string, IconName]> = {
  match: ["Matches", "check"], differs: ["Differs", "alert"], not_provided: ["Not provided", "help"], not_stated: ["Not stated in the report", "help"], looked_up: ["Used to look up the record", "search"],
};
const REQUEST_ICON: Record<string, IconName> = { approved: "edit", queued: "clock", sent: "mail", failed: "alert", response_received: "check" };

export const requestText = (r: { to: string; subject: string; body: string; attachments: { filename: string }[] }) =>
  `To: ${r.to}\nSubject: ${r.subject}\n\n${r.body}\n\nAttachments: ${r.attachments.length ? r.attachments.map((a) => a.filename).join(", ") : "none"}\n`;

export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch {
    try {
      const box = document.createElement("textarea");
      box.value = text; box.setAttribute("readonly", ""); box.style.position = "fixed"; box.style.opacity = "0";
      document.body.appendChild(box); box.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(box);
      return ok;
    } catch { return false; }
  }
}

export function downloadText(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}

export function StatusChip({ status, label }: { status: string; label: string }) {
  const { t } = useI18n();
  return <span className={`badge vstatus ${STATUS_TONE[status] ?? ""}`}><Icon name={STATUS_ICON[status] ?? "help"} size={14}/> {t(label)}</span>;
}

function SimulatedNote({ text }: { text?: string }) {
  const { t } = useI18n();
  return <div className="note amber simbanner" role="status"><strong>SIMULATED BANK RESPONSE</strong> · {t(text ?? "Fictional records written for this demo. No bank was asked, and this is not a bank's answer.")}</div>;
}

function Comparison({ v }: { v: VerificationOut }) {
  const { t, language } = useI18n();
  const when = v.checkedAt ? new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "medium" }).format(new Date(v.checkedAt)) : t("not answered");
  return <div>
    {v.banner && <SimulatedNote/>}
    {v.comparison.length > 0 ? <div className="tablewrap"><table className="compare" aria-label={t("Comparison with the bank's record")}>
      <thead><tr><th scope="col">{t("Field")}</th><th scope="col">{t("Reported value")}</th><th scope="col">{t("Bank-returned value")}</th><th scope="col">{t("Result")}</th></tr></thead>
      <tbody>{v.comparison.map((r) => { const [word, icon] = RESULT_WORDS[r.result] ?? [r.result, "help" as IconName];
        return <tr key={r.field}><th scope="row">{t(r.label)}</th><td>{r.reported ?? t("Not stated")}</td><td>{r.bank ?? t("Not provided")}</td>
          <td className={r.result === "differs" ? "differs" : undefined}><Icon name={icon} size={14}/> {t(word)}</td></tr>; })}</tbody></table></div>
      : <p className="small muted">{t("The bank returned no details to compare.")}</p>}
    <dl className="kv small">
      <dt>{t("Bank or provider")}</dt><dd>{v.provider.name ?? t("None")} · {v.mode === "live" ? t("live integration") : v.mode === "sandbox" ? t("simulated sandbox adapter") : t("no integration used")}</dd>
      <dt>{t("Response reference")}</dt><dd className="mono">{v.responseReference ?? t("None returned")}</dd>
      <dt>{t("Checked at")}</dt><dd><time dateTime={v.checkedAt ?? undefined}>{when}</time></dd>
      <dt>{t("Evidence source")}</dt><dd>{t(v.evidenceSource)}</dd>
    </dl>
    {v.detail && <p className="small">{v.detail}</p>}
    {v.stale && <p className="small muted">{t("The case changed after this check. Verify again to include the change.")}</p>}
  </div>;
}

function PaymentCard({ p, caseId, busy, run, onDraft, onTab }: { p: VerificationPayment; caseId: string; busy: boolean; run: (work: () => Promise<unknown>) => Promise<void>; onDraft: (d: Proposal) => void; onTab: (id: string) => void }) {
  const { t, language } = useI18n();
  const clock = (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  const required = p.missing.filter((m) => m.required);
  const next = p.nextAction;
  const checking = p.status === "checking";
  const askCitizen = () => run(async () => { onDraft(await wf.draftRequest(caseId, [p.paymentId], `Please give us: ${required.map((m) => m.label).join("; ")}.`)); });
  return <article className="vpayment" aria-label={p.name}>
    <div className="sectionhead"><b>{p.name}</b><StatusChip status={p.status} label={p.statusLabel}/></div>
    <p className="small muted" role="status">
      {p.lastCheckedAt ? <>{t("Last checked")}: <time dateTime={p.lastCheckedAt}>{clock(p.lastCheckedAt)}</time></> : t("Never checked with a bank")}
      {" · "}{p.integration.available ? (p.integration.simulated ? t("Simulated bank adapter (fictional records)") : `${t("Integration")}: ${p.integration.bankName}`) : t("Direct verification is not available here")}
    </p>
    {p.latest?.simulated && <SimulatedNote/>}
    {!p.integration.available && p.status !== "awaiting_bank" && <p className="small">{t("Direct verification is unavailable")}: {p.integration.reason}</p>}
    {required.length > 0 && <div className="note amber" role="status"><b>{t("Missing")}:</b> <ul className="plain-list">{required.map((m) => <li key={m.key}>{t(m.label)}</li>)}</ul>
      <span className="small">{t("Nothing is guessed. Ask the citizen with the existing question; you approve its wording.")}</span></div>}
    <p className="small"><b>{t("Next")}:</b> {t(next.label)} · {next.text}</p>
    <div className="inline-form">
      {required.length > 0
        ? <button type="button" disabled={busy} onClick={() => void askCitizen()}>{t("Ask the citizen for it")}</button>
        : <button type="button" disabled={busy || checking} aria-describedby={`v-${p.paymentId}-why`} onClick={() => void run(() => bank.verify(caseId, p.paymentId))}>
            <Icon name="shield" size={16}/> {t(checking ? "Checking…" : "Verify transaction")}</button>}
      <button type="button" className="secondary" disabled={busy || !p.canVerify} onClick={() => void run(async () => { onDraft(await wf.draftBank(caseId, [p.paymentId])); })}>
        <Icon name="mail" size={16}/> {t("Request bank information")}</button>
      {required.length > 0 && <button type="button" className="secondary" onClick={() => onTab("evidence")}>{t("Open the evidence")}</button>}
    </div>
    <span id={`v-${p.paymentId}-why`} className="small muted">{p.canVerify ? t("Asks the bank integration. Nothing is changed in the case.") : t("A bank check needs the reference and the amount.")}</span>
    <details><summary>{t("What was read, and from where")}</summary>
      <div className="tablewrap"><table className="compare"><thead><tr><th scope="col">{t("Field")}</th><th scope="col">{t("Value")}</th><th scope="col">{t("Source")}</th></tr></thead>
        <tbody>{p.inputs.map((f) => <tr key={f.key}><th scope="row">{t(f.label)}</th><td>{f.value ?? <span className="differs">{t("Missing")}</span>}{f.note ? <small className="muted"> · {t(f.note)}</small> : null}</td>
          <td>{f.source ? <>{f.source.label}<br/><span className="small muted">{t(f.source.basis)}</span></> : <span className="muted">{t("No source")}</span>}</td></tr>)}</tbody></table></div>
      {p.missing.filter((m) => !m.required).map((m) => <p className="small muted" key={m.key}>{t("Also missing")}: {t(m.label)}. {t(m.why)}</p>)}
      <p className="small muted">{t("Reading a receipt or screenshot is a preliminary check. It is never a bank confirmation.")}</p>
    </details>
    {p.latest && <details open={p.status === "bank_confirmed" || p.status === "discrepancy"}><summary>{t("Comparison with the bank's record")}</summary><Comparison v={p.latest}/></details>}
    {p.history.length > 0 && <details><summary>{t("History")} ({p.history.length})</summary>
      <ol className="plain-list small vhistory">{p.history.map((h) => <li key={h.id}><StatusChip status={h.status} label={h.label}/>{" "}
        <time dateTime={h.checkedAt ?? h.createdAt}>{clock(h.checkedAt ?? h.createdAt)}</time> · {t("attempt")} {h.attempt}{h.simulated ? ` · ${t("simulated")}` : ""}
        {h.requestedBy ? ` · ${h.requestedBy}` : ""}{h.detail ? ` · ${h.detail}` : ""}</li>)}</ol>
      <p className="small muted">{t("Each result keeps the time it was obtained. An older result is not a fresh check.")}</p></details>}
  </article>;
}

function ResponseCard({ r }: { r: BankResponseOut }) {
  const { t, language } = useI18n();
  const when = new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(r.createdAt));
  return <li className="vreply">
    <span className="small"><b>{t("Reply attached")}</b> · <time dateTime={r.createdAt}>{when}</time>{r.from ? ` · ${r.from}` : ""}{r.filename ? ` · ${r.filename}` : ""}</span>
    <span className="badge amber">{t("Officer-provided evidence: not an authenticated bank response")}</span>
    {r.needsReview && <span className="note amber small" role="status"><Icon name="alert" size={14}/> {t("Check that this belongs here")}: {r.associationNote}</span>}
    {!r.needsReview && <span className="small muted">{r.associationNote}</span>}
    {r.summary.length > 0 && <ul className="plain-list small">{r.summary.map((s) => <li key={`${s.start}`}><q className="quote">{s.text}</q> <span className="muted">({t("characters")} {s.start}–{s.end} {t("of the reply")})</span></li>)}</ul>}
    <details><summary>{t("Read the whole reply")}</summary><pre className="summary">{r.body}</pre></details>
  </li>;
}

function RequestCard({ r, caseId, busy, run }: { r: BankRequestOut; caseId: string; busy: boolean; run: (work: () => Promise<unknown>) => Promise<void> }) {
  const { t, language } = useI18n();
  const [copied, setCopied] = useState<string | null>(null);
  const clock = (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  const text = requestText(r);
  return <li className="vrequest">
    <div className="sectionhead"><b>{t("To")} {r.bankName}</b><span className={`badge vstatus ${r.state === "failed" ? "red" : r.state === "response_received" ? "green" : "amber"}`}><Icon name={REQUEST_ICON[r.state] ?? "mail"} size={14}/> {t(r.stateLabel)}</span></div>
    {r.banner && <SimulatedNote text="No email left this system."/>}
    <p className="small muted">{t(r.deliveryLabel)} · {t("Approved by")} {r.approvedBy ?? "—"} {clock(r.approvedAt)}{r.sentAt ? ` · ${t("Recorded as sent")} ${clock(r.sentAt)}` : ` · ${t("Not sent")}`}</p>
    {r.sentNote && <p className="small">{t(r.sentNote)}</p>}
    {r.lastError && <div className="note amber" role="alert"><b>{t("Sending failed")}</b>: {r.lastError} <span className="small">{t("Your approved request is kept. Try again, or copy it and send it yourself.")}</span></div>}
    <p className="small">{t("Response reference")}: <span className="mono">{r.responseRef}</span> · <span className="muted">{t("A reply that quotes it is linked to this case.")}</span></p>
    <details><summary>{t("Recipient, subject, text and attachments")}</summary>
      <dl className="kv small"><dt>{t("Recipient")}</dt><dd>{r.to}</dd><dt>{t("Subject")}</dt><dd>{r.subject}</dd>
        <dt>{t("Attachments")}</dt><dd>{r.attachments.length ? r.attachments.map((a) => <a key={a.id} href={`/api/officer/cases/${caseId}/sources/${a.id}`} target="_blank" rel="noreferrer noopener">{a.filename} </a>) : t("None")}</dd></dl>
      <pre className="summary">{r.body}</pre></details>
    <div className="inline-form">
      {r.canSend && <button type="button" disabled={busy} onClick={() => void run(() => bank.send(caseId, r.id))}><Icon name="refresh" size={16}/> {t(r.state === "failed" ? "Try sending again" : "Send now")}</button>}
      {r.canMarkSent && <button type="button" className="secondary" disabled={busy} onClick={() => void run(() => bank.markSent(caseId, r.id))}>{t("I sent this myself")}</button>}
      <IconButton name="clip" label={t("Copy the request")} onClick={() => void copyText(text).then((ok) => setCopied(ok ? t("Copied to the clipboard.") : t("Could not copy. Select the text and copy it.")))}/>
      <IconButton name="download" label={t("Download the request")} onClick={() => downloadText(`bank-request-${r.responseRef}.txt`, text)}/>
      {copied && <span className="small muted" role="status">{copied}</span>}
    </div>
    {r.replies.length > 0 && <ul className="plain-list vreplies">{r.replies.map((x) => <ResponseCard key={x.id} r={x}/>)}</ul>}
  </li>;
}

function AttachResponse({ caseId, requests, busy, run }: { caseId: string; requests: BankRequestOut[]; busy: boolean; run: (work: () => Promise<unknown>) => Promise<void> }) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [requestId, setRequestId] = useState("");
  const [from, setFrom] = useState("");
  const [filename, setFilename] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const read = async (file: File | undefined) => {
    if (!file) return;
    if (file.size > 200_000) { setProblem(t("That file is too large to attach as text (200 KB at most). Paste the relevant part instead.")); return; }
    setProblem(null); setFilename(file.name); setText(await file.text());
  };
  return <details className="attach"><summary>{t("Attach a bank response")}</summary>
    <p className="small muted">{t("Paste the bank's reply, or choose a text file. It is kept as officer-provided evidence: it is not treated as an authenticated bank response and does not change any verification.")}</p>
    <label className="small" htmlFor="resp-text">{t("The reply")}</label>
    <textarea id="resp-text" rows={5} maxLength={20000} value={text} onChange={(e) => setText(e.target.value)}/>
    <div className="inline-form">
      <label className="small">{t("Text file")} <input type="file" accept=".txt,.eml,text/plain" onChange={(e) => void read(e.target.files?.[0])}/></label>
      <label className="small">{t("From (optional)")} <input value={from} maxLength={254} onChange={(e) => setFrom(e.target.value)}/></label>
      <label className="small">{t("It answers")} <select value={requestId} onChange={(e) => setRequestId(e.target.value)}>
        <option value="">{t("Use the response reference in the text")}</option>{requests.map((r) => <option key={r.id} value={r.id}>{r.bankName} · {r.responseRef}</option>)}</select></label>
    </div>
    {problem && <p className="differs small" role="alert">{problem}</p>}
    <button type="button" disabled={busy || text.trim().length === 0}
            onClick={() => void run(async () => { await bank.attach(caseId, { text, requestId: requestId || undefined, fromAddress: from || undefined, filename: filename ?? undefined }); setText(""); setFrom(""); setFilename(null); setRequestId(""); })}>
      {t("Attach this response")}</button>
    <span className="small muted"> {t("If it fails, what you typed stays here.")}</span>
  </details>;
}

export function TransactionVerification({ caseId, reload, onChanged, onDraft, onTab }: { caseId: string; reload: number; onChanged: () => void; onDraft: (d: Proposal) => void; onTab: (id: string) => void }) {
  const { t } = useI18n();
  const [section, setSection] = useState<VerificationSection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setSection(await bank.section(caseId)); } catch (e) { setError(e instanceof Error ? e.message : "Could not load the verification"); }
  }, [caseId]);
  useEffect(() => { void load(); }, [load, reload]);
  const run = useCallback(async (work: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { const out = await work(); if (out && typeof out === "object" && "section" in out) setSection((out as { section: VerificationSection }).section); await load(); onChanged(); }
    catch (e) { setError(e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : "That did not work"); }
    finally { setBusy(false); }
  }, [load, onChanged]);
  if (!section) return error ? <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => { setError(null); void load(); } }}/> : <StatePanel kind="loading"/>;
  return <section className="card" id="sec-verification" aria-label={t("Transaction verification")}>
    <div className="sectionhead"><h2>{t("Transaction verification")}</h2>
      <IconButton name="refresh" label={t("Refresh the verification")} onClick={() => void load()}/></div>
    {error && <ErrorNotice message={error}/>}
    <p className="small muted">{t(section.notices[0])}</p>
    {section.payments.length === 0 && <p className="muted">{t("This report has no payment to verify.")}</p>}
    {section.payments.map((p) => <PaymentCard key={p.paymentId} p={p} caseId={caseId} busy={busy} run={run} onDraft={onDraft} onTab={onTab}/>)}
    <p className="small muted">{t(section.notices[1])}</p>
    {(section.requests.length > 0 || section.unlinkedResponses.length > 0) && <div className="vrequests"><h3>{t("Requests to the bank and their replies")}</h3>
      <ul className="plain-list">{section.requests.map((r) => <RequestCard key={r.id} r={r} caseId={caseId} busy={busy} run={run}/>)}</ul>
      {section.unlinkedResponses.length > 0 && <><h4>{t("Replies not linked to a request")}</h4><ul className="plain-list">{section.unlinkedResponses.map((x) => <ResponseCard key={x.id} r={x}/>)}</ul></>}</div>}
    <AttachResponse caseId={caseId} requests={section.requests} busy={busy} run={run}/>
  </section>;
}
