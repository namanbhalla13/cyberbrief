/** The admin's three screens, as designed: the intake queue, the officers and their availability, and what the coordinator has done.
 *
 * The coordinator does the preparation (reads a report, checks the roster, drafts an assignment with reasons) and the admin decides.
 * Every list comes from the application's records and says when it was read; an approval names exactly the proposal shown, and is
 * rechecked on the server against duty, capacity and the report's revision at the moment it runs.
 */
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError } from "./api";
import { Dialog, useToast } from "./dialog";
import { Icon, IconButton, PageHead } from "./icons";
import { StatePanel } from "./statepanel";
import { useI18n } from "./ui";
import { wf, type Activity, type AdminOverview, type AskResult, type CaseDetail, type CasePage, type Proposal, type StaffMember, type WorkflowConfig } from "./workflowapi";

const PAGE_SIZE = 10;
const FILTERS: [string, string][] = [["all", "All"], ["waiting", "Awaiting approval"], ["assigned", "Assigned"], ["under_review", "Under review"], ["exception", "Exceptions"]];
const errorText = (e: unknown, fallback: string) => (e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : e instanceof Error ? e.message : fallback);

function useClock() {
  const { language } = useI18n();
  return (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
}

/** The overview and a way to read it again; shared by all three screens. */
function useOverview() {
  const [data, setData] = useState<AdminOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => wf.adminOverview().then((d) => { setData(d); setError(null); }).catch((e) => setError(errorText(e, "Could not load the queue"))), []);
  useEffect(() => { void load(); }, [load]);
  return { data, error, load, clearError: () => setError(null) };
}

const caseLink = (id: string | null | undefined) => `/admin?case=${id}`;

function AskDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useI18n();
  const clock = useClock();
  const [question, setQuestion] = useState("Which submitted reports still have no officer?");
  const [answer, setAnswer] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function ask(event: FormEvent) {
    event.preventDefault();
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    try { setAnswer(await wf.adminAsk(question.trim())); } catch (e) { setError(errorText(e, "Could not answer")); } finally { setBusy(false); }
  }
  return <Dialog title={t("Ask about the queue")} open={open} onClose={onClose}>
    <form onSubmit={ask} className="form" aria-label={t("Ask about the queue")}>
      <label className="field">{t("Your question")}<input value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={300}/>
        <small>{t("Answers come from the records and say when they were read. Try: “Which submitted reports still have no officer?”, “Show cases waiting for a bank response”, or give a CB- reference or a UPI ID.")}</small></label>
      <div className="actions"><button type="submit" disabled={busy || !question.trim()}><Icon name="spark" size={18}/> {t("Ask the coordinator")}</button></div>
    </form>
    {error && <StatePanel kind="error" message={error}/>}
    {answer && <div role="status"><h3>{answer.title}</h3>
      <p>{answer.text} <span className="small muted">{t("Read at")} {clock(answer.asOf)}</span></p>
      {answer.rows.length > 0 && <div className="tablewrap"><table className="compare"><thead><tr>{answer.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
        <tbody>{answer.rows.map((r, i) => <tr key={i}>{r.map((cell, j) => <td key={j}>{j === 0 && answer.caseIds?.[i] ? <Link to={caseLink(answer.caseIds[i])} onClick={onClose} aria-label={`${t("Open report")} ${cell}`}>{cell}</Link> : cell}</td>)}</tr>)}</tbody></table></div>}</div>}
  </Dialog>;
}

// ---- the intake queue ---------------------------------------------------------------------------------------------------

export function AdminPage() {
  const { t } = useI18n();
  const clock = useClock();
  const toast = useToast();
  const { data, error, load, clearError } = useOverview();
  const [page, setPage] = useState<CasePage | null>(null);
  const [pageNo, setPageNo] = useState(1);
  const [typed, setTyped] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [pageError, setPageError] = useState<string | null>(null);
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [reviewing, setReviewing] = useState<Proposal | null>(null);
  const [pick, setPick] = useState("");
  const [moveTo, setMoveTo] = useState("");
  const [moveReason, setMoveReason] = useState("");
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const loadPage = useCallback(() => wf.adminCases({ query: search, state: filter, page: pageNo, pageSize: PAGE_SIZE })
    .then((p) => { setPage(p); setPageError(null); }).catch((e) => setPageError(errorText(e, "Could not load the reports"))), [search, filter, pageNo]);
  useEffect(() => { void loadPage(); }, [loadPage]);
  const refresh = () => Promise.all([load(), loadPage()]);

  async function run(key: string, work: () => Promise<unknown>, done?: string) {
    setBusy(key);
    setProblem(null);
    try { await work(); await refresh(); if (done) toast.say(done); return true; }
    catch (e) { setProblem(errorText(e, "That did not work")); await refresh(); return false; } finally { setBusy(null); }
  }
  const open = async (id: string) => { setProblem(null); setMoveTo(""); setMoveReason(""); try { setDetail(await wf.adminCase(id)); } catch (e) { toast.say(errorText(e, "Could not open the report")); } };
  const review = (p: Proposal) => { setProblem(null); setReviewing(p); setPick(p.payload.recommended?.id ?? ""); };
  const [params, setParams] = useSearchParams();
  const wanted = params.get("case");
  useEffect(() => { if (wanted) { void open(wanted); setParams({}, { replace: true }); } // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted]);
  const proposals = data?.proposals ?? [];
  const first = proposals[0];

  if (!data) return error ? <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => { clearError(); void load(); } }}/> : <StatePanel kind="loading"/>;
  const intakeCount = data.summary.waiting;
  const inMotion = data.summary.assigned + data.summary.under_review;
  const options = reviewing ? [reviewing.payload.recommended, ...(reviewing.payload.alternatives ?? [])].filter(Boolean) as { id: string; name: string; reasons: string[]; eligible?: boolean }[] : [];

  return <>
    <PageHead eyebrow={t("Department workspace")} title={t("Admin desk")} lead={t("A simulated coordinator view for this demo. It routes reports between fictional officers; nothing here files an official complaint or contacts a bank.")}
      action={<button type="button" className="secondary" onClick={() => setAsking(true)}><Icon name="spark" size={18}/> {t("Ask the coordinator")}</button>}/>
    <p className="small muted">{t("Read at")} {clock(data.asOf)}</p>
    {problem && <StatePanel kind="error" message={problem}/>}

    <section className="stats" aria-label={t("The queue at a glance")}>
      <div className="stat"><div className="label">{t("Awaiting approval")}</div><div className="value">{proposals.length}</div></div>
      <div className="stat"><div className="label">{t("Waiting for an officer")}</div><div className="value">{intakeCount}</div></div>
      <div className="stat"><div className="label">{t("In progress")}</div><div className="value">{inMotion}</div></div>
      <div className={`stat${data.summary.exception > 0 ? " attention" : ""}`}><div className="label">{t("Needs intervention")}</div><div className="value">{data.summary.exception}</div></div>
    </section>

    <div className="withaside">
      <section className="card" aria-label={t("All submitted reports")}>
        <div className="sectionhead"><h2>{t("Report queue")}</h2><IconButton name="refresh" label={t("Refresh report queue")} onClick={() => void refresh()}/></div>
        <p className="small muted">{t("Private drafts are never shown here.")}</p>
        <form className="toolbar" role="search" onSubmit={(e) => { e.preventDefault(); setPageNo(1); setSearch(typed.trim()); }}>
          <label className="search"><Icon name="search" size={17}/><input aria-label={t("Search reports")} placeholder={t("Reference, title, area or officer")} value={typed} maxLength={80} onChange={(e) => setTyped(e.target.value)}/></label>
          <button type="submit" className="secondary">{t("Search")}</button>
        </form>
        <div className="filters" role="group" aria-label={t("Show")}>{FILTERS.map(([k, label]) =>
          <button key={k} type="button" className={`filter${filter === k ? " active" : ""}`} aria-pressed={filter === k} onClick={() => { setPageNo(1); setFilter(k); }}>{t(label)}</button>)}</div>
        {pageError && <StatePanel kind="error" message={pageError} action={{ label: "Try again", run: () => void loadPage() }}/>}
        {page && page.items.length === 0 && <div className="empty"><h3>{t(search || filter !== "all" ? "No matching reports" : "Your queue is clear")}</h3><p className="muted">{t(search || filter !== "all" ? "Try another search or filter." : "New submitted reports will appear automatically.")}</p></div>}
        {page && page.items.length > 0 && <div className="tablewrap"><table className="compare">
          <thead><tr><th scope="col">{t("Report")}</th><th scope="col">{t("Area / status")}</th><th scope="col"><span className="visually-hidden">{t("Open")}</span></th></tr></thead>
          <tbody>{page.items.map((c) => <tr key={c.id}>
            <td><button type="button" className="rowlink" onClick={() => void open(c.id)}><span className="caseref">{c.caseRef}</span></button><small>{c.title}</small></td>
            <td>{c.area ?? t("No area given")}<small><span className={`badge ${c.assignedTo ? "green" : "amber"}`}>{c.assignedTo ? `${t("Assigned")} · ${c.assignedTo}` : c.workflowState === "exception" ? t("Needs the admin's decision") : t("Waiting for an officer")}</span></small></td>
            <td><IconButton name="arrow" label={`${t("View details of")} ${c.caseRef}`} onClick={() => void open(c.id)}/></td></tr>)}</tbody></table></div>}
        {page && <nav className="pagination" aria-label={t("Pages of reports")}>
          <span className="small muted" role="status">{t("Page {n} of {pages}", { n: page.page, pages: page.pages })} · {t("{total} reports", { total: page.total })}</span>
          <span className="actions"><IconButton name="back" label={t("Previous")} disabled={pageNo <= 1} onClick={() => setPageNo((n) => n - 1)}/>
            <IconButton name="arrow" label={t("Next")} disabled={pageNo >= page.pages} onClick={() => setPageNo((n) => n + 1)}/></span></nav>}
      </section>

      <section className="card agentcard" aria-label={t("Assignments to approve")}>
        <div className="agenthead"><span className="tileicon"><Icon name="spark"/></span><div><h3>{first ? t("Assignment prepared") : t("No assignment waiting")}</h3>
          <p>{first ? `${first.caseRef} · ${t("requires your approval")}` : t("The coordinator prepares one when a report is sent.")}</p></div></div>
        {first ? <>
          {(first.payload.recommended?.reasons ?? []).map((r) => <div className="agentstep" key={r}><span>{r}</span></div>)}
          <div className="agentdecision"><b>{first.payload.recommended?.name}</b><p>{first.payload.summary}</p>
            <span className="small muted">{t(first.origin === "model" ? "Written by the model, checked by code" : "From fixed rules")}</span></div>
          <p><button type="button" disabled={busy !== null} onClick={() => review(first)}>{t("Review assignment")} <Icon name="arrow" size={18}/></button></p>
          {proposals.length > 1 && <p className="small muted">{t("{n} more waiting", { n: proposals.length - 1 })}</p>}
          <p className="small muted">{t("No access is granted until the admin approves.")}</p></> : <p className="small muted">{t("Nothing is waiting for your approval.")}</p>}
      </section>
    </div>

    <Dialog title={detail ? `${detail.caseRef} · ${t("Report overview")}` : ""} open={detail !== null} onClose={() => setDetail(null)}
      footer={detail && <>{detail.proposal ? <button type="button" onClick={() => { const p = proposals.find((x) => x.id === detail.proposal!.id) ?? detail.proposal!; setDetail(null); review(p); }}><Icon name="users" size={18}/> {t("Review assignment")}</button>
        : !detail.assignedTo ? <button type="button" disabled={busy !== null} onClick={() => void (async () => { const d = detail; setDetail(null); const ok = await run(`propose-${d.id}`, async () => { const out = await wf.adminPropose(d.id); if ("exception" in out) throw new Error(out.exception); }, t("An assignment was prepared.")); if (ok) void 0; })()}><Icon name="spark" size={18}/> {t("Ask the coordinator to propose an officer")}</button> : null}</>}>
      {detail && <>
        <p className="quote">{detail.title}</p>
        <div className="labelvalue"><span>{t("Who has it")}</span><b>{t(detail.owner)}</b></div>
        <div className="labelvalue"><span>{t("What is stopping it")}</span><b>{t(detail.blocker)}</b></div>
        <div className="labelvalue"><span>{t("What happens next")}</span><b>{t(detail.nextAction)}</b></div>
        <div className="labelvalue"><span>{t("Area")}</span><b>{detail.area ?? t("No area given")}</b></div>
        <div className="labelvalue"><span>{t("Assigned")}</span><b>{detail.assignedTo ? `${detail.assignedTo}${detail.assignedAt ? ` · ${clock(detail.assignedAt)}` : ""}` : t("Not yet")}</b></div>
        <div className="labelvalue"><span>{t("Acknowledged")}</span><b>{detail.acknowledgedAt ? clock(detail.acknowledgedAt) : t("Not yet")}</b></div>
        {detail.followUps.map((f) => <div className="note amber" key={f.task}>{f.task}</div>)}
        {detail.assignedTo && detail.reviewState === "submitted" && <label className="field">{t("Move to")}
          <select value={moveTo} disabled={busy !== null} onChange={(e) => setMoveTo(e.target.value)}>   {/* choosing only chooses: nothing moves until it is confirmed below */}
            <option value="">—</option>{data.officers.filter((o) => o.name !== detail.assignedTo).map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</select></label>}
        {detail.assignedTo && moveTo && <div className="note amber" role="group" aria-label={t("Confirm reassignment")}>
          <p><b>{t("Current owner")}:</b> {detail.assignedTo} → <b>{t("Proposed owner")}:</b> {data.officers.find((o) => o.id === moveTo)?.name}</p>
          <label className="field">{t("Reason for moving it")}<textarea value={moveReason} maxLength={300} onChange={(e) => setMoveReason(e.target.value)}/>
            <small>{t("Kept in the report's record. The server rechecks area, duty and capacity when you confirm, and refuses if the report has moved since you opened it.")}</small></label>
          <button type="button" disabled={busy !== null || !moveReason.trim()} onClick={() => { const d = detail; const from = data.officers.find((o) => o.name === d.assignedTo)?.id; void run(`move-${d.id}`, () => wf.adminReassign(d.id, moveTo, moveReason.trim(), from), t("Reassigned.")).then((ok) => { if (ok) setDetail(null); }); }}>{t("Confirm reassignment")}</button></div>}
        <details><summary>{t("What the coordinator did")}</summary><ul className="plain-list small">{detail.activity.map((a, i) => <li key={i}><time dateTime={a.at}>{clock(a.at)}</time> · {a.steps.join(" → ")}</li>)}</ul></details>
      </>}
    </Dialog>

    <Dialog title={t("Review the assignment")} open={reviewing !== null} onClose={() => setReviewing(null)}
      footer={reviewing && <><button type="button" className="secondary" onClick={() => setReviewing(null)}>{t("Leave pending")}</button>
        <button type="button" disabled={busy !== null || !pick} onClick={() => { const p = reviewing; setReviewing(null); void run(`approve-${p.id}`, () => wf.adminApprove(p.id, p.digest, pick !== p.payload.recommended?.id ? pick : undefined), t("Assigned. The officer will be asked to acknowledge it.")); }}><Icon name="check" size={18}/> {t("Approve assignment")}</button></>}>
      {reviewing && <>
        <p><b><span className="caseref">{reviewing.caseRef}</span> {reviewing.title}</b></p>
        <p><strong>{t("Suggested")}: {reviewing.payload.recommended?.name}</strong> — {reviewing.payload.summary}</p>
        <label className="field">{t("Assign to")}
          <select value={pick} onChange={(e) => setPick(e.target.value)}>{options.map((o) => <option key={o.id} value={o.id}>{o.name}{o.id === reviewing.payload.recommended?.id ? ` (${t("suggested")})` : ""}</option>)}</select></label>
        <ul className="plain-list small">{(options.find((o) => o.id === pick)?.reasons ?? []).map((r) => <li key={r}>{r}</li>)}</ul>
        <div className="note">{t("At approval the server rechecks the area, duty, capacity and the report's revision. An unavailable choice needs a fresh proposal.")}</div>
        {problem && <StatePanel kind="error" message={problem}/>}
      </>}
    </Dialog>
    <AskDialog open={asking} onClose={() => setAsking(false)}/>
  </>;
}

// ---- officers and availability --------------------------------------------------------------------------------------------

export function AdminOfficersPage() {
  const { t } = useI18n();
  const toast = useToast();
  const { data, error, load, clearError } = useOverview();
  const [editing, setEditing] = useState<StaffMember | null>(null);
  const [duty, setDuty] = useState(true);
  const [capacity, setCapacity] = useState("3");
  const [areasSel, setAreasSel] = useState<string[]>([]);
  const [discovery, setDiscovery] = useState(false);
  const [config, setConfig] = useState<WorkflowConfig | null>(null);
  useEffect(() => { void wf.config().then(setConfig).catch(() => undefined); }, []);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!data) return error ? <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => { clearError(); void load(); } }}/> : <StatePanel kind="loading"/>;
  const edit = (o: StaffMember) => { setEditing(o); setDuty(o.onDuty); setCapacity(String(o.capacity)); setAreasSel(o.areas); setDiscovery(Boolean(o.areaDiscovery)); setProblem(null); };
  const areaName = (id: string) => config?.areas.find((a) => a.id === id)?.name ?? id;
  async function save() {
    const cap = Number(capacity);
    if (!Number.isInteger(cap) || cap < 0 || cap > 50) { setProblem(t("Enter a whole number from 0 to 50.")); return; }
    if (areasSel.length === 0) { setProblem(t("Choose at least one area.")); return; }
    setBusy(true);
    try { await wf.adminOfficer(editing!.id, { onDuty: duty, capacity: cap, areas: areasSel, areaDiscovery: discovery }); setEditing(null); await load(); toast.say(t("Availability saved. Waiting reports are routed again.")); }
    catch (e) { setProblem(errorText(e, "That did not work")); } finally { setBusy(false); }
  }
  return <>
    <PageHead eyebrow={t("Department workspace")} title={t("Officers and availability")} lead={t("See workload before approving or changing an assignment.")}/>
    <section className="card" aria-label={t("Officers and workload")}>
      <div className="tablewrap"><table className="compare"><thead><tr><th scope="col">{t("Officer")}</th><th scope="col">{t("Coverage")}</th><th scope="col">{t("Workload")}</th><th scope="col">{t("Availability")}</th><th scope="col"><span className="visually-hidden">{t("Actions")}</span></th></tr></thead>
        <tbody>{data.officers.map((o) => <tr key={o.id}>
          <th scope="row">{o.name}<small>{o.languages.join(", ")}</small></th><td>{o.areas.map(areaName).join(", ")}{o.areaDiscovery ? <small>{t("Can search other reports in these areas")}</small> : null}</td>
          <td>{o.active} / {o.capacity}{o.unacknowledged ? <small>{o.unacknowledged} {t("not yet acknowledged")}</small> : null}</td>
          <td><span className={`badge ${!o.onDuty ? "red" : o.active >= o.capacity ? "amber" : "green"}`}>{t(!o.onDuty ? "Off duty" : o.active >= o.capacity ? "At capacity" : "On duty")}</span></td>
          <td><IconButton name="edit" label={`${t("Edit availability for")} ${o.name}`} onClick={() => edit(o)}/></td></tr>)}</tbody></table></div>
    </section>
    <Dialog title={t("Officer availability")} open={editing !== null} onClose={() => setEditing(null)}
      footer={<button type="button" disabled={busy} onClick={() => void save()}><Icon name="check" size={18}/> {t("Save availability")}</button>}>
      <p><b>{editing?.name}</b></p>
      <label className="field">{t("Duty status")}<select value={duty ? "on" : "off"} onChange={(e) => setDuty(e.target.value === "on")}><option value="on">{t("On duty")}</option><option value="off">{t("Off duty")}</option></select></label>
      <fieldset className="field"><legend>{t("Areas covered")}</legend>
        {config?.areas.map((a) => <label key={a.id} className="check"><input type="checkbox" checked={areasSel.includes(a.id)} onChange={(e) => setAreasSel((cur) => e.target.checked ? [...cur, a.id] : cur.filter((x) => x !== a.id))}/> {a.name}</label>)}</fieldset>
      <label className="check"><input type="checkbox" checked={discovery} onChange={(e) => setDiscovery(e.target.checked)}/> {t("May search other officers' reports in these areas")}</label>
      <p className="small muted">{t("This is discovery only. It shows a short summary of a match. It does not let them open that report or its evidence.")}</p>
      <label className="field">{t("Maximum active cases")}<input type="number" min={0} max={50} value={capacity} onChange={(e) => setCapacity(e.target.value)}/></label>
      {problem && <StatePanel kind="error" message={problem}/>}
    </Dialog>
  </>;
}

// ---- the coordinator -------------------------------------------------------------------------------------------------------

function ActivityItem({ a }: { a: Activity }) {
  const { t } = useI18n();
  const clock = useClock();
  return <li><b>{a.caseId && a.caseRef ? <Link to={caseLink(a.caseId)} aria-label={`${t("Open report")} ${a.caseRef}`}>{a.caseRef}</Link> : a.caseRef ?? "—"}</b><p>{a.steps.join(" → ")}</p><time dateTime={a.at} className="small muted">{clock(a.at)}</time></li>;
}

export function AdminCoordinatorPage() {
  const { t } = useI18n();
  const toast = useToast();
  const { data, error, load, clearError } = useOverview();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!data) return error ? <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => { clearError(); void load(); } }}/> : <StatePanel kind="loading"/>;
  async function retry() {
    setBusy(true);
    try { await wf.adminRetryEvents(); await load(); toast.say(t("Failed work was put back in the queue.")); } catch (e) { toast.say(errorText(e, "That did not work")); } finally { setBusy(false); }
  }
  return <>
    <PageHead eyebrow={t("Department workspace")} title={t("Coordinator activity")} lead={t("See completed checks, waiting approvals and exceptions.")}
      action={<button type="button" onClick={() => setAsking(true)}><Icon name="spark" size={18}/> {t("Ask about the queue")}</button>}/>
    <div className="withaside">
      <section className="card" aria-label={t("What the coordinator did")}><h2>{t("Work in motion")}</h2>
        {data.activity.length === 0 && <p className="muted">{t("Nothing yet.")}</p>}
        <ol className="timeline">{data.activity.map((a, i) => <ActivityItem key={i} a={a}/>)}</ol>
        <div className="note">{t("These steps are the coordinator's recorded work: each line is a real result, with the time it happened.")}</div>
      </section>
      <div className="stack">
        <section className="card" aria-label={t("Background work")}><h2>{t("Background work")}</h2>
          <p className="small">{t("{n} waiting", { n: data.events.pending })} · {t("{n} failed", { n: data.events.failed })}</p>
          {data.events.recentFailures.map((f) => <p key={f.id} className="small differs">{f.kind.replace(/_/g, " ")}: {f.error ?? t("failed")}</p>)}
          {data.events.failed > 0 && <button type="button" className="secondary" disabled={busy} onClick={() => void retry()}>{t("Retry failed work")}</button>}
          {data.followUps.length > 0 && <><h3>{t("Overdue follow-ups")}</h3><ul className="plain-list">{data.followUps.map((f, i) => <li key={i}><Link to={caseLink(f.caseId)} aria-label={`${t("Open report")} ${f.caseRef}`}>{f.caseRef}</Link>: {f.task}</li>)}</ul></>}</section>
        <section className="card"><h2>{t("Exception handling")}</h2>
          {[["help", "Missing area", "The citizen is asked to choose an area. It is never guessed, and routing restarts when they do."], ["users", "No eligible officer", "The report stays unassigned and comes to the admin."], ["refresh", "Evidence changed", "A stale proposal is replaced and needs a fresh approval."]].map(([i, a, b]) =>
            <div className="task" key={a}><span className="tileicon"><Icon name={i as "help"}/></span><div><b>{t(a)}</b><p className="small muted">{t(b)}</p></div></div>)}</section>
      </div>
    </div>
    <AskDialog open={asking} onClose={() => setAsking(false)}/>
  </>;
}
