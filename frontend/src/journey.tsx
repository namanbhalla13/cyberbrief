/** The citizen's journey, as designed: Home -> tell your story -> add evidence -> check each payment -> send -> follow the report.
 *
 * Every step is backed by the real records: the report is created on the server at the first step, evidence is uploaded and read by
 * the real pipeline, each payment is confirmed or corrected through the review API (a correction is a new version, never an overwrite),
 * and the report is sent only when the citizen presses Send. Money comes from the existing integer-paise accounting; nothing on these
 * pages computes a total.
 */
import { useCallback, useEffect, useState, type ChangeEvent } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError, api, newIdempotencyKey, type CaseSnapshot, type CaseSummary, type DemoPackOut, type FactOut, type PaymentOut, type ReviewAction } from "./api";
import { Dialog, useToast } from "./dialog";
import { AgentCard, Icon, IconButton, PageHead } from "./icons";
import { StatePanel } from "./statepanel";
import { draftStore, scopeOf } from "./draftstore";
import { caseReference, formatInr, useAuth, useCase, useI18n } from "./ui";
import { wf, type Progress, type WorkflowConfig } from "./workflowapi";

const errorText = (e: unknown, fallback: string) => (e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : e instanceof Error ? e.message : fallback);

function Steps({ at }: { at: number }) {
  const { t } = useI18n();
  return <ol className="steps" aria-label={t("Your progress")}>{["Tell your story", "Add evidence", "Check details", "Send report"].map((x, i) =>
    <li key={x} className={i <= at ? "active" : ""} aria-current={i === at ? "step" : undefined}>{i + 1}. {t(x)}</li>)}</ol>;
}

/** The draft report this browser tab is working on for the signed-in person, if there is one. Another account never sees it. */
function useDraft() {
  const { session } = useAuth();
  const scope = scopeOf(session);
  const [caseId, setCaseId] = useState<string | null>(() => draftStore.caseId(scope));
  useEffect(() => { setCaseId(draftStore.caseId(scope)); }, [scope]);
  const set = (id: string | null) => { draftStore.setCaseId(scope, id); setCaseId(id); };
  return { caseId, set, scope };
}

const payName = (p: PaymentOut) => String((p.fields.counterparty?.value as { value?: string } | null)?.value ?? "");
const payRef = (p: PaymentOut) => String((p.fields.reference?.value as { value?: string } | null)?.value ?? "");
const payAmount = (p: PaymentOut) => (p.fields.amount?.value as { amountMinor?: string } | null)?.amountMinor ?? null;
const payDate = (p: PaymentOut) => p.fields.occurred_at?.rawText ?? String((p.fields.occurred_at?.value as { value?: string } | null)?.value ?? "");
/** Every detail of a payment that "These details are right" confirms is shown on its card, with the label the person reads. Nothing is confirmed unseen. */
const FIELD_LABELS: Record<string, string> = {
  counterparty: "Paid to", reference: "Reference", status: "Payment status", direction: "Direction", account_scope: "Account", reference_type: "Reference type",
  occurred_at: "Date", amount: "Amount", currency: "Currency",
};
const SHOWN_AT_TOP = ["amount", "occurred_at"];
/** The details a person can correct right on the card, in the order they read them; each correction is a new version and the earlier value stays. */
const EDITABLE = ["amount", "counterparty", "reference", "status", "direction", "occurred_at"];
const HINTS: Record<string, string> = {
  amount: "e.g. 10,000.00", counterparty: "name or VPA", reference: "exactly as shown, keep leading zeros", status: "successful, pending or failed",
  direction: "debit or credit", occurred_at: "e.g. 12 Oct 2026 10:42 AM or 2026-10-12",
};
const editableKeys = (p: PaymentOut) => EDITABLE.filter((k) => p.fields[k]);
const WORDS = ["successful", "pending", "failed", "debit", "credit"];
const fieldValue = (f: FactOut | undefined): string => {
  if (!f?.value) return "";
  const v = f.value as { value?: unknown; amountMinor?: string };
  if (v.amountMinor) return formatInr(v.amountMinor);
  return f.rawText ?? String(v.value ?? "");
};
const detailKeys = (p: PaymentOut) => Object.keys(p.fields).filter((k) => !SHOWN_AT_TOP.includes(k) && p.fields[k]?.value);
const CONFIRMED = ["user_confirmed", "reviewer_checked"];
const isChecked = (p: PaymentOut) => CONFIRMED.includes(p.identityReviewState) && Object.values(p.fields).filter((f) => f.value).every((f) => CONFIRMED.includes(f.reviewState));

// ---- home ---------------------------------------------------------------------------------------------------------------

export function JourneyHome() {
  const { t, language } = useI18n();
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => { setFailed(false); api.listCases().then(setCases).catch(() => { setFailed(true); setCases([]); }); }, []);
  useEffect(load, [load]);
  const recent = [...(cases ?? [])].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))[0];
  return <>
    <PageHead eyebrow={t("A clearer next step")} title={t("Let’s make your next step clear.")} lead={t("Prepare a report, keep your evidence together, and know who is helping.")}/>
    <div className="withaside">
      <div className="stack">
        <section className="hero">
          <div className="eyebrow">{t("Start with what you know")}</div>
          <h2>{t("You do not need a perfect account to begin.")}</h2>
          <p className="muted">{t("Tell us what happened or add a screenshot. You can check and correct every detail before sending.")}</p>
          <p className="actions"><Link className="button" to="/report/story"><Icon name="plus" size={18}/> {t("Start a report")}</Link>
            <Link className="button secondary" to="/assistant"><Icon name="chat" size={18}/> {t("Chat with CyberBrief")}</Link></p>
        </section>
        <section className="card" aria-label={t("Your recent report")}>
          <div className="sectionhead"><h2>{t("Your recent report")}</h2><Link className="button quiet" to="/track">{t("View all")} →</Link></div>
          {cases === null && !failed && <StatePanel kind="loading"/>}
          {failed && <StatePanel kind="unavailable" action={{ label: "Try again", run: load }}/>}
          {cases !== null && !failed && !recent && <div className="empty"><h3>{t("No reports yet")}</h3><p className="muted">{t("Start with your account or a screenshot.")}</p></div>}
          {recent && <ReportRow c={recent} language={language}/>}
        </section>
      </div>
      <section className="card"><h2>{t("What happens next")}</h2>
        {[["Prepare", "Add your account and evidence."], ["Review", "Confirm the details you recognize."], ["Follow up", "Respond to specific requests in one place."]].map(([a, b], i) =>
          <div className="task" key={a}><span className="number">{i + 1}</span><div><b>{t(a)}</b><p className="small muted">{t(b)}</p></div></div>)}
        <div className="note">{t("This demo uses fictional information and does not file an official complaint.")}</div>
      </section>
    </div>
  </>;
}

function stateOf(c: CaseSummary): { text: string; tone: "green" | "amber" | "" } {
  if (c.reviewState === "closed") return { text: "Closed", tone: "" };
  if (c.closureRequested) return { text: "Closure requested", tone: "amber" };
  if (c.reviewState === "returned") return { text: "Your action needed", tone: "amber" };
  if (c.reviewState === "submitted" || c.reviewState === "reviewed") return { text: "In review", tone: "green" };
  return { text: "Draft", tone: "amber" };
}

function ReportRow({ c, language }: { c: CaseSummary; language: string }) {
  const { t } = useI18n();
  const s = stateOf(c);
  const when = new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium" }).format(new Date(c.updatedAt));
  const to = c.reviewState === "private" ? `/report/story?case=${c.id}` : `/reports/${c.id}`;
  return <div className="reportrow">
    <span className="tileicon"><Icon name="file"/></span>
    <div className="grow"><span className="mono muted">{caseReference(c.id)}</span><b>{c.title}</b><p className="small muted">{t("Updated")} {when}{c.openQuestionCount > 0 ? ` · ${t("{n} open question(s)", { n: c.openQuestionCount })}` : ""}</p></div>
    <span className={`badge ${s.tone}`}>{t(s.text)}</span>
    <Link className="secondary icon-button button" to={to} aria-label={`${t("Open")} ${c.title}`} title={t("Open")}><Icon name="arrow" size={18}/></Link>
  </div>;
}

export function JourneyReports() {
  const { t, language } = useI18n();
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => { setFailed(false); api.listCases().then(setCases).catch(() => { setFailed(true); setCases([]); }); }, []);
  useEffect(load, [load]);
  return <>
    <PageHead eyebrow={t("A clearer next step")} title={t("Your reports")} lead={t("See what needs your attention and return to any saved draft.")}
      action={<Link className="button" to="/report/story"><Icon name="plus" size={18}/> {t("New report")}</Link>}/>
    <section className="card" aria-label={t("Your reports")}>
      {cases === null && <StatePanel kind="loading"/>}
      {failed && <StatePanel kind="unavailable" action={{ label: "Try again", run: load }}/>}
      {cases !== null && !failed && cases.length === 0 && <div className="empty"><h2>{t("No reports yet")}</h2><p className="muted">{t("Your reports will appear here as soon as you start.")}</p>
        <Link className="button" to="/report/story">{t("Start a report")}</Link></div>}
      {(cases ?? []).map((c) => <ReportRow key={c.id} c={c} language={language}/>)}
    </section>
  </>;
}

// ---- step 1: tell your story ------------------------------------------------------------------------------------------

/** The public demo takes curated fictional scenarios only, so Start leads here instead of to a free-text form the server would refuse. */
function CuratedStart() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { set } = useDraft();
  const [packs, setPacks] = useState<DemoPackOut[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => { setError(null); api.listDemoPacks().then(setPacks).catch((e) => { setPacks([]); setError(errorText(e, "The scenarios could not be loaded")); }); }, []);
  useEffect(load, [load]);
  async function start(pack: DemoPackOut) {
    setBusy(pack.packId);
    setError(null);
    try { const s = await api.loadDemoCase(pack.packId); set(s.case.id); navigate("/report/evidence"); }
    catch (e) { setError(errorText(e, "That scenario could not be started")); } finally { setBusy(null); }
  }
  return <>
    <PageHead eyebrow={t("A clearer next step")} title={t("Choose a fictional scenario")} lead={t("This public demo uses curated, fictional reports only. Real details cannot be entered here.")}/>
    <Steps at={0}/>
    <section className="card" aria-label={t("Fictional scenarios")}>
      {packs === null && <StatePanel kind="loading"/>}
      {error && <StatePanel kind="error" message={error} action={{ label: "Try again", run: load }}/>}
      {(packs ?? []).map((p) => <div className="reportrow" key={p.packId}>
        <span className="tileicon"><Icon name="file"/></span>
        <div className="grow"><b>{p.title}</b><p className="small muted">{p.initialSources} {t("source(s)")}{p.hasAudio ? ` · ${t("includes a voice recording")}` : ""}</p></div>
        <button type="button" disabled={busy !== null} onClick={() => void start(p)}>{t(busy === p.packId ? "Starting…" : "Start this fictional scenario")} <Icon name="arrow" size={18}/></button>
      </div>)}
      <div className="note">{t("This demo uses fictional information and does not file an official complaint.")}</div>
    </section>
  </>;
}

export function ReportStory() {
  const [params] = useSearchParams();
  const { session } = useAuth();
  const explicit = params.get("case");
  if (session?.publicFixturesOnly && !explicit) return <CuratedStart/>;
  return <StoryForm key={`${scopeOf(session)}:${explicit ?? "new"}`} explicit={explicit}/>;   // a different person never inherits what is on screen   // a new report and a resumed one never share what is on screen
}

/** The words as last saved: the newest typed account. Older ones stay on the report as history. */
const latestStory = (s: CaseSnapshot) => [...s.sources].filter((x) => x.kind === "typed_account").sort((x, y) => y.receivedAt.localeCompare(x.receivedAt))[0]?.textContent ?? "";

function StoryForm({ explicit }: { explicit: string | null }) {
  const { t, language } = useI18n();
  const navigate = useNavigate();
  const toast = useToast();
  const { revalidate } = useAuth();
  const { set, scope } = useDraft();
  const [caseId, setCaseId] = useState<string | null>(explicit);   // null means a new report: Start never reuses another one
  const [story, setStory] = useState(() => draftStore.story(scope, explicit) ?? "");   // unsent words of THIS report; a new report never borrows another one's
  const [saved, setSaved] = useState({ story: "", lang: "en", area: "" });
  const [area, setArea] = useState("");
  const [lang, setLang] = useState<"en" | "hi">(language === "hi" ? "hi" : "en");
  const [config, setConfig] = useState<WorkflowConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [voice, setVoice] = useState(false);
  const { session: who } = useAuth();
  const publicOnly = Boolean(who?.publicFixturesOnly);   // curated scenarios: the server refuses free text, so the words are shown but not editable
  const [loaded, setLoaded] = useState(!explicit);   // a resumed draft is read from the server before any of its words are shown
  useEffect(() => { void wf.config().then(setConfig).catch(() => setError("The list of areas could not be loaded.")); }, []);
  useEffect(() => {
    if (!explicit) return;
    let live = true;
    Promise.all([api.getCase(explicit), wf.progress(explicit).catch(() => null)]).then(([s, p]) => {
      if (!live) return;
      const server = latestStory(s);
      const savedLang = s.case.language === "hi" ? "hi" : "en";
      set(explicit);
      setStory(draftStore.story(scope, explicit) ?? server);   // words typed but not yet saved win over the saved copy
      setLang(savedLang);
      setArea(p?.area ?? "");
      setSaved({ story: server, lang: savedLang, area: p?.area ?? "" });
      setLoaded(true);
    }).catch(() => { if (live) navigate("/report/story", { replace: true }); });   // gone, deleted or not this person's: start clean
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [explicit, scope]);
  useEffect(() => { if (loaded) draftStore.setStory(scope, caseId, story); }, [story, loaded, scope, caseId]);

  /** Saves what changed on the server (creating the report the first time) and goes on. Earlier wording stays on the report as history. */
  async function save(next: string) {
    setError(null);
    if (!story.trim() && !caseId) { toast.say(t("Tell us a little about what happened, or attach evidence.")); }
    setBusy(true);
    try {
      if (!(await revalidate())) { setError("Someone else signed in on this browser, so nothing was saved. Your words are not sent."); return; }   // never send one person's words as another's
      let id = caseId;
      if (!id) {
        const title = (story.trim().split(/[.\n]/)[0] || t("Draft report")).slice(0, 80);
        id = (await api.createCase({ title, language: lang, incidentAccount: story.trim() || null })).case.id;
        setCaseId(id);
        draftStore.setStory(scope, null, null);   // the new-report box is empty again for the next incident
        setSaved((s) => ({ ...s, story: story.trim(), lang }));
      } else {
        if (!publicOnly && story.trim() && story.trim() !== saved.story.trim()) { await api.addText(id, story.trim(), "typed_account", newIdempotencyKey()); setSaved((s) => ({ ...s, story: story.trim() })); }
        if (lang !== saved.lang) { await wf.setLanguage(id, lang); setSaved((s) => ({ ...s, lang })); }
      }
      if (area && area !== saved.area) { await wf.setArea(id, area); setSaved((s) => ({ ...s, area })); }
      draftStore.setStory(scope, id, story);
      set(id);
      navigate(next.replace(":id", id));
    } catch (e) {
      setError(errorText(e, "Could not save your account"));   // what was typed stays in the box
    } finally { setBusy(false); }
  }

  if (!loaded) return <StatePanel kind="loading"/>;
  return <>
    <PageHead eyebrow={t("A clearer next step")} title={t("Tell us what happened.")} lead={t("A few sentences are enough. You can add details later.")}/>
    <Steps at={0}/>
    <div className="withaside">
      <section className="card">
        <div className="form">
          <label className="field">{t("Your account")}
            <textarea value={story} onChange={(e) => setStory(e.target.value)} maxLength={4000} rows={5} readOnly={publicOnly}/>
            <small>{publicOnly ? t("This is a fictional scenario, so its wording cannot be changed here.") : t("Do not include passwords, PINs or one-time codes.")}</small></label>
          <div className="actions">
            <IconButton name="clip" label={t("Attach evidence")} disabled={busy} onClick={() => void save("/report/evidence")}/>
            <IconButton name="chat" label={t("Voice input")} disabled={busy} onClick={() => setVoice(true)}/>
            <span className="small muted">{t("Type, attach a screenshot, or record a spoken account.")}</span>
          </div>
          <div className="fieldrow">
            <label className="field">{t("Area")}
              <select value={area} onChange={(e) => setArea(e.target.value)}><option value="">{t("Choose an area")}</option>
                {config?.areas.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select>
              <small>{t("Used to find the right reviewer.")}</small></label>
            <label className="field">{t("Preferred language")}
              <select value={lang} onChange={(e) => setLang(e.target.value as "en" | "hi")}><option value="en">English</option><option value="hi">हिन्दी</option></select>
              <small>{t("The language your report is kept in.")}</small></label>
          </div>
          {error && <StatePanel kind="error" message={error}/>}
        </div>
        <div className="footeractions"><span className="small muted">{t("Private until you send it")}</span>
          <button type="button" disabled={busy || (!story.trim() && !publicOnly)} onClick={() => void save("/report/evidence")}>{t(busy ? "Saving…" : "Continue")} <Icon name="arrow" size={18}/></button></div>
      </section>
      <AgentCard title={t("Intake assistant")} subtitle={t("Suggestions only. You decide.")}
        steps={[{ title: t("Your words, kept"), text: t("Your account is saved as you wrote it.") }, { title: t("You check the result"), text: t("Nothing is shared until you send it.") }, { title: t("One question at a time"), text: t("Ask only for what is missing.") }]}
        focus={`${t("Current focus")}|${t("Start with a few sentences. You can add a screenshot next.")}`}/>
    </div>
    <Dialog title={t("Voice account")} open={voice} onClose={() => setVoice(false)}
      footer={<><button type="button" className="secondary" onClick={() => setVoice(false)}>{t("Not now")}</button>
        <button type="button" disabled={busy} onClick={() => { setVoice(false); void save("/cases/:id/material"); }}>{t("Record or upload audio")}</button></>}>
      <p>{t("A spoken account is recorded with your permission and transcribed. You then correct the transcript before it is used.")}</p>
    </Dialog>
  </>;
}

// ---- step 2: add evidence -----------------------------------------------------------------------------------------------

export function ReportEvidence() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const toast = useToast();
  const { caseId } = useDraft();
  const { session } = useAuth();
  const publicOnly = Boolean(session?.publicFixturesOnly);
  const { snapshot, error, reload } = useCase(caseId ?? undefined);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!caseId) return <Navigate to="/report/story" replace/>;

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (!files.length) return;
    setBusy(true);
    setProblem(null);
    try {
      for (const file of files) {
        const current = await api.getCase(caseId!);
        await api.uploadImage(caseId!, file, newIdempotencyKey(), current.case.currentRevision);   // a real upload; the file is kept with the report
      }
      await reload();
      toast.say(`${files.length} ${t("file(s) added to your report.")}`);
    } catch (e) { setProblem(errorText(e, "A file could not be added")); } finally { setBusy(false); }
  }

  async function read() {
    setReading(true);
    setProblem(null);
    try {
      const current = await api.getCase(caseId!);
      try {
        const run = await api.createRun(caseId!, { expectedRevision: current.case.currentRevision }, newIdempotencyKey());
        for (let i = 0; i < 180; i++) {   // the pipeline reads the evidence; wait for it to say it is done, or that it failed
          const now = await api.getRun(caseId!, run.id);
          if (!["queued", "running"].includes(now.status)) { if (now.status === "failed") throw new Error(t("Reading failed. You can try again.")); break; }
          await new Promise((r) => setTimeout(r, 1000));
        }
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 400)) throw e;   // "nothing new to read" is not a failure: carry on with what is there
      }
      navigate("/report/review");
    } catch (e) { setProblem(errorText(e, "The evidence could not be read")); } finally { setReading(false); }
  }

  const files = (snapshot?.sources ?? []).filter((s) => ["image", "audio", "typed_account", "supplied_official_update"].includes(s.kind));
  return <>
    <PageHead eyebrow={t("A clearer next step")} title={t("Add what you already have.")} lead={t("Receipts and conversations help your reviewer understand the account.")}/>
    <Steps at={1}/>
    <div className="withaside">
      <section className="card">
        {publicOnly ? <div className="note">{t("This public demo uses the scenario you chose. Adding your own files is not available here.")}</div> : <div className="dropzone"><Icon name="download" size={30}/><h3>{t("Receipts, screenshots or a written account")}</h3>
          <p className="small muted">{t("Files are uploaded to your private report and read for payment details.")}</p>
          <label className="button secondary" htmlFor="evidence-files"><Icon name="clip" size={18}/> {t("Choose files")}</label>
          <input id="evidence-files" type="file" multiple accept="image/png,image/jpeg,image/webp" hidden onChange={(e) => void upload(e)} disabled={busy}/></div>}
        {busy && <p role="status" className="small muted">{t("Adding your files…")}</p>}
        {error && <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => void reload() }}/>}
        {problem && <StatePanel kind="error" message={problem}/>}
        {snapshot && files.length === 0 && <div className="empty"><p className="muted">{t("Nothing added yet. You can also go on with just your written account.")}</p></div>}
        {files.map((f) => <div className="uploaditem" key={f.id}>
          <span className="tileicon"><Icon name="file"/></span>
          <div className="grow"><b className="small">{f.displayFilename}</b><small>{t("Added to your report")} · {f.kind.replace(/_/g, " ")}</small></div>
          {f.kind !== "typed_account" && <a className="secondary icon-button button" href={api.sourceUrl(caseId!, f.id)} target="_blank" rel="noreferrer noopener" aria-label={`${t("View")} ${f.displayFilename}`} title={t("View")}><Icon name="eye" size={18}/></a>}
        </div>)}
        <div className="footeractions"><Link className="button secondary" to={`/report/story?case=${caseId}`}><Icon name="back" size={18}/> {t("Back")}</Link>
          <button type="button" disabled={busy || reading || files.length === 0} onClick={() => void read()}><Icon name="spark" size={18}/> {t("Read my evidence")}</button></div>
      </section>
      <AgentCard title={t("Evidence assistant")} subtitle={t("Suggestions only. You decide.")}
        steps={[{ title: t("Readable details"), text: t("Amounts and references are read from your files.") }, { title: t("Unclear stays unclear"), text: t("An unreadable detail is marked, never guessed.") }, { title: t("You confirm"), text: t("Each payment is checked by you next.") }]}/>
    </div>
    <Dialog title={t("Reading your evidence")} open={reading} onClose={() => undefined}><p role="status"><span className="spinner"/> {t("Reading your files and preparing the payment details…")}</p></Dialog>
  </>;
}

// ---- step 3: check each payment -----------------------------------------------------------------------------------------

export function ReportReview() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const toast = useToast();
  const { caseId } = useDraft();
  const { snapshot, error, reload } = useCase(caseId ?? undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [fix, setFix] = useState<PaymentOut | null>(null);
  const [fixField, setFixField] = useState("amount");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const waiting = Boolean(snapshot?.case.activeRunId);
  useEffect(() => { if (!waiting) return; const id = window.setInterval(() => void reload(), 2000); return () => window.clearInterval(id); }, [waiting, reload]);
  if (!caseId) return <Navigate to="/report/story" replace/>;
  if (error) return <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => void reload() }}/>;
  if (!snapshot) return <StatePanel kind="loading"/>;

  const payments = snapshot.payments.filter((p) => !p.mergedIntoPaymentId && p.includedInIncident !== false);
  const confirmed = payments.filter(isChecked).length;
  const act = async (key: string, actions: ReviewAction[], done: string) => {
    setBusy(key);
    setProblem(null);
    try { await api.review(caseId, snapshot.case.currentRevision, actions); await reload(); toast.say(done); }
    catch (e) { setProblem(errorText(e, "That could not be saved")); await reload(); } finally { setBusy(null); }
  };
  const confirmAll = (p: PaymentOut): ReviewAction[] => {
    const facts = Object.values(p.fields).filter((f) => f.value && !CONFIRMED.includes(f.reviewState) && f.reviewState !== "excluded");
    const out: ReviewAction[] = facts.map((f) => ({ type: "confirm", targetType: "fact", targetId: f.factId }));
    if (!CONFIRMED.includes(p.identityReviewState)) out.push({ type: "confirm", targetType: "payment", targetId: p.id });
    return out;
  };
  const edit = (p: PaymentOut) => { setFix(p); setFixField(editableKeys(p)[0] ?? "amount"); setAmount(""); setReason(""); };
  const pendingMinor = snapshot.totals.pendingMinor;

  return <>
    <PageHead eyebrow={t("A clearer next step")} title={t("Check the important details.")} lead={t("Confirm each payment separately. Correcting one detail does not change the others.")}/>
    <Steps at={2}/>
    <div className="withaside">
      <section className="card">
        <div className="sectionhead"><h2>{t("Your payments")}</h2><span className="badge green">{confirmed} {t("of")} {payments.length} {t("checked")}</span></div>
        <div className="progressline" aria-hidden="true"><span style={{ width: `${payments.length ? (100 * confirmed) / payments.length : 0}%` }}/></div>
        {waiting && <StatePanel kind="loading" message={t("Your evidence is still being read.")}/>}
        {!waiting && payments.length === 0 && <div className="empty"><p className="muted">{t("No payment was found in your evidence. You can add a clearer screenshot, or go on and send your account as it is.")}</p>
          <Link className="button secondary" to="/report/evidence">{t("Add evidence")}</Link></div>}
        {payments.map((p, i) => {
          const done = isChecked(p);
          const src = p.sourceIds[0];
          return <div className={`reviewpayment${done ? " confirmed" : ""}`} key={p.id} aria-label={`${t("Payment")} ${i + 1}`}>
            <div className="paymenttop"><div><span className="small muted">{t("Payment")} {i + 1}{payDate(p) ? ` · ${payDate(p)}` : ""}</span><div className="amount">{formatInr(payAmount(p), t("Amount not clear"))}</div></div>
              <span className={`badge ${done ? "green" : "amber"}`}>{t(done ? "Confirmed by you" : "Needs your check")}</span></div>
            <div className="detailgrid">
              <div><span>{t("Paid to")}</span><b>{payName(p) || t("Not clear")}</b></div><div><span>{t("Reference")}</span><b className="mono">{payRef(p) || t("Not clear")}</b></div>
              {["status", "direction"].map((k) => <div key={k}><span>{t(FIELD_LABELS[k])}</span><b>{fieldValue(p.fields[k]) ? (WORDS.includes(fieldValue(p.fields[k]).toLowerCase()) ? t(fieldValue(p.fields[k]).toLowerCase()) : fieldValue(p.fields[k])) : t("Not clear")}</b></div>)}
              {detailKeys(p).filter((k) => !["counterparty", "reference", "status", "direction"].includes(k)).map((k) => <div key={k}><span>{t(FIELD_LABELS[k] ?? k)}</span><b>{fieldValue(p.fields[k])}</b></div>)}
            </div>
            <div className="actions">
              <button type="button" className={done ? "secondary" : ""} disabled={busy !== null || done || !payAmount(p)} onClick={() => void act(p.id, confirmAll(p), `${t("Payment")} ${i + 1} ${t("confirmed.")}`)}><Icon name="check" size={18}/> {t(done ? "Confirmed" : "These details are right")}</button>
              <IconButton name="edit" label={`${t("Edit payment")} ${i + 1}`} disabled={busy !== null || editableKeys(p).length === 0} onClick={() => edit(p)}/>
              {src && <a className="secondary icon-button button" href={api.sourceUrl(caseId, src)} target="_blank" rel="noreferrer noopener" aria-label={`${t("View receipt for payment")} ${i + 1}`} title={t("View")}><Icon name="eye" size={18}/></a>}
            </div>
          </div>;
        })}
        <p className="small"><Link to={`/cases/${caseId}/review`}>{t("Open the full check page: every field, duplicates and the original files")} →</Link></p>
        {pendingMinor && pendingMinor !== "0" && <div className="note amber">{t("A separate pending payment of")} {formatInr(pendingMinor)} {t("is not counted as a successful debit.")}</div>}
        {snapshot.duplicateCandidates.length > 0 && <div className="note amber">{t("Two payments may be the same one. Whether to count them once is your decision.")} <Link to={`/cases/${caseId}/review`}>{t("Decide on the full check page")}</Link></div>}
        {problem && <StatePanel kind="error" message={problem}/>}
        <div className="footeractions"><Link className="button secondary" to="/report/evidence"><Icon name="back" size={18}/> {t("Back")}</Link>
          <button type="button" onClick={() => navigate("/report/send")}>{t("Review report")} <Icon name="arrow" size={18}/></button></div>
      </section>
      <div className="stack">
        <section className="card"><h3>{t("Your amounts, kept separate")}</h3>
          <div className="labelvalue"><span>{t("You reported")}</span><b>{formatInr(snapshot.totals.citizenReportedMinor, t("Not stated"))}</b></div>
          <div className="labelvalue"><span>{t("Checked successful payments")}</span><b>{confirmed > 0 ? formatInr(snapshot.totals.reviewedDebitMinor) : t("Not reviewed yet")}</b></div>
          <div className="labelvalue"><span>{t("Pending payment")}</span><b>{formatInr(pendingMinor, t("None"))}</b></div></section>
        <AgentCard title={t("Evidence assistant")} subtitle={t("Suggestions only. You decide.")}
          steps={[{ title: t("Sources stay attached"), text: t("Every detail can be traced to its file.") }, { title: t("You control the decision"), text: t("Nothing counts until you confirm it.") }]}/>
      </div>
    </div>
    <Dialog title={`${t("Correct payment")} ${fix ? payments.indexOf(fix) + 1 : ""}`} open={fix !== null} onClose={() => setFix(null)}
      footer={<><button type="button" className="secondary" onClick={() => setFix(null)}>{t("Cancel")}</button>
        <button type="button" disabled={!amount.trim() || busy !== null} onClick={() => { const p = fix!; setFix(null); void act(p.id, [{ type: "correct", targetType: "fact", targetId: p.fields[fixField].factId, rawText: fixField === "amount" ? `INR ${amount.trim()}` : amount.trim(), reason: reason.trim() || null }], t("Correction saved. The earlier value is kept in the history.")); }}><Icon name="check" size={18}/> {t("Save correction")}</button></>}>
      {fix && <>
        <label className="field">{t("Which detail")}<select value={fixField} onChange={(e) => { setFixField(e.target.value); setAmount(""); }}>
          {editableKeys(fix).map((k) => <option key={k} value={k}>{t(FIELD_LABELS[k])}</option>)}</select></label>
        <p className="small muted">{t("Now")}: {fieldValue(fix.fields[fixField]) || t("Not clear")}</p>
        <label className="field">{t("New value")}<input inputMode={fixField === "amount" ? "decimal" : undefined} value={amount} onChange={(e) => setAmount(e.target.value)} placeholder={t(HINTS[fixField] ?? "")}/></label>
      </>}
      <label className="field">{t("Why (optional)")}<input value={reason} onChange={(e) => setReason(e.target.value)}/></label>
      <p className="small muted">{t("The earlier value is kept in the report's history.")}</p>
    </Dialog>
  </>;
}

// ---- step 4: send -----------------------------------------------------------------------------------------------------------

export function ReportSend() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const toast = useToast();
  const { caseId, scope } = useDraft();
  const { revalidate } = useAuth();
  const { snapshot, error, reload } = useCase(caseId ?? undefined);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [progressFailed, setProgressFailed] = useState(false);
  const [config, setConfig] = useState<WorkflowConfig | null>(null);
  const [area, setArea] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const loadProgress = useCallback(() => { if (caseId) { setProgressFailed(false); void wf.progress(caseId).then(setProgress).catch(() => setProgressFailed(true)); } }, [caseId]);   // a failed read is shown as unavailable, never as missing input
  useEffect(loadProgress, [loadProgress]);
  useEffect(() => { void wf.config().then(setConfig).catch(() => undefined); }, []);
  if (!caseId) return <Navigate to="/report/story" replace/>;
  if (error) return <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => void reload() }}/>;
  if (!snapshot) return <StatePanel kind="loading"/>;

  const payments = snapshot.payments.filter((p) => !p.mergedIntoPaymentId && p.includedInIncident !== false);
  const story = latestStory(snapshot) || (draftStore.story(scope, caseId) ?? "");
  async function send() {
    if (!progress?.area) { toast.say(t("Choose the report area before routing it.")); return; }
    setBusy(true);
    setProblem(null);
    try {
      if (!(await revalidate())) { setProblem("Someone else signed in on this browser, so this report was not sent."); return; }
      await api.submitForReview(caseId!);
      draftStore.setStory(scope, caseId, null);
      draftStore.setCaseId(scope, null);   // storage only: clearing this page's own state would send it back to step one before it can leave
      toast.say(t("Report sent. An assignment is being prepared for the admin."));
      navigate(`/reports/${caseId}`);
    } catch (e) { setProblem(errorText(e, "The report could not be sent")); } finally { setBusy(false); }
  }
  return <>
    <PageHead eyebrow={t("A clearer next step")} title={t("Ready to send your report?")} lead={t("You control when this private draft becomes visible to a reviewer.")}/>
    <Steps at={3}/>
    <div className="withaside">
      <section className="card"><h2>{snapshot.case.title}</h2>
        {story && <p className="quote">{story}</p>}
        {progressFailed ? <StatePanel kind="error" message={t("Where your report is could not be checked, so it cannot be sent yet. Your report itself is safe.")} action={{ label: "Try again", run: loadProgress }}/>
          : !progress ? <StatePanel kind="loading"/>
          : progress.area ? <div className="labelvalue"><span>{t("Area")}</span><b>{progress.areaName}</b></div>
          : <div className="note amber"><label className="field">{t("Choose your area")}<select value={area} onChange={(e) => setArea(e.target.value)}><option value="">{t("Choose an area")}</option>
              {config?.areas.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select><small>{t("Your report cannot be sent to a reviewer until it has an area.")}</small></label>
            <button type="button" className="secondary" disabled={busy || !area} onClick={() => { setBusy(true); setProblem(null); wf.setArea(caseId, area).then(() => loadProgress()).catch((e) => setProblem(errorText(e, "That did not work"))).finally(() => setBusy(false)); }}>{t("Save area")}</button></div>}
        <div className="labelvalue"><span>{t("Evidence")}</span><b>{snapshot.sources.filter((s) => s.kind !== "typed_account").length} {t("files")}</b></div>
        <div className="labelvalue"><span>{t("Payments checked")}</span><b>{payments.filter(isChecked).length} {t("of")} {payments.length}</b></div>
        <div className="note">{t("Sending shares this report with the review team. This is a CyberBrief demo record, not an official complaint.")}</div>
        {payments.some((p) => !isChecked(p)) && <p className="small muted">{t("Unchecked details will be clearly marked for the reviewer.")}</p>}
        {problem && <StatePanel kind="error" message={problem}/>}
        <div className="footeractions"><Link className="button secondary" to="/report/review"><Icon name="back" size={18}/> {t("Back to details")}</Link>
          <button type="button" disabled={busy || !progress?.area} onClick={() => void send()}><Icon name="check" size={18}/> {t(busy ? "Sending…" : "Send for review")}</button></div>
      </section>
      <AgentCard title={t("Coordinator preview")} subtitle={t("Suggestions only. You decide.")}
        steps={[{ title: t("An assignment is prepared"), text: t("After you send, the coordinator prepares an officer for the admin.") }, { title: t("An admin approves"), text: t("No officer sees your report until the admin approves.") }]}/>
    </div>
  </>;
}

// ---- follow the report ---------------------------------------------------------------------------------------------------

export function TrackReport() {
  const { t, language } = useI18n();
  const toast = useToast();
  const { caseId } = useParams();
  const { snapshot, error, reload } = useCase(caseId);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [dialog, setDialog] = useState<"answer" | "refund" | "close" | null>(null);
  const [text, setText] = useState("");
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [progressFailed, setProgressFailed] = useState(false);
  const [config, setConfig] = useState<WorkflowConfig | null>(null);
  const [area, setArea] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const loadProgress = useCallback(() => { if (caseId) { setProgressFailed(false); void wf.progress(caseId).then(setProgress).catch(() => setProgressFailed(true)); } }, [caseId]);
  useEffect(() => { void wf.config().then(setConfig).catch(() => undefined); }, []);
  useEffect(loadProgress, [loadProgress, snapshot?.case.currentRevision, snapshot?.case.reviewState]);
  // what was typed to a reviewer is kept for this browser tab (cleared on sign-out), so leaving the page or a failed send loses nothing
  const answerKey = `cb-answers-${caseId}`;
  const saveDraftAnswer = (requestId: string, value: string) => { try { const all = JSON.parse(window.sessionStorage.getItem(answerKey) ?? "{}"); all[requestId] = value; window.sessionStorage.setItem(answerKey, JSON.stringify(all)); } catch { /* storage may be blocked; typing still works */ } };
  const savedDraftAnswer = (requestId: string): string => { try { return JSON.parse(window.sessionStorage.getItem(answerKey) ?? "{}")[requestId] ?? ""; } catch { return ""; } };
  if (error) return <StatePanel kind="error" message={error} action={{ label: "Try again", run: () => void reload() }}/>;
  if (!snapshot) return <StatePanel kind="loading"/>;
  if (!progress) return <>
    <PageHead eyebrow={caseReference(snapshot.case.id)} title={snapshot.case.title} lead={`${t("Revision")} ${snapshot.case.currentRevision}`}/>
    {progressFailed ? <StatePanel kind="error" message={t("Where your report is could not be loaded. Your report itself is safe.")} action={{ label: "Try again", run: loadProgress }}/> : <StatePanel kind="loading"/>}
    <section className="card"><h3>{t("Money and status")}</h3>
      <div className="labelvalue"><span>{t("You reported")}</span><b>{formatInr(snapshot.totals.citizenReportedMinor, t("Not stated"))}</b></div>
      <div className="labelvalue"><span>{t("Outstanding")}</span><b>{formatInr(snapshot.recovery.outstandingMinor)}</b></div></section>
  </>;

  const c = snapshot.case;
  const open = progress.requests.filter((r) => !r.answered);
  const clock = (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  const closed = c.reviewState === "closed";
  const eyebrow = closed ? "Record closed" : open.length ? "Your turn" : progress.reviewer ? "Your reviewer is working" : "Report received";
  const lastOpen = open[0];

  async function run(work: () => Promise<unknown>, done: string) {
    setBusy(true);
    setProblem(null);
    try { await work(); setDialog(null); setText(""); setAmount(""); await reload(); loadProgress(); toast.say(done); }
    catch (e) { setProblem(errorText(e, "That did not work")); } finally { setBusy(false); }
  }
  async function download() {
    try {
      const fresh = await api.getCase(c.id);
      const out = await api.createExport(c.id, "html", fresh.case.currentRevision, newIdempotencyKey());
      window.open(`/api/cases/${c.id}/exports/${out.id}`, "_blank", "noopener");
      toast.say(t("Your report summary was prepared."));
    } catch (e) { toast.say(errorText(e, "The summary could not be prepared")); }
  }

  const official = snapshot.latestOfficialStatus.event;
  const needsArea = !progress.area && !progress.reviewer && c.reviewState === "submitted";
  return <>
    <PageHead eyebrow={caseReference(c.id)} title={c.title} lead={`${progress.areaName ?? t("No area chosen")} · ${t("Revision")} ${c.currentRevision}`}
      action={<IconButton name="download" label={t("Download a summary of this report")} onClick={() => void download()}/>}/>
    <div className="withaside">
      <div className="stack">
        <section className="hero"><div className="eyebrow">{t(eyebrow)}</div>
          <h2>{t(closed ? "Your CyberBrief record is closed." : progress.step)}</h2>
          <p className="muted">{closed ? t("This does not change any official complaint status.") : t(progress.nextAction)}</p>
          {progress.responsible && !closed && <p className="small"><b>{t("Who acts next")}:</b> {progress.responsible === "You" ? t("You") : progress.responsible}</p>}
          <p className="actions">{lastOpen ? <button type="button" onClick={() => { setText(savedDraftAnswer(lastOpen.id)); setDialog("answer"); }}><Icon name="mail" size={18}/> {t("Answer the request")}</button>
            : <Link className="button secondary" to={`/cases/${c.id}/receipt`}><Icon name="file" size={18}/> {t("View receipt")}</Link>}
            <Link className="button quiet" to={`/cases/${c.id}`}>{t("Full details and accounting")} →</Link></p>
        </section>
        {needsArea && <section className="card" aria-label={t("Choose your area")}><h2>{t("Choose your area")}</h2>
          <p className="muted">{t("Your report cannot be sent to a reviewer until it has an area.")}</p>
          <label className="field">{t("Area")}<select value={area} onChange={(e) => setArea(e.target.value)}><option value="">{t("Choose an area")}</option>
            {config?.areas.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>
          <button type="button" disabled={busy || !area} onClick={() => void run(async () => { await wf.setArea(c.id, area); }, t("Area saved. Your report is being routed."))}>{t("Save area")}</button>
          {problem && dialog === null && <StatePanel kind="error" message={problem}/>}</section>}
        <section className="card" aria-label={t("Report activity")}><h2>{t("Report activity")}</h2>
          <ol className="timeline">
            <li><b>{t("Report received")}</b><p>{t("Your evidence and account were saved.")}</p><time>{c.submittedAt ? clock(c.submittedAt) : ""}</time></li>
            <li className={progress.reviewer ? "" : "current"}><b>{progress.reviewer ? `${t("Assigned to")} ${progress.reviewer}` : !progress.area ? t("Waiting for your area") : progress.assignmentPrepared ? t("Assignment awaiting approval") : t("Waiting to be routed")}</b>
              <p>{progress.reviewer ? `${t("Your reviewer covers")} ${progress.areaName ?? ""}.` : !progress.area ? t("Choose your area so the report can be routed.") : progress.assignmentPrepared ? t("The coordinator has prepared an assignment for the admin.") : t("Your report is queued to be routed to a reviewer.")}</p></li>
            {progress.requests.map((r) => <li key={r.id} className={r.answered ? "" : "current"}><b>{t("Clarification requested")}</b><p>{r.answered ? t("Your reply was received.") : r.body}</p><time>{clock(r.createdAt)}</time></li>)}
            {progress.bankVerificationPending && <li><b>{t("Verification request recorded")}</b><p>{t("Simulated bank correspondence. No bank was contacted.")}</p></li>}
          </ol></section>
      </div>
      <div className="stack">
        <section className="card"><h3>{t("Money and status")}</h3>
          <div className="labelvalue"><span>{t("You reported")}</span><b>{formatInr(snapshot.totals.citizenReportedMinor, t("Not stated"))}</b></div>
          <div className="labelvalue"><span>{t("Reviewed successful debits")}</span><b>{formatInr(snapshot.recovery.reviewedDebitMinor)}</b></div>
          <div className="labelvalue"><span>{t("Reviewed refunds")}</span><b>{formatInr(snapshot.recovery.matchedRefundMinor)}</b></div>
          <div className="labelvalue"><span>{t("Outstanding")}</span><b>{formatInr(snapshot.recovery.outstandingMinor)}</b></div>
          <hr/><p className="small muted">{t("Official complaint status")}</p>
          <span className="badge">{official ? `${official.rawStatus}` : t("No official update recorded")}</span></section>
        <section className="card"><h3>{t("Something changed?")}</h3>
          <p><button type="button" className="quiet" onClick={() => { setProblem(null); setDialog("refund"); }}><Icon name="plus" size={18}/> {t("Report a refund")}</button></p>
          {!closed && <p><button type="button" className="quiet" onClick={() => { setProblem(null); setDialog("close"); }}><Icon name="folder" size={18}/> {t("Ask to close this record")}</button></p>}</section>
      </div>
    </div>

    <Dialog title={t("Reply to your reviewer")} open={dialog === "answer"} onClose={() => { if (lastOpen) saveDraftAnswer(lastOpen.id, text); setDialog(null); }}
      footer={<button type="button" disabled={busy || (!text.trim() && files.length === 0)} onClick={() => void run(async () => {
        const sourceIds: string[] = [];
        for (const file of files) sourceIds.push((await api.uploadImage(c.id, file, newIdempotencyKey(), (await api.getCase(c.id)).case.currentRevision)).id);   // real uploads, kept with the report
        await wf.reply(c.id, lastOpen!.id, { text: text.trim(), sourceIds, expectedRevision: (await api.getCase(c.id)).case.currentRevision });
        setFiles([]);
        saveDraftAnswer(lastOpen!.id, "");
      }, t("Reply sent. Your reviewer will read it."))}><Icon name="mail" size={18}/> {t(busy ? "Sending…" : "Send my answer")}</button>}>
      {lastOpen && <p className="quote">{lastOpen.body}</p>}
      <label className="field">{t("Your answer")}<textarea value={text} onChange={(e) => { setText(e.target.value); if (lastOpen) saveDraftAnswer(lastOpen.id, e.target.value); }} maxLength={2000}/></label>
      <label className="field">{t("Attach a receipt or screenshot (optional)")}
        <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []))}/>
        <small>{files.length ? files.map((f) => f.name).join(", ") : t("PNG, JPEG or WebP. It is added to your report and your reviewer sees it with this answer.")}</small></label>
      {problem && <StatePanel kind="error" message={problem}/>}
    </Dialog>
    <Dialog title={t("Report a refund")} open={dialog === "refund"} onClose={() => setDialog(null)}
      footer={<button type="button" disabled={busy || !text.trim()} onClick={() => void run(async () => { await api.reportRefund(c.id, { text: text.trim(), amountRaw: amount.trim() ? `INR ${amount.trim()}` : null, expectedRevision: (await api.getCase(c.id)).case.currentRevision }, newIdempotencyKey()); }, t("Refund saved as a claim to be checked. Outstanding amounts do not change until it is reviewed."))}><Icon name="check" size={18}/> {t("Save refund claim")}</button>}>
      <label className="field">{t("What happened")}<textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={1000}/></label>
      <label className="field">{t("Amount in rupees (optional)")}<input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)}/></label>
      <div className="note amber">{t("A report of a refund does not change the outstanding amount until supporting evidence and allocation are reviewed.")}</div>
      {problem && <StatePanel kind="error" message={problem}/>}
    </Dialog>
    <Dialog title={t("Ask to close this record")} open={dialog === "close"} onClose={() => setDialog(null)}
      footer={<button type="button" disabled={busy || !text.trim()} onClick={() => void run(async () => { await api.prepareResolution(c.id, true, (await api.getCase(c.id)).case.currentRevision, text.trim()); }, t("Closure request sent for officer review. The record stays open."))}><Icon name="mail" size={18}/> {t("Send closure request")}</button>}>
      <label className="field">{t("Why would you like to close it?")}<textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={500}/></label>
      <p className="small muted">{t("Your reviewer reads the request. Sending it does not close an official complaint.")}</p>
      {problem && <StatePanel kind="error" message={problem}/>}
    </Dialog>
  </>;
}

// ---- help ---------------------------------------------------------------------------------------------------------------------

export function JourneyHelp() {
  const { t } = useI18n();
  return <>
    <PageHead eyebrow={t("A clearer next step")} title={t("Help, at the right moment.")} lead={t("Clear answers without leaving your report.")}/>
    <section className="card">{[
      ["What happens after I send a report?", "An admin approves an eligible officer. You can see who acts next in your report."],
      ["Can I correct a detail?", "Yes. Return to the payment and edit the specific field. The earlier value is kept in the history."],
      ["Does a refund close my complaint?", "No. Recovery, the CyberBrief workflow and official status are separate."],
      ["Is this connected to a bank or the police?", "No. CyberBrief uses fictional records and simulated correspondence."],
    ].map(([q, a]) => <details key={q}><summary>{t(q)}</summary><p className="muted">{t(a)}</p></details>)}</section>
    <p className="small"><Link to="/assistant">{t("Chat with CyberBrief")} →</Link></p>
  </>;
}

export type { CaseSnapshot };
