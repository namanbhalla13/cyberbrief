/** The screens before a case exists: landing, sign-in, home, register and tracking. */
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api, type CaseSummary, type DemoPackOut, type Language, type OtpRequestOut } from "./api";
import { ErrorNotice, JourneySteps, MoneyText, caseReference, needsYou, nextStep, sumMinor, useAuth, useI18n } from "./ui";


// ---- from features/landing/LandingPage --------------------------------------------------------

export function LandingPage() {
    const { t } = useI18n();
    return <div className="citizen-flow">
    <section className="welcome">
      <p className="eyebrow"> {t("Your story. In your own words.")} </p>
      <h1> {t("Worried about an online payment?")} </h1>
      <p> {t("Put your story, payment photos and messages in one place. We will help you check the details and prepare a report.")} </p>
      <p className="language-note">हिन्दी में बोलें या लिखें · Speak or write in English</p>
      <div className="actions"><Link className="button" to="/login?next=%2Fassistant"> {t("Chat with CyberBrief")} → </Link><Link className="button secondary" to="/login?next=%2Fregister"> {t("Fill in the form instead")} </Link><Link className="button secondary" to="/login?next=%2Ftrack"> {t("Open my saved reports")} </Link></div>
      <p className="small muted"> {t("A practice demo with fictional information. This does not file a police complaint.")} </p>
    </section>
    <section className="card">
      <h2> {t("One step at a time")} </h2>
      <ol className="simple-steps">
        <li><span aria-hidden="true">1</span><div><strong> {t("Tell us what happened")} </strong><p> {t("Use simple words. Hindi, English or a mix is fine.")} </p></div></li>
        <li><span aria-hidden="true">2</span><div><strong> {t("Add photos or a recording")} </strong><p> {t("Use what you have. You can add more later.")} </p></div></li>
        <li><span aria-hidden="true">3</span><div><strong> {t("Check and save")} </strong><p> {t("Correct any mistakes, then keep a copy of your report.")} </p></div></li>
        <li><span aria-hidden="true">4</span><div><strong> {t("Send for review")} </strong><p> {t("A reviewer is chosen for your area. You can see who has your report.")} </p></div></li>
        <li><span aria-hidden="true">5</span><div><strong> {t("Follow progress")} </strong><p> {t("You see who acts next. If the reviewer needs one detail, you answer just that.")} </p></div></li>
      </ol>
    </section>
    <details className="card"><summary> {t("How your information is used")} </summary><p> {t("AI suggests details from your files. You check them before they count. CyberBrief does not verify bank payments, recover money or change an official complaint.")} </p></details>
  </div>;
}


// ---- from features/auth/LoginPage -------------------------------------------------------------

type Kind = "citizen" | "officer" | "admin";
export function LoginPage() {
    const { t } = useI18n();
    const navigate = useNavigate();
    const [params] = useSearchParams();
    const next = params.get("next");
    const { ensureSession, setSession } = useAuth();
    const [kind, setKind] = useState<Kind>("citizen");
    const [mobile, setMobile] = useState("");
    const [code, setCode] = useState("");
    const [challenge, setChallenge] = useState<OtpRequestOut | null>(null);
    const [officerId, setOfficerId] = useState("");
    const [password, setPassword] = useState("");
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    function fail(e: unknown, fallback: string) {
        if (e instanceof ApiError)
            setError([e.message, e.nextStep].filter(Boolean).join(" "));
        else
            setError(e instanceof Error ? e.message : fallback);
    }
    function switchKind(to: Kind) {
        setKind(to);
        setError(null);
        setChallenge(null);
        setCode("");
    }
    async function sendCode(event: FormEvent) {
        event.preventDefault();
        setBusy(true);
        setError(null);
        try {
            await ensureSession();
            setChallenge(await api.requestOtp(mobile.trim()));
            setCode("");
        }
        catch (e) {
            if (e instanceof ApiError && e.status === 401) {
                // the browser's session was ended by a sign-out or sign-in elsewhere an instant ago: start a new one and ask once more
                try {
                    setSession(await api.startSession());
                    setChallenge(await api.requestOtp(mobile.trim()));
                    setCode("");
                    return;
                }
                catch (again) {
                    fail(again, "Could not send a code");
                    return;
                }
            }
            fail(e, "Could not send a code");
        }
        finally {
            setBusy(false);
        }
    }
    async function verify(event: FormEvent) {
        event.preventDefault();
        setBusy(true);
        setError(null);
        try {
            setSession(await api.verifyOtp(mobile.trim(), code.trim()));
            navigate(next ?? "/home", { replace: true });
        }
        catch (e) {
            fail(e, "Could not sign you in");
        }
        finally {
            setBusy(false);
        }
    }
    async function officerSignIn(event: FormEvent) {
        event.preventDefault();
        setBusy(true);
        setError(null);
        try {
            await ensureSession();
            setSession(await api.officerLogin(officerId.trim(), password));
            navigate(next ?? "/officer", { replace: true });
        }
        catch (e) {
            fail(e, "Could not sign you in");
        }
        finally {
            setBusy(false);
        }
    }
    async function adminSignIn(event: FormEvent) {
        event.preventDefault();
        setBusy(true);
        setError(null);
        try {
            await ensureSession();
            setSession(await api.adminLogin(officerId.trim(), password));
            navigate(next ?? "/admin", { replace: true });
        }
        catch (e) {
            fail(e, "Could not sign you in");
        }
        finally {
            setBusy(false);
        }
    }
    return (<>
      <h1> {t("Sign in")} </h1>
      <p className="muted"> {t("This is CyberBrief's own demo sign-in. It is not a bank login, and it is not a login to cybercrime.gov.in. Never enter a code or password you were sent by anyone else here.")} </p>

      <section className="card">
        <fieldset className="rolepick">
          <legend> {t("Who are you signing in as?")} </legend>
          <label>
            <input type="radio" name="role" checked={kind === "citizen"} onChange={() => switchKind("citizen")}/>
            <span>
              <strong> {t("Citizen")} </strong>
              <span className="small muted"> {t("Report a fraud and track it")} </span>
            </span>
          </label>
          <label>
            <input type="radio" name="role" checked={kind === "officer"} onChange={() => switchKind("officer")}/>
            <span>
              <strong> {t("Officer")} </strong>
              <span className="small muted"> {t("Review complaint packets")} </span>
            </span>
          </label>
          <label>
            <input type="radio" name="role" checked={kind === "admin"} onChange={() => switchKind("admin")}/>
            <span>
              <strong> {t("Admin")} </strong>
              <span className="small muted"> {t("Assign reports to reviewers")} </span>
            </span>
          </label>
        </fieldset>

        {error && (<ErrorNotice message={error}/>)}

        {kind === "citizen" && challenge === null && (<form onSubmit={sendCode}>
            <label> {t("Mobile number")} <input value={mobile} onChange={(e) => setMobile(e.target.value.replace(/[?-?]/g, d => String(d.charCodeAt(0) - 0x0966)).replace(/\D/g, ""))} inputMode="numeric" maxLength={10} placeholder={t("98765 43210")} autoComplete="tel-national" required/>
            </label>
            <button type="submit" disabled={busy || mobile.trim().length < 10}> {t(busy ? "Sending…" : "Send code")} </button>
          </form>)}

        {kind === "citizen" && challenge !== null && (<form onSubmit={verify}>
            {challenge.demoCode ? (<div className="warn">
                <strong> {t("Demo mode \u2014 no SMS is sent.")} </strong> {t("The code below was generated by this demo so you can finish signing in. A real deployment would text it to your phone and would never show it on screen.")} <output className="otp" aria-label={t("Demo one-time code")}>
                  {challenge.demoCode}
                </output>
              </div>) : (<p className="warn"> {t("A code was sent to your phone.")} </p>)}
            <label> {t("6-digit code")} <input value={code} onChange={(e) => setCode(e.target.value.replace(/[?-?]/g, d => String(d.charCodeAt(0) - 0x0966)).replace(/\D/g, ""))} inputMode="numeric" maxLength={6} autoComplete="one-time-code" required/>
            </label>
            <div className="actions">
              <button type="submit" disabled={busy || code.trim().length !== 6}> {t(busy ? "Signing in…" : "Verify and continue")} </button>
              <button type="button" className="secondary" disabled={busy} onClick={() => setChallenge(null)}> {t("Change number")} </button>
            </div>
          </form>)}

        {kind === "admin" && (<form onSubmit={adminSignIn}>
            <p className="warn">
              <strong> {t("Demo sign-in \u2014 a simulated coordinator.")} </strong> {t("The admin role routes fictional reports between fictional officers. It carries no authority over any real organisation.")}
            </p>
            <label> {t("Admin ID")} <input value={officerId} onChange={(e) => setOfficerId(e.target.value)} required autoComplete="username"/>
            </label>
            <label> {t("Password")} <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password"/>
            </label>
            <button type="submit" disabled={busy || !officerId.trim() || !password}> {t(busy ? "Signing in…" : "Sign in")} </button>
          </form>)}

        {kind === "officer" && (<form onSubmit={officerSignIn}>
            <p className="warn">
              <strong> {t("Demo sign-in \u2014 no police directory is contacted.")} </strong> {t("The officer role is a simulated reviewer for this demo. It carries no police authority and changes nothing in any government system. Ask whoever set up this demo for an officer ID, or create one with")} <span className="mono"> {t("scripts/seed_officer.py")} </span>.
            </p>
            <label> {t("Officer ID")} <input value={officerId} onChange={(e) => setOfficerId(e.target.value)} required autoComplete="username"/>
            </label>
            <label> {t("Password")} <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password"/>
            </label>
            <button type="submit" disabled={busy || !officerId.trim() || !password}> {t(busy ? "Signing in…" : "Sign in")} </button>
          </form>)}
      </section>
    </>);
}


// ---- from features/home/CitizenHomePage -------------------------------------------------------

/**
 * The dashboard: your own numbers first, then the one report most likely to need you.
 *
 * Every figure is read back from the case list rather than computed here, and the two money
 * totals stay apart for the same reason they do everywhere else -- what you said you lost and
 * what your receipts confirm are different kinds of claim, and are never added together.
 */
export function CitizenHomePage() {
    const { t } = useI18n();
    const [cases, setCases] = useState<CaseSummary[] | null>(null);
    useEffect(() => { api.listCases().then(setCases).catch(() => setCases([])); }, []);
    const all = cases ?? [];
    const attention = all.filter(needsYou);
    const withOfficer = all.filter((c) => c.reviewState === "submitted" || c.reviewState === "reviewed");
    const closed = all.filter((c) => c.reviewState === "closed");
    const reported = sumMinor(all, (c) => c.citizenReportedMinor);
    const confirmed = sumMinor(all, (c) => c.reviewedDebitMinor);
    // the one to pick up again: still in your hands, most recently touched
    const resume = all.filter((c) => c.reviewState === "private" || c.reviewState === "returned")
        .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))[0];
    const recent = [...all].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, 5);
    return <div className="citizen-flow">
    <section className="welcome"><p className="eyebrow"> {t("A clearer next step")} </p><h1> {t("How can we help?")} </h1><p> {t("Start a report or continue where you left off.")} </p>
      <ol className="journey-strip" aria-label={t("How it works")}>
        {["Tell us what happened", "Add your proof", "Check the details", "Send for review", "Follow progress"].map((step, i) => <li key={step}><span className="step-no" aria-hidden="true">{i + 1}</span> {t(step)}</li>)}
      </ol>
      <p><Link className="button" to="/assistant"> {t("Chat with CyberBrief")} </Link></p></section>
    {/* A first-time citizen has no numbers, and a grid of zeros is a worse welcome than none. */}
    {cases !== null && all.length === 0 ? <>
    <div className="tiles home-choices">
      <Link className="tile primary-choice" to="/assistant"><span className="choice-icon" aria-hidden="true">＋</span><h2> {t("Start a report")} </h2><p> {t("Tell us what happened. We will guide you through each step.")} </p><span className="tile-go"> {t("Start \u2192")} </span></Link>
      <Link className="tile" to="/track"><span className="choice-icon" aria-hidden="true">↗</span><h2> {t("My saved reports")} </h2><p> {t("Continue a draft, add a refund or view an update.")} </p><span className="tile-go"> {t("Open \u2192")} </span></Link>
      <Link className="tile" to="/assistant"><span className="choice-icon" aria-hidden="true">…</span><h2> {t("Talk it through")} </h2><p> {t("Tell the assistant what happened. It drafts your complaint from the chat, and you check it.")} </p><span className="tile-go"> {t("Open →")} </span></Link>
    </div>
    <p className="small muted"> {t("Only use made-up information in this demo. Your report is saved to your CyberBrief account.")} </p>
    </> : <>
    <div className="stats">
      <div className="stat">
        <div className="label"> {t("Reports registered")} </div>
        <div className="value">{cases === null ? "—" : all.length}</div>
        <div className="hint"> {t("in your account")} </div>
      </div>
      <div className={`stat${attention.length > 0 ? " attention" : ""}`}>
        <div className="label"> {t("Needs your attention")} </div>
        <div className="value">{cases === null ? "—" : attention.length}</div>
        <div className="hint"> {t("waiting on you")} </div>
      </div>
      <div className="stat">
        <div className="label"> {t("With an officer")} </div>
        <div className="value">{cases === null ? "—" : withOfficer.length}</div>
        <div className="hint"> {t("sent for demo review")} </div>
      </div>
      <div className="stat">
        <div className="label"> {t("Closed")} </div>
        <div className="value">{cases === null ? "—" : closed.length}</div>
        <div className="hint"> {t("no longer tracked here")} </div>
      </div>
    </div>

    <div className="stats">
      <div className="stat">
        <div className="label"> {t("You told us you lost")} </div>
        <div className="value"><MoneyText minor={reported} fallback={t("not stated")}/></div>
        <div className="hint"> {t("across all your reports")} </div>
      </div>
      <div className="stat">
        <div className="label"> {t("Confirmed from receipts")} </div>
        <div className="value"><MoneyText minor={confirmed} fallback={t("nothing checked yet")}/></div>
        <div className="hint"> {t("you checked these yourself")} </div>
      </div>
    </div>
    <p className="small muted" style={{ marginTop: -8 }}> {t("These are two different things and are never added together. A zero confirmed subtotal means nothing has been checked yet, not that no money was lost.")} </p>

    {resume && <section className="nextstep">
      <h2> {t("Continue where you left off")} </h2>
      <p className="case-meta">
        <span className="caseref">{caseReference(resume.id)}</span>
        <strong>{resume.title}</strong>
        <span className={`badge badge-case-${nextStep(resume).tone}`}> {t(nextStep(resume).text)} </span>
      </p>
      <div className="actions">
        <Link className="button" to={`/cases/${resume.id}`}> {t("Open this report")} </Link>
        <Link className="button secondary" to="/register"> {t("Start a report")} </Link>
        <Link className="button secondary" to="/assistant"> {t("Talk it through")} </Link>
      </div>
    </section>}

    <section className="card">
      <h2> {t("Recent reports")} </h2>
      <ul className="plain">
        {recent.map((c) => {
        const step = nextStep(c);
        return <li key={c.id} className="caserow">
          <span className="caseref">{caseReference(c.id)}</span>
          <Link to={`/cases/${c.id}`}>{c.title}</Link>
          <span className={`badge badge-case-${step.tone}`}> {t(step.text)} </span>
          {c.openQuestionCount > 0 && <span className="muted small">{t("{n} open question(s)", { n: c.openQuestionCount })}</span>}
        </li>;
    })}
      </ul>
      <p className="small"><Link to="/track"> {t("See all my reports")} </Link></p>
    </section>

    {!resume && <div className="actions"><Link className="button" to="/register"> {t("Start a report")} </Link><Link className="button secondary" to="/assistant"> {t("Talk it through")} </Link></div>}
    </>}
  </div>;
}


// ---- from features/complaint/RegisterComplaintPage --------------------------------------------

export function RegisterComplaintPage() {
    const { language: uiLanguage, t } = useI18n();
    const { session } = useAuth();
    const navigate = useNavigate();
    const [packs, setPacks] = useState<DemoPackOut[]>([]);
    const [loadingPack, setLoadingPack] = useState<string | null>(null);
    const [title, setTitle] = useState("");
    const [language, setLanguage] = useState<Language | null>(null);
    const [account, setAccount] = useState("");
    const [reference, setReference] = useState("");
    const [error, setError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    useEffect(() => {
        api.listDemoPacks().then(setPacks).catch(() => setPacks([]));
    }, []);
    async function loadDemo(packId: string) {
        setLoadingPack(packId);
        setError(null);
        try {
            const snapshot = await api.loadDemoCase(packId);
            navigate(`/cases/${snapshot.case.id}/material`);
        }
        catch (e) {
            setError(e instanceof Error ? e.message : "Could not load the demo case");
        }
        finally {
            setLoadingPack(null);
        }
    }
    async function submit(event: FormEvent) {
        event.preventDefault();
        setSaving(true);
        setError(null);
        try {
            const snapshot = await api.createCase({
                title: title.trim() || account.trim().slice(0, 70) || t("My online payment report"),
                language: language ?? uiLanguage,
                complaintReference: reference.trim() || null,
                incidentAccount: account.trim() || null,
            });
            navigate(`/cases/${snapshot.case.id}/material`);
        }
        catch (e) {
            setError(e instanceof Error ? e.message : "Could not create the case");
        }
        finally {
            setSaving(false);
        }
    }
    const extra = packs.filter((p) => !p.packId.startsWith("demo-"));
    return (<div className="citizen-flow">
      <JourneySteps current={1}/>
      <h1> {t("Tell us what happened")} </h1>
      <p className="muted"> {t("Write a few words, or continue to record your voice. You do not need to know every detail.")} </p>

      <details className="card" open={session?.publicFixturesOnly || undefined}>
        <summary> {t("Demo tools")} </summary>
        <h2 id="demo-cases"> {t("Load a fictional demo case")} </h2>
        <p className="small muted"> {t("Try it with a made-up case. Each load creates your own copy; Demo B also lets you add refund and official-letter updates one at a time.")} </p>
        <div className="actions">
          {packs
            .filter((p) => p.packId.startsWith("demo-"))
            .map((p) => (<button key={p.packId} type="button" disabled={loadingPack !== null} onClick={() => void loadDemo(p.packId)}>{loadingPack === p.packId ? t("Loading…") : p.title}</button>))}
          {extra.length > 0 && (<details>
              <summary className="small"> {t("More development packs (")} {extra.length})</summary>
              <div className="actions">
                {extra.map((p) => (<button key={p.packId} type="button" className="secondary" disabled={loadingPack !== null} onClick={() => void loadDemo(p.packId)} title={p.tags.join(", ")}>
                    {p.packId}: {p.title}
                  </button>))}
              </div>
            </details>)}
        </div>
        {packs.length === 0 && <p className="muted small"> {t("No demo packs available.")} </p>}
      </details>

      {!session?.publicFixturesOnly && <form onSubmit={submit} className="card" aria-label={t("Your account")}>
        <label> {t("What happened? (optional now; you can also record audio later)")} <textarea value={account} onChange={(e) => setAccount(e.target.value)} rows={4} maxLength={10000} placeholder={t("For example: I paid for clothes online, but the order never arrived.")}/>
        </label>
        <details className="optional-details"><summary> {t("Add a title or complaint number (optional)")} </summary>
        <label> {t("Short case title")} <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} placeholder={t("e.g. Clothes order never delivered")}/></label>
        <label> {t("Language of your account")} <select value={language ?? uiLanguage} onChange={(e) => setLanguage(e.target.value as Language)}><option value="en"> {t("English")} </option><option value="hi"> {t("Hindi")} </option><option value="hinglish"> {t("Hinglish")} </option></select></label>
        <label> {t("Existing complaint reference (optional)")} <input value={reference} onChange={(e) => setReference(e.target.value)} maxLength={100} placeholder={t("Acknowledgement number, if you already reported officially")}/>
        </label>
        </details>
        <p className="muted small"> {t("Do not enter Aadhaar, PAN, passwords or OTPs. This demo processes fictional data only.")} </p>
        {error && (<ErrorNotice message={error}/>)}
        <button type="submit" disabled={saving}> {t(saving ? "Creating…" : "Continue →")} </button>
      </form>}
    </div>);
}


// ---- from features/complaint/TrackComplaintsPage ----------------------------------------------

export function TrackComplaintsPage() {
    const { t } = useI18n();
    const [cases, setCases] = useState<CaseSummary[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    useEffect(() => {
        api.listCases().then(setCases).catch(() => { setCases([]); setError("Could not connect. Check your connection and try again."); });
    }, []);
    async function remove(caseId: string) {
        if (!window.confirm(t("Delete this complaint and every stored file, revision and export? This cannot be undone.")))
            return;
        try {
            await api.deleteCase(caseId);
            setCases((prev) => (prev ?? []).filter((c) => c.id !== caseId));
        }
        catch (e) {
            setError(e instanceof Error ? e.message : "Could not delete the complaint");
        }
    }
    return (<>
      <h1> {t("Track your complaints")} </h1>
      {error && (<ErrorNotice message={error}/>)}

      <section className="card">
        {cases === null && <p className="muted"> {t("Loading\u2026")} </p>}
        {cases !== null && cases.length === 0 && (<p className="muted"> {t("No complaints yet.")} <Link to="/register"> {t("Register a complaint \u2192")} </Link>
          </p>)}
        <ul className="plain">
          {(cases ?? []).map((c) => {
            const step = nextStep(c);
            return (<li key={c.id} className="caserow">
                <span className="caseref">{caseReference(c.id)}</span>
                <Link to={`/cases/${c.id}`}>{c.title}</Link>
                <span className={`badge badge-case-${step.tone}`}> {t(step.text)} </span>
                {c.submittedAt && (<span className="muted small"> {t("submitted")} {new Date(c.submittedAt).toLocaleDateString()}</span>)}
                {c.reviewState !== "private" && <Link className="small" to={`/cases/${c.id}/receipt`}> {t("Receipt")} </Link>}
                <button type="button" className="linklike danger" onClick={() => void remove(c.id)}> {t("Delete")} </button>
              </li>);
        })}
        </ul>
      </section>

      <section className="card">
        <h2> {t("Official reporting")} </h2>
        <p> {t("Financial cyber fraud in India should be reported immediately by calling")} <strong>1930</strong> {t("or at")} {" "}
          <a href="https://cybercrime.gov.in/" target="_blank" rel="noreferrer noopener"> {t("cybercrime.gov.in")} </a> {t(". Preparing a packet here must not delay that. Sending a complaint to an officer inside this demo is not filing an official complaint.")} </p>
        <details>
          <summary> {t("What this demo stores")} </summary>
          <p className="small"> {t("Uploaded files, extracted proposals, your corrections and exports, in a private folder on the server. While you are signed in these are kept with your account rather than expiring with the session; delete a complaint at any time and its originals, derived files, revisions, exports and processing state go with it.")} </p>
        </details>
      </section>
    </>);
}

