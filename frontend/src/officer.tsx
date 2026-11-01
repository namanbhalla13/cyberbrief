/** The officer desk: the queue, and the one decision an officer records. */
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api, type CaseSnapshot, type ReviewOutcome } from "./api";
import { Icon, IconButton, PageHead } from "./icons";
import { OfficerDeskPanels } from "./officerdesk";
import { StatePanel } from "./statepanel";
import { DeskNotices } from "./officerconsult";
import { OfficerWork } from "./officerwork";
import { wf, type WorkList } from "./workflowapi";
import { ErrorNotice, MoneyText, useI18n } from "./ui";


// ---- from features/officer/OfficerQueuePage ---------------------------------------------------

/**
 * Complaints citizens have sent for review. An officer sees nothing else: the backend refuses
 * officer sessions on every citizen route, and an unsubmitted case is a 404 from here.
 */
export function OfficerQueuePage() {
    const { t, language } = useI18n();
    const [work, setWork] = useState<WorkList | null>(null);
    const [state, setState] = useState("all");
    const [pageNo, setPageNo] = useState(1);
    const [error, setError] = useState<string | null>(null);
    const [reloads, setReloads] = useState(0);
    const load = () => setReloads((n) => n + 1);
    useEffect(() => {
        let live = true;   // a slow answer for an earlier filter must never replace the list for the one chosen since
        setError(null);
        wf.officerWork(state, pageNo).then((w) => { if (live) setWork(w); })
            .catch((e) => { if (live) { setWork((w) => w ?? { counts: {}, items: [], filters: {} }); setError(e instanceof Error ? e.message : "Could not load the queue"); } });
        return () => { live = false; };
    }, [state, pageNo, reloads]);
    const items = work?.items ?? [];
    const counts = work?.counts ?? {};
    const FILTERS: [string, string][] = [["all", "All"], ["acknowledge", "Needs acknowledgement"], ["review", "Needs review"], ["waiting", "Waiting for a reply"], ["connections", "New connections to review"]];
    const when = (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium" }).format(new Date(iso));
    return (<>
      <PageHead eyebrow={t("Investigation workspace")} title={t("Officer desk")} lead={t("A simulated reviewer view for this demo. Nothing here files an official complaint, contacts a bank, or changes an official complaint status.")}/>
      <section className="stats" aria-label={t("Your work at a glance")}>
        <div className="stat"><div className="label">{t("Assigned to you")}</div><div className="value">{counts.all ?? 0}</div></div>
        <div className="stat"><div className="label">{t("Needs acknowledgement")}</div><div className="value">{counts.acknowledge ?? 0}</div></div>
        <div className="stat"><div className="label">{t("Needs review")}</div><div className="value">{counts.review ?? 0}</div></div>
        <div className="stat"><div className="label">{t("Waiting for a reply")}</div><div className="value">{counts.waiting ?? 0}</div></div>
        <button type="button" className={`stat statbutton${state === "connections" ? " active" : ""}`} aria-pressed={state === "connections"} onClick={() => { setPageNo(1); setState("connections"); }}>
          <span className="label">{t("New connections")}</span><span className="value">{counts.connections ?? 0}</span><span className="hint">{t("Open the reports with matches to review")}</span></button>
      </section>
      <DeskNotices/>
      <section className="card" aria-label={t("Sent for review")}>
        <div className="sectionhead"><h2>{t("Assigned to you")}</h2><IconButton name="refresh" label={t("Refresh your work")} onClick={load}/></div>
        <div className="filters" role="group" aria-label={t("Show")}>{FILTERS.map(([k, label]) =>
          <button key={k} type="button" className={`filter${state === k ? " active" : ""}`} aria-pressed={state === k} onClick={() => { setPageNo(1); setState(k); }}>{t(label)}{k === "all" ? "" : ` (${counts[k] ?? 0})`}</button>)}</div>
        {error && <StatePanel kind="error" message={error} action={{ label: "Try again", run: load }}/>}
        {work === null && <StatePanel kind="loading"/>}
        {work !== null && !error && items.length === 0 && <div className="empty"><h3>{t(state === "all" ? "Nothing needs your review" : "Nothing in this group")}</h3><p className="muted">{t(state === "all" ? "New assignments will appear here once an admin approves them." : "Choose another group to see the rest of your work.")}</p></div>}
        {items.map((c) => <div className="reportrow" key={c.id}>
          <span className="tileicon"><Icon name="file"/></span>
          <div className="grow"><span className="mono muted">{c.caseRef}</span><b>{c.title}</b>
            <p className="small"><b>{t("Next")}:</b> {t(c.nextAction)}{c.dueAt ? ` · ${t("due")} ${when(c.dueAt)}` : ""}</p></div>
          {c.overdue && <span className="badge amber">{t("overdue")}</span>}
          {c.closureRequested && <span className="badge amber">{t("closure requested")}</span>}
          <span className={`badge ${c.reviewState === "reviewed" || c.reviewState === "closed" ? "green" : "amber"}`}>{c.reviewState}</span>
          {c.filters.includes("connections") && <span className="badge amber">{t("new connection")}</span>}
          {c.reviewState !== "closed" && <Link className="button secondary" to={`/officer/cases/${c.id}?investigate=1#related`}><Icon name="spark" size={16}/> {t("Investigate")}</Link>}
          <Link className="button" to={`/officer/cases/${c.id}`}>{t("Open case")} <Icon name="arrow" size={18}/></Link>
        </div>)}
        {work !== null && (work.pages ?? 1) > 1 && <nav className="pagination" aria-label={t("Pages of reports")}>
          <span className="small muted" role="status">{t("Page {n} of {pages}", { n: work.page ?? 1, pages: work.pages ?? 1 })} · {t("{total} reports", { total: work.total ?? 0 })}</span>
          <span className="actions"><IconButton name="back" label={t("Previous")} disabled={(work.page ?? 1) <= 1} onClick={() => setPageNo((n) => n - 1)}/>
            <IconButton name="arrow" label={t("Next")} disabled={(work.page ?? 1) >= (work.pages ?? 1)} onClick={() => setPageNo((n) => n + 1)}/></span></nav>}
      </section>
    </>);
}


// ---- from features/officer/OfficerCasePage ----------------------------------------------------

/**
 * Read-only view of a submitted packet, plus the one action an officer has.
 *
 * Officers cannot upload, correct or delete anything: the backend refuses officer sessions on
 * every citizen route, so this page only reads and records a review.
 */
export function OfficerCasePage() {
    const { t } = useI18n();
    const { caseId } = useParams();
    const [snapshot, setSnapshot] = useState<CaseSnapshot | null>(null);
    const [note, setNote] = useState("");
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [denied, setDenied] = useState(false);
    const [tab, setTab] = useState<string | undefined>(undefined);   // set when a suggestion makes a draft, so Messages opens
    const [deskVersion, setDeskVersion] = useState(0);   // a review recorded here shows up in the desk's audit log
    useEffect(() => {
        if (!caseId)
            return;
        api.officerCase(caseId).then(setSnapshot).catch((e) => { setDenied(e instanceof ApiError && (e.status === 403 || e.status === 404)); setError(e instanceof Error ? e.message : "Could not load"); });
    }, [caseId]);
    if (!caseId)
        return null;
    if (!snapshot)
        return denied ? <StatePanel kind="denied" message={t("This report is not assigned to you, or it does not exist.")}><Link to="/officer"> {t("← Officer desk")} </Link></StatePanel>
            : error ? <StatePanel kind="error" message={error}/> : <StatePanel kind="loading"/>;
    // the API returns summaries newest first, so [0] is the live one
    const latest = snapshot.resolutions[0] ?? null;
    // a summary carrying the citizen's own reason is a request to close, and is answered differently
    const closureAsked = Boolean(latest?.requestNote) && !latest?.reviewOutcome;
    async function record(outcome: ReviewOutcome) {
        setBusy(true);
        setError(null);
        try {
            setSnapshot(await api.officerReview(caseId!, latest!.id, outcome, note || null, snapshot!.case.currentRevision));
            setDeskVersion((v) => v + 1);
        }
        catch (e) {
            setError(e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : "Could not record the review");
        }
        finally {
            setBusy(false);
        }
    }
    return (<>
      <h1>{snapshot.case.title}</h1>
      <p className="case-meta muted">
        <Link to="/officer"> {t("\u2190 Officer desk")} </Link>
        <span className={`badge badge-case-${snapshot.case.reviewState}`}>{snapshot.case.reviewState}</span>
        <span> {t("revision")} {snapshot.case.currentRevision}</span>
      </p>
      {error && (<ErrorNotice message={error}/>)}

      <OfficerDeskPanels caseId={caseId} snapshot={snapshot} refresh={deskVersion} onDrafted={() => { setTab("messages"); setDeskVersion((v) => v + 1); }}>
      {(parts) => <OfficerWork caseId={caseId} snapshot={snapshot} parts={parts} tab={tab} onTab={setTab} reload={deskVersion} onChanged={() => setDeskVersion((v) => v + 1)}>
      <div className="grid-2">
        <section className="card">
          <h2> {t("Reviewed accounting")} </h2>
          <dl className="kv">
            <dt> {t("Citizen-reported incident amount")} </dt>
            <dd>
              <MoneyText minor={snapshot.totals.citizenReportedMinor}/>
            </dd>
            <dt> {t("Reviewed distinct successful debits")} </dt>
            <dd>
              <strong>
                <MoneyText minor={snapshot.totals.reviewedDebitMinor}/>
              </strong>
            </dd>
            <dt> {t("Outstanding after reviewed refunds")} </dt>
            <dd>
              <MoneyText minor={snapshot.recovery.outstandingMinor}/>
            </dd>
          </dl>
          <p className="small muted"> {t("Figures come from material the citizen uploaded and confirmed. Nothing here has been verified against a bank.")} </p>
        </section>

        <section className="card">
          <h2> {t(closureAsked ? "Closure request" : "Resolution summary")} </h2>
          {latest?.requestNote && (<div className="saidby">
              <span className="who"> {t("The citizen asked to close this")} </span>
              {latest.requestNote}
            </div>)}
          {latest ? (<>
              <p className="small">
                {latest.kind.replace(/_/g, " ")} · {t(latest.eligible ? "eligible for review" : "not eligible")} {latest.stale ? t(" · stale") : ""}
                
                
              </p>
              <pre className="summary">{latest.summaryText}</pre>
              {latest.reviewOutcome && (<p className="small"> {t("Recorded:")} <strong>{latest.reviewOutcome.replace(/_/g, " ")}</strong> {t("by")} {" "}
                  {latest.reviewedByRole}{latest.reviewNote ? ` — ${latest.reviewNote}` : ""}
                  
                </p>)}
            </>) : (<p className="muted small"> {t("The citizen has not prepared a summary yet.")} </p>)}
          <div className="inline-form">
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("Reviewer note (optional)")} aria-label={t("Reviewer note")}/>
            {closureAsked ? (
        // agreeing to close is not gated on eligibility: someone may want to stop pursuing
        // a case that was never fully reviewed, and that is their decision to make.
        <button type="button" disabled={busy || !latest || latest.stale} onClick={() => void record("closure_agreed")}> {t("Agree, close this record")} </button>) : (<button type="button" disabled={busy || !latest || latest.stale || !latest.eligible || Boolean(latest.reviewOutcome)} onClick={() => void record("packet_reviewed")}> {t("Record packet reviewed")} </button>)}
            <button type="button" className="secondary" disabled={busy || !latest || latest.stale || Boolean(latest.reviewOutcome)} onClick={() => void record("more_information_requested")}> {t(closureAsked ? "Ask for more information" : "Send the packet back to the citizen")} </button>
          </div>
          <p className="small muted"> {t("Recording a review affects only this packet. Closing a record ends CyberBrief's tracking of it. The official complaint status is a separate track and is not changed by anything on this page.")} </p>
        </section>
      </div>
      </OfficerWork>}
      </OfficerDeskPanels>
    </>);
}
