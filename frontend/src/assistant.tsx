/** The citizen chat assistant: talk it through, and the complaint is drafted from what you said.
 *
 * The assistant only proposes. Every change to a complaint happens here, in the browser, through the
 * same API calls the rest of the app uses, and only when the citizen asks for it: by tapping a quick
 * reply the server offered ("Yes, start my complaint"), by attaching a file, or by answering a
 * question the assistant has just asked. Nothing the assistant says next comes from the model: the
 * server writes it from the complaint record once the browser reports what happened.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, TERMINAL_RUN_STATES, api, newIdempotencyKey, request, type CaseSnapshot, type FactOut, type Language, type PaymentOut, type ReviewAction } from "./api";
import { blobToWav16k, useRecorder } from "./intake";
import { ErrorNotice, MoneyText, RECOVERY_LABELS, STATUS_CATEGORY_LABELS, caseReference, nextStep, questionText, useAuth, useCase, useI18n, type UiLanguage } from "./ui";


// ---- wire types ---------------------------------------------------------------------------------

interface Draft { title: string; account: string; source: "assistant" | "your_words" }
interface HelpBlock { type: "help"; id: string; titleEn: string; textEn: string; titleHi: string; textHi: string }
interface SuggestionBlock {
  type: "suggestion";
  kind: "register" | "add_to_existing" | "check_with_citizen" | "not_online" | "nothing_to_register";
  reason?: string;
  reasons: { slot: string; quote: string }[];
  draft: Draft;
  caseId?: string;
  caseRef?: string;
  reference?: string;
  demoPackId?: string | null;
}
interface StatusBlock {
  type: "status";
  caseId: string; caseRef: string; title: string; reviewState: string; packetStatus: string; sourceCount: number; reading: boolean;
  openQuestions: number; closureRequested: boolean; officerOutcome: string | null; officerNote: string | null;
  citizenReportedMinor: string | null; reviewedDebitMinor: string; recoveryState: string; matchedRefundMinor: string;
  outstandingMinor: string; awaitingReview: boolean; official: { rawStatus: string; category: string } | null; officialMessage: string; qualification: string;
}
/** The read-only lookups: what the assistant fetched from the citizen's own records, never its own wording. */
interface DetailsBlock {
  type: "details"; caseId: string; caseRef: string; title: string;
  payments: { amountMinor: string | null; to: string | null; date: string | null; reference: string | null; checked: boolean }[];
  refunds: { amountMinor: string | null }[];
  citizenReportedMinor: string | null; reviewedDebitMinor: string; awaitingReview: boolean; openQuestions: string[];
}
interface EvidenceBlock {
  type: "evidence"; caseRef: string; reading: boolean; paymentsRead: number;
  files: { name: string; kind: string; state: "waiting" | "reading" | "read" | "failed"; duplicate: boolean }[];
}
interface RecapBlock { type: "recap"; caseRef: string | null; notes: { slot: string; quote: string }[]; decisions: ChoiceId[]; leftForReview: number }
interface ComplaintsBlock { type: "complaints"; items: { caseId: string; caseRef: string; title: string; reviewState: string; current: boolean }[] }
interface NoteBlock { type: "note"; text: string }
type ChoiceId = "start_complaint" | "register_anyway" | "new_complaint" | "link_existing" | "confirm_payments" | "reject_payments"
  | "answer_question" | "refund_report" | "official_update" | "submit" | "close" | "decline"
  | "fix_amount" | "fix_date" | "fix_reference" | "fix_payee" | "fix_skip" | "apply_correction";
/** What the browser does when a quick reply is tapped. Only the fields that reply carries are present. */
interface ChoiceAction {
  draft?: Draft; demoPackId?: string | null; caseId?: string; caseRef?: string | null; paymentIds?: string[];
  questionId?: string; questionText?: string; text?: string; amountText?: string | null; category?: string;
  paymentId?: string; field?: string;
}
/** A quick reply. `label` is what the citizen's tap says in the chat; `auto` means the reply is the answer to the
 *  question just asked, so it is saved without a tap. */
interface ChoiceItem { id: ChoiceId; label: string; action?: ChoiceAction; auto?: boolean }
interface ChoicesBlock { type: "choices"; items: ChoiceItem[] }
interface QuestionBlock { type: "question"; id: string; kind: string; text: string }
interface LinkBlock { type: "link"; kind: "receipt"; caseId: string }
type Block = HelpBlock | SuggestionBlock | StatusBlock | ChoicesBlock | QuestionBlock | LinkBlock
  | DetailsBlock | EvidenceBlock | RecapBlock | ComplaintsBlock | NoteBlock;

interface MessageOut { id: string; ordinal: number; role: "citizen" | "assistant" | "event"; text: string; blocks: Block[]; meta: Record<string, unknown>; createdAt: string }
interface ConversationOut {
  id: string; caseId: string | null; caseRef: string | null; language: Language; aiMode: "live" | "fixture"; publicFixturesOnly: boolean;
  messages: MessageOut[]; suggestedReplies: string[]; canType: boolean; messagesLeft: number;
  stage: "reading" | "thinking" | "checking" | null;   // where a reply being written has got to
}
interface ConversationSummary { id: string; caseId: string | null; caseRef: string | null; updatedAt: string; lastText: string }
type EventKind = "complaint_started" | "linked_existing" | "attachment_read" | "details_confirmed" | "details_rejected"
  | "field_chosen" | "correction_applied" | "answer_saved" | "refund_reported" | "official_update_saved" | "submitted" | "closure_requested" | "declined";

const assistantApi = {
  start: (payload: { caseId?: string; language?: Language }) => request<ConversationOut>("POST", "/api/assistant/conversations", { json: payload }),
  get: (id: string) => request<ConversationOut>("GET", `/api/assistant/conversations/${id}`),
  list: () => request<ConversationSummary[]>("GET", "/api/assistant/conversations"),
  say: (id: string, text: string, chip: boolean) => request<ConversationOut>("POST", `/api/assistant/conversations/${id}/messages`, { json: { text, chip } }),
  link: (id: string, caseId: string) => request<ConversationOut>("POST", `/api/assistant/conversations/${id}/link`, { json: { caseId } }),
  event: (id: string, body: { kind: EventKind; choice?: ChoiceId; detail?: string; messageId?: string }) =>
    request<ConversationOut>("POST", `/api/assistant/conversations/${id}/events`, { json: body }),
  voice: (id: string, wav: Blob) => {
    const form = new FormData();
    form.append("file", wav, "voice.wav");
    return request<{ text: string; aiMode: string; masked: string[] }>("POST", `/api/assistant/conversations/${id}/voice`, { form });
  },
};


// ---- fixed wording ------------------------------------------------------------------------------

// Older conversations hold one-line event marks. New ones say it in a message from the assistant instead.
const EVENT_LABELS: Record<string, string> = {
  complaint_started: "Complaint started",
  linked_existing: "Added to your existing complaint",
  attachment_added: "Screenshot added",
  draft_update_started: "Reading what you added",
  details_confirmed: "Details confirmed by you",
  answer_saved: "Answer saved",
  refund_reported: "Refund report added",
  official_update_saved: "Official update recorded as supplied by you",
  submitted: "Sent for demo review",
  closure_requested: "Asked to close this complaint",
};
const REVIEW_STATE_WORDS: Record<string, string> = {
  private: "Not sent yet",
  submitted: "With an officer",
  returned: "More information asked for",
  reviewed: "Packet reviewed",
  closed: "Closed — CyberBrief stopped tracking this record",
};
const OUTCOME_WORDS: Record<string, string> = {
  packet_reviewed: "The officer recorded your packet as reviewed.",
  more_information_requested: "The officer asked for more information.",
  closure_agreed: "The officer agreed to close this CyberBrief record.",
};


// ---- the conversation's own behaviour -----------------------------------------------------------

/** One citizen message in flight. `text` is what they typed, before the server masks it; it is shown
 *  only until the saved list arrives, and is then destroyed rather than merged. */
/** The steps of a wait, shown as they really happen: from the server's own stage for a reply, and from the
 *  uploads and the run for an attachment. A step is never marked done before it is. */
interface Guide { steps: string[]; active: number; detail?: string }
const REPLY_STEPS = ["Reading your message", "Writing a reply", "Checking the reply"];
const REPLY_STAGES = ["reading", "thinking", "checking"];
const ATTACH_STEPS = ["Adding your screenshots", "Reading what is in them", "Working out what is still missing"];
const MAX_ATTACH = 5;
const RESUME_WINDOW_MS = 3 * 24 * 60 * 60 * 1000;   // a chat left alone for longer than this starts afresh
const MAX_DETAIL_CHARS = 180;

interface PendingTurn { key: string; text: string; chip: boolean; conversationId: string; afterCount: number; restore: boolean; typing?: string }

const choicesOf = (m: MessageOut): ChoiceItem[] => m.blocks.find((b): b is ChoicesBlock => b.type === "choices")?.items ?? [];

const NEAR_BOTTOM_PX = 120;
const COMPOSER_MAX_PX = 160;

/** A time of day for a message. Formatted data, not interface copy, so it goes through Intl. */
function clockTime(iso: string, language: UiLanguage): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { hour: "numeric", minute: "2-digit" }).format(at);
}

/** The server answers 200 with an ordinary message when the model could not be reached, and marks it.
 *  That mark is the only honest signal that nothing was actually answered. */
function replyFailed(m: MessageOut): boolean {
  return m.role === "assistant" && typeof m.meta.error === "string" && m.meta.error !== "";
}

const smoothly = (): ScrollBehavior => (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

/** The conversation's scroller: a box of its own on a laptop, the page itself on a phone. */
function scrollerFor(el: HTMLElement | null): HTMLElement | null {
  if (el && el.scrollHeight > el.clientHeight + 1) return el;
  return document.scrollingElement as HTMLElement | null;
}

/** Keeps the newest message in view, but only while the citizen is already there: reading something
 *  further up must never be interrupted. When it is, a button offers the way back down. */
function useChatScroll(tailCount: number, pendingKey: string | null) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);
  const pinned = useRef(true);
  const settling = useRef(0);
  const [away, setAway] = useState(false);

  const toBottom = useCallback((behavior: ScrollBehavior) => {
    const target = scrollerFor(scrollRef.current);
    if (!target) return;
    // A scroll we started is not the citizen scrolling away. Its own events, read half way through
    // the journey, would say "not near the bottom" and unpin the view it is in the middle of pinning.
    settling.current = Date.now() + (behavior === "smooth" ? 700 : 120);
    target.scrollTo({ top: target.scrollHeight, behavior });
    pinned.current = true;
    setAway(false);
  }, []);

  const read = useCallback(() => {
    const target = scrollerFor(scrollRef.current);
    if (!target || Date.now() < settling.current) return;
    pinned.current = target.scrollHeight - target.scrollTop - target.clientHeight <= NEAR_BOTTOM_PX;
    setAway(!pinned.current);
  }, []);

  useEffect(() => {
    window.addEventListener("scroll", read, { passive: true });
    return () => { window.removeEventListener("scroll", read); };
  }, [read]);

  // Following the conversation is instant: an animation under a view the citizen is already reading
  // is motion for its own sake. The one animated scroll is the one they ask for, below.
  useLayoutEffect(() => {
    if (tailCount === 0) return;
    if (pinned.current) toBottom("auto");
  }, [tailCount, toBottom]);

  useLayoutEffect(() => { if (pinned.current) toBottom("auto"); }, [pendingKey, toBottom]);

  // Anything inside that grows after it is laid out: the complaint panel filling in once the record
  // loads, the draft editor opening, a status card arriving. Watching the whole inner column rather
  // than the list alone, because content above the messages pushes them out of view just as surely.
  useEffect(() => {
    const inner = logRef.current;
    if (!inner || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const box = scrollRef.current;
      const active = document.activeElement;
      if (!pinned.current) return;
      // never pull the view away from a field they are typing in
      if (box && active instanceof HTMLElement && active !== box && box.contains(active)) return;
      toBottom("auto");
    });
    observer.observe(inner);
    return () => { observer.disconnect(); };
  }, [toBottom]);

  /** Follow the newest message again. Asking for something is asking to see the answer, so every
   *  tap that adds to the conversation calls this, wherever the citizen had scrolled to. */
  const stick = useCallback(() => { pinned.current = true; }, []);

  return { scrollRef, logRef, away, read, stick, jump: () => toBottom(smoothly()) };
}


// ---- small pieces -------------------------------------------------------------------------------

function HelpCard({ block }: { block: HelpBlock }) {
  const { language, t } = useI18n();
  const hindi = language === "hi";
  return <section className="chat-card help" aria-label={hindi ? block.titleHi : block.titleEn}>
    {/* said in words, not by a colour or an emblem: these are CyberBrief's own fixed sentences */}
    <p className="card-eyebrow"> {t("From CyberBrief's help")} </p>
    <h3 lang={hindi ? "hi" : "en"}>{hindi ? block.titleHi : block.titleEn}</h3>
    <p lang={hindi ? "hi" : "en"}>{hindi ? block.textHi : block.textEn}</p>
  </section>;
}

function StatusCard({ block }: { block: StatusBlock }) {
  const { t } = useI18n();
  return <section className="chat-card status" aria-label={t("Where your complaint stands")}>
    <h3> {t("Where your complaint stands")} </h3>
    <p className="small muted"><span className="caseref">{block.caseRef}</span> {t("A CyberBrief reference, not an official complaint number.")}</p>
    <dl className="kv">
      <dt> {t("Your complaint")} </dt><dd>{t(REVIEW_STATE_WORDS[block.reviewState] ?? block.reviewState)}</dd>
      {block.reading && <><dt> {t("Evidence")} </dt><dd>{t("Reading your evidence…")}</dd></>}
      <dt> {t("Open questions")} </dt><dd>{block.openQuestions}</dd>
      <dt> {t("You told us you lost")} </dt><dd><MoneyText minor={block.citizenReportedMinor} fallback={t("not stated")}/></dd>
      <dt> {t("Confirmed from receipts")} </dt><dd>{block.awaitingReview ? t("Not reviewed yet") : <MoneyText minor={block.reviewedDebitMinor}/>}</dd>
      <dt> {t("Money back, as reviewed")} </dt><dd>{t(RECOVERY_LABELS[block.recoveryState] ?? block.recoveryState)}</dd>
      <dt> {t("Official update")} </dt>
      <dd>{block.official ? `“${block.official.rawStatus}” · ${t(STATUS_CATEGORY_LABELS[block.official.category] ?? block.official.category)}` : t(block.officialMessage)}</dd>
    </dl>
    {block.officerOutcome && <div className="saidby answer"><span className="who"> {t("Officer's reply")} </span>
      {t(OUTCOME_WORDS[block.officerOutcome] ?? block.officerOutcome)}{block.officerNote ? ` “${block.officerNote}”` : ""}</div>}
    {block.closureRequested && <p className="small"> {t("You asked for this complaint to be closed. An officer has not answered yet.")} </p>}
    <p className="small muted"> {t(block.qualification)} </p>
  </section>;
}

const SOURCE_STATE_WORDS: Record<EvidenceBlock["files"][number]["state"], string> = {
  waiting: "Waiting to be read", reading: "Reading now", read: "Read", failed: "Could not be read",
};
const RECAP_SLOT_WORDS: Record<string, string> = {
  money_paid: "Whether you paid", payment_method: "How you paid", what_happened: "What happened", when: "When", believes_scam: "What you think happened",
};
const DECISION_WORDS: Record<string, string> = {
  start_complaint: "You asked me to start your complaint.", register_anyway: "You asked me to register it.",
  link_existing: "You added this to a complaint you already had.", confirm_payments: "You confirmed the payments I read.",
  reject_payments: "You said a payment I read was not right.", answer_question: "You answered a question about your complaint.",
  refund_report: "You told me about money that came back.", official_update: "You gave me an official update.",
  submit: "You sent your complaint for review.", close: "You asked for your complaint to be closed.",
};

function DetailsCard({ block }: { block: DetailsBlock }) {
  const { t } = useI18n();
  return <section className="chat-card details" aria-label={t("What is in your complaint")}>
    <p className="card-eyebrow"> {t("From your complaint record")} </p>
    <h3> {t("What is in your complaint")} </h3>
    <p className="small muted"><span className="caseref">{block.caseRef}</span> {block.title}</p>
    {block.payments.length === 0 ? <p> {t("No payments read yet.")} </p> : <ul className="record-list">
      {block.payments.map((p, i) => <li key={i}>
        <span className="record-main">
          <MoneyText minor={p.amountMinor} fallback={t("Amount not readable")}/>{p.to ? ` ${t("to {name}", { name: p.to })}` : ""}
        </span>
        {(p.date || p.reference) && <span className="small muted">
          {[p.date, p.reference ? t("reference {ref}", { ref: p.reference }) : null].filter(Boolean).join(" · ")}
        </span>}
        <span className={`badge ${p.checked ? "badge-user_confirmed" : "badge-unreviewed"}`}> {t(p.checked ? "Checked" : "Needs your check")} </span>
      </li>)}
    </ul>}
    {block.refunds.length > 0 && <dl className="kv"><dt> {t("Refunds noted")} </dt>
      <dd>{block.refunds.map((r, i) => <span key={i}>{i > 0 ? ", " : ""}<MoneyText minor={r.amountMinor} fallback={t("Amount not readable")}/></span>)}</dd></dl>}
    <dl className="kv">
      <dt> {t("You told us you lost")} </dt><dd><MoneyText minor={block.citizenReportedMinor} fallback={t("not stated")}/></dd>
      <dt> {t("Confirmed from receipts")} </dt><dd>{block.awaitingReview ? t("Not reviewed yet") : <MoneyText minor={block.reviewedDebitMinor}/>}</dd>
    </dl>
    {block.openQuestions.length > 0 && <div><p className="small muted"> {t("Still to answer")} </p>
      <ul className="plain-list">{block.openQuestions.map((q, i) => <li key={i}>{q}</li>)}</ul></div>}
  </section>;
}

function EvidenceCard({ block }: { block: EvidenceBlock }) {
  const { t } = useI18n();
  return <section className="chat-card evidence" aria-label={t("Your files and what was read from them")}>
    <p className="card-eyebrow"> {t("From your complaint record")} </p>
    <h3> {t("Your files and what was read from them")} </h3>
    {block.files.length === 0 ? <p> {t("No files added yet.")} </p> : <ul className="record-list">
      {block.files.map((f, i) => <li key={i}>
        <span className="record-main">{f.name}</span>
        <span className="small muted">{t(SOURCE_STATE_WORDS[f.state])}{f.duplicate ? ` · ${t("Same file as one already added")}` : ""}</span>
      </li>)}
    </ul>}
    {block.reading && <p className="small"> {t("Reading your evidence…")} </p>}
    <p className="small muted">{block.paymentsRead === 1 ? t("1 payment read from your files") : t("{n} payments read from your files", { n: block.paymentsRead })}</p>
  </section>;
}

function RecapCard({ block }: { block: RecapBlock }) {
  const { t } = useI18n();
  const empty = block.notes.length === 0 && block.decisions.length === 0 && block.leftForReview === 0;
  return <section className="chat-card recap" aria-label={t("What I have noted from our chat")}>
    <p className="card-eyebrow"> {t("From our chat, in your words")} </p>
    <h3> {t("What I have noted from our chat")} </h3>
    {empty && <p> {t("I have not noted anything from this chat yet.")} </p>}
    {block.notes.length > 0 && <dl className="kv">
      {block.notes.map((n) => <span key={n.slot} className="kv-row"><dt> {t(RECAP_SLOT_WORDS[n.slot] ?? n.slot)} </dt><dd>“{n.quote}”</dd></span>)}
    </dl>}
    {(block.decisions.length > 0 || block.leftForReview > 0) && <ul className="plain-list">
      {block.decisions.map((d) => <li key={d}>{t(DECISION_WORDS[d] ?? d)}</li>)}
      {block.leftForReview > 0 && <li>{block.leftForReview === 1 ? t("1 payment is left for review") : t("{n} payments are left for review", { n: block.leftForReview })}</li>}
    </ul>}
  </section>;
}

function ComplaintsCard({ block }: { block: ComplaintsBlock }) {
  const { t } = useI18n();
  return <section className="chat-card complaints" aria-label={t("Your complaints")}>
    <p className="card-eyebrow"> {t("From your complaint record")} </p>
    <h3> {t("Your complaints")} </h3>
    {block.items.length === 0 ? <p> {t("You have no complaints yet.")} </p> : <ul className="record-list">
      {block.items.map((c) => <li key={c.caseId}>
        {/* a new tab, so looking at another complaint never costs the conversation */}
        <span className="record-main"><span className="caseref">{c.caseRef}</span> <a href={`/cases/${c.caseId}`} target="_blank" rel="noreferrer">{c.title} ↗</a></span>
        <span className="small muted">{t(REVIEW_STATE_WORDS[c.reviewState] ?? c.reviewState)}</span>
        {c.current && <span className="badge badge-case-draft"> {t("This chat")} </span>}
      </li>)}
    </ul>}
  </section>;
}

function GuideSteps({ guide }: { guide: Guide }) {
  const { t } = useI18n();
  return <ol className="guide" aria-label={t("Progress")}>
    {guide.steps.map((step, i) => {
      const state = i < guide.active ? "done" : i === guide.active ? "active" : "todo";
      // a mark and a word, never colour alone
      return <li key={step} className={state} aria-current={state === "active" ? "step" : undefined}>
        <span className="guide-mark" aria-hidden="true">{state === "done" ? "\u2713" : state === "active" ? "\u2022" : "\u25CB"}</span>
        {t(step)}{state === "active" && guide.detail ? ` (${guide.detail})` : ""}
        {state === "done" && <span className="visually-hidden"> {t("done")} </span>}
      </li>;
    })}
  </ol>;
}

function SuggestionCard({ block, publicMode }: { block: SuggestionBlock; publicMode: boolean }) {
  const { t } = useI18n();
  const lead: Record<SuggestionBlock["kind"], string> = {
    register: "It sounds like you should register a complaint. I can draft it from what you've told me.",
    add_to_existing: "This looks like the payment in {ref}. Add it there instead?",
    check_with_citizen: block.reason === "late_from_known_store"
      ? "You said the order is late from a store you know. CyberBrief is for payment scams. If you think this was one, I can register it."
      : "From what you've said, I'm not sure this was a payment scam. If you think it was one, I can register it.",
    not_online: "CyberBrief records online payments such as UPI, cards, net banking or wallets. A cash payment can't be recorded here.",
    nothing_to_register: "CyberBrief complaints are about payments you made. Since nothing was paid, there's nothing to register.",
  };
  // what would be filed, shown so that saying yes is an informed yes; changing it is just more talking
  const filing = !publicMode && (block.kind === "register" || block.kind === "check_with_citizen" || block.kind === "add_to_existing");
  return <section className="chat-card suggestion" aria-label={t("Suggestion")}>
    <p><strong>{t(lead[block.kind], { ref: block.caseRef ?? "" })}</strong></p>
    {block.reasons.length > 0 && <div className="reasons"><p className="small muted"> {t("Why I suggest this")} </p>
      {block.reasons.map((r) => <p key={r.slot} className="reason-quote">{t("You said: “{quote}”", { quote: r.quote })}</p>)}</div>}
    {filing && <div className="draft-preview">
      <p className="small muted"> {t(block.draft.source === "assistant" ? "What I would file, drafted from our chat" : "What I would file, in your own words")} </p>
      <p className="draft-title">{block.draft.title}</p>
      <p className="draft-account">{block.draft.account}</p>
    </div>}
  </section>;
}

/** The complaint at a glance, above the conversation. Nothing to press: everything is done by talking. */
function CaseStrip({ snapshot }: { snapshot: CaseSnapshot }) {
  const { t } = useI18n();
  const c = snapshot.case;
  const step = nextStep(c);
  return <section className="case-strip" aria-label={t("Your complaint")}>
    <span className="caseref">{caseReference(c.id)}</span>
    <span className="case-title">{c.title}</span>
    <span className={`badge badge-case-${step.tone}`}> {t(step.text)} </span>
    {/* a new tab, so looking at the whole complaint never costs the conversation */}
    <a className="case-open" href={`/cases/${c.id}`} target="_blank" rel="noreferrer"> {t("Open the full complaint")} ↗</a>
  </section>;
}

function readableConfirmations(payment: PaymentOut): ReviewAction[] {
  const facts: FactOut[] = Object.values(payment.fields);
  const actions: ReviewAction[] = facts
    .filter((f) => f.value !== null && (f.reviewState === "unreviewed" || f.reviewState === "unresolved"))
    .map((f) => ({ type: "confirm", targetType: "fact", targetId: f.factId }));
  if (payment.identityReviewState === "unreviewed") actions.push({ type: "confirm", targetType: "payment", targetId: payment.id });
  return actions;
}

// ---- icons: one outline family, drawn here so there is nothing to download ------------------------

const ICON = { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;
const AssistantIcon = () => <svg {...ICON}><path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-6l-4 3.5V17H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/></svg>;
const PersonIcon = () => <svg {...ICON}><circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c.8-3.6 3.6-5.5 7-5.5s6.2 1.9 7 5.5"/></svg>;
const PaperclipIcon = () => <svg {...ICON}><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7.1-7.1l9-9a3.3 3.3 0 0 1 4.7 4.7l-9 9a1.7 1.7 0 0 1-2.4-2.4l8.2-8.2"/></svg>;
/** Who is speaking, drawn. Never an emblem: no shield, seal or badge that could read as an official voice. */
const Avatar = ({ role, ghost }: { role: "citizen" | "assistant"; ghost?: boolean }) =>
  <span className={`avatar ${role}${ghost ? " ghost" : ""}`} aria-hidden="true">{role === "citizen" ? <PersonIcon/> : <AssistantIcon/>}</span>;


// ---- voice --------------------------------------------------------------------------------------

function VoiceButton({ disabled, onHeard }: { disabled: boolean; onHeard: (wav: Blob) => Promise<void> }) {
  const { t } = useI18n();
  const recorder = useRecorder(120);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (recorder.state !== "stopped" || !recorder.blob) return;
    const blob = recorder.blob;
    recorder.reset();
    setBusy(true);
    void blobToWav16k(blob).then(({ wav }) => onHeard(wav)).finally(() => setBusy(false));
  }, [recorder, onHeard]);
  if (recorder.state === "unsupported") return null;
  if (recorder.state === "recording")
    // The seconds are kept out of the button's name on purpose: a name that changes four times a
    // second is announced four times a second. They are decoration beside a name that stands still.
    return <button type="button" className="secondary voicebutton" aria-pressed={true} onClick={recorder.stop}>
      {t("Stop recording")}<span className="secs" aria-hidden="true"> {t("({n}s)", { n: recorder.seconds })}</span>
    </button>;
  return <button type="button" className="secondary voicebutton" aria-pressed={false}
                 disabled={disabled || busy} onClick={() => void recorder.start()}>
    {t(busy ? "Checking what you said…" : "Speak")}
  </button>;
}


// ---- the page -----------------------------------------------------------------------------------

export function AssistantPage() {
  const { language: uiLanguage, t } = useI18n();
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const [conversation, setConversation] = useState<ConversationOut | null>(null);
  const [text, setText] = useState("");
  const [heard, setHeard] = useState(false);
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const [guide, setGuide] = useState<Guide | null>(null);
  const [acting, setActing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoFailed, setAutoFailed] = useState<string | null>(null);   // a reply whose answer could not be saved by itself
  const starting = useRef(false);
  // The conversation this page has loaded or started. Putting its id in the address re-runs the
  // loading effect; without this a second fetch could land after a reply and overwrite it.
  const loadedId = useRef<string | null>(null);
  const inFlight = useRef(false);                                    // two taps on Enter are one message
  const lastSent = useRef<{ id: string | null; text: string }>({ id: null, text: "" });
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const chipFocus = useRef(false);
  const { snapshot, reload, setSnapshot } = useCase(conversation?.caseId ?? undefined);
  const publicMode = Boolean(session?.publicFixturesOnly || conversation?.publicFixturesOnly);
  const busy = pending !== null || acting;
  // The echo stands only until the saved list arrives. That list grows by at least two messages in
  // the same commit, so the count alone says whether the reply has landed -- there is no frame in
  // which the typed words and the masked ones are both on screen.
  const showPending = pending !== null && conversation !== null
    && conversation.id === pending.conversationId && conversation.messages.length === pending.afterCount;
  const chat = useChatScroll(conversation?.messages.length ?? 0, showPending ? pending.key : null);
  // A laptop gives the conversation its own scroller; see `body.chat-mode` in styles.css.
  useEffect(() => {
    document.body.classList.add("chat-mode");
    return () => { document.body.classList.remove("chat-mode"); };
  }, []);

  const fail = useCallback((e: unknown, fallback: string) => {
    setError(e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : e instanceof Error ? e.message : fallback);
  }, []);

  useEffect(() => {
    const existing = params.get("c");
    if (existing) {
      if (loadedId.current === existing) return;
      loadedId.current = existing;
      assistantApi.get(existing)
        .then((fetched) => setConversation((current) => (current && current.id === fetched.id ? current : fetched)))
        .catch((e) => {
          // a link to a chat this account does not have (an old tab, another sign-in) starts a fresh one instead of dead-ending
          if (e instanceof ApiError && e.status === 404) { loadedId.current = null; setParams(params.get("case") ? { case: params.get("case") as string } : {}, { replace: true }); return; }
          fail(e, "Could not open the conversation");
        });
      return;
    }
    if (starting.current) return;
    starting.current = true;
    const caseId = params.get("case") ?? undefined;
    const open = (c: ConversationOut) => { loadedId.current = c.id; setConversation(c); setParams({ c: c.id }, { replace: true }); };
    const begin = async () => {
      // A reload or a return visit picks the conversation up where it was left, so nothing said is lost.
      // "Start a new chat" asks for a fresh one, and the public demo always starts clean.
      if (params.get("new") !== "1" && !publicMode) {
        const recent = (await assistantApi.list()).find((c) => (caseId ? c.caseId === caseId : true) && Date.now() - Date.parse(c.updatedAt) < RESUME_WINDOW_MS);
        if (recent) {
          const resumed = await assistantApi.get(recent.id);
          if (resumed.canType) { open(resumed); return; }
        }
      }
      open(await assistantApi.start({ caseId, language: uiLanguage }));
    };
    begin().catch((e) => fail(e, "Could not start the assistant")).finally(() => { starting.current = false; });
  }, [params, setParams, uiLanguage, fail, publicMode]);

  function newChat() {
    if (busy) return;
    loadedId.current = null;
    setConversation(null);
    setText("");
    setError(null);
    setAutoFailed(null);
    const caseId = params.get("case");
    setParams(caseId ? { case: caseId, new: "1" } : { new: "1" });
  }

  // The box empties and the words appear on the tap, not when the reply lands ten seconds later.
  // The server masks what it stores, so the saved message replaces this one rather than joining it.
  async function send(raw: string, options: { chip?: boolean; restore?: boolean } = {}) {
    const message = raw.trim();
    if (!conversation || !message || inFlight.current) return;
    const turn: PendingTurn = {
      key: newIdempotencyKey(), text: message, chip: options.chip ?? false,
      conversationId: conversation.id, afterCount: conversation.messages.length, restore: options.restore ?? true,
    };
    inFlight.current = true;
    chipFocus.current = turn.chip;
    chat.stick();
    setPending(turn);
    setText("");
    setHeard(false);
    setError(null);
    setGuide({ steps: REPLY_STEPS, active: 0 });
    // Only where the reply has got to is read from this; the messages come back with the reply itself.
    const watch = window.setInterval(() => {
      void assistantApi.get(turn.conversationId).then((fresh) => {
        const at = fresh.stage ? REPLY_STAGES.indexOf(fresh.stage) : -1;
        if (at >= 0) setGuide((g) => (g && g.steps === REPLY_STEPS && at > g.active ? { ...g, active: at } : g));
      }).catch(() => undefined);
    }, 1200);
    try {
      const next = await assistantApi.say(turn.conversationId, message, turn.chip);
      window.clearInterval(watch);
      setConversation(next);
      setPending(null);
      setGuide(null);
      // Released here, with the controls, and not in `finally`: the refresh below can still be in
      // the air when the reply is already on screen, and a message tapped in that gap must not
      // vanish into a guard that is still held.
      inFlight.current = false;
      const saved = [...next.messages].reverse().find((m) => m.role === "citizen");
      lastSent.current = { id: saved?.id ?? null, text: message };   // their own words, by the id they were saved under
      if (next.caseId) await reload();
      // The assistant had just asked a question and this was the answer: save it as the answer, no tap.
      const last = next.messages[next.messages.length - 1];
      const auto = last && last.role === "assistant" && last.meta.autoDone !== true ? choicesOf(last).find((i) => i.auto) : undefined;
      if (auto && next.caseId) await (auto.id === "apply_correction" ? applyCorrection(auto, last.id, next) : saveAnswer(auto, last.id, next));
    } catch (e) {
      setPending(null);
      if (turn.restore) setText((live) => (live.trim() ? live : turn.text));   // nothing typed since: give the words back
      fail(e, "Could not send your message");
    } finally {
      window.clearInterval(watch);
      setGuide(null);
      inFlight.current = false;
    }
  }

  /** Ask again after a reply that never arrived. The server already stored the question, so this is
   *  a new message rather than a repair -- the chat shows it asked twice, which is what happened. */
  async function retry(failed: MessageOut) {
    if (!conversation || busy) return;
    const index = conversation.messages.findIndex((m) => m.id === failed.id);
    if (index < 1) return;
    const asked = conversation.messages.slice(0, index).reverse().find((m) => m.role === "citizen");
    if (!asked) return;
    const mine = lastSent.current;
    await send(mine.id === asked.id && mine.text ? mine.text : asked.text, { chip: asked.meta.chip === true, restore: false });
  }

  async function submitTyped(event: FormEvent) {
    event.preventDefault();
    await send(text);
  }

  // Keyed on the text, so the box also shrinks back after a send and grows for a voice transcript.
  useLayoutEffect(() => {
    const el = composerRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, COMPOSER_MAX_PX)}px`;
    el.style.overflowY = el.scrollHeight > COMPOSER_MAX_PX ? "auto" : "hidden";
  }, [text]);

  // A phone keyboard has no Shift+Enter, so there Enter stays a newline and the button sends.
  const enterSends = useMemo(() => !window.matchMedia("(pointer: coarse)").matches, []);
  function onComposerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.nativeEvent.isComposing || !enterSends) return;   // an IME uses Enter to settle a word
    event.preventDefault();
    if (!busy && text.trim()) void send(text);
  }

  // Never steal focus: only put it back when a tapped chip unmounted and left it nowhere.
  useEffect(() => {
    if (pending || !chipFocus.current) return;
    chipFocus.current = false;
    if (document.activeElement && document.activeElement !== document.body) return;
    composerRef.current?.focus({ preventScroll: true });
  }, [pending]);

  const onHeard = useCallback(async (wav: Blob) => {
    if (!conversation) return;
    try {
      const result = await assistantApi.voice(conversation.id, wav);
      setText(result.text);
      setHeard(true);
    } catch (e) {
      fail(e, "The recording could not be transcribed");
    }
  }, [conversation, fail]);

  /** Do what the citizen chose through the ordinary API, then report it so the chat can say what comes next.
   *  The tap is their message and shows at once; the assistant's follow-up replaces the waiting bubble. */
  async function act(kind: EventKind, perform: (caseId: string | null, progress: (active: number, detail?: string) => void) => Promise<string | void>,
                     opts: { choice?: ChoiceItem; detail?: string; eventDetail?: string; messageId?: string; typing?: string; guide?: string[]; base?: ConversationOut } = {}): Promise<boolean> {
    const base = opts.base ?? conversation;
    if (!base || inFlight.current) return false;
    inFlight.current = true;
    chat.stick();
    setError(null);
    setActing(true);
    setGuide(opts.guide ? { steps: opts.guide, active: 0 } : null);
    const progress = (active: number, detail?: string) => setGuide((g) => (g ? { ...g, active, detail } : g));
    setPending({
      key: newIdempotencyKey(), text: opts.choice ? opts.choice.label : opts.detail ? `\u{1F4CE} ${opts.detail}` : "",
      chip: true, conversationId: base.id, afterCount: base.messages.length, restore: false, typing: opts.typing,
    });
    try {
      const linkedTo = await perform(base.caseId, progress);
      let next = base;
      if (typeof linkedTo === "string" && linkedTo !== base.caseId) next = await assistantApi.link(base.id, linkedTo);
      if (opts.guide) progress(opts.guide.length - 1);   // what is left to say is written from the record, next
      next = await assistantApi.event(next.id, { kind, choice: opts.choice?.id, detail: opts.eventDetail ?? opts.detail, messageId: opts.messageId });
      setConversation(next);
      setPending(null);
      if (next.caseId) setSnapshot(await api.getCase(next.caseId));
      return true;
    } catch (e) {
      setPending(null);
      fail(e, "That did not work");
      if (base.caseId) await reload();
      return false;
    } finally {
      setActing(false);
      setGuide(null);
      inFlight.current = false;
    }
  }

  async function waitForRun(caseId: string, runId: string) {
    for (let i = 0; i < 80; i++) {
      const run = await api.getRun(caseId, runId);
      if (TERMINAL_RUN_STATES.has(run.status)) return;
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
  }

  // Every call below asks for the record as it is now, not as it was when the page last drew: a tap
  // can follow a read that finished a moment ago, and a stale revision is refused.
  const freshRevision = async (caseId: string) => (await api.getCase(caseId)).case.currentRevision;

  /** Files attached in the chat are added, read together, and what was read is asked about: no further taps. */
  const attach = (files: File[]) => {
    const names = files.map((f) => f.name).join(", ");
    return act("attachment_read", async (existing, progress) => {
      // A screenshot may be the easiest way to explain what happened, so it can come first: a private draft holds it.
      let caseId = existing;
      if (!caseId) caseId = (await api.createCase({ title: "Draft from a screenshot", language: conversation?.language ?? "en", incidentAccount: "" })).case.id;
      for (const [i, file] of files.entries()) {
        progress(0, files.length > 1 ? t("{n} of {total}", { n: i + 1, total: files.length }) : undefined);
        await api.uploadImage(caseId, file, newIdempotencyKey(), await freshRevision(caseId));
      }
      progress(1);
      const run = await api.createRun(caseId, { expectedRevision: await freshRevision(caseId) }, newIdempotencyKey());
      await waitForRun(caseId, run.id);
      return caseId;
    }, {
      detail: names.length <= MAX_DETAIL_CHARS ? names : t("{n} files", { n: files.length }),
      typing: files.length > 1 ? "Reading your screenshots…" : "Reading your screenshot…", guide: ATTACH_STEPS,
    });
  };

  async function saveAnswer(item: ChoiceItem, messageId?: string, base?: ConversationOut) {
    const a = item.action ?? {};
    const ok = await act("answer_saved", async (caseId) => {
      if (caseId && a.questionId && a.text) await api.answerQuestion(caseId, a.questionId, a.text, await freshRevision(caseId));
    }, { choice: messageId ? undefined : item, messageId, base });
    if (!ok && messageId) setAutoFailed(messageId);   // it did not save by itself: offer it as a reply instead
  }

  /** The citizen typed the right value for a field they said was wrong: change that one detail through the ordinary review route. */
  async function applyCorrection(item: ChoiceItem, messageId?: string, base?: ConversationOut) {
    const a = item.action ?? {};
    const factKey: Record<string, string> = { amount: "amount", date: "occurred_at", reference: "reference", payee: "counterparty" };
    const ok = await act("correction_applied", async (caseId) => {
      if (!caseId || !a.paymentId || !a.field || !a.text) return;
      const fresh = await api.getCase(caseId);
      const fact = fresh.payments.find((p) => p.id === a.paymentId)?.fields[factKey[a.field] ?? ""];
      if (!fact) throw new Error(t("That detail is not on this payment, so it cannot be changed here."));
      await api.review(caseId, fresh.case.currentRevision, [{ type: "correct", targetType: "fact", targetId: fact.factId, rawText: a.text }]);
    }, { choice: messageId ? undefined : item, messageId, base, eventDetail: a.text });
    if (!ok && messageId) setAutoFailed(messageId);
  }

  function runChoice(item: ChoiceItem) {
    const a = item.action ?? {};
    switch (item.id) {
      case "fix_amount": case "fix_date": case "fix_reference": case "fix_payee": case "fix_skip":
        return act("field_chosen", async () => undefined, { choice: item });
      case "apply_correction": return applyCorrection(item);
      case "start_complaint":
      case "register_anyway":
      case "new_complaint": {
        const pack = publicMode ? a.demoPackId ?? null : null;
        return act("complaint_started", async () => {
          const created = pack
            ? await api.loadDemoCase(pack)
            : await api.createCase({ title: (a.draft?.title ?? "").trim(), language: conversation?.language ?? "en", incidentAccount: (a.draft?.account ?? "").trim() });
          return created.case.id;
        }, { choice: item });
      }
      case "link_existing": return act("linked_existing", async () => a.caseId, { choice: item });
      case "confirm_payments": return act("details_confirmed", async (caseId) => {
        if (!caseId) return;
        const fresh = await api.getCase(caseId);
        const listed = new Set(a.paymentIds ?? []);
        const actions = fresh.payments.filter((p) => listed.has(p.id)).flatMap(readableConfirmations);
        if (actions.length) await api.review(caseId, fresh.case.currentRevision, actions);
      }, { choice: item });
      case "reject_payments": return act("details_rejected", async () => undefined, { choice: item });
      case "answer_question": return saveAnswer(item);
      case "refund_report": return act("refund_reported", async (caseId) => {
        if (caseId && a.text) await api.reportRefund(caseId, { text: a.text, amountRaw: a.amountText, kind: "unknown", expectedRevision: await freshRevision(caseId) }, newIdempotencyKey());
      }, { choice: item });
      case "official_update": return act("official_update_saved", async (caseId) => {
        if (caseId && a.text && a.category) await api.recordStatus(caseId, { rawStatus: a.text, category: a.category, expectedRevision: await freshRevision(caseId) });
      }, { choice: item });
      case "submit": return act("submitted", async (caseId) => { if (caseId) await api.submitForReview(caseId); }, { choice: item });
      case "close": return act("closure_requested", async (caseId) => {
        if (caseId && a.text) await api.prepareResolution(caseId, true, await freshRevision(caseId), a.text);
      }, { choice: item });
      case "decline": return act("declined", async () => undefined, { choice: item });
    }
  }

  function pickFile(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (files.length > MAX_ATTACH) { setError(t("Choose up to {n} screenshots at a time.", { n: MAX_ATTACH })); return; }
    if (files.length) void attach(files);
  }

  const linked = Boolean(conversation?.caseId);
  const lastMessage = conversation?.messages[conversation.messages.length - 1];
  const lastId = lastMessage?.id;
  const left = conversation?.messagesLeft ?? 0;
  // The assistant's last message decides what can be tapped. Once anything newer is said, the options are gone.
  const options = lastMessage && lastMessage.role === "assistant"
    ? choicesOf(lastMessage).filter((i) => !i.auto || autoFailed === lastMessage.id) : [];
  const canAttach = Boolean(conversation?.canType) && !publicMode;
  return <div className="assistant-page">
    <header className="assistant-head">
      <div className="head-row">
        <h1> {t("Talk it through")} </h1>
        {conversation && <span className={`badge ${conversation.aiMode === "fixture" ? "badge-fixture" : "badge-live"}`}>
          {t(conversation.aiMode === "fixture" ? "Replayed demo replies (not live)" : "Live Meta model")}</span>}
        {snapshot && <CaseStrip snapshot={snapshot}/>}
        {conversation && conversation.messages.some((m) => m.role === "citizen") &&
          <button type="button" className="secondary newchat" disabled={busy} onClick={newChat}> {t("Start a new chat")} </button>}
      </div>
      <p className="assistant-scope"> {t("CyberBrief assistant · helps with your CyberBrief complaint only")} </p>
      <p className="small muted"> {t("Answers come from your own complaint and CyberBrief's help. Nothing is saved or sent unless you ask for it or agree to it.")} </p>
    </header>

    <div className="chat-scroll" ref={chat.scrollRef} onScroll={chat.read}>
     <div className="chat-inner" ref={chat.logRef}>
      {!conversation && !error && <p className="muted"> {t("Loading…")} </p>}
      {conversation && <div className="chat-live" role="log" aria-label={t("Conversation")} aria-busy={showPending || undefined}>
        <ol className="chat-log">
          {conversation.messages.map((m, index) => {
            if (m.role === "event") return <li key={m.id} className="chat-event">{t(EVENT_LABELS[String(m.meta.kind)] ?? String(m.meta.kind))}</li>;
            const previous = conversation.messages[index - 1];
            const grouped = previous !== undefined && previous.role === m.role;
            const failed = replyFailed(m);
            return <li key={m.id} className={`chat-item ${m.role}${grouped ? " grouped" : ""}${failed ? " failed" : ""}`}>
              <Avatar role={m.role} ghost={grouped}/>
              <div className="chat-main">
                <div className="bubble">
                  {!grouped && <span className="who">
                    {t(m.role === "citizen" ? "You" : "Assistant")}{" · "}
                    <time dateTime={m.createdAt}>{clockTime(m.createdAt, uiLanguage)}</time>
                  </span>}
                  {m.text}
                  {failed && <span className="bubble-note"> {t("Not delivered — the assistant could not be reached.")} </span>}
                  {failed && m.id === lastId && <span className="retry">
                    <button type="button" className="secondary" disabled={busy} onClick={() => void retry(m)}> {t("Try again")} </button>
                  </span>}
                </div>
                {m.blocks.map((b, i) => {
                  const blockKey = `${m.id}#${i}`;
                  if (b.type === "help") return <HelpCard key={blockKey} block={b}/>;
                  if (b.type === "status") return <StatusCard key={blockKey} block={b}/>;
                  if (b.type === "suggestion") return <SuggestionCard key={blockKey} block={b} publicMode={publicMode}/>;
                  if (b.type === "details") return <DetailsCard key={blockKey} block={b}/>;
                  if (b.type === "evidence") return <EvidenceCard key={blockKey} block={b}/>;
                  if (b.type === "recap") return <RecapCard key={blockKey} block={b}/>;
                  if (b.type === "complaints") return <ComplaintsCard key={blockKey} block={b}/>;
                  if (b.type === "note") return <div key={blockKey} className="bubble question">{b.text}</div>;
                  if (b.type === "question") return <div key={blockKey} className="bubble question">{questionText(b, uiLanguage)}</div>;
                  if (b.type === "link") return <p key={blockKey} className="chat-links">
                    <a href={`/cases/${b.caseId}/receipt`} target="_blank" rel="noreferrer"> {t("View receipt")} ↗</a></p>;
                  return null;   // quick replies are shown beside the message box, only while they are the latest thing said
                })}
              </div>
            </li>;
          })}
          {showPending && pending && <>
            {pending.text && <li className="chat-item citizen pending">
              <Avatar role="citizen"/>
              <div className="chat-main"><div className="bubble"><span className="who">{t("You")}</span>{pending.text}</div></div>
            </li>}
            <li className="chat-item assistant pending">
              <Avatar role="assistant"/>
              <div className="chat-main"><div className="bubble">
                <span className="who">{t("Assistant")}</span>
                <span className="typing" aria-hidden="true"><i/><i/><i/></span>
                {t(pending.typing ?? "Assistant is replying…")}
                {guide && <GuideSteps guide={guide}/>}
              </div></div>
            </li>
          </>}
        </ol>
      </div>}
     </div>
    </div>

    <div className="composer">
      {chat.away && <button type="button" className="secondary jump-latest" onClick={chat.jump}> {t("Go to the latest message")} </button>}
      {error && <ErrorNotice message={error}/>}
      {conversation && <>
        {options.length > 0 && <div className="chips choices" role="group" aria-label={t("Your options")}>
          {options.map((item) => {
            const needsPack = publicMode && (item.id === "start_complaint" || item.id === "register_anyway" || item.id === "new_complaint") && !item.action?.demoPackId;
            return <button key={`${lastId}#${item.id}`} type="button" disabled={busy || needsPack}
                           data-state={showPending && pending?.text === item.label ? "sending" : undefined}
                           onClick={() => void runChoice(item)}>{item.label}</button>;
          })}
        </div>}
        {conversation.suggestedReplies.length > 0 && <div className="chips" role="group" aria-label={t("Suggested replies")}>
          {conversation.suggestedReplies.map((reply) => {
            // the name must not change -- only how it looks -- or nothing could find it by name again
            const tapped = showPending && pending?.chip === true && pending.text === reply.trim();
            return <button key={reply} type="button" disabled={busy} data-state={tapped ? "sending" : undefined}
                           onClick={() => void send(reply, { chip: true, restore: false })}>{reply}</button>;
          })}
        </div>}
        {conversation.canType ? <form onSubmit={submitTyped} className="composer-row" aria-label={t("Send a message")} aria-busy={showPending || undefined}>
          <label className="composer-field">
            <span className="visually-hidden"> {t("Your message")} </span>
            <textarea ref={composerRef} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onComposerKey}
                      rows={1} maxLength={2000} placeholder={t("Type here, in Hindi, English or both")}/>
          </label>
          {canAttach && <label className={`button secondary attachbutton${busy ? " disabled" : ""}`}
                               title={t(linked ? "Attach screenshots" : "Attach a screenshot: I will start a private draft to hold it")}>
            <PaperclipIcon/>
            <input type="file" multiple accept="image/png,image/jpeg,image/webp" onChange={pickFile} disabled={busy} aria-label={t("Choose a screenshot")}/>
          </label>}
          <VoiceButton disabled={busy} onHeard={onHeard}/>
          <button type="submit" disabled={busy || !text.trim()}> {t(showPending ? "Sending…" : "Send")} </button>
          {heard && <p className="small muted"> {t("I heard this — check it, then send.")} </p>}
        </form> : <p className="small muted"> {t(publicMode ? "The public demo accepts the suggested replies only." : "This conversation has reached its message limit.")} </p>}
        <p className="small muted"> {t("Do not type OTPs, PINs, passwords, card numbers, Aadhaar or PAN. Anything that looks like one is removed before it is saved.")} </p>
        {conversation.canType && left > 0 && left <= 5 && <p className="small muted" role="status">
          {left === 1 ? t("1 message left in this chat") : t("{n} messages left in this chat", { n: left })}
        </p>}
      </>}
    </div>
  </div>;
}
