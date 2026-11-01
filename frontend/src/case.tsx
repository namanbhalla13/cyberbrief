/** The complaint itself: checking what was read, answering, closing, and the receipt. */
import { ProgressPanel } from "./progress";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, TERMINAL_RUN_STATES, api, newIdempotencyKey, type CaseSnapshot, type ExportOut, type PaymentOut, type ResolutionOut, type ReviewAction } from "./api";
import { QuestionsPanel, RecoveryPanel, StatusPanel } from "./review";
import { AiModeBadge, ErrorNotice, JourneySteps, MoneyText, caseReference, factDisplay, questionText, useCase, useI18n } from "./ui";


// ---- from features/case/CheckSummary ----------------------------------------------------------

/**
 * The plain-language version of the check step.
 *
 * It still confirms every field individually — the promise that nothing counts until a person
 * confirms it is kept exactly. What changes is that the person sees one readable line per
 * payment instead of nine labelled form rows with four buttons each.
 */
export function CheckSummary({ caseId, snapshot, onSnapshot, onError, }: {
    caseId: string;
    snapshot: CaseSnapshot;
    onSnapshot: (s: CaseSnapshot) => void;
    onError: (m: string | null) => void;
}) {
    const { t } = useI18n();
    const [busy, setBusy] = useState(false);
    const canonical = snapshot.payments.filter((p) => !p.mergedIntoPaymentId && !p.excludedReason);
    const duplicates = snapshot.duplicateCandidates;
    function pending(p: PaymentOut): ReviewAction[] {
        const actions: ReviewAction[] = Object.values(p.fields)
            .filter((f) => f.value && !["user_confirmed", "reviewer_checked", "excluded"].includes(f.reviewState))
            .map((f) => ({ type: "confirm" as const, targetType: "fact" as const, targetId: f.factId }));
        if (!["user_confirmed", "reviewer_checked"].includes(p.identityReviewState)) {
            actions.push({ type: "confirm", targetType: "payment", targetId: p.id });
        }
        return actions;
    }
    /**
     * One batch per payment, sequentially.
     *
     * The server takes at most 50 actions per batch and rejects a stale `expectedRevision` with
     * a 409, so each batch has to use the revision returned by the previous one. Firing these in
     * parallel would make all but the first fail.
     */
    async function confirmAll() {
        setBusy(true);
        onError(null);
        let current = snapshot;
        try {
            for (const payment of canonical) {
                const actions = pending(payment);
                if (actions.length === 0)
                    continue;
                for (let i = 0; i < actions.length; i += 50) {
                    current = await api.review(caseId, current.case.currentRevision, actions.slice(i, i + 50));
                }
                onSnapshot(current);
            }
            // the amount the citizen stated is a case-level fact, outside any payment batch
            const caseFacts = Object.values(current.caseFacts).filter((f) => f.value && !["user_confirmed", "reviewer_checked", "excluded"].includes(f.reviewState));
            if (caseFacts.length > 0) {
                current = await api.review(caseId, current.case.currentRevision, caseFacts.map((f) => ({ type: "confirm" as const, targetType: "fact" as const, targetId: f.factId })));
                onSnapshot(current);
            }
        }
        catch (e) {
            onError(e instanceof Error ? e.message : "Could not save your confirmation");
        }
        finally {
            setBusy(false);
        }
    }
    async function resolveDuplicate(keepId: string, otherId: string, same: boolean) {
        setBusy(true);
        onError(null);
        try {
            onSnapshot(await api.review(caseId, snapshot.case.currentRevision, [
                same
                    ? { type: "merge", targetType: "payment", targetId: otherId, otherPaymentId: keepId }
                    : { type: "distinct", targetType: "payment", targetId: otherId, otherPaymentId: keepId },
            ]));
        }
        catch (e) {
            onError(e instanceof Error ? e.message : "Could not save that");
        }
        finally {
            setBusy(false);
        }
    }
    const label = (id: string) => snapshot.payments.find((p) => p.id === id)?.label ?? "a payment";
    return (<section className="card" aria-labelledby="check-summary">
      <h2 id="check-summary"> {t("What we read from your receipts")} </h2>
      <p className="small muted"> {t("Nothing counts towards your total until you say it is right. Check the lines below.")} </p>

      {duplicates.length > 0 && (<div className="warn" style={{ marginBottom: 14 }}>
          <p style={{ margin: "0 0 8px" }}>
            <strong>
              {label(duplicates[0].paymentIds[0])} {t("and")} {label(duplicates[0].paymentIds[1])}
            </strong>{" "} {t("show the same reference. Are they the same payment, counted twice?")} </p>
          <div className="actions">
            <button type="button" disabled={busy} onClick={() => void resolveDuplicate(duplicates[0].paymentIds[0], duplicates[0].paymentIds[1], true)}> {t("Yes, the same payment")} </button>
            <button type="button" className="secondary" disabled={busy} onClick={() => void resolveDuplicate(duplicates[0].paymentIds[0], duplicates[0].paymentIds[1], false)}> {t("No, two different payments")} </button>
          </div>
        </div>)}

      {canonical.length === 0 && <p className="muted"> {t("No payments have been read yet.")} </p>}

      {canonical.map((p) => {
            const amount = p.fields.amount;
            const date = p.fields.occurred_at;
            const ref = p.fields.reference;
            const status = p.fields.status;
            const unreadable = Object.values(p.fields).filter((f) => !f.value).length;
            return (<div key={p.id} className="readrow">
            <span className="amount">{amount ? factDisplay(amount) : t("Amount unreadable")}</span>
            <span>{date?.value ? factDisplay(date) : <span className="gap"> {t("date not readable")} </span>}</span>
            {ref?.value && <span className="mono small"> {t("ref")} {factDisplay(ref)}</span>}
            {status?.value && <span className="small muted">{t(factDisplay(status))}</span>}
            {p.fields.direction?.value && <span>{t(factDisplay(p.fields.direction))}</span>}
            {p.sourceIds.map((id) => <a key={id} href={api.sourceUrl(caseId, id)} target="_blank" rel="noreferrer noopener">{t("View original file")}</a>)}
            <details className="payment-check-details"><summary>{t("Proposed details")}</summary>
              <dl className="kv small">{Object.entries(p.fields).map(([kind, fact]) => <div key={kind}><dt>{t(({amount: "Amount", currency: "Currency", reference: "Transaction reference", reference_type: "Reference type", status: "Payment status", direction: "Direction", occurred_at: "Date", counterparty: "Paid to / received from", account_scope: "Your account (VPA)"} as Record<string,string>)[kind] ?? kind)}</dt><dd>{fact.value ? t(factDisplay(fact)) : t("Unreadable")}</dd></div>)}</dl>
            </details>
            {unreadable > 0 && <span className="gap small">{unreadable} {t("field(s) we could not read")} </span>}
          </div>);
        })}

      {snapshot.totals.citizenReportedMinor && (<p className="small muted" style={{ marginTop: 12 }}> {t("You told us you lost")} <MoneyText minor={snapshot.totals.citizenReportedMinor}/> {t(". That stays as you said it, separate from what the receipts show.")} </p>)}

      <div className="actions">
        <button type="button" disabled={busy} onClick={() => void confirmAll()}> {t(busy ? "Saving…" : "Yes, that's all right")} </button>
        <Link className="button secondary" to={`/cases/${caseId}/review`}> {t("Something's wrong \u2192")} </Link>
      </div>
      <p className="small muted"> {t("\u201CSomething's wrong\u201D opens the full editor, where you can correct any single field, leave it unresolved, or exclude it.")} </p>
    </section>);
}


// ---- from features/case/AnswerStep ------------------------------------------------------------

/** Answers the citizen has typed that have not been read yet. */
function unreadAnswers(snapshot: CaseSnapshot): number {
    return snapshot.sources.filter((s) => s.kind === "user_answer" && ["stored", "failed"].includes(s.status)).length;
}
/**
 * Step 3: everything the citizen is asked before submitting, in one place.
 *
 * Two kinds of question live here and they are genuinely different. `CheckSummary` asks them to
 * confirm what was read out of their own receipts — the duplicate prompt and the per-payment
 * read-back. `QuestionsPanel` asks about gaps the record itself has: an unreadable amount, a
 * conflicting status, a date given as "yesterday".
 *
 * Answering a question stores it as a new source but does not extract anything from it. Rather
 * than sending the citizen back to the evidence screen to start a run by hand — which is what
 * this app used to do — the one button here does it for them.
 */
export function AnswerStep({ caseId, snapshot, onSnapshot, onError, openCount, }: {
    caseId: string;
    snapshot: CaseSnapshot;
    onSnapshot: (s: CaseSnapshot) => void;
    onError: (m: string | null) => void;
    openCount: number;
}) {
    const { t } = useI18n();
    const [busy, setBusy] = useState(false);
    const openQuestions = snapshot.questions.filter((q) => q.state === "open");
    const unread = unreadAnswers(snapshot);
    /** Read the answers just given, so the questions they resolve disappear. */
    async function useAnswers() {
        setBusy(true);
        onError(null);
        try {
            await api.createRun(caseId, { expectedRevision: snapshot.case.currentRevision }, newIdempotencyKey());
            // picking up activeRunId hands polling to the case page, which already does it
            onSnapshot(await api.getCase(caseId));
        }
        catch (e) {
            onError(e instanceof ApiError ? `${e.message} ${e.nextStep ?? ""}`.trim() : "Could not read your answers");
        }
        finally {
            setBusy(false);
        }
    }
    return (<>
      {openCount > 0 && (<CheckSummary caseId={caseId} snapshot={snapshot} onSnapshot={onSnapshot} onError={onError}/>)}

      {(openQuestions.length > 0 || unread > 0) && (<section className="card" aria-labelledby="questions">
          <h2 id="questions"> {t("A few questions")} </h2>
          <p className="small muted"> {t("These come from gaps in what we could read, not from guesswork. Answer what you can and skip the rest \u2014 you can submit either way.")} </p>
          <QuestionsPanel caseId={caseId} snapshot={snapshot} onSnapshot={onSnapshot} onError={onError}/>
          {unread > 0 && (<div className="actions">
              <button type="button" disabled={busy} onClick={() => void useAnswers()}>{busy ? t("Reading…") : t("Use my answers")}</button>
            </div>)}
        </section>)}
    </>);
}


// ---- from features/case/CloseRequest ----------------------------------------------------------

/** The most recent summary the citizen prepared as a closure request, if any. */
export function latestCloseRequest(snapshot: CaseSnapshot): ResolutionOut | null {
    return snapshot.resolutions.find((r) => r.requestNote) ?? null;
}
/**
 * Asking for this CyberBrief record to be closed.
 *
 * Only the asking lives here. The officer's written answer is shown by the case page itself,
 * above the fold, because an answer the citizen has to open a drawer to find is not an answer.
 *
 * "Closed" here means CyberBrief stops tracking the record. It does not close, withdraw or
 * affect an official complaint, and the text next to the button says exactly that, because a
 * person giving up on a loss is the last person who should be left with a wrong impression.
 */
export function CloseRequest({ caseId, snapshot, onSnapshot, onError, }: {
    caseId: string;
    snapshot: CaseSnapshot;
    onSnapshot: (s: CaseSnapshot) => void;
    onError: (m: string | null) => void;
}) {
    const { t } = useI18n();
    const [reason, setReason] = useState("");
    const [busy, setBusy] = useState(false);
    const request = latestCloseRequest(snapshot);
    async function send() {
        setBusy(true);
        onError(null);
        try {
            onSnapshot(await api.prepareResolution(caseId, true, snapshot.case.currentRevision, reason.trim()));
            setReason("");
        }
        catch (e) {
            onError(e instanceof ApiError ? `${e.message} ${e.nextStep ?? ""}`.trim() : "Could not send your request");
        }
        finally {
            setBusy(false);
        }
    }
    // waiting on an answer: show what was asked and offer no second request
    if (request && !request.reviewOutcome) {
        return (<>
        <div className="saidby">
          <span className="who"> {t("You asked to close this")} </span>
          {request.requestNote}
        </div>
        <p className="small muted"> {t("Waiting for an officer to answer. You can still add more details meanwhile.")} </p>
      </>);
    }
    // an answered request leaves the form available again: an officer who asked for more
    // information is expecting the citizen to come back, not to be shut out.
    return (<>
      <label> {t("Why should this be closed?")} <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} maxLength={1000} placeholder={t("For example: I got my money back from the seller. Or: I no longer want to pursue this.")} aria-label={t("Reason for closing")}/>
      </label>
      <div className="actions">
        <button type="button" disabled={busy || !reason.trim()} onClick={() => void send()}> {t(busy ? "Sending…" : "Request to close")} </button>
      </div>
      <p className="small muted"> {t("An officer reads your reason and answers. Closing ends CyberBrief's tracking of this record only \u2014 it does not close, withdraw or affect an official complaint.")} </p>
    </>);
}


// ---- from features/case/ReceiptPage -----------------------------------------------------------

const SOURCE_NAMES: Record<string, [
    string,
    string
]> = {
    image: ["screenshot or receipt", "screenshots and receipts"],
    supplied_official_update: ["official update you supplied", "official updates you supplied"],
    audio: ["recording", "recordings"],
    transcript: ["transcript", "transcripts"],
    typed_account: ["written account", "written accounts"],
    user_answer: ["answer you typed", "answers you typed"],
};

/**
 * The acknowledgement a citizen keeps after submitting.
 *
 * Everything on it is read back from the record rather than asserted: what was included, what the
 * citizen said they lost, what the receipts confirmed, and — the part that makes a skippable
 * question stage honest — what is still unconfirmed.
 *
 * The digest comes from a real export pinned to this revision, so the receipt can be reproduced
 * rather than merely looking official.
 */
export function ReceiptPage() {
    const { t, language } = useI18n();
    const { caseId } = useParams();
    const { snapshot, error } = useCase(caseId);
    const [stamp, setStamp] = useState<ExportOut | null>(null);
    const revision = snapshot?.case.currentRevision;
    useEffect(() => {
        if (!caseId || revision === undefined)
            return;
        // a deterministic key: reopening the receipt at the same revision returns the same record
        api.createExport(caseId, "html", revision, `receipt-${caseId}-${revision}`).then(setStamp).catch(() => setStamp(null));
    }, [caseId, revision]);
    if (!caseId)
        return null;
    if (!snapshot)
        return <p className="muted">{error ?? t("Loading…")}</p>;
    const { case: c, totals, questions, payments, sources } = snapshot;
    const counts = new Map<string, number>();
    for (const s of sources)
        counts.set(s.kind, (counts.get(s.kind) ?? 0) + 1);
    const confirmed = payments.filter((p) => !p.mergedIntoPaymentId && !p.excludedReason);
    const unconfirmed = questions.filter((q) => q.state === "open");
    if (c.reviewState === "private") {
        return (<>
        <h1> {t("Nothing submitted yet")} </h1>
        <p className="muted"> {t("A receipt is issued when you submit a complaint.")} <Link to={`/cases/${caseId}`}> {t("Go back to your complaint")} </Link>.
        </p>
      </>);
    }
    return (<>
      <article className="receipt">
        <div className="receipt-head">
          <div>
            <p className="small muted" style={{ margin: 0 }}> {t("Acknowledgement")} </p>
            <span className="receipt-ref">{caseReference(caseId)}</span>
          </div>
          <div className="small muted">{c.submittedAt ? new Date(c.submittedAt).toLocaleString() : t("submission time not recorded")}</div>
        </div>

        <p>
          <strong>{c.title}</strong>
        </p>
        <p className="small muted"> {t("This is a CyberBrief reference, not an official complaint number. Submitting here does not file an official complaint and does not change any official complaint status. To report officially, call 1930 or use cybercrime.gov.in.")} </p>

        <h2> {t("What you sent")} </h2>
        <ul className="plain small">
          {[...counts.entries()]
            .filter(([kind]) => kind in SOURCE_NAMES)
            .map(([kind, n]) => (<li key={kind}>{n} {t(SOURCE_NAMES[kind][n === 1 ? 0 : 1])}</li>))}
          <li>{confirmed.length} {t("payments read from your evidence")}</li>
        </ul>

        <h2> {t("Amounts")} </h2>
        <dl className="kv">
          <dt> {t("You told us you lost")} </dt>
          <dd>
            <MoneyText minor={totals.citizenReportedMinor}/>
          </dd>
          <dt> {t("Confirmed by you from the receipts")} </dt>
          <dd>
            <MoneyText minor={totals.reviewedDebitMinor}/>
          </dd>
        </dl>
        <p className="small muted"> {t("These are two different things and are never added together. A zero confirmed subtotal means nothing has been checked yet, not that no money was lost. Nothing here has been verified against a bank.")} </p>

        <h2> {t("Not yet confirmed")} </h2>
        {unconfirmed.length === 0 ? (<p className="small"> {t("Nothing outstanding. Every question was answered or did not apply.")} </p>) : (<>
            <p className="small"> {t("You submitted with")} {unconfirmed.length} {t("question")} {t("still open. They are recorded as unanswered, not as agreed:")} </p>
            <ul className="plain small">
              {unconfirmed.map((q) => (<li key={q.id}>• {questionText(q, language)}</li>))}
            </ul>
          </>)}

        <h2> {t("Reference")} </h2>
        <p className="small muted"> {t("Revision")} {c.currentRevision}
          {stamp ? (<>
              {" "} {t("\u00B7 content digest")} <span className="mono">{stamp.contentDigest.slice(0, 16)}…</span> ·{" "}
              <a href={stamp.downloadPath} target="_blank" rel="noreferrer noopener"> {t("full report")} </a>
            </>) : null}
        </p>
      </article>

      <div className="actions">
        <button type="button" onClick={() => window.print()}> {t("Print this receipt")} </button>
        <Link className="button secondary" to={`/cases/${caseId}`}> {t("Back to my complaint")} </Link>
        <Link className="button secondary" to="/track"> {t("Track my complaints")} </Link>
      </div>
    </>);
}


// ---- from features/case/CasePage --------------------------------------------------------------

const STATE_LABELS: Record<string, string> = {
    private: "Not sent yet",
    submitted: "With an officer",
    returned: "More information asked for",
    reviewed: "Packet reviewed",
    closed: "Closed",
};
function openCount(snapshot: CaseSnapshot): number {
    const t = snapshot.totals;
    return t.unreviewedIds.length + t.unresolvedIds.length + t.duplicateCandidateIds.length + t.conflictIds.length;
}
export function CasePage() {
    const { t } = useI18n();
    const { caseId } = useParams();
    const navigate = useNavigate();
    const { snapshot, error, setSnapshot, setError, reload } = useCase(caseId);
    const [exports, setExports] = useState<ExportOut[]>([]);
    const [busy, setBusy] = useState(false);
    const pollTimer = useRef<number | null>(null);
    // a run started on the evidence screen can still be going when this page opens
    useEffect(() => {
        const active = snapshot?.case.activeRunId;
        if (!active)
            return;
        const tick = async () => {
            try {
                const run = await api.getRun(caseId!, active);
                if (TERMINAL_RUN_STATES.has(run.status)) {
                    await reload();
                    return;
                }
            }
            catch {
                return;
            }
            pollTimer.current = window.setTimeout(tick, 1500);
        };
        void tick();
        return () => {
            if (pollTimer.current)
                window.clearTimeout(pollTimer.current);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [snapshot?.case.activeRunId]);
    if (!caseId)
        return null;
    if (!snapshot)
        return <p className="muted">{error ?? t("Loading…")}</p>;
    const { case: c, totals, recovery } = snapshot;
    const open = openCount(snapshot);
    const openQuestions = snapshot.questions.filter((q) => q.state === "open").length;
    const confirmed = snapshot.payments.filter((p) => !p.mergedIntoPaymentId && !p.excludedReason);
    const processing = c.packetStatus === "processing" || Boolean(c.activeRunId);
    const shared = c.reviewState !== "private";
    const closed = c.reviewState === "closed";
    const toCheck = open > 0 && !closed;
    const closeRequest = latestCloseRequest(snapshot);
    const steps = [
        { name: "Told us", done: true },
        { name: "Evidence added", done: c.sourceCount > 0 },
        { name: "Questions answered", done: shared || (open === 0 && openQuestions === 0 && c.sourceCount > 0) },
        { name: "Submitted", done: shared },
    ];
    const currentStep = steps.findIndex((s) => !s.done);
    async function makeExport(format: "html" | "json") {
        setError(null);
        try {
            const out = await api.createExport(caseId!, format, snapshot!.case.currentRevision, newIdempotencyKey());
            setExports((prev) => [out, ...prev]);
            window.open(out.downloadPath, "_blank", "noopener");
        }
        catch (e) {
            setError(e instanceof Error ? e.message : "Export failed");
        }
    }
    /** Submit, then hand the citizen their acknowledgement. */
    async function submitComplaint() {
        setBusy(true);
        setError(null);
        try {
            setSnapshot(await api.submitForReview(caseId!));
            navigate(`/cases/${caseId}/receipt`);
        }
        catch (e) {
            setError(e instanceof Error ? e.message : "Could not submit this complaint");
        }
        finally {
            setBusy(false);
        }
    }
    return (<>
      <h1>{c.title}</h1>
      <p className="case-meta muted">
        <span className="caseref">{caseReference(caseId)}</span>
        <span className={`badge badge-case-${c.reviewState}`}> {t(STATE_LABELS[c.reviewState] ?? c.reviewState)} </span>
        {snapshot.aiModesUsed.map((m) => (<AiModeBadge key={m} mode={m}/>))}
      </p>
      <p className="small muted" style={{ marginTop: -8 }}> {t("This is your CyberBrief reference, not an official complaint number.")} </p>
      <ProgressPanel caseId={caseId} snapshot={snapshot} onChanged={() => void reload()}/>

      {!shared && <JourneySteps current={3}/>}
      {shared && <ol className="tracker">
        {steps.map((s, i) => (<li key={s.name} className={s.done ? "done" : i === currentStep ? "current" : ""}> {t(s.name)} </li>))}
      </ol>}

      {error && (<ErrorNotice message={error}/>)}

      {/* one thing to do next */}
      <section className="nextstep">
        {c.sourceCount === 0 ? (<>
            <h2> {t("Add your evidence")} </h2>
            <p> {t("Upload the payment screenshots and any messages from the seller, or just tell us what happened.")} </p>
            <div className="actions">
              <Link className="button" to={`/cases/${caseId}/material`}> {t("Add evidence")} </Link>
            </div>
          </>) : processing ? (<>
            <h2> {t("Reading your evidence\u2026")} </h2>
            <p> {t("This takes a few seconds. The page updates on its own.")} </p>
          </>) : closed ? (<>
            <h2> {t("This complaint is closed")} </h2>
            <p> {t("CyberBrief has stopped tracking it. Your acknowledgement stays available, and an official complaint, if you filed one, is unaffected.")} </p>
            <div className="actions">
              <Link className="button" to={`/cases/${caseId}/receipt`}> {t("View receipt")} </Link>
            </div>
          </>) : !shared ? (<>
            <h2> {t(open + openQuestions > 0 ? "Check your details" : "Ready to save your report")} </h2>
            <p> {t(open + openQuestions > 0 ? "We read these from your evidence. Nothing is counted until you confirm it, and you can submit without answering everything." : "Your details are checked. Save your report and send it to the demo reviewer.")} </p>
            <div className="actions">
              {open + openQuestions > 0 ? <a className="button" href="#check-details"> {t("Check details below \u2193")} </a> : <button type="button" disabled={busy} onClick={() => void submitComplaint()}> {t(busy ? "Submitting…" : "Save and send for demo review")} </button>}
              <button type="button" className="secondary" onClick={() => void makeExport("html")}> {t("Printable report")} </button>
            </div>
            {open + openQuestions > 0 && (<p className="small">
                {open + openQuestions} {t("item(s) will be recorded as not yet confirmed on your receipt.")} </p>)}
            <p className="small"> {t("Submitting happens inside this demo only. It does not file an official complaint and does not change your official complaint status.")} </p>
          </>) : (<>
            <h2> {t(STATE_LABELS[c.reviewState] ?? c.reviewState)} </h2>
            <p> {t(c.reviewState === "returned" ? "An officer asked for more information. Add it below and it will be seen." : "You can still add more details, or ask for this complaint to be closed.")} </p>
            <div className="actions">
              <Link className="button" to={`/cases/${caseId}/receipt`}> {t("View receipt")} </Link>
              <Link className="button secondary" to={`/cases/${caseId}/material`}> {t("Add more details")} </Link>
            </div>
          </>)}
      </section>

      {/* an officer's written answer belongs next to the status, not inside a drawer */}
      {closeRequest?.reviewOutcome === "more_information_requested" && closeRequest.reviewNote && (<div className="saidby answer">
          <span className="who"> {t("The officer asked")} </span>
          {closeRequest.reviewNote}
        </div>)}

      {/* the money, in three plain numbers */}
      <div className="stats">
        <div className="stat">
          <div className="label"> {t("You told us you lost")} </div>
          <div className="value">
            <MoneyText minor={totals.citizenReportedMinor}/>
          </div>
          <div className="hint"> {t("as you stated it")} </div>
        </div>
        <div className="stat">
          <div className="label"> {t("Confirmed from receipts")} </div>
          <div className="value">
            {totals.unreviewedIds.length > 0 && totals.reviewedDebitIds.length === 0 ? t("Not reviewed yet") : <MoneyText minor={totals.reviewedDebitMinor}/>}
          </div>
          <div className="hint">{totals.reviewedDebitIds.length} {t("payment(s) you checked")} </div>
        </div>
        {/* a closed complaint has nothing left to check, so it must not point at a section
            that no longer renders */}
        <div className={`stat${toCheck ? " attention" : ""}`}>
          <div className="label"> {t(toCheck ? "Still to check" : "Still outstanding")} </div>
          <div className="value">{toCheck ? open : <MoneyText minor={recovery.outstandingMinor}/>}</div>
          <div className="hint"> {t(toCheck ? "confirm these below" : "after refunds you confirmed")} </div>
        </div>
      </div>
      <p className="small muted" style={{ marginTop: -8 }}> {t("A zero confirmed subtotal can mean \u201Cnothing checked yet\u201D. It never means \u201Cno money lost\u201D.")} </p>

      <div id="check-details"/>
      {!processing && !closed && (<AnswerStep caseId={caseId} snapshot={snapshot} onSnapshot={setSnapshot} onError={setError} openCount={open}/>)}
      {!processing && !closed && !shared && open + openQuestions > 0 && <section className="card">
        <h2> {t("Save when you are ready")} </h2><p> {t("You can leave details unanswered. They will be marked as not confirmed.")} </p>
        <button type="button" disabled={busy} onClick={() => void submitComplaint()}> {t(busy ? "Submitting…" : "Save and send for demo review")} </button>
        <p className="small muted"> {t("This sends your report to a demo reviewer, not the police.")} </p>
      </section>}

      {open === 0 && confirmed.length > 0 && (<section className="card">
          <h2> {t("Your payments")} </h2>
          {confirmed.map((p) => (<div key={p.id} className="readrow">
              <span className="amount">{p.fields.amount ? factDisplay(p.fields.amount) : "—"}</span>
              <span>{p.fields.occurred_at?.value ? factDisplay(p.fields.occurred_at) : t("date not readable")}</span>
              {p.fields.reference?.value && <span className="mono small"> {t("ref")} {factDisplay(p.fields.reference)}</span>}
              {p.fields.status?.value && <span className="small muted">{t(factDisplay(p.fields.status))}</span>}
            </div>))}
          <p className="small muted"> {t("You confirmed these. To change one, open")} <Link to={`/cases/${caseId}/review`}> {t("the full editor")} </Link>.
          </p>
        </section>)}

      <nav className="quietlinks" aria-label={t("More for this complaint")}>
        {/* A closed record takes no more evidence. Recording an official update still makes
            sense: that track is independent and does not stop when CyberBrief stops tracking. */}
        {!closed && <Link to={`/cases/${caseId}/material`}> {t("Add more details")} </Link>}
        <a href="#official-update"> {t("Add an update you received")} </a>
        <a href="#progress"> {t("See progress")} </a>
        <Link to={`/assistant?case=${caseId}`}> {t("Ask the assistant about this complaint")} </Link>
      </nav>

      {/* Folded away, but still a named ARIA region once opened, so assistive tech and the
            browser tests can address these sections the same way they always could. */}
      {shared && !closed && (<details className="card" id="close-request">
          <summary> {t("Request to close this complaint")} </summary>
          <section aria-label={t("Closure request")}>
            <CloseRequest caseId={caseId} snapshot={snapshot} onSnapshot={setSnapshot} onError={setError}/>
          </section>
        </details>)}

      <details className="card" id="money-back">
        <summary> {t("Did you get any money back?")} </summary>
        <section aria-label={t("Recovery updates")}>
          <RecoveryPanel caseId={caseId} snapshot={snapshot} onSnapshot={setSnapshot} onError={setError}/>
        </section>
      </details>

      <details className="card" id="official-update">
        <summary> {t("Add an update you received")} </summary>
        <section aria-label={t("Latest recorded official complaint update")}>
          <StatusPanel caseId={caseId} snapshot={snapshot} onSnapshot={setSnapshot} onError={setError}/>
        </section>
      </details>

      <details className="card" id="progress">
        <summary> {t("Progress and history")} </summary>
        <ol className="small">
          {snapshot.revisions.map((r) => (<li key={r.revision}>
              {new Date(r.createdAt).toLocaleString()} — {r.summary}
            </li>))}
        </ol>
        {/* The printable report and the submit action live in the next-step card above, so they
            appear exactly once and at the moment they make sense. */}
        <div className="actions">
          <button type="button" className="secondary" onClick={() => void makeExport("json")}> {t("JSON snapshot")} </button>
        </div>
        {exports.length > 0 && (<ul className="plain small">
            {exports.map((x) => (<li key={x.id}>
                <a href={x.downloadPath} target="_blank" rel="noreferrer noopener">
                  {x.format.toUpperCase()} {t("\u00B7 revision")} {x.revision}
                </a>{" "} {t("\u00B7 digest")} <span className="mono">{x.contentDigest.slice(0, 12)}…</span> · {x.aiMode}
              </li>))}
          </ul>)}
      </details>
    </>);
}
