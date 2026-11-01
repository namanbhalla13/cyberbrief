/** The detailed editor and the panels the complaint page folds away. */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useOutletContext, useParams } from "react-router-dom";
import { ApiError, CRITICAL_FIELDS, FIELD_ORDER, api, newIdempotencyKey, type CaseSnapshot, type ChangeSummary, type FactOut, type PaymentOut, type QuestionOut, type RefundOut, type ReviewAction, type SessionOut, type SourceOut } from "./api";
import { CategoryBadge, ErrorNotice, IssueBadge, MoneyText, RECOVERY_LABELS, ReviewStateBadge, STATUS_CATEGORY_LABELS, effectiveDateText, factDisplay, formatInr, questionText, rupeesToMinor, useCase, useI18n } from "./ui";


// ---- from features/overview/QuestionsPanel ----------------------------------------------------

const KIND_LABELS: Record<string, string> = {
    duplicate_candidate: "Possible duplicate", amount_conflict: "Amount conflict", status_conflict: "Status conflict",
    missing_field: "Unreadable field", pending_status: "Pending payment", reported_vs_reviewed_gap: "Reported vs reviewed",
    relative_date: "Date wording", unallocated_credit: "Incoming credit",
};
export function QuestionsPanel({ caseId, snapshot, onSnapshot, onError }: {
    caseId: string;
    snapshot: CaseSnapshot;
    onSnapshot: (s: CaseSnapshot) => void;
    onError: (m: string | null) => void;
}) {
    const { t, language } = useI18n();
    const open = snapshot.questions.filter((q) => q.state === "open");
    const shown = open.filter((q) => q.shown);
    const answered = snapshot.questions.filter((q) => q.state === "answered");
    const labels = new Map(snapshot.payments.map((p) => [p.id, p.label]));
    async function act(fn: () => Promise<CaseSnapshot>) {
        try {
            onSnapshot(await fn());
            onError(null);
        }
        catch (e) {
            if (e instanceof ApiError && e.status === 409) {
                onSnapshot(await api.getCase(caseId));
                onError(`${e.message} ${e.nextStep ?? ""}`.trim());
            }
            else {
                onError(e instanceof Error ? e.message : "Action failed");
            }
        }
    }
    return (<>
      {open.length === 0 && answered.length === 0 && <p className="muted"> {t("Nothing open. Every proposed detail has been reviewed and no gaps remain.")} </p>}
      {shown.map((q) => (<QuestionRow key={q.id} question={q} labels={labels} onAnswer={(text) => act(() => api.answerQuestion(caseId, q.id, text, snapshot.case.currentRevision))} onDismiss={(reason) => act(() => api.dismissQuestion(caseId, q.id, reason, snapshot.case.currentRevision))}/>))}
      {open.length > shown.length && <p className="small muted">{open.length - shown.length} {t("more question(s) will appear as these are answered (at most five at a time).")} </p>}
      {answered.length > 0 && (<>
          <h3> {t("Answered")} </h3>
          <ul className="plain small">
            {answered.map((q) => <li key={q.id}>{questionText(q, language)} <span className="muted"> {t("\u2014 answered.")} </span></li>)}
          </ul>
        </>)}
      <p className="small muted"> {t("Questions are derived from actual gaps and conflicts in the reviewed record, never invented. Answers are stored as new sources with their own provenance.")} </p>
    </>);
}
function QuestionRow({ question, labels, onAnswer, onDismiss }: {
    question: QuestionOut;
    labels: Map<string, string>;
    onAnswer: (text: string) => Promise<void>;
    onDismiss: (reason: string) => Promise<void>;
}) {
    const { t, language } = useI18n();
    const [text, setText] = useState("");
    const [dismissing, setDismissing] = useState(false);
    const [reason, setReason] = useState("");
    const related = question.relatedPaymentIds.map((id) => labels.get(id) ?? id.slice(0, 8)).join(", ");
    return (<div className="question">
      <p><span className="badge"> {t(KIND_LABELS[question.kind] ?? question.kind)} </span>{related && <span className="muted small"> {related}</span>}<br />{questionText(question, language)}</p>
      {dismissing ? (<span className="inline-form">
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("Why does this not apply?")} aria-label={t("Dismissal reason")}/>
          <button type="button" disabled={!reason.trim()} onClick={() => void onDismiss(reason.trim())}> {t("Dismiss")} </button>
          <button type="button" className="secondary" onClick={() => setDismissing(false)}> {t("Cancel")} </button>
        </span>) : (<span className="inline-form">
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder={t("Your answer (becomes a new source)")} aria-label={t("Answer: {0}", {0: questionText(question, language)})}/>
          <button type="button" disabled={!text.trim()} onClick={() => { void onAnswer(text.trim()); setText(""); }}> {t("Answer")} </button>
          <button type="button" className="secondary" onClick={() => setDismissing(true)}> {t("Not applicable")} </button>
        </span>)}
    </div>);
}


// ---- from features/overview/RecoveryPanel -----------------------------------------------------

export function RecoveryPanel({ caseId, snapshot, onSnapshot, onError }: {
    caseId: string;
    snapshot: CaseSnapshot;
    onSnapshot: (s: CaseSnapshot) => void;
    onError: (m: string | null) => void;
}) {
    const { t } = useI18n();
    const { recovery, refunds, allocations, payments } = snapshot;
    const labels = new Map<string, string>([...payments.map((p) => [p.id, p.label] as const), ...refunds.map((r) => [r.id, r.label] as const)]);
    const visibleRefunds = refunds.filter((r) => !r.mergedIntoRefundId);
    // integer paise arithmetic with BigInt: remaining = amount - counted allocations
    const counted = (key: "refundId" | "paymentId", id: string) => allocations.filter((a) => a.counted && a[key] === id).reduce((sum, a) => sum + BigInt(a.amountMinor), 0n);
    const amountOf = (fields: Record<string, {
        value: Record<string, unknown> | null;
    }>) => BigInt(String(fields.amount?.value?.amountMinor ?? "0"));
    const allocatable = refunds
        .filter((r) => r.category === "eligible")
        .map((r) => ({ ...r, remaining: amountOf(r.fields) - counted("refundId", r.id) }))
        .filter((r) => r.remaining > 0n);
    const coverable = payments
        .filter((p) => p.category === "reviewed_debit")
        .map((p) => ({ ...p, remaining: amountOf(p.fields) - counted("paymentId", p.id) }))
        .filter((p) => p.remaining > 0n);
    const [refundId, setRefundId] = useState("");
    const [paymentId, setPaymentId] = useState("");
    const [amount, setAmount] = useState("");
    const [reason, setReason] = useState("");
    const [claimText, setClaimText] = useState("");
    const [claimAmount, setClaimAmount] = useState("");
    const [claimKind, setClaimKind] = useState("unknown");
    const [claimDate, setClaimDate] = useState("");
    const [statementText, setStatementText] = useState("");
    const [statementMode, setStatementMode] = useState<"none" | "some" | "unsure">("none");
    const [statementAmount, setStatementAmount] = useState("");
    async function act(fn: () => Promise<CaseSnapshot>) {
        try {
            onSnapshot(await fn());
            onError(null);
            return true;
        }
        catch (e) {
            if (e instanceof ApiError && e.status === 409)
                onSnapshot(await api.getCase(caseId));
            onError(e instanceof ApiError ? `${e.message} ${e.nextStep ?? ""}`.trim() : e instanceof Error ? e.message : "Action failed");
            return false;
        }
    }
    async function submitAllocation() {
        const minor = rupeesToMinor(amount);
        if (!refundId || !paymentId || !minor) {
            onError("Choose a reviewed refund, a reviewed debit, and a rupee amount.");
            return;
        }
        const ok = await act(() => api.allocate(caseId, { refundId, paymentId, amountMinor: minor, reason: reason || null, expectedRevision: snapshot.case.currentRevision }, newIdempotencyKey()));
        if (ok) {
            setAmount("");
            setReason("");
        }
    }
    async function submitClaim() {
        if (!claimText.trim())
            return;
        const ok = await act(() => api.reportRefund(caseId, { text: claimText.trim(), amountRaw: claimAmount || null, kind: claimKind, dateText: claimDate || null, expectedRevision: snapshot.case.currentRevision }, newIdempotencyKey()));
        if (ok) {
            setClaimText("");
            setClaimAmount("");
            setClaimDate("");
        }
    }
    async function submitStatement() {
        if (!statementText.trim())
            return;
        const minor = statementMode === "none" ? "0" : statementMode === "some" ? rupeesToMinor(statementAmount) : null;
        if (statementMode === "some" && !minor) {
            onError("Enter the amount you believe is still outstanding.");
            return;
        }
        const ok = await act(() => api.outstandingStatement(caseId, { text: statementText.trim(), outstandingMinor: minor, expectedRevision: snapshot.case.currentRevision }));
        if (ok)
            setStatementText("");
    }
    return (<>
      <p> {t("Recovery summary:")} <strong> {t(RECOVERY_LABELS[recovery.state] ?? recovery.state)} </strong></p>
      {recovery.reasons.length > 0 && <p className="small muted">{recovery.reasons.join("; ")}.</p>}
      <dl className="kv">
        <dt> {t("Reviewed debit baseline")} </dt><dd><MoneyText minor={recovery.reviewedDebitMinor}/></dd>
        <dt> {t("Reviewed matched refunds")} </dt><dd><MoneyText minor={recovery.matchedRefundMinor}/></dd>
        <dt> {t("Outstanding against reviewed payments")} </dt><dd><strong><MoneyText minor={recovery.outstandingMinor}/></strong></dd>
        <dt> {t("Reported refunds awaiting a reviewed record")} </dt><dd><MoneyText minor={recovery.claimedRefundMinor} fallback={t("none")}/></dd>
        <dt> {t("Reviewed refunds not yet allocated")} </dt><dd><MoneyText minor={recovery.unallocatedEligibleMinor} fallback={t("none")}/></dd>
        <dt> {t("Your statement on outstanding money")} </dt>
        <dd>{recovery.citizenOutstandingText ? <>{recovery.citizenOutstandingMinor === "0" ? t("Nothing outstanding") : recovery.citizenOutstandingMinor ? <MoneyText minor={recovery.citizenOutstandingMinor}/> : t("Unsure")} <span className="muted small">“{recovery.citizenOutstandingText}”</span></> : <span className="muted"> {t("not captured yet")} </span>}</dd>
      </dl>
      <p className="small muted"> {t("Reported and reviewed amounts are shown separately. A refund counts only after its record is reviewed, confirmed as related, and allocated to a reviewed debit. \u201CRefund initiated\u201D or \u201Camount frozen\u201D is not money received.")} </p>

      <h3> {t("Refund records")} </h3>
      {visibleRefunds.length === 0 ? <p className="muted small"> {t("None yet.")} </p> : (<ul className="plain small">
          {visibleRefunds.map((r) => (<li key={r.id} className="refundrow">
              <strong>{r.label}</strong> {r.fields.amount ? factDisplay(r.fields.amount) : t("amount unknown")} · {t(r.evidenceKind === "claim" ? "reported by you" : "credit record")} ·{" "}
              <CategoryBadge category={r.category} reason={r.categoryReason}/>
              {r.categoryReason && <span className="muted"> {r.categoryReason}</span>}
              {refunds.filter((m) => m.mergedIntoRefundId === r.id).map((m) => <span key={m.id} className="muted"> {t("\u00B7 includes")} {m.label}</span>)}
            </li>))}
        </ul>)}
      <p className="small"><Link to={`/cases/${caseId}/review`}> {t("Review refund records on the check-details screen")} </Link>.</p>

      <h3> {t("Allocations")} </h3>
      {allocations.length === 0 ? <p className="muted small"> {t("No refund has been matched to a payment yet.")} </p> : (<ul className="plain small">
          {allocations.map((a) => (<li key={a.id}>
              {labels.get(a.refundId)} → {labels.get(a.paymentId)}: <MoneyText minor={a.amountMinor}/> · {a.state.replace(/_/g, " ")}
              {a.counted ? <span className="muted"> {t("\u00B7 counted")} </span> : <span className="error"> {t("\u00B7 not counted")} </span>}
              {a.reason && <span className="muted"> · {a.reason}</span>}
            </li>))}
        </ul>)}
      {allocatable.length > 0 && coverable.length > 0 && (<div className="inline-form">
          <select value={refundId} onChange={(e) => setRefundId(e.target.value)} aria-label={t("Refund to allocate")}>
            <option value=""> {t("Refund\u2026")} </option>
            {allocatable.map((r) => <option key={r.id} value={r.id}>{r.label} · {formatInr(String(r.remaining))} {t("left to allocate")} </option>)}
          </select>
          <select value={paymentId} onChange={(e) => setPaymentId(e.target.value)} aria-label={t("Payment to cover")}>
            <option value=""> {t("Covers payment\u2026")} </option>
            {coverable.map((p) => <option key={p.id} value={p.id}>{p.label} · {formatInr(String(p.remaining))} {t("not yet covered")} </option>)}
          </select>
          <input value={amount} onChange={(e) => setAmount(e.target.value)} placeholder={t("Amount in \u20B9")} aria-label={t("Allocation amount in rupees")}/>
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("Note (optional)")} aria-label={t("Allocation note")}/>
          <button type="button" onClick={() => void submitAllocation()}> {t("Match refund to payment")} </button>
        </div>)}

      <h3> {t("Report money that came back")} </h3>
      <div className="inline-form">
        <input value={claimText} onChange={(e) => setClaimText(e.target.value)} placeholder={t("e.g. I received INR 5,000 back from the seller")} aria-label={t("Refund report")}/>
        <input value={claimAmount} onChange={(e) => setClaimAmount(e.target.value)} placeholder={t("Amount as shown, e.g. INR 5,000")} aria-label={t("Refund amount")}/>
        <select value={claimKind} onChange={(e) => setClaimKind(e.target.value)} aria-label={t("Refund kind")}>
          <option value="unknown"> {t("Kind: not sure")} </option>
          <option value="merchant_refund"> {t("Merchant refund")} </option>
          <option value="bank_reversal"> {t("Bank reversal")} </option>
          <option value="recovered_funds"> {t("Recovered funds")} </option>
        </select>
        <input value={claimDate} onChange={(e) => setClaimDate(e.target.value)} placeholder={t("Date, e.g. 20 Oct 2026")} aria-label={t("Refund date")}/>
        <button type="button" onClick={() => void submitClaim()} disabled={!claimText.trim()}> {t("Report refund")} </button>
      </div>
      <p className="small muted"> {t("A report is shown immediately as awaiting review. Upload the credit receipt and review it before it is counted.")} </p>

      <h3> {t("Is any money still outstanding?")} </h3>
      <div className="inline-form">
        <select value={statementMode} onChange={(e) => setStatementMode(e.target.value as "none" | "some" | "unsure")} aria-label={t("Outstanding money")}>
          <option value="none"> {t("Nothing is outstanding")} </option>
          <option value="some"> {t("Some money is still outstanding")} </option>
          <option value="unsure"> {t("I am not sure")} </option>
        </select>
        {statementMode === "some" && <input value={statementAmount} onChange={(e) => setStatementAmount(e.target.value)} placeholder={t("Amount in \u20B9")} aria-label={t("Outstanding amount")}/>}
        <input value={statementText} onChange={(e) => setStatementText(e.target.value)} placeholder={t("In your words")} aria-label={t("Outstanding statement")}/>
        <button type="button" onClick={() => void submitStatement()} disabled={!statementText.trim()}> {t("Record statement")} </button>
      </div>
    </>);
}


// ---- from features/overview/StatusPanel -------------------------------------------------------

export function StatusPanel({ caseId, snapshot, onSnapshot, onError }: {
    caseId: string;
    snapshot: CaseSnapshot;
    onSnapshot: (s: CaseSnapshot) => void;
    onError: (m: string | null) => void;
}) {
    const { t } = useI18n();
    const { latestOfficialStatus: latest, officialStatusEvents: events, sources } = snapshot;
    const [raw, setRaw] = useState("");
    const [category, setCategory] = useState("unknown");
    const [issuer, setIssuer] = useState("");
    const [effective, setEffective] = useState("");
    const [sourceId, setSourceId] = useState("");
    const candidates = sources.filter((s) => ["supplied_official_update", "image"].includes(s.kind));
    async function submit() {
        if (!raw.trim())
            return;
        try {
            onSnapshot(await api.recordStatus(caseId, { rawStatus: raw.trim(), category, issuerAsSupplied: issuer || null, effectiveAtText: effective || null,
                sourceIds: sourceId ? [sourceId] : [], expectedRevision: snapshot.case.currentRevision }));
            onError(null);
            setRaw("");
            setIssuer("");
            setEffective("");
            setSourceId("");
        }
        catch (e) {
            if (e instanceof ApiError && e.status === 409)
                onSnapshot(await api.getCase(caseId));
            onError(e instanceof Error ? e.message : "Could not record the status");
        }
    }
    return (<>
      {latest.event ? (<>
          <p><strong> {t(STATUS_CATEGORY_LABELS[latest.event.category] ?? latest.event.category)} </strong> — “{latest.event.rawStatus}”</p>
          <dl className="kv small">
            <dt> {t("Supplied issuer")} </dt><dd>{latest.event.issuerAsSupplied ?? t("not stated")}</dd>
            <dt> {t("Effective date")} </dt><dd>{effectiveDateText(latest.event.effectiveAt)}</dd>
            <dt> {t("Recorded in CyberBrief")} </dt><dd>{new Date(latest.event.receivedAt).toLocaleString()} {t("by")} {latest.event.enteredByRole.replace(/_/g, " ")}</dd>
            <dt> {t("Sources")} </dt><dd>{latest.event.sourceIds.length ? latest.event.sourceIds.map((id) => sources.find((s) => s.id === id)?.displayFilename ?? id.slice(0,8)).join(", ") : t("entered manually")}</dd>
          </dl>
          {latest.conflict && <p className="warn small">{latest.message}</p>}
        </>) : (<p><strong> {t("No official update recorded.")} </strong></p>)}
      <p className="small muted">{latest.qualification} {latest.message && !latest.conflict && latest.event ? latest.message : ""}</p>
      {events.length > 0 && (<>
          <h3> {t("Recorded updates")} </h3>
          <ul className="plain small">
            {events.map((e) => (<li key={e.id}>
                {t(STATUS_CATEGORY_LABELS[e.category] ?? e.category)} {t("\u00B7 effective")} {effectiveDateText(e.effectiveAt)} {t("\u00B7 recorded")} {new Date(e.receivedAt).toLocaleDateString()} · <ReviewStateBadge state={e.reviewState}/>
                <br /><span className="muted">“{e.rawStatus}”</span>
              </li>))}
          </ul>
        </>)}
      <h3> {t("Record an update you received")} </h3>
      <div className="inline-form">
        <input value={raw} onChange={(e) => setRaw(e.target.value)} placeholder={t("Exact wording of the status")} aria-label={t("Status wording")}/>
        <select value={category} onChange={(e) => setCategory(e.target.value)} aria-label={t("Status category")}>
          {Object.entries(STATUS_CATEGORY_LABELS).map(([k, v]) => <option key={k} value={k}> {t(v)} </option>)}
        </select>
        <input value={issuer} onChange={(e) => setIssuer(e.target.value)} placeholder={t("Issuer as written")} aria-label={t("Issuer")}/>
        <input value={effective} onChange={(e) => setEffective(e.target.value)} placeholder={t("Date on the update, e.g. 20 Oct 2026")} aria-label={t("Effective date")}/>
        {candidates.length > 0 && (<select value={sourceId} onChange={(e) => setSourceId(e.target.value)} aria-label={t("Linked source")}>
            <option value=""> {t("Link a source (optional)")} </option>
            {candidates.map((s) => <option key={s.id} value={s.id}>{s.displayFilename}</option>)}
          </select>)}
        <button type="button" onClick={() => void submit()} disabled={!raw.trim()}> {t("Record status")} </button>
      </div>
      <p className="small muted"> {t("Uploading the communication as \u201Cofficial update you received\u201D on the Material page proposes this automatically for you to confirm. Recording a status never changes recovery, and recovery never changes this status.")} </p>
    </>);
}


// ---- from features/review/ReviewPage ----------------------------------------------------------

type Act = (actions: ReviewAction[]) => Promise<void>;
const FIELD_LABELS: Record<string, string> = {
    amount: "Amount", currency: "Currency", reference: "Transaction reference", reference_type: "Reference type",
    status: "Payment status", direction: "Direction", occurred_at: "Date", counterparty: "Paid to / received from",
    account_scope: "Your account (VPA)", citizen_reported_amount: "Amount you reported", citizen_outstanding_statement: "Your statement on outstanding money",
};
const CORRECTION_HINTS: Record<string, string> = {
    amount: "e.g. INR 10,000.00", currency: "three-letter code, e.g. INR", reference: "exactly as shown, keep leading zeros",
    reference_type: "upi_ref, utr, transaction_id, order_id or other", status: "successful, pending or failed",
    direction: "debit or credit", occurred_at: "e.g. 12 Oct 2026 10:42 AM or 2026-10-12", counterparty: "name or VPA",
    account_scope: "your VPA or account label", citizen_reported_amount: "e.g. INR 25,000",
};
export function ReviewPage() {
    const { t } = useI18n();
    const { caseId } = useParams();
    const { snapshot, error, busy, review } = useCase(caseId);
    const [selectedSource, setSelectedSourceState] = useState<string | null>(null);
    const [seekMs, setSeekMs] = useState<number | null>(null);
    const setSelectedSource = (id: string, seek?: number) => {
        setSelectedSourceState(id);
        setSeekMs(seek ?? null);
    };
    const sourcesById = useMemo(() => new Map((snapshot?.sources ?? []).map((s) => [s.id, s])), [snapshot]);
    const paymentsById = useMemo(() => new Map((snapshot?.payments ?? []).map((p) => [p.id, p])), [snapshot]);
    if (!caseId)
        return null;
    if (!snapshot)
        return <p className="muted">{error ?? t("Loading…")}</p>;
    const activeSourceId = selectedSource ?? snapshot.sources.find((s) => s.kind === "image")?.id ?? snapshot.sources[0]?.id ?? null;
    const activeSource = activeSourceId ? sourcesById.get(activeSourceId) ?? null : null;
    const canonical = snapshot.payments.filter((p) => !p.mergedIntoPaymentId);
    const refunds = snapshot.refunds.filter((r) => !r.mergedIntoRefundId);
    const statusProposals = snapshot.officialStatusEvents.filter((e) => e.reviewState === "unreviewed");
    return (<>
      <h1> {t("Check extracted details")} </h1>
      <p className="muted"> {t("Each proposed field must be confirmed, corrected, left unresolved, or excluded. Only confirmed INR debits that succeeded count towards the reviewed total. Revision")} {snapshot.case.currentRevision}. <Link to={`/cases/${caseId}`}> {t("Back to overview")} </Link>
      </p>
      {error && <ErrorNotice message={error}/>}

      <div className="two-panel">
        <section className="panel source-panel" aria-label={t("Original source")}>
          <h2> {t("Source")} </h2>
          <label className="small"> {t("Showing")} {" "}
            <select value={activeSourceId ?? ""} onChange={(e) => setSelectedSource(e.target.value)} aria-label={t("Select source")}>
              {snapshot.sources.map((s) => <option key={s.id} value={s.id}>{s.displayFilename} · {s.kind.replace(/_/g, " ")}</option>)}
            </select>
          </label>
          {activeSource && <SourceViewer caseId={caseId} source={activeSource} sourcesById={sourcesById} seekMs={seekMs}/>}
        </section>

        <section className="panel facts-panel" aria-label={t("Proposed details")}>
          {snapshot.duplicateCandidates.length > 0 && (<div className="card warn">
              <h2> {t("Possible duplicates")} </h2>
              {snapshot.duplicateCandidates.map((c) => {
                const [a, b] = c.paymentIds.map((id) => paymentsById.get(id)!);
                return (<div key={c.paymentIds.join("-")} className="candidate">
                    <p>
                      <strong>{a.label}</strong> {t("and")} <strong>{b.label}</strong> {t("show the same reference")} {" "}
                      <span className="mono">{a.fields.reference ? factDisplay(a.fields.reference) : ""}</span> {t(". Do they show the same payment?")} {c.issues.length > 0 && <span className="muted small"> {t("Open points:")} {c.issues.map((i) => i.replace(/_/g, " ")).join(", ")}.</span>}
                    </p>
                    <button type="button" disabled={busy} onClick={() => void review([{ type: "merge", targetType: "payment", targetId: b.id, otherPaymentId: a.id }])}> {t("Same payment: count once (keep")} {a.label})
                    </button>{" "}
                    <button type="button" disabled={busy} className="secondary" onClick={() => void review([{ type: "distinct", targetType: "payment", targetId: a.id, otherPaymentId: b.id }])}> {t("Different payments")} </button>
                  </div>);
            })}
            </div>)}

          {statusProposals.length > 0 && (<div className="card warn">
              <h2> {t("Proposed official status from a supplied communication")} </h2>
              {statusProposals.map((e) => (<StatusProposal key={e.id} eventId={e.id} category={e.category} rawStatus={e.rawStatus} issuer={e.issuerAsSupplied} effective={effectiveDateText(e.effectiveAt)} sourceName={e.sourceIds.map((id) => sourcesById.get(id)?.displayFilename ?? id.slice(0, 8)).join(", ")} busy={busy} onAction={review} onShowSource={() => e.sourceIds[0] && setSelectedSource(e.sourceIds[0])}/>))}
            </div>)}

          {Object.values(snapshot.caseFacts).map((fact) => (<div className="card" key={fact.factId}>
              <h2> {t(fact.kind === "citizen_reported_amount" ? "Your account" : "Your statement")} </h2>
              <FactRow fact={fact} busy={busy} onAction={review} onShowSource={setSelectedSource}/>
              <p className="small muted"> {t("This is what you reported. It is shown separately from reviewed totals and is never rewritten to match them.")} </p>
            </div>))}

          {canonical.length === 0 && refunds.length === 0 && <p className="muted"> {t("No proposed transactions yet. Process material first.")} </p>}
          {canonical.map((p) => (<PaymentCard key={p.id} payment={p} merged={snapshot.payments.filter((m) => m.mergedIntoPaymentId === p.id)} busy={busy} onAction={review} onShowSource={setSelectedSource} sourcesById={sourcesById}/>))}
          {refunds.map((r) => (<RefundCard key={r.id} refund={r} all={snapshot.refunds} busy={busy} onAction={review} onShowSource={setSelectedSource} sourcesById={sourcesById}/>))}
        </section>
      </div>
    </>);
}
function SourceViewer({ caseId, source, sourcesById, seekMs }: {
    caseId: string;
    source: SourceOut;
    sourcesById: Map<string, SourceOut>;
    seekMs: number | null;
}) {
    const { t } = useI18n();
    const audioRef = useRef<HTMLAudioElement | null>(null);
    useEffect(() => {
        if (seekMs !== null && audioRef.current) {
            audioRef.current.currentTime = seekMs / 1000;
            void audioRef.current.play().catch(() => undefined);
        }
    }, [seekMs, source.id]);
    const audioSource = source.kind === "audio" ? source : source.audioSourceId ? sourcesById.get(source.audioSourceId) ?? null : null;
    if (source.kind === "audio" || source.kind === "transcript") {
        return (<figure className="viewer">
        {audioSource && <audio ref={audioRef} controls preload="metadata" src={api.sourceUrl(caseId, audioSource.id)} aria-label={t("Recording {0}", {0: audioSource.displayFilename})}/>}
        {source.kind === "transcript" && (<>
            <blockquote>{source.textContent}</blockquote>
            {source.transcriptTurns && source.transcriptTurns.length > 1 && (<ol className="small">
                {source.transcriptTurns.map((turn, i) => (<li key={i}>
                    <button type="button" className="linklike" onClick={() => { if (audioRef.current && turn.startMs !== undefined) {
                        audioRef.current.currentTime = turn.startMs / 1000;
                        void audioRef.current.play().catch(() => undefined);
                    } }}>
                      ▶ {Math.floor((turn.startMs ?? 0) / 1000)} {t("s")} </button>{" "}
                    {turn.transcript}
                  </li>))}
              </ol>)}
          </>)}
        <figcaption className="small muted">
          {source.displayFilename} · {source.kind === "audio" ? t("{0} s recording", {0: Math.round((source.audioDurationMs ?? 0) / 1000)}) : t("transcript ({0})", {0: source.transcriptionMode ?? "unknown"})} {t("\u00B7 received")} {new Date(source.receivedAt).toLocaleString()}
          <br /> {t("Transcripts are proposals: correct them on the Material page. Time ranges come from transcription turns, not word timestamps.")} </figcaption>
      </figure>);
    }
    if (source.kind === "image" || source.kind === "supplied_official_update") {
        return (<figure className="viewer">
        <img src={api.sourceUrl(caseId, source.id)} alt={t("Original upload {0}", {0: source.displayFilename})}/>
        <figcaption className="small muted">
          {source.displayFilename} · {source.kind.replace(/_/g, " ")} · {source.imageWidth}×{source.imageHeight} {t("\u00B7 SHA-256")} <span className="mono">{source.sha256.slice(0, 16)}…</span>
          <br /> {t("Region highlights are shown only when they can be validated; otherwise the full source is opened.")} </figcaption>
      </figure>);
    }
    return (<figure className="viewer">
      <blockquote>{source.textContent}</blockquote>
      <figcaption className="small muted">{source.displayFilename} · {source.kind.replace(/_/g, " ")} {t("\u00B7 received")} {new Date(source.receivedAt).toLocaleString()}</figcaption>
    </figure>);
}
function confirmBatch(fields: Record<string, FactOut>, identityDone: boolean, targetType: "payment" | "refund", targetId: string): {
    batch: ReviewAction[];
    count: number;
} {
    const confirmable = Object.values(fields).filter((f) => f.value && !["user_confirmed", "reviewer_checked", "excluded"].includes(f.reviewState));
    const batch: ReviewAction[] = confirmable.map((f) => ({ type: "confirm", targetType: "fact", targetId: f.factId }));
    if (!identityDone)
        batch.push({ type: "confirm", targetType, targetId });
    return { batch, count: confirmable.length };
}
function ExcludeControl({ busy, label, onExclude }: {
    busy: boolean;
    label: string;
    onExclude: (reason: string) => void;
}) {
    const { t } = useI18n();
    const [open, setOpen] = useState(false);
    const [reason, setReason] = useState("");
    if (!open)
        return <button type="button" className="secondary" disabled={busy} onClick={() => setOpen(true)}> {t(label)} </button>;
    return (<span className="inline-form">
      <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("Reason")} aria-label={t("Exclusion reason")}/>
      <button type="button" disabled={busy || !reason.trim()} onClick={() => { onExclude(reason.trim()); setOpen(false); }}> {t("Exclude")} </button>
      <button type="button" className="secondary" onClick={() => setOpen(false)}> {t("Cancel")} </button>
    </span>);
}
function PaymentCard({ payment, merged, busy, onAction, onShowSource, sourcesById }: {
    payment: PaymentOut;
    merged: PaymentOut[];
    busy: boolean;
    onAction: Act;
    onShowSource: (id: string) => void;
    sourcesById: Map<string, SourceOut>;
}) {
    const { t } = useI18n();
    const identityDone = payment.identityReviewState === "user_confirmed" || payment.identityReviewState === "reviewer_checked";
    const { batch, count } = confirmBatch(payment.fields, identityDone, "payment", payment.id);
    return (<div className="card payment">
      <div className="payment-head">
        <h2>{payment.label}</h2>
        <CategoryBadge category={payment.category} reason={payment.categoryReason}/>
        <ReviewStateBadge state={payment.identityReviewState}/>
      </div>
      {payment.categoryReason && <p className="small muted">{payment.categoryReason}</p>}
      <p className="small muted"> {t("Sources:")} {" "}
        {payment.sourceIds.map((sid) => <button key={sid} type="button" className="linklike" onClick={() => onShowSource(sid)}>{sourcesById.get(sid)?.displayFilename ?? sid.slice(0, 8)}</button>)}
        {merged.length > 0 && <> {t("\u00B7 counted once with")} {merged.map((m) => m.label).join(", ")}</>}
      </p>
      {FIELD_ORDER.filter((k) => payment.fields[k]).map((k) => <FactRow key={k} fact={payment.fields[k]} busy={busy} onAction={onAction} onShowSource={onShowSource}/>)}
      <div className="actions">
        {payment.identityReviewState !== "excluded" && (<>
            <button type="button" disabled={busy || batch.length === 0} onClick={() => void onAction(batch)} title={t("Confirms every field that has a readable value plus the payment identity, in one revision. Fields without a value stay unreviewed.")}> {t("Confirm all readable fields (")} {count} {t(") and this payment")} </button>
            <button type="button" className="secondary" disabled={busy || identityDone} onClick={() => void onAction([{ type: "confirm", targetType: "payment", targetId: payment.id }])}> {t("Confirm identity only")} </button>
          </>)}
        {payment.identityReviewState === "excluded"
            ? <button type="button" className="secondary" disabled={busy} onClick={() => void onAction([{ type: "include", targetType: "payment", targetId: payment.id }])}> {t("Include again")} </button>
            : <ExcludeControl busy={busy} label="Exclude with a reason" onExclude={(reason) => void onAction([{ type: "exclude", targetType: "payment", targetId: payment.id, reason }])}/>}
        {merged.map((m) => <button key={m.id} type="button" className="secondary" disabled={busy} onClick={() => void onAction([{ type: "unmerge", targetType: "payment", targetId: m.id }])}> {t("Undo merge of")} {m.label}</button>)}
      </div>
    </div>);
}
function RefundCard({ refund, all, busy, onAction, onShowSource, sourcesById }: {
    refund: RefundOut;
    all: RefundOut[];
    busy: boolean;
    onAction: Act;
    onShowSource: (id: string) => void;
    sourcesById: Map<string, SourceOut>;
}) {
    const { t } = useI18n();
    const [linkTarget, setLinkTarget] = useState("");
    const identityDone = refund.identityReviewState === "user_confirmed" || refund.identityReviewState === "reviewer_checked";
    const { batch, count } = confirmBatch(refund.fields, identityDone, "refund", refund.id);
    const merged = all.filter((m) => m.mergedIntoRefundId === refund.id);
    const documents = all.filter((d) => d.id !== refund.id && d.evidenceKind === "document" && !d.mergedIntoRefundId);
    const reference = refund.fields.reference?.value?.value as string | undefined;
    const sameReference = all.filter((o) => o.id !== refund.id && !o.mergedIntoRefundId && reference && (o.fields.reference?.value?.value as string | undefined) === reference);
    return (<div className="card payment refund">
      <div className="payment-head">
        <h2>{refund.label} <span className="muted small"> {t("refund \u00B7")} {t(refund.evidenceKind === "claim" ? "reported by you" : "credit record")} </span></h2>
        <CategoryBadge category={refund.category} reason={refund.categoryReason}/>
        <ReviewStateBadge state={refund.identityReviewState}/>
        <span className="badge"> {t(refund.relatesToIncident === "confirmed" ? "Related to this case" : refund.relatesToIncident === "denied" ? "Marked unrelated" : "Relation not confirmed")} </span>
      </div>
      {refund.categoryReason && <p className="small muted">{refund.categoryReason}</p>}
      <p className="small muted"> {t("Sources:")} {" "}
        {refund.sourceIds.map((sid) => <button key={sid} type="button" className="linklike" onClick={() => onShowSource(sid)}>{sourcesById.get(sid)?.displayFilename ?? sid.slice(0, 8)}</button>)}
        {merged.length > 0 && <> {t("\u00B7 includes")} {merged.map((m) => m.label).join(", ")}</>}
      </p>
      {FIELD_ORDER.filter((k) => refund.fields[k]).map((k) => <FactRow key={k} fact={refund.fields[k]} busy={busy} onAction={onAction} onShowSource={onShowSource}/>)}
      <div className="actions">
        {refund.evidenceKind === "document" && refund.identityReviewState !== "excluded" && (<button type="button" disabled={busy || batch.length === 0} onClick={() => void onAction(batch)}> {t("Confirm all readable fields (")} {count} {t(") and this refund")} </button>)}
        {refund.relatesToIncident !== "confirmed" && refund.identityReviewState !== "excluded" && (<button type="button" disabled={busy} onClick={() => void onAction([{ type: "relates", targetType: "refund", targetId: refund.id }])}> {t("This credit relates to my case")} </button>)}
        {refund.identityReviewState === "excluded"
            ? <button type="button" className="secondary" disabled={busy} onClick={() => void onAction([{ type: "include", targetType: "refund", targetId: refund.id }])}> {t("Include again")} </button>
            : <ExcludeControl busy={busy} label={refund.relatesToIncident === "confirmed" ? "Exclude (e.g. reversed)" : "Not related to my case"} onExclude={(reason) => void onAction([{ type: refund.relatesToIncident === "confirmed" ? "exclude" : "unrelated", targetType: "refund", targetId: refund.id, reason }])}/>}
        {refund.evidenceKind === "claim" && documents.length > 0 && (<span className="inline-form">
            <select value={linkTarget} onChange={(e) => setLinkTarget(e.target.value)} aria-label={t("Credit record for {0}", {0: refund.label})}>
              <option value=""> {t("This report describes credit record\u2026")} </option>
              {documents.map((d) => <option key={d.id} value={d.id}>{d.label} {d.fields.amount ? factDisplay(d.fields.amount) : ""}</option>)}
            </select>
            <button type="button" disabled={busy || !linkTarget} onClick={() => void onAction([{ type: "merge", targetType: "refund", targetId: refund.id, otherPaymentId: linkTarget }])}> {t("Link")} </button>
          </span>)}
        {sameReference.map((o) => (<button key={o.id} type="button" className="secondary" disabled={busy} onClick={() => void onAction([{ type: "merge", targetType: "refund", targetId: o.id, otherPaymentId: refund.id }])}> {t("Same refund as")} {o.label} {t(": count once")} </button>))}
        {merged.map((m) => <button key={m.id} type="button" className="secondary" disabled={busy} onClick={() => void onAction([{ type: "unmerge", targetType: "refund", targetId: m.id }])}> {t("Unlink")} {m.label}</button>)}
      </div>
    </div>);
}
function StatusProposal({ eventId, category, rawStatus, issuer, effective, sourceName, busy, onAction, onShowSource }: {
    eventId: string;
    category: string;
    rawStatus: string;
    issuer: string | null;
    effective: string;
    sourceName: string;
    busy: boolean;
    onAction: Act;
    onShowSource: () => void;
}) {
    const { t } = useI18n();
    return (<div className="candidate">
      <p><strong> {t(STATUS_CATEGORY_LABELS[category] ?? category)} </strong> — “{rawStatus}”<br />
        <span className="small muted"> {t("Issuer as written:")} {issuer ?? t("not stated")} {t("\u00B7 effective")} {effective} {t("\u00B7 from")} <button type="button" className="linklike" onClick={onShowSource}>{sourceName || "source"}</button></span></p>
      <button type="button" disabled={busy} onClick={() => void onAction([{ type: "confirm", targetType: "official_status", targetId: eventId }])}> {t("This is what the communication says")} </button>{" "}
      <ExcludeControl busy={busy} label="Not a status update" onExclude={(reason) => void onAction([{ type: "exclude", targetType: "official_status", targetId: eventId, reason }])}/>
    </div>);
}
function FactRow({ fact, busy, onAction, onShowSource }: {
    fact: FactOut;
    busy: boolean;
    onAction: Act;
    onShowSource: (id: string, seekMs?: number) => void;
}) {
    const { t } = useI18n();
    const [mode, setMode] = useState<"idle" | "correct" | "exclude">("idle");
    const [draft, setDraft] = useState("");
    const [reason, setReason] = useState("");
    const confirmed = fact.reviewState === "user_confirmed" || fact.reviewState === "reviewer_checked";
    const anchor = fact.anchors[0];
    const correctable = fact.kind in CORRECTION_HINTS;
    return (<div className={`fact ${CRITICAL_FIELDS.has(fact.kind) ? "critical" : ""}`}>
      <div className="fact-main">
        <span className="fact-label"> {t(FIELD_LABELS[fact.kind] ?? fact.kind)} </span>
        <span className="fact-value">{factDisplay(fact)}</span>
        {fact.rawText && fact.value && fact.kind !== "citizen_outstanding_statement" && <span className="muted small"> {t("shown as \u201C")} {fact.rawText}”</span>}
        <IssueBadge issue={fact.issue}/>
        <ReviewStateBadge state={fact.reviewState}/>
        <span className="muted small"> {t("v")} {fact.version}</span>
      </div>
      <div className="fact-anchors small">
        {anchor ? (<button type="button" className="linklike" onClick={() => onShowSource(anchor.sourceId, anchor.audioRangeMs?.start)}>{anchor.audioRangeMs ? t("▶ Play from {0}s", {0: Math.floor(anchor.audioRangeMs.start / 1000)}) : t("Open source")}{anchor.quote ? `: “${anchor.quote.slice(0,80)}${anchor.quote.length > 80 ? "…" : ""}”` : ""}</button>) : <span className="muted"> {t("No source anchor")} </span>}
        {anchor && !anchor.quoteVerified && anchor.quote && <span className="muted"> {t("(model-reported excerpt, not verified against pixels)")} </span>}
      </div>
      <div className="fact-actions">
        {mode === "idle" && (<>
            <button type="button" disabled={busy || confirmed || !fact.value} onClick={() => void onAction([{ type: "confirm", targetType: "fact", targetId: fact.factId }])}> {t("Confirm")} </button>
            {correctable && <button type="button" className="secondary" disabled={busy} onClick={() => { setDraft(fact.rawText ?? ""); setMode("correct"); }}> {t("Correct")} </button>}
            <button type="button" className="secondary" disabled={busy || fact.reviewState === "unresolved"} onClick={() => void onAction([{ type: "unresolved", targetType: "fact", targetId: fact.factId, reason: "left unresolved by reviewer" }])}> {t("Leave unresolved")} </button>
            <button type="button" className="secondary" disabled={busy || fact.reviewState === "excluded"} onClick={() => setMode("exclude")}> {t("Exclude")} </button>
          </>)}
        {mode === "correct" && (<span className="inline-form">
            <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={t(CORRECTION_HINTS[fact.kind] ?? "")} aria-label={t("Corrected {0}", {0: t(FIELD_LABELS[fact.kind] ?? fact.kind)})}/>
            <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("Why? (optional)")} aria-label={t("Correction reason")}/>
            <button type="button" disabled={busy || !draft.trim()} onClick={() => { void onAction([{ type: "correct", targetType: "fact", targetId: fact.factId, rawText: draft.trim(), reason: reason.trim() || null }]); setMode("idle"); setReason(""); }}> {t("Save correction")} </button>
            <button type="button" className="secondary" onClick={() => setMode("idle")}> {t("Cancel")} </button>
          </span>)}
        {mode === "exclude" && (<span className="inline-form">
            <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("Reason for excluding this field")} aria-label={t("Exclusion reason")}/>
            <button type="button" disabled={busy || !reason.trim()} onClick={() => { void onAction([{ type: "exclude", targetType: "fact", targetId: fact.factId, reason: reason.trim() }]); setMode("idle"); setReason(""); }}> {t("Exclude field")} </button>
            <button type="button" className="secondary" onClick={() => setMode("idle")}> {t("Cancel")} </button>
          </span>)}
      </div>
    </div>);
}


// ---- from features/reviewer/ReviewerPage ------------------------------------------------------

export function ReviewerPage() {
    const { t } = useI18n();
    const { caseId } = useParams();
    const { session } = useOutletContext<{
        session: SessionOut;
    }>();
    const { snapshot, error, setSnapshot, setError } = useCase(caseId);
    const [changes, setChanges] = useState<ChangeSummary | null>(null);
    const [allowPartial, setAllowPartial] = useState(false);
    const [note, setNote] = useState("");
    useEffect(() => {
        if (!caseId || !snapshot)
            return;
        api.changes(caseId).then(setChanges).catch(() => setChanges(null));
    }, [caseId, snapshot]);
    if (!caseId)
        return null;
    if (!snapshot)
        return <p className="muted">{error ?? t("Loading…")}</p>;
    const isReviewer = session.role === "officer";
    const latest = snapshot.resolutions[0] ?? null;
    const labels = new Map<string, string>([...snapshot.payments.map((p) => [p.id, p.label] as const), ...snapshot.refunds.map((r) => [r.id, r.label] as const)]);
    const debits = snapshot.payments.filter((p) => !p.mergedIntoPaymentId);
    const claims = snapshot.refunds.filter((r) => r.category === "claim_only");
    const openQuestions = snapshot.questions.filter((q) => q.state === "open");
    async function act(fn: () => Promise<typeof snapshot & object>) {
        try {
            const next = await fn();
            if (next)
                setSnapshot(next);
            setError(null);
        }
        catch (e) {
            if (e instanceof ApiError && e.status === 409)
                setSnapshot(await api.getCase(caseId!));
            setError(e instanceof ApiError ? `${e.message} ${e.nextStep ?? ""}`.trim() : e instanceof Error ? e.message : "Action failed");
        }
    }
    return (<>
      <div className="warn"><strong> {t("Demo reviewer view.")} </strong> {t("A simulated helpdesk reviewer working on fictional data. Actions here manage the CyberBrief packet only; they never operate on a government case.")} </div>
      <h1>{snapshot.case.title}</h1>
      <p className="muted"> {t("Revision")} {snapshot.case.currentRevision} {t("\u00B7 last acknowledged review at revision")} {snapshot.case.lastReviewedRevision} {t("\u00B7 acting as")} {session.role.replace(/_/g, " ")}</p>
      {error && <ErrorNotice message={error}/>}

      <div className="grid-2">
        <section className="card">
          <h2> {t("What changed since the previous review")} </h2>
          {!changes ? <p className="muted small"> {t("Loading\u2026")} </p> : (<ul className="plain small">
              <li>{changes.sourcesAdded.length} {t("new source(s),")} {changes.paymentsAdded.length} {t("new payment(s),")} {changes.refundsAdded.length} {t("new refund record(s)")} </li>
              <li>{changes.factsChanged.length} {t("fact change(s),")} {changes.newConflicts.length} {t("new conflict(s)")} </li>
              <li>{changes.allocationsAdded.length} {t("allocation(s) added,")} {changes.allocationsInvalidated.length} {t("invalidated")} </li>
              <li>{changes.officialStatusAdded.length} {t("official status record(s) added")} </li>
              {changes.balanceChanges.map((b) => <li key={b.field}>{b.field}: {b.from ?? "—"} → {b.to ?? "—"}</li>)}
            </ul>)}
          <p className="small"><Link to={`/cases/${caseId}/updates`}> {t("Full change summary")} </Link></p>
        </section>

        <section className="card">
          <h2> {t("Original debits and matched refunds")} </h2>
          <table className="small">
            <thead><tr><th> {t("Payment")} </th><th> {t("Amount")} </th><th> {t("Category")} </th><th> {t("Matched refunds")} </th></tr></thead>
            <tbody>
              {debits.map((p) => {
            const matched = snapshot.allocations.filter((a) => a.paymentId === p.id && a.counted);
            return (<tr key={p.id}>
                    <td>{p.label}</td>
                    <td>{p.fields.amount ? factDisplay(p.fields.amount) : "—"}</td>
                    <td><CategoryBadge category={p.category} reason={p.categoryReason}/></td>
                    <td>{matched.length === 0 ? <span className="muted"> {t("none")} </span> : matched.map((a) => <span key={a.id}>{labels.get(a.refundId)} <MoneyText minor={a.amountMinor}/>; </span>)}</td>
                  </tr>);
        })}
            </tbody>
          </table>
          <p className="small"> {t("Baseline")} <MoneyText minor={snapshot.recovery.reviewedDebitMinor}/> {t("\u00B7 matched")} <MoneyText minor={snapshot.recovery.matchedRefundMinor}/> {t("\u00B7 outstanding")} <strong><MoneyText minor={snapshot.recovery.outstandingMinor}/></strong> · {t(RECOVERY_LABELS[snapshot.recovery.state] ?? snapshot.recovery.state)} </p>
        </section>

        <section className="card">
          <h2> {t("Reported amounts still awaiting support")} </h2>
          {claims.length === 0 ? <p className="muted small"> {t("None.")} </p> : <ul className="plain small">{claims.map((r) => <li key={r.id}>{r.label}: {r.fields.amount ? factDisplay(r.fields.amount) : t("amount unknown")} {t("\u2014 reported by the citizen, no reviewed credit record")} </li>)}</ul>}
          {snapshot.totals.citizenReportedMinor && <p className="small"> {t("Citizen-reported incident amount")} <MoneyText minor={snapshot.totals.citizenReportedMinor}/> {t("vs reviewed debits")} <MoneyText minor={snapshot.totals.reviewedDebitMinor}/>.</p>}
          <h2> {t("Unresolved issues")} </h2>
          {openQuestions.length === 0 && snapshot.totals.unreviewedIds.length === 0 ? <p className="muted small"> {t("None open.")} </p> : (<ul className="plain small">
              {snapshot.totals.unreviewedIds.length > 0 && <li>{snapshot.totals.unreviewedIds.length} {t("payment(s) unreviewed")} </li>}
              {openQuestions.map((q) => <li key={q.id}>{q.text}</li>)}
            </ul>)}
          <h2> {t("Official complaint status")} </h2>
          <p className="small">{snapshot.latestOfficialStatus.event ? `${snapshot.latestOfficialStatus.event.category} — “${snapshot.latestOfficialStatus.event.rawStatus}”` : t("No official update recorded.")} <span className="muted">{snapshot.latestOfficialStatus.qualification}</span></p>
        </section>

        <section className="card">
          <h2> {t("Draft resolution summary")} </h2>
          {latest ? (<>
              <p className="small">{latest.kind.replace(/_/g, " ")} {t("\u00B7 prepared at revision")} {latest.revision} {t("by")} {latest.preparedByRole.replace(/_/g, " ")} · {t(latest.eligible ? "eligible for review" : "not yet eligible")} {latest.stale ? t(" · stale (case changed since)") : ""}</p>
              {latest.blockingReasons.length > 0 && <ul className="small error">{latest.blockingReasons.map((b) => <li key={b}>{b}</li>)}</ul>}
              <pre className="summary">{latest.summaryText}</pre>
              {latest.reviewOutcome && <p className="small"> {t("Recorded:")} <strong>{latest.reviewOutcome.replace(/_/g, " ")}</strong> {t("by")} {latest.reviewedByRole?.replace(/_/g, " ")} {t("at")} {latest.reviewedAt ? new Date(latest.reviewedAt).toLocaleString() : ""}</p>}
            </>) : <p className="muted small"> {t("No summary prepared yet.")} </p>}
          <div className="actions">
            <label className="small" style={{ display: "inline-flex", gap: 6, alignItems: "center", fontWeight: 400 }}>
              <input type="checkbox" checked={allowPartial} onChange={(e) => setAllowPartial(e.target.checked)} style={{ width: "auto", display: "inline", margin: 0 }}/> {t("Allow a partial / unresolved summary")} </label>
            <button type="button" onClick={() => void act(() => api.prepareResolution(caseId, allowPartial, snapshot.case.currentRevision))}> {t("Prepare resolution summary")} </button>
          </div>
          <h3> {t("Reviewer actions")} </h3>
          {!isReviewer && <p className="small muted"> {t("Send this complaint for review from the overview, then sign in as an officer to record a packet review. The officer role is a simulated label, not police authority.")} </p>}
          <div className="inline-form">
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("Reviewer note (optional)")} aria-label={t("Reviewer note")}/>
            <button type="button" disabled={!isReviewer || !latest || latest.stale || !latest.eligible || Boolean(latest.reviewOutcome)} onClick={() => void act(() => api.reviewResolution(caseId, latest!.id, "packet_reviewed", note || null, snapshot.case.currentRevision))}> {t("Record packet reviewed")} </button>
            <button type="button" className="secondary" disabled={!isReviewer || !latest || latest.stale || Boolean(latest.reviewOutcome)} onClick={() => void act(() => api.reviewResolution(caseId, latest!.id, "more_information_requested", note || null, snapshot.case.currentRevision))}> {t("Request more information")} </button>
          </div>
          <p className="small muted"> {t("Recording a review affects only this packet. The official complaint status is a separate track and is not changed by anything on this page.")} </p>
        </section>
      </div>
    </>);
}


// ---- from features/updates/UpdatePage ---------------------------------------------------------

const BALANCE_LABELS: Record<string, string> = {
    reviewedDebitMinor: "Reviewed distinct successful debits", pendingMinor: "Reviewed pending", matchedRefundMinor: "Reviewed matched refunds",
    outstandingMinor: "Outstanding against reviewed payments", recoveryState: "Recovery state",
};
export function UpdatePage() {
    const { t } = useI18n();
    const { caseId } = useParams();
    const { snapshot, error, setSnapshot, setError } = useCase(caseId);
    const [changes, setChanges] = useState<ChangeSummary | null>(null);
    useEffect(() => {
        if (!caseId || !snapshot)
            return;
        api.changes(caseId).then(setChanges).catch((e: Error) => setError(e.message));
    }, [caseId, snapshot, setError]);
    if (!caseId)
        return null;
    if (!snapshot || !changes)
        return <p className="muted">{error ?? t("Loading…")}</p>;
    const labels = new Map<string, string>([...snapshot.payments.map((p) => [p.id, p.label] as const), ...snapshot.refunds.map((r) => [r.id, r.label] as const)]);
    const unreviewedProposals = snapshot.payments.filter((p) => p.category === "unreviewed").length + snapshot.refunds.filter((r) => r.category === "unreviewed").length
        + snapshot.officialStatusEvents.filter((e) => e.reviewState === "unreviewed").length;
    const nothing = changes.sourcesAdded.length + changes.paymentsAdded.length + changes.refundsAdded.length + changes.factsChanged.length + changes.allocationsAdded.length
        + changes.allocationsInvalidated.length + changes.officialStatusAdded.length + changes.balanceChanges.length + changes.questionsOpened.length === 0;
    async function acknowledge() {
        try {
            setSnapshot(await api.acknowledgeUpdate(caseId!, snapshot!.case.currentRevision));
            setError(null);
        }
        catch (e) {
            if (e instanceof ApiError && e.status === 409)
                setSnapshot(await api.getCase(caseId!));
            setError(e instanceof Error ? e.message : "Could not acknowledge");
        }
    }
    return (<>
      <h1> {t("Add an update")} </h1>
      <p className="muted"> {t("Say or type what changed, attach a credit receipt or an official message on the")} <Link to={`/cases/${caseId}/material`}> {t("Material")} </Link> {t("page, report a refund on the")} {" "}
        <Link to={`/cases/${caseId}`}> {t("Overview")} </Link> {t(", and review proposals on")} <Link to={`/cases/${caseId}/review`}> {t("Check details")} </Link> {t(". This page shows what changed since you last reviewed the case (revision")} {changes.sinceRevision} → {changes.currentRevision} {t("). Existing confirmed facts are never replaced by newer extractions.")} </p>
      {error && <ErrorNotice message={error}/>}
      {nothing && <p className="card"> {t("Nothing has changed since your last review.")} </p>}

      <div className="grid-2">
        <section className="card">
          <h2> {t("New sources")} </h2>
          {changes.sourcesAdded.length === 0 ? <p className="muted small"> {t("None.")} </p> : <ul className="plain small">{changes.sourcesAdded.map((s) => <li key={s.id}>{s.displayFilename} · {s.kind.replace(/_/g, " ")} {t("\u00B7 r")} {s.revision}</li>)}</ul>}
          <h2> {t("New facts")} </h2>
          {changes.paymentsAdded.length + changes.refundsAdded.length === 0 ? <p className="muted small"> {t("None.")} </p> : (<ul className="plain small">
              {changes.paymentsAdded.map((p) => <li key={p.id}> {t("Payment")} {p.label} {t("proposed (r")} {p.revision})</li>)}
              {changes.refundsAdded.map((r) => <li key={r.id}> {t("Refund")} {r.label} {t(r.evidenceKind === "claim" ? "reported" : "record proposed")} {t("(r")} {r.revision})</li>)}
            </ul>)}
          {unreviewedProposals > 0 && <p className="small"><Link to={`/cases/${caseId}/review`} className="button"> {t("Review")} {unreviewedProposals} {t("proposal(s) \u2192")} </Link></p>}
        </section>

        <section className="card">
          <h2> {t("Suggested changes to existing facts")} </h2>
          {changes.factsChanged.length === 0 ? <p className="muted small"> {t("None.")} </p> : (<ul className="plain small">
              {changes.factsChanged.map((f) => <li key={`${f.factId}-${f.reviewState}`}>{labels.get(f.subjectId) ?? f.subjectType} · {f.kind.replace(/_/g, " ")} → {f.reviewState.replace(/_/g, " ")}{f.rawText ? ` “${f.rawText}”` : ""} {t("by")} {f.reviewedBy ?? t("system")}</li>)}
            </ul>)}
          <h2> {t("New conflicts")} </h2>
          {changes.newConflicts.length === 0 ? <p className="muted small"> {t("None.")} </p> : <ul className="plain small">{changes.newConflicts.map((c, i) => <li key={i}>{c.paymentIds.map((id) => labels.get(id) ?? id.slice(0, 8)).join(" and ")} {t("share a reference")} </li>)}</ul>}
        </section>

        <section className="card">
          <h2> {t("Refund allocations")} </h2>
          {changes.allocationsAdded.length + changes.allocationsInvalidated.length === 0 ? <p className="muted small"> {t("None.")} </p> : (<ul className="plain small">
              {changes.allocationsAdded.map((a) => <li key={a.id}>{labels.get(a.refundId)} → {labels.get(a.paymentId)} <MoneyText minor={a.amountMinor}/> · {a.state.replace(/_/g, " ")}</li>)}
              {changes.allocationsInvalidated.map((a) => <li key={`inv-${a.id}`} className="error">{labels.get(a.refundId)} → {labels.get(a.paymentId)} {t("invalidated")} </li>)}
            </ul>)}
          <h2> {t("Official status")} </h2>
          {changes.officialStatusAdded.length === 0 ? <p className="muted small"> {t("None.")} </p> : <ul className="plain small">{changes.officialStatusAdded.map((e) => <li key={e.id}>{e.category.replace(/_/g, " ")} · “{e.rawStatus}” · {e.reviewState.replace(/_/g, " ")}</li>)}</ul>}
        </section>

        <section className="card">
          <h2> {t("Balances")} </h2>
          {changes.balanceChanges.length === 0 ? (<p className="muted small"> {t("Unchanged.")} </p>) : (<ul className="plain small">
              {changes.balanceChanges.map((b) => (<li key={b.field}> {t(BALANCE_LABELS[b.field] ?? b.field)} : {b.field === "recoveryState" ? `${t(RECOVERY_LABELS[b.from ?? ""] ?? b.from ?? "—")} → ${t(RECOVERY_LABELS[b.to ?? ""] ?? b.to ?? "—")}` : `${b.from === null ? "—" : formatInr(b.from)} → ${b.to === null ? "—" : formatInr(b.to)}`}</li>))}
            </ul>)}
          <h2> {t("Questions")} </h2>
          <p className="small">{changes.questionsOpened.length} {t("opened \u00B7")} {changes.questionsResolved.length} {t("resolved")} </p>
          <div className="actions">
            <button type="button" onClick={() => void acknowledge()} disabled={nothing}> {t("I have reviewed these changes")} </button>
          </div>
          <p className="small muted"> {t("Acknowledging only moves the \u201Csince\u201D marker for this summary; it does not confirm any fact.")} </p>
        </section>
      </div>
    </>);
}
