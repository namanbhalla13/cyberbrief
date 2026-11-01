/** The HTTP client and every wire type it exchanges with the backend. */


// ---- from api/types ---------------------------------------------------------------------------

// Hand-maintained mirror of backend/app/schemas/api.py (camelCase on the wire).
// Money values are integer paise encoded as decimal strings; never do float arithmetic on them.

export type Role = "citizen" | "officer" | "admin";
export type Language = "en" | "hi" | "hinglish";
export type AiMode = "live" | "fixture";
export type ReviewState = "unreviewed" | "user_confirmed" | "reviewer_checked" | "unresolved" | "excluded";
/** "closed" ends CyberBrief's tracking of a record. It never means an official complaint closed. */
export type CaseReviewState = "private" | "submitted" | "returned" | "reviewed" | "closed";

export interface SessionOut {
  role: Role;
  label: string | null;
  demoMode: boolean;
  expiresAt: string;
  csrfToken: string;
  publicFixturesOnly: boolean;
  aiMode: AiMode;
  /** False for the anonymous demo path, which still works without signing in. */
  authenticated: boolean;
  accountKind: Role | null;
  mobileMasked: string | null;
  accountRef: string | null;
  officerId: string | null;
  displayName: string | null;
}

export interface OtpRequestOut {
  expiresAt: string;
  resendAfterSeconds: number;
  /** Shown only in demo mode, where no SMS is sent. Null in a real deployment. */
  demoCode: string | null;
}

export interface CaseSummary {
  id: string;
  title: string;
  language: Language;
  complaintReference: string | null;
  currentRevision: number;
  lastReviewedRevision: number;
  packetStatus: string;
  activeRunId: string | null;
  createdAt: string;
  updatedAt: string;
  sourceCount: number;
  demoPackId: string | null;
  demoStepsRemaining: number;
  reviewState: CaseReviewState;
  /** The citizen asked for this record to be closed and no officer has answered yet. */
  closureRequested: boolean;
  submittedAt: string | null;
  /** Headline figures from the last stamped revision. `null` means nothing has been stamped
   *  yet, which is not the same as zero and must never be rendered as a figure. */
  citizenReportedMinor: string | null;
  reviewedDebitMinor: string | null;
  openQuestionCount: number;
}

export interface DemoPackOut {
  packId: string;
  title: string;
  tags: string[];
  initialSources: number;
  stagedUpdates: number;
  hasAudio: boolean;
}

export interface SourceOut {
  id: string;
  kind: string;
  provenanceKind: string;
  mediaType: string;
  displayFilename: string;
  byteSize: number;
  sha256: string;
  status: string;
  receivedAt: string;
  addedInRevision: number;
  imageWidth: number | null;
  imageHeight: number | null;
  textContent: string | null;
  duplicateOfSourceId: string | null;
  hasThumbnail: boolean;
  audioDurationMs: number | null;
  audioSourceId: string | null;
  supersedesSourceId: string | null;
  supersededBySourceId: string | null;
  transcriptTurns: { turnId?: number; startMs?: number; endMs?: number; transcript?: string }[] | null;
  transcriptionMode: string | null;
}

export interface AnchorOut {
  sourceId: string;
  segmentId: string | null;
  quote: string | null;
  quoteVerified: boolean;
  imageRegion: { x: number; y: number; width: number; height: number } | null;
  audioRangeMs: { start: number; end: number } | null;
  notes: string[];
}

export interface FactOut {
  factId: string;
  version: number;
  kind: string;
  value: Record<string, unknown> | null;
  rawText: string | null;
  reviewState: ReviewState;
  issue: string | null;
  anchors: AnchorOut[];
  reviewedBy: string | null;
  reviewedAt: string | null;
  reason: string | null;
  supersedesVersion: number | null;
  sourceObservationId: string | null;
}

export interface PaymentOut {
  id: string;
  label: string;
  includedInIncident: boolean;
  identityReviewState: ReviewState;
  mergedIntoPaymentId: string | null;
  excludedReason: string | null;
  confirmedDistinctFrom: string[];
  category: string;
  categoryReason: string | null;
  fields: Record<string, FactOut>;
  sourceIds: string[];
  observationIds: string[];
}

export interface DuplicateCandidateOut {
  paymentIds: string[];
  basis: string;
  issues: string[];
}

export interface TotalsOut {
  reviewedDebitMinor: string;
  reviewedDebitIds: string[];
  pendingMinor: string;
  pendingIds: string[];
  unreviewedIds: string[];
  unresolvedIds: string[];
  duplicateCandidateIds: string[];
  conflictIds: string[];
  failedIds: string[];
  creditIds: string[];
  nonInrIds: string[];
  excluded: { paymentId: string; reason: string }[];
  mergedIds: string[];
  categories: Record<string, string>;
  citizenReportedMinor: string | null;
}

export interface RunOut {
  id: string;
  status: string;
  stage: string;
  baseRevision: number;
  inputSourceIds: string[];
  aiMode: AiMode;
  modelId: string;
  promptVersion: string;
  schemaVersion: string;
  attempts: number;
  usage: Record<string, unknown>;
  perSourceStatus: Record<string, { status: string; errorType?: string; message?: string; latencyMs?: number; observations?: number }>;
  errorType: string | null;
  errorMessage: string | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface RevisionOut {
  revision: number;
  kind: string;
  summary: string;
  actorRole: string;
  createdAt: string;
}

export interface ReviewEventOut {
  id: string;
  revision: number;
  actorRole: string;
  action: string;
  targetType: string;
  targetId: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

export interface QuestionOut {
  id: string;
  kind: string;
  text: string;
  priority: number;
  state: "open" | "answered" | "resolved" | "dismissed";
  relatedPaymentIds: string[];
  relatedFactIds: string[];
  relatedSourceIds: string[];
  answerSourceId: string | null;
  dismissReason: string | null;
  createdRevision: number;
  resolvedRevision: number | null;
  shown: boolean;
}

export interface RefundOut {
  id: string;
  label: string;
  kind: string;
  evidenceKind: "document" | "claim";
  relatesToIncident: "unconfirmed" | "confirmed" | "denied";
  identityReviewState: ReviewState;
  mergedIntoRefundId: string | null;
  excludedReason: string | null;
  confirmedDistinctFrom: string[];
  category: string;
  categoryReason: string | null;
  fields: Record<string, FactOut>;
  sourceIds: string[];
}

export interface AllocationOut {
  id: string;
  refundId: string;
  paymentId: string;
  amountMinor: string;
  state: string;
  reason: string | null;
  createdByRole: string;
  createdRevision: number;
  invalidatedRevision: number | null;
  counted: boolean;
  issue: string | null;
}

export interface RecoveryOut {
  state: string;
  reviewedDebitMinor: string;
  matchedRefundMinor: string;
  outstandingMinor: string;
  eligibleRefundMinor: string;
  unallocatedEligibleMinor: string;
  claimedRefundMinor: string;
  allocationIssues: Record<string, unknown>[];
  reasons: string[];
  citizenOutstandingMinor: string | null;
  citizenOutstandingText: string | null;
}

export interface OfficialStatusEventOut {
  id: string;
  rawStatus: string;
  category: string;
  issuerAsSupplied: string | null;
  sourceIds: string[];
  effectiveAt: Record<string, unknown> | null;
  receivedAt: string;
  enteredByRole: string;
  reviewState: ReviewState;
  origin: string;
  createdRevision: number;
}

export interface LatestStatusOut {
  event: OfficialStatusEventOut | null;
  undatedCount: number;
  conflict: boolean;
  qualification: string;
  message: string;
}

/** `closure_agreed` is only valid in answer to a summary that carries a `requestNote`. */
export type ReviewOutcome = "packet_reviewed" | "more_information_requested" | "closure_agreed";

export interface ResolutionOut {
  id: string;
  revision: number;
  kind: "full_recovery" | "partial_or_unresolved";
  recoveryState: string;
  summaryText: string;
  eligible: boolean;
  blockingReasons: string[];
  preparedByRole: string;
  /** The citizen's reason for asking that this record be closed; null for an ordinary summary. */
  requestNote: string | null;
  reviewedAt: string | null;
  reviewedByRole: string | null;
  reviewOutcome: ReviewOutcome | null;
  reviewNote: string | null;
  stale: boolean;
  createdAt: string;
}

export interface ChangeSummary {
  sinceRevision: number;
  currentRevision: number;
  sourcesAdded: { id: string; kind: string; displayFilename: string; revision: number }[];
  paymentsAdded: { id: string; label: string; revision: number }[];
  refundsAdded: { id: string; label: string; evidenceKind: string; revision: number }[];
  factsAdded: Record<string, unknown>[];
  factsChanged: { factId: string; kind: string; subjectType: string; subjectId: string; reviewState: string; reviewedBy: string | null; reason: string | null; rawText: string | null }[];
  newConflicts: { paymentIds: string[]; issues: string[] }[];
  allocationsAdded: { id: string; refundId: string; paymentId: string; amountMinor: string; state: string }[];
  allocationsInvalidated: { id: string; refundId: string; paymentId: string; reason: string | null }[];
  questionsOpened: { id: string; kind: string; text: string }[];
  questionsResolved: { id: string; kind: string; state: string }[];
  officialStatusAdded: { id: string; rawStatus: string; category: string; reviewState: string; revision: number }[];
  resolutions: { id: string; kind: string; eligible: boolean; reviewOutcome: string | null; revision: number }[];
  balancesThen: Record<string, string | null> | null;
  balancesNow: Record<string, string | null>;
  balanceChanges: { field: string; from: string | null; to: string | null }[];
  revisions: { revision: number; kind: string; summary: string; actorRole: string; createdAt: string }[];
}

export interface CaseSnapshot {
  case: CaseSummary;
  sources: SourceOut[];
  payments: PaymentOut[];
  totals: TotalsOut;
  duplicateCandidates: DuplicateCandidateOut[];
  caseFacts: Record<string, FactOut>;
  questions: QuestionOut[];
  refunds: RefundOut[];
  allocations: AllocationOut[];
  recovery: RecoveryOut;
  officialStatusEvents: OfficialStatusEventOut[];
  latestOfficialStatus: LatestStatusOut;
  resolutions: ResolutionOut[];
  runs: RunOut[];
  revisions: RevisionOut[];
  reviewEvents: ReviewEventOut[];
  aiModesUsed: string[];
}

export type ReviewActionType =
  | "confirm" | "correct" | "unresolved" | "exclude" | "include" | "merge" | "unmerge" | "distinct" | "relates" | "unrelated";

export interface ReviewAction {
  type: ReviewActionType;
  targetType: "fact" | "payment" | "refund" | "official_status";
  targetId: string;
  value?: Record<string, unknown> | null;
  rawText?: string | null;
  reason?: string | null;
  otherPaymentId?: string | null;
}

export interface ExportOut {
  id: string;
  revision: number;
  format: "html" | "json";
  contentDigest: string;
  aiMode: string;
  generatedAt: string;
  downloadPath: string;
}

export const TERMINAL_RUN_STATES = new Set(["proposed", "applied", "failed", "interrupted", "cancelled"]);
export const FIELD_ORDER = [
  "amount", "currency", "reference", "reference_type", "status", "direction", "occurred_at", "counterparty", "account_scope",
] as const;
export const CRITICAL_FIELDS = new Set(["amount", "currency", "reference", "direction", "status", "occurred_at"]);


// ---- from api/client --------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public nextStep: string | null = null,
    public details: unknown = null,
  ) {
    super(message);
  }
}

function readCookie(name: string): string | null {
  const match = document.cookie.split("; ").find((row) => row.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

interface RequestOptions {
  json?: unknown;
  form?: FormData;
  idempotencyKey?: string;
}

export async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers();
  if (method !== "GET") headers.set("X-CSRF-Token", readCookie("cb_csrf") ?? "");
  if (options.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = options.form;
  }
  const response = await fetch(path, { method, headers, body, credentials: "same-origin" });
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get("content-type") ?? "";
  const data: unknown = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const err = (typeof data === "object" && data !== null ? data : {}) as Record<string, unknown>;
    throw new ApiError(
      response.status,
      typeof err.error === "string" ? err.error : "http_error",
      typeof err.message === "string" ? err.message : response.statusText,
      typeof err.nextStep === "string" ? err.nextStep : null,
      err.details ?? null,
    );
  }
  return data as T;
}

/** The officer's review desk. A bank check here is against a simulated ledger and changes nothing in the case. */
export type SupportLevel = "bank_matched" | "bank_differs" | "bank_not_found" | "cannot_verify" | "citizen_confirmed" | "ai_proposed";
export interface BankField { field: string; claimed: string | null; bank: string | null; result: "match" | "differs" | "not_stated" }
export interface BankCheck {
  result: "matched" | "differs" | "not_found" | "cannot_verify"; reason: string; reference: string | null; fields: BankField[];
  ledgerVersion: string; checkedAt: string; revision: number; stale: boolean;
}
export interface PaymentSupport { paymentId: string; name: string; level: SupportLevel; label: string; reasons: string[]; bank: BankCheck | null }
export interface TimelineItem { at: string; kind: string; title: string; detail: string | null; level: SupportLevel | null; sourceIds: string[]; paymentId: string | null }
export interface AuditEvent {
  id: string; revision: number; kind: string; subjectType: string | null; subjectId: string | null; actorRole: string;
  detail: Record<string, unknown>; createdAt: string;
}
export interface Proposal {
  key: string; kind: "run_bank_check" | "request_more_information" | "record_packet_reviewed" | "agree_closure" | "wait";
  title: string; reason: string; paymentIds: string[]; noteDraft: string | null; origin: "model" | "rules"; decision: "accepted" | "dismissed" | null;
}
export interface NextActions {
  id: string; aiMode: "live" | "fixture"; modelId: string; generatedAt: string; revision: number; stale: boolean; notice: string | null; proposals: Proposal[];
}
export interface OfficerDesk {
  ledgerName: string; ledgerVersion: string; notice: string; payments: PaymentSupport[]; timeline: TimelineItem[]; audit: AuditEvent[]; actions: NextActions | null;
}

export const api = {
  startSession: () => request<SessionOut>("POST", "/api/session"),
  getSession: () => request<SessionOut>("GET", "/api/session"),

  // Sign-in. The one-time code is CyberBrief's own demo login: no SMS is sent, and it is never
  // a code from a bank or from the official cybercrime portal.
  requestOtp: (mobile: string) => request<OtpRequestOut>("POST", "/api/auth/otp/request", { json: { mobile } }),
  verifyOtp: (mobile: string, code: string) => request<SessionOut>("POST", "/api/auth/otp/verify", { json: { mobile, code } }),
  officerLogin: (officerId: string, password: string) =>
    request<SessionOut>("POST", "/api/auth/officer/login", { json: { officerId, password } }),
  adminLogin: (adminId: string, password: string) =>
    request<SessionOut>("POST", "/api/auth/admin/login", { json: { officerId: adminId, password } }),
  logout: () => request<void>("POST", "/api/auth/logout"),

  submitForReview: (caseId: string) => request<CaseSnapshot>("POST", `/api/cases/${caseId}/submit-review`),
  withdrawReview: (caseId: string) => request<CaseSnapshot>("POST", `/api/cases/${caseId}/withdraw-review`),
  officerQueue: () => request<CaseSummary[]>("GET", "/api/officer/cases"),
  officerCase: (caseId: string) => request<CaseSnapshot>("GET", `/api/officer/cases/${caseId}`),
  officerDesk: (caseId: string) => request<OfficerDesk>("GET", `/api/officer/cases/${caseId}/desk`),
  officerBankCheck: (caseId: string) => request<OfficerDesk>("POST", `/api/officer/cases/${caseId}/bank-check`),
  officerNextActions: (caseId: string) => request<OfficerDesk>("POST", `/api/officer/cases/${caseId}/next-actions`),
  officerActionDecision: (caseId: string, body: { proposalSetId: string; key: string; decision: "accepted" | "dismissed"; note?: string }) =>
    request<OfficerDesk>("POST", `/api/officer/cases/${caseId}/next-actions/decision`, { json: body }),
  officerReview: (caseId: string, resolutionId: string, outcome: ReviewOutcome, note: string | null, expectedRevision: number) =>
    request<CaseSnapshot>("POST", `/api/officer/cases/${caseId}/resolution-reviews`, {
      json: { action: "review", resolutionId, outcome, note, expectedRevision },
    }),

  listCases: () => request<CaseSummary[]>("GET", "/api/cases"),
  listDemoPacks: () => request<DemoPackOut[]>("GET", "/api/demo-cases"),
  loadDemoCase: (packId: string) => request<CaseSnapshot>("POST", `/api/demo-cases/${encodeURIComponent(packId)}`),
  nextDemoUpdate: (caseId: string) => request<CaseSnapshot>("POST", `/api/cases/${caseId}/demo-updates/next`),
  createCase: (payload: { title: string; language: Language; complaintReference?: string | null; incidentAccount?: string | null }) =>
    request<CaseSnapshot>("POST", "/api/cases", { json: payload }),
  getCase: (caseId: string) => request<CaseSnapshot>("GET", `/api/cases/${caseId}`),
  deleteCase: (caseId: string) => request<void>("DELETE", `/api/cases/${caseId}`),

  uploadImage: (caseId: string, file: File, idempotencyKey: string, expectedRevision?: number, kind: "image" | "supplied_official_update" = "image") => {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("kind", kind);
    if (expectedRevision !== undefined) form.append("expectedRevision", String(expectedRevision));
    return request<SourceOut>("POST", `/api/cases/${caseId}/sources`, { form, idempotencyKey });
  },
  uploadAudio: (caseId: string, wav: Blob, filename: string, idempotencyKey: string) => {
    const form = new FormData();
    form.append("file", wav, filename);
    form.append("kind", "audio");
    return request<SourceOut>("POST", `/api/cases/${caseId}/sources`, { form, idempotencyKey });
  },
  correctTranscript: (caseId: string, sourceId: string, text: string, expectedRevision: number) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/sources/${sourceId}/transcript`, { json: { text, expectedRevision } }),
  addText: (caseId: string, text: string, kind: "typed_account" | "user_answer", idempotencyKey: string) => {
    const form = new FormData();
    form.append("text", text);
    form.append("kind", kind);
    return request<SourceOut>("POST", `/api/cases/${caseId}/sources`, { form, idempotencyKey });
  },
  sourceUrl: (caseId: string, sourceId: string, variant: "original" | "thumbnail" = "original") =>
    `/api/cases/${caseId}/sources/${sourceId}${variant === "thumbnail" ? "?variant=thumbnail" : ""}`,

  createRun: (caseId: string, payload: { sourceIds?: string[]; retryRunId?: string; expectedRevision?: number }, idempotencyKey: string) =>
    request<RunOut>("POST", `/api/cases/${caseId}/runs`, { json: payload, idempotencyKey }),
  getRun: (caseId: string, runId: string) => request<RunOut>("GET", `/api/cases/${caseId}/runs/${runId}`),

  review: (caseId: string, expectedRevision: number, actions: ReviewAction[]) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/reviews`, { json: { expectedRevision, actions } }),

  createExport: (caseId: string, format: "html" | "json", expectedRevision: number, idempotencyKey: string) =>
    request<ExportOut>("POST", `/api/cases/${caseId}/exports`, { json: { format, expectedRevision }, idempotencyKey }),

  reportRefund: (caseId: string, payload: { text: string; amountRaw?: string | null; kind?: string; dateText?: string | null; expectedRevision: number }, idempotencyKey: string) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/refunds`, { json: payload, idempotencyKey }),
  allocate: (caseId: string, payload: { refundId: string; paymentId: string; amountMinor: string; reason?: string | null; expectedRevision: number }, idempotencyKey: string) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/refund-allocations`, { json: payload, idempotencyKey }),
  outstandingStatement: (caseId: string, payload: { text: string; outstandingMinor: string | null; expectedRevision: number }) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/recovery-statement`, { json: payload }),
  recordStatus: (caseId: string, payload: { rawStatus: string; category: string; issuerAsSupplied?: string | null; effectiveAtText?: string | null; sourceIds?: string[]; expectedRevision: number }) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/official-status-events`, { json: payload }),
  /** `note` turns an ordinary summary into a request that this CyberBrief record be closed. */
  prepareResolution: (caseId: string, allowPartial: boolean, expectedRevision: number, note?: string) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/resolution-reviews`, { json: { action: "prepare", allowPartial, note: note ?? null, expectedRevision } }),
  reviewResolution: (caseId: string, resolutionId: string, outcome: ReviewOutcome, note: string | null, expectedRevision: number) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/resolution-reviews`, { json: { action: "review", resolutionId, outcome, note, expectedRevision } }),
  changes: (caseId: string, since?: number) =>
    request<ChangeSummary>("GET", `/api/cases/${caseId}/changes${since === undefined ? "" : `?since=${since}`}`),
  acknowledgeUpdate: (caseId: string, expectedRevision: number) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/updates/acknowledge`, { json: { expectedRevision } }),

  answerQuestion: (caseId: string, questionId: string, text: string, expectedRevision: number) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/questions/${questionId}/answers`, { json: { text, expectedRevision } }),
  dismissQuestion: (caseId: string, questionId: string, reason: string, expectedRevision: number) =>
    request<CaseSnapshot>("POST", `/api/cases/${caseId}/questions/${questionId}/dismiss`, { json: { reason, expectedRevision } }),
};
