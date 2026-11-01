/** What an officer can learn about other reports: the connections found for this one, how an earlier report was handled, and the assistant that
 * runs those searches on request.
 *
 * Everything shown is read from records the officer may search. An exact identifier, a similar story and a similar name are kept apart and
 * labelled in words. Nothing here sends, shares or decides: a draft the assistant prepares waits for the officer in the Messages tab.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { Dialog } from "./dialog";
import { Icon, IconButton } from "./icons";
import { StatePanel } from "./statepanel";
import { MoneyText, useI18n } from "./ui";
import { CaseConsultations } from "./officerconsult";
import { consult, investigate, wf, type Candidate, type Comparison, type ConnectionSearch, type Handling, type Origin, type Run, type RunAction, type RunCard, type RelatedEvidence } from "./workflowapi";

const RELATION: Record<string, string> = { same: "Same", same_identifier: "Same identifier", different: "Different", only_mine: "Only in this report", only_theirs: "Only in the earlier report", not_stated: "Not stated", info: "For information" };
const STRONG = new Set(["exact_identifier", "possible_duplicate"]);
const message = (e: unknown, fallback: string) => (e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : e instanceof Error ? e.message : fallback);

export type Command = { type: "compare" | "handling"; handle: string; n: number } | null;

// ---- the comparison --------------------------------------------------------------------------------------------------------------

function ComparisonDialog({ caseId, handle, onClose, onHandling, onSearchAgain }: { caseId: string; handle: string | null; onClose: () => void; onHandling: (h: string) => void; onSearchAgain: () => void }) {
  const { t } = useI18n();
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null); setError(null);
    if (!handle) return;
    let live = true;
    investigate.comparison(caseId, handle).then((c) => { if (live) setData(c); }).catch((e) => { if (live) setError(message(e, "Could not load the comparison")); });
    return () => { live = false; };
  }, [caseId, handle]);
  const groups = data ? [...new Set(data.rows.map((r) => r.group))] : [];
  const cell = (c: Comparison["rows"][number]["mine"]) => <>
    <span className="cellvalue">{c.text ?? <span className="muted">{c.text === null ? "—" : ""}</span>}</span>
    {c.originLabel && c.text ? <span className="small muted block">{t(c.originLabel)}</span> : null}
    {c.openUrl && c.filename ? <a className="small block" href={c.openUrl} target="_blank" rel="noreferrer noopener">{c.filename}</a> : null}
  </>;
  return <Dialog title={data ? `${data.current.caseRef} · ${data.earlier.caseRef}` : t("Comparison")} open={handle !== null} onClose={onClose}
    footer={data ? <button type="button" className="secondary" onClick={() => { onClose(); onHandling(data.handle); }}>{t("How was it handled?")}</button> : undefined}>
    {error && <StatePanel kind="error" message={error}/>}
    {!data && !error && <StatePanel kind="loading"/>}
    {data && <div role="region" aria-label={t("Side-by-side comparison")}>
      {(data.stale || data.decisionStale) && <div className="note amber" role="alert"><b>{t("Out of date")}:</b> {data.staleReason ? data.staleReason : t("You decided on this connection before something in a report changed. Look at it again.")}
        <button type="button" className="secondary" onClick={onSearchAgain}>{t("Search again")}</button></div>}
      <p><span className={`badge ${STRONG.has(data.resultKind) ? "green" : "amber"}`}><Icon name={STRONG.has(data.resultKind) ? "check" : "search"} size={14}/>{t(data.band)}</span></p>
      <p className="small">{t(data.bandNote)}</p>
      <div className="tablewrap"><table className="compare comparison"><thead><tr><th scope="col">{t("Detail")}</th><th scope="col">{t("This report")} ({data.current.caseRef})</th><th scope="col">{t("Earlier report")} ({data.earlier.caseRef})</th><th scope="col">{t("Compared")}</th></tr></thead>
        {groups.map((g) => <tbody key={g}><tr><th colSpan={4} className="groupname">{g}</th></tr>
          {data.rows.filter((r) => r.group === g).map((r, i) => <tr key={i} className={r.relation === "different" ? "rowdiffers" : undefined}>
            <th scope="row">{t(r.label)}</th><td data-label={t("This report")}>{cell(r.mine)}</td><td data-label={t("Earlier report")}>{cell(r.theirs)}</td>
            <td data-label={t("Compared")}><span className={`badge ${r.relation === "different" || r.relation === "only_mine" || r.relation === "only_theirs" ? "amber" : r.relation === "same" || r.relation === "same_identifier" ? "green" : ""}`}>{t(RELATION[r.relation])}</span></td></tr>)}
        </tbody>)}</table></div>
      {data.contradictions.length > 0 && <div className="note amber"><b>{t("To look at")}:</b><ul className="plain-list">{data.contradictions.map((c) => <li key={c}>{c}</li>)}</ul></div>}
      <p className="small muted">Owner of the earlier report: {data.earlier.owner ?? "—"} · {data.earlier.area ?? ""}. {t(data.note)}</p>
    </div>}
  </Dialog>;
}

// ---- how an earlier report was handled -------------------------------------------------------------------------------------------

function HandlingView({ caseId, handle }: { caseId: string; handle: string }) {
  const { t, language } = useI18n();
  const [data, setData] = useState<Handling | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null); setError(null);
    let live = true;
    investigate.handling(caseId, handle).then((h) => { if (live) setData(h); }).catch((e) => { if (live) setError(message(e, "Could not load the handling record")); });
    return () => { live = false; };
  }, [caseId, handle]);
  if (error) return <StatePanel kind="error" message={error}/>;
  if (!data) return <StatePanel kind="loading"/>;
  const when = (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium" }).format(new Date(iso));
  const r = data.recovery;
  return <div role="region" aria-label={t("Past handling of an earlier report")} className="handling">
    {data.staleReason && <div className="note amber" role="alert">{data.staleReason}</div>}
    <h3>{data.caseRef} · {data.title}</h3>
    <p className="small muted">{t("Handled by")}: <b>{data.owner.name ?? "—"}</b>{data.area ? ` · ${data.area}` : ""}. {t(data.scope)}</p>
    <section aria-label={t("Recorded actions")}>
      <h4>{t("What was done")}</h4>
      <ol className="timeline">{data.timeline.map((e) => <li key={`${e.recordType}-${e.recordId}`}>
        <time dateTime={e.at}>{when(e.at)}</time> <b>{e.label}</b>{e.actor.name ? <span className="muted"> · {e.actor.name}</span> : null}
        {e.simulated ? <span className="badge badge-fixture">{t("Simulated")}</span> : null}{e.reviewed ? <span className="badge green">{t("Reviewed")}</span> : null}
        {e.detail ? <span className="small block muted">{e.detail}</span> : null}
        {e.wording ? <q className="quote block">{e.wording}</q> : null}</li>)}</ol>
      {data.timeline.length === 0 && <p className="muted">{t("No actions are recorded on this report.")}</p>}
    </section>
    <div className="statusgrid">
      <section className="statuspanel" aria-label={t("Money recorded as returned")}><h4>{t("Recorded as returned")}</h4>
        <p><MoneyText minor={String(r.recoveredMinor)}/> {t("of")} <MoneyText minor={String(r.reviewedDebitMinor)}/></p>
        <p className="small muted">{t("Still outstanding")}: <MoneyText minor={String(r.outstandingMinor)}/>{r.checkedBy ? ` · ${t(r.checkedBy === "officer" ? "checked by an officer" : "confirmed by the citizen")}` : ""}</p>
        <p className="small muted">{t(r.note)}</p></section>
      <section className="statuspanel" aria-label={t("Outcome recorded by an officer")}><h4>{t("Outcome an officer recorded")}</h4>
        <p><span className={`badge ${data.outcome.label === "unknown" ? "" : "amber"}`}>{t(data.outcome.label === "unknown" ? "Unknown" : data.outcome.label)}</span></p>
        <p className="small muted">{t(data.outcome.note)}</p></section>
      <section className="statuspanel" aria-label={t("Case status")}><h4>{t("CyberBrief's tracking")}</h4>
        <p>{data.caseStatus.reviewState}{data.caseStatus.workflowState ? ` · ${data.caseStatus.workflowState.replace(/_/g, " ")}` : ""}</p>
        <p className="small muted">{t(data.caseStatus.note)}</p>
        {data.officialStatus ? <p className="small">{t("Official status, as supplied")}: {data.officialStatus.category.replace(/_/g, " ")} ({t("not verified")})</p> : <p className="small muted">{t("No official status was supplied.")}</p>}</section>
      <section className="statuspanel" aria-label={t("Simulated bank and citizen responses")}><h4>{t("Replies")}</h4>
        <p className="small">{t("The citizen replied")}: {data.citizenResponse.replies} · {t("evidence added")}: {data.evidenceObtained.files + data.evidenceObtained.written}</p>
        <p className="small">{t("Simulated bank replies")}: {data.simulatedBankResponse.replies} ({data.simulatedBankResponse.reviewed} {t("reviewed")})</p>
        <p className="small muted">{t(data.simulatedBankResponse.note)}</p></section>
    </div>
    <section aria-label={t("Still unresolved")}><h4>{t("Still unresolved")}</h4>
      {data.unresolved.length === 0 ? <p className="muted">{t("Nothing is recorded as open.")}</p> : <ul className="plain-list">{data.unresolved.map((u) => <li key={u.label}>{u.label}</li>)}</ul>}</section>
    <section aria-label={t("What an officer learned")}><h4>{t("What an officer wrote")}</h4>
      {data.lessons.length === 0 ? <p className="muted">{t("No officer has recorded a lesson for this report.")}</p> : data.lessons.map((l) => <div className="lesson" key={l.id}>
        <p><span className="badge amber">{t(l.outcomeKind)}</span> <span className="small muted">{l.author ?? ""} · {when(l.at)} · {t(l.reviewState === "reviewed" ? "reviewed" : "entered, not yet reviewed")}</span></p><p>{l.text}</p></div>)}</section>
    <p className="small muted"><b>{t("Generated from the records")}:</b> {data.generatedSummary.text}</p>
  </div>;
}

// ---- the connections: matches and past handling ------------------------------------------------------------------------------------

function CandidateCard({ c, busy, onCompare, onHandling, onReview, onEvidence, onConsult }: { c: Candidate; busy: boolean; onCompare: () => void; onHandling: () => void; onReview: (state: "reviewed" | "dismissed", reason?: string) => void; onEvidence?: () => void; onConsult?: () => void }) {
  const { t } = useI18n();
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");
  const strong = STRONG.has(c.resultKind);
  return <article className="candidate" aria-label={`${c.caseRef} · ${t(c.band)}`}>
    <div className="sectionhead"><b><span className="mono">{c.caseRef}</span> · {c.title}</b>
      <span className="badges"><span className={`badge ${strong ? "green" : "amber"}`}><Icon name={strong ? "check" : "search"} size={14}/>{t(c.band)}</span>
        {c.state === "reviewed" && <span className="badge green">{t("Reviewed relationship")}</span>}{c.state === "dismissed" && <span className="badge">{t("Dismissed")}</span>}
        {c.possibleDuplicate && <span className="badge amber">{t("Possible duplicate")}</span>}</span></div>
    <p className="small muted">{c.area ?? ""} · {c.owner.name ? `${t("Owner")}: ${c.owner.name}${c.owner.isYou ? ` (${t("you")})` : ""}` : t("Not yet assigned")}</p>
    {c.decisionStale && <div className="note amber" role="status">{t("You decided on this before something in a report changed. Look at it again.")}</div>}
    <h4 className="small">{t("Why it appears")}</h4>
    <ul className="plain-list">{c.matchedSignals.map((s, i) => <li key={`${s.kind}-${i}`}><b>{t(s.label)}</b>{s.value && s.entityType !== "method" ? `: ${s.value}` : ""} <span className="small muted">({t(s.strength === "strong" ? "strong signal" : "weak signal")}{s.attribution ? `; ${t(s.attribution)}` : ""})</span>
      {s.mine?.openUrl && <a className="small" href={s.mine.openUrl} target="_blank" rel="noreferrer noopener"> {t("Source in this report")}</a>}
      {s.theirs?.openUrl && <a className="small" href={s.theirs.openUrl} target="_blank" rel="noreferrer noopener"> {t("Source in the earlier report")}</a>}</li>)}</ul>
    <p className="small">{t(c.bandNote)}</p>
    {c.sameReportAs && c.sameReportAs.length > 0 && <p className="small differs">{t("Gives the same transaction reference as")} {c.sameReportAs.join(", ")}: {t("counted once")}.</p>}
    {(c.differences.length > 0 || c.contradictions.length > 0) && <><h4 className="small">{t("Differences and limits")}</h4>
      <ul className="plain-list small">{c.differences.map((d) => <li key={d}>{d}</li>)}{c.contradictions.map((d) => <li key={d} className="differs">{d}</li>)}</ul></>}
    <p className="small muted">{t("Read as")}: {t(c.sourceQuality.mine)} · {t(c.sourceQuality.theirs)}</p>
    {c.state === "dismissed" && c.reason && <p className="small">{t("Why it was dismissed")}: {c.reason}</p>}
    <div className="inline-form">
      <button type="button" disabled={busy} onClick={onCompare}>{t("Compare")} {c.caseRef}</button>
      <button type="button" className="secondary" disabled={busy} onClick={onHandling}>{t("How was it handled?")}</button>
      {onEvidence && <button type="button" className="secondary" disabled={busy} onClick={onEvidence}>{t("Open the earlier report's evidence")}</button>}
      {onConsult && <button type="button" className="secondary" disabled={busy} onClick={onConsult}><Icon name="mail" size={16}/> {t("Ask")} {c.owner.name ?? t("the officer")} {t("about this case")}</button>}
      {c.state === "possible" && strong && <button type="button" className="secondary" disabled={busy} onClick={() => onReview("reviewed")}>{t("Mark as reviewed link")}</button>}
      {c.state !== "dismissed" && <button type="button" className="secondary" disabled={busy} onClick={() => setDismissing((v) => !v)} aria-expanded={dismissing}>{t("Not related")}</button>}
    </div>
    {dismissing && <div className="inline-form">
      <label className="small" htmlFor={`why-${c.handle}`}>{t("Why is it not related?")}</label>
      <input id={`why-${c.handle}`} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)}/>
      <button type="button" disabled={busy || !reason.trim()} onClick={() => { onReview("dismissed", reason.trim()); setDismissing(false); setReason(""); }}>{t("Dismiss this connection")}</button></div>}
  </article>;
}

const MISSING: Record<string, string> = { recipient_upi: "a recipient UPI ID", recipient_upi_complete: "a complete recipient UPI ID (the one stated is masked or partial)", transaction_reference: "a complete transaction reference" };

export function ConnectionsPanel({ caseId, reload, command, onChanged, onAsk, onConsultation }: { caseId: string; reload: number; command: Command; onChanged: () => void; onAsk: (prompt: string) => void; onConsultation: (id: string) => void }) {
  const { t } = useI18n();
  const [view, setView] = useState<"matches" | "handling">("matches");
  const [found, setFound] = useState<ConnectionSearch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [compare, setCompare] = useState<string | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<RelatedEvidence | null>(null);
  const latest = useRef(0);   // two searches can be in flight (the page reloads after a decision); only the newest answer is shown
  const search = useCallback(async () => {
    const mine = ++latest.current;
    setError(null);
    try { const out = await investigate.search(caseId); if (mine === latest.current) setFound(out); }
    catch (e) { if (mine === latest.current) setError(message(e, "Could not search")); }
  }, [caseId]);
  useEffect(() => { void search(); }, [search, reload]);
  useEffect(() => {
    if (!command) return;
    if (command.type === "compare") setCompare(command.handle);
    else { setView("handling"); setHandle(command.handle); }
  }, [command]);
  const act = async (work: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await work(); await search(); onChanged(); } catch (e) { setError(message(e, "That did not work")); } finally { setBusy(false); }
  };
  const list = found?.candidates ?? [];
  const review = list.filter((c) => c.state === "possible" && STRONG.has(c.resultKind));
  const weak = list.filter((c) => c.state === "possible" && !STRONG.has(c.resultKind));
  const decided = list.filter((c) => c.state !== "possible");
  const openHandling = (h: string) => { setHandle(h); setView("handling"); };
  const card = (c: Candidate) => <li key={c.handle}><CandidateCard c={c} busy={busy} onCompare={() => setCompare(c.handle)} onHandling={() => openHandling(c.handle)}
    onReview={(state, reason) => void act(() => investigate.review(caseId, c.handle, state, reason))}
    onEvidence={c.linkId && c.resultKind === "exact_identifier" ? () => void act(async () => { setEvidence(await wf.relatedEvidence(caseId, c.linkId!)); }) : undefined}
    onConsult={STRONG.has(c.resultKind) && c.state !== "dismissed" && !c.owner.isYou ? () => void act(async () => { onConsultation((await consult.draft(caseId, c.handle)).id); }) : undefined}/></li>;
  return <section className="card" id="sec-related" aria-label={t("Connections to other reports")}>
    <div className="sectionhead"><h2>{t("Connections")}</h2>
      <span className="inline-form"><IconButton name="refresh" label={t("Search again")} onClick={() => void search()} disabled={busy}/>
        <button type="button" className="secondary" onClick={() => onAsk("Find earlier reports with this recipient.")}><Icon name="spark" size={16}/> {t("Ask investigator")}</button></span></div>
    <div className="subnav" role="group" aria-label={t("Connections views")}>
      <button type="button" className={`filter${view === "matches" ? " active" : ""}`} aria-pressed={view === "matches"} onClick={() => setView("matches")}>{t("Matches")}{review.length ? ` (${review.length})` : ""}</button>
      <button type="button" className={`filter${view === "handling" ? " active" : ""}`} aria-pressed={view === "handling"} onClick={() => setView("handling")}>{t("Past handling")}</button></div>
    {error && <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => void search() }}/>}
    {!found && !error && <StatePanel kind="loading"/>}
    {found && view === "matches" && <>
      <p className="small muted" role="status">{t("Searched")}: {t(found.scope.label)} · {t("{n} report(s) searched", { n: found.scanned })}.</p>
      {found.missingData.length > 0 && <div className="note amber"><b>{t("Missing from this report")}:</b> {found.missingData.map((m) => t(MISSING[m] ?? m)).join("; ")}.
        {" "}{t("Without it a search by recipient is not possible.")} <button type="button" className="secondary" onClick={() => onAsk("What evidence is missing before the next action?")}>{t("Ask what is missing")}</button></div>}
      {list.length === 0 && found.missingData.length === 0 && <div className="empty"><h3>{t("No earlier report matches")}</h3><p className="muted">{t("None of the reports you may search shares this report's recipient or reported method. That does not rule out a report you cannot search.")}</p></div>}
      {review.length > 0 && <section aria-label={t("Strong leads to review")}><h3>{t("Exact identifier matches")}</h3><ul className="candidates">{review.map(card)}</ul></section>}
      {weak.length > 0 && <section aria-label={t("Weaker leads")}><h3>{t("Weaker leads")}</h3><p className="small muted">{t("These do not share an identifier. A similar story or name is only something to look at.")}</p><ul className="candidates">{weak.map(card)}</ul></section>}
      {decided.length > 0 && <section aria-label={t("Already decided")}><h3>{t("Already decided")}</h3><ul className="candidates">{decided.map(card)}</ul></section>}
      <CaseConsultations caseId={caseId} reload={reload} onOpen={onConsultation}/>
      {found.truncated && <p className="small muted">{t("Only the first {n} are shown.", { n: list.length })}</p>}
      <ul className="plain-list small muted">{found.caveats.map((c) => <li key={c}>{t(c)}</li>)}</ul>
    </>}
    {found && view === "handling" && <>
      {list.length === 0 ? <div className="empty"><h3>{t("No connection to read")}</h3><p className="muted">{t("Run a search first. Past handling is shown for an earlier report that was found.")}</p></div> : <>
        <label htmlFor="pick-handling" className="small">{t("Earlier report")}</label>
        <select id="pick-handling" value={handle ?? ""} onChange={(e) => setHandle(e.target.value || null)}>
          <option value="">{t("Choose a report")}</option>{list.map((c) => <option key={c.handle} value={c.handle}>{c.caseRef} · {c.title} ({t(c.band)})</option>)}</select>
        {handle ? <HandlingView caseId={caseId} handle={handle}/> : <p className="muted">{t("Choose an earlier report to see how it was handled.")}</p>}</>}
    </>}
    <ComparisonDialog caseId={caseId} handle={compare} onClose={() => setCompare(null)} onHandling={openHandling} onSearchAgain={() => { setCompare(null); void search(); }}/>
    <Dialog title={evidence ? `${evidence.caseRef} · ${evidence.title}` : ""} open={evidence !== null} onClose={() => setEvidence(null)}>
      {evidence && <div role="region" aria-label={t("Earlier report evidence")}>
        <p className="small">{evidence.linkedBecause}</p>
        <ul className="plain-list">{evidence.sharedPayments.map((p) => <li key={`${p.name}-${p.reference}`}>{p.name}: {p.amount} · {t("reference")} {p.reference} · {p.status} · {p.date}</li>)}</ul>
        <ul className="plain-list">{evidence.sources.map((x) => <li key={x.id}><a href={x.url} target="_blank" rel="noreferrer noopener">{x.filename}</a></li>)}</ul>
        <p className="small muted">{evidence.note} {t("Opening this was recorded in the audit trail.")}</p></div>}
    </Dialog>
  </section>;
}

// ---- the assistant -----------------------------------------------------------------------------------------------------------------

export const DEFAULT_GOAL = "Investigate this report: find earlier complaints with the same recipient or a similar reported method, show how they were handled and prepare next steps.";
export const PROMPTS = ["Find earlier reports with this recipient.", "Compare the strongest match.", "How was that case handled?", "What evidence is missing before the next action?"];
const STATE_WORDS: Record<Run["state"], string> = { queued: "Queued", running: "Working", waiting_for_approval: "Waiting for your approval", completed: "Completed", failed: "Stopped: a step failed", cancelled: "Cancelled", stale: "Out of date" };
const TOOL_WORDS: Record<string, string> = { search_related_cases: "Searched for earlier reports", compare_cases: "Compared with a match", get_case_handling_history: "Read how it was handled", get_resolution_playbook: "Counted comparable outcomes",
  inspect_evidence_gaps: "Checked for gaps", prepare_action_plan: "Prepared next steps", analyze_reported_method: "Read the reported method", get_case_brief: "Read the report",
  draft_officer_consultation: "Drafted a question for a colleague", read_consultation_replies: "Read the colleague's reply" };
const ORIGIN_WORDS: Record<Origin, string> = { citizen_provided: "Citizen-provided", extracted: "Read from evidence, not confirmed", officer_recorded: "Officer-recorded", system_record: "System record", model_suggestion: "Model suggestion" };
const KIND_WORDS: Record<string, string> = { exact_identifier: "Exact identifier match", possible_duplicate: "Possible duplicate report", generic_identifier: "Shared service identifier: the match is real but cannot point at one seller",
  similar_method: "Similar reported method: a weak lead", similar_name: "Similar account name: a weak lead" };
const FOUND = new Set(["matches", "comparison", "handling", "replies", "playbook", "method", "gaps", "brief", "verification_inputs", "verification", "verification_history", "verification_discrepancies"]);
const NEXT = new Set(["consultation", "plan", "verification_draft"]);

/** Where the planner's steps came from, stated plainly: a live model, a replayed fixture, or a fallback to fixed rules (with why). */
function originLine(run: Run, t: (s: string) => string): { text: string; tone: string } {
  if (run.origin === "live") return { text: `${t("Live model chose the steps")}${run.modelId ? ` (${run.modelId})` : ""}`, tone: "green" };
  if (run.origin === "fixture") return { text: t("Fixture replay: no model was called"), tone: "" };
  if (run.telemetry?.planner === "fixed") return { text: t("Fixed steps: an update always reads the reply and plans again"), tone: "" };
  return { text: `${t("Fallback: fixed rules chose the steps")}${run.telemetry?.reason ? ` (${run.telemetry.reason})` : ""}`, tone: "amber" };
}

function Sources({ card }: { card: RunCard }) {
  const { t } = useI18n();
  if (card.supporting.length === 0) return null;
  return <ul className="plain-list small sources" aria-label={t("Supporting records")}>{card.supporting.map((s, i) => <li key={i}>
    <span className="originchip">{t(ORIGIN_WORDS[s.origin ?? "citizen_provided"])}</span> {s.label}{s.excerpt ? <>: <q className="quote">{s.excerpt}</q></> : null}
    {s.openUrl && <a href={s.openUrl} target="_blank" rel="noreferrer noopener"> {t("Open source")}</a>}</li>)}</ul>;
}

function Finding({ card, onAction }: { card: RunCard; onAction: (a: RunAction) => void }) {
  const { t } = useI18n();
  const found = card.kind === "matches" ? (card.data.candidates ?? []) as { handle: string; resultKind: string; caseRef: string }[] : [];
  return <article className="runcard" aria-label={t(card.title)}>
    <h4>{t(card.title)}</h4>
    <p><b>{card.finding}</b></p>
    {found.length > 0 && <ul className="plain-list small" aria-label={t("How each earlier report matches")}>{found.slice(0, 5).map((f) => <li key={f.handle}><span className="mono">{f.caseRef}</span>: {t(KIND_WORDS[f.resultKind] ?? f.resultKind)}</li>)}</ul>}
    <Sources card={card}/>
    {card.mainAction && <button type="button" className="secondary" onClick={() => onAction(card.mainAction!)}>{card.mainAction.label}</button>}
    {card.kind === "playbook" && card.data.counts && <p className="small">{t("Comparable earlier reports")}: {card.data.counts.comparable} · {t("recorded recovery")}: {card.data.counts.withRecordedRecovery} · {t("outcome unknown")}: {card.data.counts.outcomeUnknown} · {t("still unresolved")}: {card.data.counts.stillUnresolved}{card.data.fictionalData ? ` · ${t("fictional data")}` : ""}</p>}
  </article>;
}

type Step = { title: string; reason: string; requiredInputs: string[]; blockingIssues: string[]; review: string; historical: string | null; fromReply?: { by: string; said: string; helped: string; attribution: string; messageId: string } };

function NextAction({ card, onAction }: { card: RunCard; onAction: (a: RunAction) => void }) {
  const { t } = useI18n();
  return <article className="runcard" aria-label={t(card.title)}>
    <h4>{t(card.title)}</h4>
    <p><b>{card.finding}</b></p>
    {card.kind === "plan" && <ol className="plansteps">{((card.data.steps ?? []) as Step[]).map((s) => <li key={s.title}>
      <b>{s.title}</b><span className="small block">{s.reason}</span>
      {s.fromReply && <span className="small block"><span className="originchip">{t("Officer-recorded")}</span> <q className="quote">{s.fromReply.said}</q> {s.fromReply.attribution}</span>}
      {s.historical && <span className="small block muted">{s.historical}</span>}
      <span className="small block muted">{t("Needs")}: {s.requiredInputs.join(" · ")}. {s.review}.</span>{s.blockingIssues.map((b) => <span key={b} className="small block differs">{b}</span>)}</li>)}</ol>}
    {card.mainAction && <button type="button" onClick={() => onAction(card.mainAction!)}>{card.mainAction.label}</button>}
  </article>;
}

export function InvestigatorDrawer({ caseId, initialGoal, onClose, onAction, onRunSettled }: { caseId: string; initialGoal: string; onClose: () => void; onAction: (a: RunAction) => void; onRunSettled: () => void }) {
  const { t } = useI18n();
  const [goal, setGoal] = useState(initialGoal);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLTextAreaElement | null>(null);
  const settled = useRef<string | null>(null);
  useEffect(() => { box.current?.focus(); }, []);
  useEffect(() => { setGoal(initialGoal); }, [initialGoal]);
  useEffect(() => {   // the most recent run for this report, if there is one, so closing and reopening loses nothing
    let live = true;
    investigate.runs(caseId).then((rs) => { if (live && rs.length) setRun(rs[0]); }).catch(() => undefined);
    return () => { live = false; };
  }, [caseId]);
  useEffect(() => {
    if (!run || (run.state !== "queued" && run.state !== "running")) {
      if (run && settled.current !== run.id) { settled.current = run.id; onRunSettled(); }
      return;
    }
    const timer = window.setInterval(() => { investigate.run(caseId, run.id).then(setRun).catch((e) => setError(message(e, "Could not read the progress"))); }, 1000);
    return () => window.clearInterval(timer);
  }, [run, caseId, onRunSettled]);
  const start = async () => {
    setBusy(true); setError(null);
    try { settled.current = null; setRun(await investigate.start(caseId, goal.trim())); } catch (e) { setError(message(e, "Could not start")); } finally { setBusy(false); }
  };
  const cancel = async () => { if (!run) return; try { setRun(await investigate.cancel(caseId, run.id)); } catch (e) { setError(message(e, "Could not cancel")); } };
  const show = async (id: string) => { try { setRun(await investigate.run(caseId, id)); } catch (e) { setError(message(e, "Could not open that run")); } };
  const active = run?.state === "queued" || run?.state === "running";
  const pending = run?.drafts.filter((d) => d.state === "pending") ?? [];
  const drafted = run?.cards.filter((c) => c.kind === "consultation" && c.data.state === "draft") ?? [];
  const found = run?.cards.filter((c) => FOUND.has(c.kind)) ?? [];
  const next = run?.cards.filter((c) => NEXT.has(c.kind)) ?? [];
  const matters = found.filter((c) => c.why || c.limitations.length > 0);
  const origin = run ? originLine(run, t) : null;
  return <aside className="investigator" aria-label={t("Ask investigator")} onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
    <div className="sectionhead"><h2><Icon name="spark" size={18}/> {t("Ask investigator")}</h2><IconButton name="close" label={t("Close the investigator")} onClick={onClose}/></div>
    <p className="small muted">{t("It searches only the reports you may search, reads them and prepares drafts. It never sends, shares or decides anything.")}</p>
    <label htmlFor="inv-goal" className="small">{t("Your question")}</label>
    <textarea id="inv-goal" ref={box} rows={4} maxLength={400} value={goal} onChange={(e) => setGoal(e.target.value)}/>
    <div className="inline-form">
      <button type="button" disabled={busy || active || goal.trim().length < 3} onClick={() => void start()}>{t(run && !active ? "Run again" : "Investigate")}</button>
      {active && <button type="button" className="secondary" onClick={() => void cancel()}>{t("Cancel")}</button>}
      <details className="promptmenu"><summary>{t("More questions")}</summary>
        <div role="group" aria-label={t("Suggested questions")}>{PROMPTS.map((p) => <button key={p} type="button" className="quiet" onClick={() => setGoal(p)}>{t(p)}</button>)}</div></details>
    </div>
    {error && <StatePanel kind="error" message={error}/>}
    <div role="status" aria-live="polite" className="runstate">{run && <><span className={`badge ${run.state === "failed" ? "red" : run.state === "completed" ? "green" : "amber"}`}>{run.state === "queued" ? t("Queued") : active && run.stage ? `${t("Working")}: ${run.stage}` : t(STATE_WORDS[run.state])}</span>
      {active && <span className="small muted"> {t("{n} of up to {max} steps done", { n: run.toolCalls, max: run.toolBudget })}</span>}
      {run.partial && run.state !== "failed" && <span className="badge amber">{t("Partial: stopped at its limit")}</span>}</>}</div>
    {run && <>
      {origin && <p className="small muted"><span className={`badge ${origin.tone}`}>{origin.text}</span></p>}
      {run.phase === 2 && run.parentRunId && <div className="note" role="status">{t("Updated after a colleague's reply. The earlier plan is kept.")} <button type="button" className="quiet" onClick={() => void show(run.parentRunId!)}>{t("Show the plan this updates")}</button></div>}
      {run.supersededBy && <div className="note amber" role="status">{t("A later update replaced this plan; it is kept for the record.")} <button type="button" className="quiet" onClick={() => void show(run.supersededBy!)}>{t("Show the update")}</button></div>}
      {run.state === "stale" && <div className="note amber" role="alert">{t("Out of date")}: {run.staleReason}. <button type="button" className="secondary" onClick={() => void start()} disabled={busy}>{t("Run again")}</button></div>}
      {run.state === "failed" && <div className="note" role="alert">{run.error === "permission_lost" ? t("You no longer have access to this report, so the run stopped.") : run.error} <button type="button" className="secondary" onClick={() => void start()} disabled={busy}>{t("Retry")}</button></div>}
      {run.state === "cancelled" && <div className="note" role="status">{t("Cancelled. What was found before then is kept below.")}</div>}
      {run.summary && run.state !== "failed" && <p className="runsummary"><b>{run.summary.text}</b> <span className="small muted">({run.summary.origin === "model" ? t("the model chose which findings to show first; the wording is the system's") : t("findings listed by fixed rules")})</span></p>}

      {found.length > 0 && <section aria-label={t("What we found")}><h3>{t("What we found")}</h3>{found.map((c) => <Finding key={c.id} card={c} onAction={onAction}/>)}</section>}
      {matters.length > 0 && <section aria-label={t("Why it may matter")}><h3>{t("Why it may matter, and its limits")}</h3>
        <ul className="plain-list small">{matters.map((c) => <li key={c.id}>{c.why ? <>{c.why} </> : null}{c.limitations.map((l) => <span key={l} className="block muted">{l}</span>)}</li>)}</ul></section>}
      {next.length > 0 && <section aria-label={t("Suggested next action")}><h3>{t("Suggested next action")}</h3>{next.map((c) => <NextAction key={c.id} card={c} onAction={onAction}/>)}</section>}
      {run.cards.length === 0 && !active && run.state !== "failed" && <div className="empty"><h3>{t("Nothing to show")}</h3><p className="muted">{t("The run found nothing it could report on.")}</p></div>}
      <section aria-label={t("Waiting for your decision")}><h3 className="small">{t("Waiting for your decision")}</h3>
        {pending.length === 0 && drafted.length === 0 ? <p className="small muted">{t("Nothing is waiting on you from this run.")}</p> : <>
          {pending.map((d) => <div className="queueitem" key={d.id}><p className="small">{String(d.payload.text ?? d.payload.body ?? "").slice(0, 160)}</p>
            <button type="button" onClick={() => onAction({ type: "review_draft", label: "", proposalId: d.id })}>{t("Review the drafted question")}</button></div>)}
          {drafted.map((c) => <div className="queueitem" key={c.id}><p className="small">{t("A question for")} {c.data.recipient}</p>
            <button type="button" onClick={() => onAction({ type: "review_consultation", label: "", consultationId: c.data.consultationId })}>{t("Review and send the question")}</button></div>)}</>}
        <p className="small muted">{t("Nothing has been sent. A draft goes to the citizen only when you approve its exact wording.")}</p></section>
      <details className="tracedetails"><summary>{t("What it did")} ({run.toolCalls}/{run.toolBudget})</summary>
        <ol className="trace">{run.trace.map((s) => <li key={s.n} className={s.state}>{t(TOOL_WORDS[s.tool] ?? s.tool)}{s.state === "skipped" ? ` — ${t("not run")}: ${s.why ?? ""}` : s.state === "failed" ? ` — ${t("failed")}` : ""}{s.ms !== undefined ? <span className="muted small"> · {s.ms} ms</span> : null}</li>)}</ol>
        {run.traceLine && <p className="small">{run.traceLine}</p>}
        <p className="small muted">{run.recordsUsed.length ? `${t("Records used")}: ${run.recordsUsed.join(", ")}` : ""}{run.rejectedCitations ? ` · ${run.rejectedCitations} ${t("citation(s) dropped: the excerpt was not in the record")}` : ""}</p></details>
    </>}
  </aside>;
}
