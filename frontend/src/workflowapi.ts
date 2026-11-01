/** Calls and shapes for the three-role workflow: the admin coordinator, the officer's case file, the citizen's progress. */
import { request } from "./api";

export interface WorkflowConfig { department: string; areas: { id: string; name: string }[]; routingPolicy: string }
export interface StaffMember {
  id: string; name: string; officerId: string; areas: string[]; languages: string[]; onDuty: boolean; capacity: number; active: number;
  eligible: boolean; reasons: string[]; unacknowledged?: number; areaDiscovery?: boolean;
}
export interface Proposal {
  id: string; caseId: string; caseRef: string; agent: string; kind: string; digest: string; baseRevision: number; origin: "model" | "rules";
  state: string; result: Record<string, unknown>; createdAt: string; title?: string;
  payload: Record<string, unknown> & {
    summary?: string; recommended?: { id: string; name: string; reasons: string[] }; alternatives?: { id: string; name: string; reasons: string[]; eligible: boolean }[];
    text?: string; body?: string; subject?: string; bankName?: string; to?: string; references?: string[]; paymentIds?: string[]; about?: string;
    attachments?: string[]; attachmentNames?: Record<string, string>; responseRef?: string; questions?: string[]; delivery?: "simulated" | "email" | "manual"; deliveryNote?: string | null; contactMissing?: boolean;
  };
  bankRequest?: BankRequestOut;
}
export interface CaseRow {
  id: string; caseRef: string; title: string; area: string | null; areaId: string | null; language: string; reviewState: string;
  workflowState: string | null; assignedTo: string | null; submittedAt: string | null; openTasks: string[];
}
export interface CasePage { items: CaseRow[]; total: number; page: number; pageSize: number; pages: number; asOf: string }
export interface Activity { caseId?: string | null; caseRef: string | null; agent: string; steps: string[]; outcome: string; at: string }
export interface EventHealth { pending: number; done: number; failed: number; recentFailures: { id: string; kind: string; error: string | null; attempts: number; caseId: string }[] }
export interface CaseDetail extends CaseRow {
  owner: string; blocker: string; nextAction: string; assignedAt: string | null; acknowledgedAt: string | null; proposal: Proposal | null;
  followUps: { task: string; dueAt: string | null }[]; activity: Activity[]; events: { kind: string; state: string; error: string | null; at: string }[];
}
export interface AdminOverview {
  events: EventHealth; summary: Record<"all" | "waiting" | "exception" | "assigned" | "under_review" | "closed", number>;
  asOf: string; intake: CaseRow[]; cases: CaseRow[]; officers: StaffMember[]; proposals: Proposal[]; activity: Activity[];
  followUps: { caseId: string; caseRef: string; task: string; dueAt: string | null }[];
}
export interface AskResult { title: string; columns: string[]; rows: string[][]; text: string; asOf: string; tool?: string; caseIds?: string[] }
export type Proposed = { proposal: Proposal } | { exception: string };

export interface CaseMessage {
  id: string; channel: "citizen" | "bank"; direction: "outgoing" | "incoming"; requestId: string | null; body: string; paymentIds: string[];
  sourceIds: string[]; data: Record<string, unknown>; state: string; simulated: boolean; createdAt: string;
  comparison?: { reference: string; verdict: string; bank: Record<string, string | null> }[] | null;
}
export interface CaseFile {
  caseRef: string; workflowState: string | null; area: string | null; assignedTo: string | null; acknowledged: boolean;
  checklist: { key: string; title: string; done: boolean }[]; tasks: { id: string; refId: string | null; kind: string; title: string; state: string; dueAt: string | null }[];
  messages: CaseMessage[]; drafts: Proposal[]; activity: Activity[];
}
export interface ClaimChange {
  kind: string; was: string | null; now: string; reviewState: string; confirmedByCitizen: boolean; conflict: boolean; sourceIds: string[]; revision: number;
  state?: "proposed" | "unclear" | "conflicting" | "reviewed"; sources?: { id: string; filename: string }[]; quote?: string | null;
}
export interface ClaimReview {
  why: string; next: { type: "ask_citizen" | "bank_request" | "reassess"; label: string; requires: string[]; topic: string | null };
}
export interface Claim {
  paymentId?: string; review?: ClaimReview | null;
  freshness: "not_assessed" | "current" | "needs_review"; changes: ClaimChange[]; newSources: { id: string; filename: string }[];
  key: string; claim: string; citizenSaid: string; supportedBy: string[]; support: { level: string; label: string }; contradictions: string[];
  assessment: { value: string; note: string | null; version: number; at: string; revision: number } | null;
}
export interface Related {
  id: string; state: string; signal: string; value: string; visible: boolean; caseRef: string | null; title: string | null; area: string | null;
  workflowState: string | null; hasBankReply: boolean | null; note: string;
}
export interface RelatedEvidence {
  caseRef: string; title: string; area: string | null; linkedBecause: string; note: string; simulated: boolean;
  sharedPayments: { name: string; amount: string; reference: string; status: string; date: string; sourceIds: string[] }[];
  sources: { id: string; filename: string; kind: string; receivedAt: string | null; url: string }[];
}
export interface Progress {
  step: string; responsible: string; nextAction: string; reviewer: string | null; area: string | null; areaName: string | null;
  workflowState: string | null; bankVerificationPending: boolean; assignmentPrepared: boolean; note: string | null;
  requests: (CaseMessage & { answered: boolean })[]; replies: CaseMessage[];
}

const post = <T>(path: string, json?: unknown) => request<T>("POST", path, json === undefined ? undefined : { json });

export interface WorkRow {
  id: string; caseRef: string; title: string; area: string | null; reviewState: string; filters: string[]; nextAction: string;
  dueAt: string | null; overdue: boolean; closureRequested: boolean; updatedAt: string;
}
export interface WorkList { counts: Record<string, number>; items: WorkRow[]; filters: Record<string, string>; total?: number; page?: number; pages?: number }

export const wf = {
  officerWork: (state: string, page = 1) => request<WorkList>("GET", `/api/officer/work?state=${encodeURIComponent(state)}&page=${page}&pageSize=10`),
  config: () => request<WorkflowConfig>("GET", "/api/workflow"),
  adminOverview: () => request<AdminOverview>("GET", "/api/admin/overview"),
  adminCases: (p: { query?: string; state?: string; page?: number; pageSize?: number }) => {
    const q = new URLSearchParams();
    if (p.query) q.set("query", p.query);
    if (p.state && p.state !== "all") q.set("state", p.state);
    q.set("page", String(p.page ?? 1));
    q.set("pageSize", String(p.pageSize ?? 10));
    return request<CasePage>("GET", `/api/admin/cases?${q.toString()}`);
  },
  adminCase: (id: string) => request<CaseDetail>("GET", `/api/admin/cases/${id}`),
  adminRetryEvents: () => post<{ retried: number; events: EventHealth }>("/api/admin/events/retry"),
  adminPropose: (caseId: string) => post<Proposed>(`/api/admin/cases/${caseId}/propose-assignment`),
  adminApprove: (id: string, digest: string, officerId?: string) => post<Proposal>(`/api/admin/proposals/${id}/approve`, { digest, officerId }),
  adminReject: (id: string) => post<Proposal>(`/api/admin/proposals/${id}/reject`),
  adminOfficer: (id: string, body: { onDuty?: boolean; capacity?: number; areas?: string[]; areaDiscovery?: boolean }) => post<StaffMember[]>(`/api/admin/officers/${id}`, body),
  adminAsk: (question: string) => post<AskResult>("/api/admin/ask", { question }),
  adminReassign: (caseId: string, officerId: string, reason?: string, fromOfficerId?: string) => post<CaseRow>(`/api/admin/cases/${caseId}/reassign`, { officerId, reason, fromOfficerId }),

  file: (caseId: string) => request<CaseFile>("GET", `/api/officer/cases/${caseId}/file`),
  acknowledge: (caseId: string) => post<CaseFile>(`/api/officer/cases/${caseId}/acknowledge`),
  claims: (caseId: string) => request<Claim[]>("GET", `/api/officer/cases/${caseId}/claims`),
  assess: (caseId: string, key: string, assessment: string, note?: string) => post<Claim[]>(`/api/officer/cases/${caseId}/claims/${encodeURIComponent(key)}/assessment`, { assessment, note }),
  draftRequest: (caseId: string, paymentIds: string[], topic?: string) => post<Proposal>(`/api/officer/cases/${caseId}/requests/draft`, { paymentIds, topic }),
  draftBank: (caseId: string, paymentIds: string[]) => post<Proposal>(`/api/officer/cases/${caseId}/bank-requests/draft`, { paymentIds }),
  editDraft: (caseId: string, id: string, text?: string, attachments?: string[]) => request<Proposal>("PATCH", `/api/officer/cases/${caseId}/proposals/${id}`, { json: { text, attachments } }),
  approveDraft: (caseId: string, id: string, digest: string) => post<Proposal>(`/api/officer/cases/${caseId}/proposals/${id}/approve`, { digest }),
  rejectDraft: (caseId: string, id: string) => post<Proposal>(`/api/officer/cases/${caseId}/proposals/${id}/reject`),
  simulateReply: (caseId: string, messageId: string) => post<CaseFile>(`/api/officer/cases/${caseId}/bank-requests/${messageId}/simulate-reply`),
  markReviewed: (caseId: string, messageId: string) => post<CaseFile>(`/api/officer/cases/${caseId}/messages/${messageId}/mark-reviewed`),
  related: (caseId: string) => request<Related[]>("GET", `/api/officer/cases/${caseId}/related`),
  relatedEvidence: (caseId: string, linkId: string) => request<RelatedEvidence>("GET", `/api/officer/cases/${caseId}/related/${linkId}/evidence`),
  reviewRelated: (caseId: string, id: string, state: "reviewed" | "dismissed") => post<Related[]>(`/api/officer/cases/${caseId}/related/${id}/review`, { state }),

  progress: (caseId: string) => request<Progress>("GET", `/api/cases/${caseId}/progress`),
  setLanguage: (caseId: string, language: "en" | "hi") => post<{ language: string }>(`/api/cases/${caseId}/language`, { language }),
  setArea: (caseId: string, area: string) => post<{ area: string; areaName: string }>(`/api/cases/${caseId}/area`, { area }),
  reply: (caseId: string, requestId: string, body: { text: string; sourceIds: string[]; expectedRevision?: number }) =>
    post<{ ok: boolean }>(`/api/cases/${caseId}/requests/${requestId}/reply`, body),
};


// ---- the investigation assistant: connections, comparison, handling history and the bounded run ------------------------------

export interface SourceRef { kind: string; factId?: string | null; sourceId?: string | null; filename?: string | null; origin?: string; excerpt?: string | null; openUrl?: string | null; pointer?: string }
export interface Signal { kind: string; label: string; strength: "strong" | "weak"; entityType: string; value: string; independentReports?: number; attribution?: string | null; mine?: SourceRef; theirs?: SourceRef }
export interface Candidate {
  handle: string; resultKind: string; band: string; bandNote: string; state: "possible" | "reviewed" | "dismissed"; reason?: string | null; decisionStale: boolean;
  possibleDuplicate: boolean; sameReportAs?: string[]; caseRef: string; title: string; area: string | null; workflowState: string | null; reviewState: string;
  owner: { name: string | null; isYou: boolean }; dates: { submittedAt: string | null; payments: { from: string | null; to: string | null } };
  matchedSignals: Signal[]; differences: string[]; contradictions: string[]; sourceQuality: { mine: string; theirs: string }; revisions: { mine: number; theirs: number };
  asOf: string; linkId: string | null;
}
export interface ConnectionSearch {
  asOf: string; scope: { label: string; policyVersion: string; areaDiscovery: boolean }; scanned: number; total: number; truncated: boolean;
  candidates: Candidate[]; missingData: string[]; basis: string | null; caveats: string[];
}
export interface Cell { text: string | null; origin?: string | null; originLabel?: string | null; sourceId?: string | null; filename?: string | null; openUrl?: string | null }
export interface CompareRow { group: string; label: string; mine: Cell; theirs: Cell; relation: "same" | "same_identifier" | "different" | "only_mine" | "only_theirs" | "not_stated" | "info" }
export interface Comparison {
  handle: string; resultKind: string; band: string; bandNote: string; state: string; reason?: string | null; current: { caseRef: string; title: string; revision: number };
  earlier: { caseRef: string; title: string; revision: number; area: string | null; owner: string | null }; rows: CompareRow[]; sharedEvidence: { label: string; value: string; strength: string }[];
  differences: string[]; contradictions: string[]; basis: string; stale: boolean; staleReason?: string | null; decisionStale: boolean; asOf: string; linkId: string | null; note: string;
}
export interface HandlingEntry {
  at: string; type: string; label: string; actor: { role: string; name: string | null }; wording?: string; detail?: string; recordType: string; recordId: string; state?: string; simulated?: boolean; reviewed?: boolean;
}
export interface Handling {
  handle: string; caseRef: string; title: string; area: string | null; owner: { name: string | null; isYou: boolean }; scope: string; sharedSignals: { label: string; value: string; strength: string }[];
  differences: string[]; timeline: HandlingEntry[]; evidenceObtained: { files: number; written: number }; citizenResponse: { replies: number; reviewed: number };
  simulatedBankResponse: { replies: number; reviewed: number; note: string };
  recovery: { recoveredMinor: number; reviewedDebitMinor: number; outstandingMinor: number; state: string; checkedBy: string | null; note: string };
  caseStatus: { reviewState: string; workflowState: string | null; note: string }; officialStatus: { category: string; receivedAt: string; issuer: string | null; note: string } | null;
  resolution: { kind: string; reviewOutcome: string | null; reviewedAt: string | null } | null; unresolved: { label: string; kind: string }[];
  outcome: { label: string; recordedBy: string | null; note: string };
  lessons: { id: string; version: number; outcomeKind: string; text: string; reviewState: string; author: string | null; at: string }[];
  generatedSummary: { text: string; origin: string }; asOf: string; staleReason?: string | null;
}
export interface RunAction { type: "open_comparison" | "open_handling" | "review_draft" | "review_consultation"; label: string; handle?: string; proposalId?: string; consultationId?: string }
/** Who stated a finding's source: the citizen, a reading of their evidence, an officer's own record, or a model's choice. */
export type Origin = "citizen_provided" | "extracted" | "officer_recorded" | "system_record" | "model_suggestion";
export interface RunCard {
  id: string; tool: string; kind: string; title: string; finding: string; why: string; limitations: string[]; mainAction: RunAction | null; handles: string[];
  supporting: { label: string; excerpt: string | null; side: string; handle: string | null; openUrl: string | null; origin?: Origin }[]; data: Record<string, any>;
}
export interface RunTrace { n: number; tool: string; reason: string; state: "done" | "failed" | "skipped"; counts: Record<string, number>; ms?: number; used?: string[]; why?: string; error?: string }
export interface Run {
  id: string; goal: string; state: "queued" | "running" | "waiting_for_approval" | "completed" | "failed" | "cancelled" | "stale"; stage: string | null; origin: "live" | "fixture" | "rules";
  provider: string | null; modelId: string | null; toolCalls: number; toolBudget: number; trace: RunTrace[]; traceLine: string | null; cards: RunCard[];
  summary: { text: string; origin: "model" | "rules"; citations: { ref: string; excerpt: string }[]; selected?: string[] } | null; phase?: number; parentRunId?: string | null; supersededBy?: string | null;
  telemetry?: { planner?: string; reason?: string; summary?: { outcome?: string; error?: string }; planAttempts?: { ok: boolean; error?: string }[] }; partial: boolean; rejectedCitations: number; recordsUsed: string[]; drafts: Proposal[];
  error: string | null; staleReason: string | null; createdAt: string; finishedAt: string | null;
}

export const investigate = {
  summary: (caseId: string) => request<{ toReview: number }>("GET", `/api/officer/cases/${caseId}/connections/summary`),
  search: (caseId: string) => post<ConnectionSearch>(`/api/officer/cases/${caseId}/connections/search`),
  comparison: (caseId: string, handle: string, basis?: string) => request<Comparison>("GET", `/api/officer/cases/${caseId}/connections/${handle}/comparison${basis ? `?basis=${encodeURIComponent(basis)}` : ""}`),
  handling: (caseId: string, handle: string) => request<Handling>("GET", `/api/officer/cases/${caseId}/connections/${handle}/handling`),
  review: (caseId: string, handle: string, state: "reviewed" | "dismissed", reason?: string) => post<{ handle: string; state: string }>(`/api/officer/cases/${caseId}/connections/${handle}/review`, { state, reason }),
  start: (caseId: string, goal: string) => post<Run>(`/api/officer/cases/${caseId}/investigations`, { goal }),
  run: (caseId: string, runId: string) => request<Run>("GET", `/api/officer/cases/${caseId}/investigations/${runId}`),
  runs: (caseId: string) => request<Run[]>("GET", `/api/officer/cases/${caseId}/investigations`),
  cancel: (caseId: string, runId: string) => post<Run>(`/api/officer/cases/${caseId}/investigations/${runId}/cancel`),
  update: (caseId: string, runId: string, consultationId: string) => post<Run>(`/api/officer/cases/${caseId}/investigations/${runId}/update`, { consultationId }),
};

// ---- consultation: one officer asks the officer who holds an earlier report -----------------------------------------------------------

export interface ConsultationMessage {
  id: string; seq: number; kind: "request" | "reply" | "system"; authorName: string | null; authorRole: "sender" | "recipient" | "system"; body: string; at: string;
  data: { verified?: string; helpedAction?: string; note?: string | null; event?: string };
}
export interface ConsultationItem {
  id: string; role: "sender" | "recipient"; state: "draft" | "sent" | "replied" | "closed" | "cancelled"; caseId: string | null; createdAt: string; sentAt: string | null; unread: boolean;
  awaitingYourReply: boolean; replyCount: number; lastActivityAt: string; otherParty: { name: string | null }; aboutCaseRef: string | null; yourCaseRef: string;
}
export interface Consultation extends ConsultationItem {
  payload: { question: string | null; senderCaseRef?: string; earlierCaseRef?: string; sharedKind?: string; sharedIdentifier?: string; recipientName?: string; shares?: string[]; keeps_back?: string[]; withheld?: string };
  payloadDigest: string; approvedDigest: string | null; stale: string[]; needsRefresh: boolean; yourReport: { caseRef: string; title: string | null; area: string | null; workflowState: string | null; path: string | null; available: boolean } | null;
  messages: ConsultationMessage[]; helpedOptions: { id: string; label: string }[]; attribution: string;
  actions: { canEdit: boolean; canApprove: boolean; canRefresh: boolean; canCancel: boolean; canReply: boolean; canClose: boolean; canUpdatePlan: boolean };
}
export interface ConsultationList { items: ConsultationItem[]; unread: number; awaitingYourReply: number }
export interface ConsultationSummary { unread: number; awaitingYourReply: number; waiting: number; firstUnreadId: string | null }

export const consult = {
  summary: () => request<ConsultationSummary>("GET", "/api/officer/consultations/summary"),
  list: (box: "all" | "inbox" | "sent" = "all") => request<ConsultationList>("GET", `/api/officer/consultations?box=${box}`),
  get: (id: string) => request<Consultation>("GET", `/api/officer/consultations/${id}`),
  forCase: (caseId: string) => request<Consultation[]>("GET", `/api/officer/cases/${caseId}/consultations`),
  draft: (caseId: string, handle: string) => post<Consultation>(`/api/officer/cases/${caseId}/consultations/draft`, { handle }),
  edit: (id: string, question: string) => request<Consultation>("PATCH", `/api/officer/consultations/${id}`, { json: { question } }),
  refresh: (id: string) => post<Consultation>(`/api/officer/consultations/${id}/refresh`),
  approve: (id: string, digest: string) => post<Consultation>(`/api/officer/consultations/${id}/approve`, { digest }),
  reply: (id: string, body: { verified: string; helpedAction: string; note?: string; key: string }) => post<Consultation>(`/api/officer/consultations/${id}/reply`, body),
  close: (id: string) => post<Consultation>(`/api/officer/consultations/${id}/close`),
  cancel: (id: string) => post<Consultation>(`/api/officer/consultations/${id}/cancel`),
};


// ---- bank-connected transaction verification and the officer's request to a bank ----------------------------------------------

export interface VerificationField {
  key: string; label: string; value: string | null; note?: string | null;
  source: { label: string; sourceIds: string[]; basis: string; reviewState: string } | null;
}
export interface ComparisonRow { field: string; label: string; reported: string | null; bank: string | null; result: "match" | "differs" | "not_provided" | "not_stated" | "looked_up" }
export interface VerificationOut {
  id: string; paymentId: string; attempt: number; status: string; label: string; mode: "live" | "sandbox" | "none"; simulated: boolean; banner: string | null; productionConfirmation: boolean;
  provider: { id: string | null; name: string | null }; reasonCode: string | null; detail: string | null; responseReference: string | null; returned: Record<string, unknown> | null;
  comparison: ComparisonRow[]; evidenceSource: string; requestedBy: string | null; providerCalls: number; checkedAt: string | null; createdAt: string; caseRevision: number; stale: boolean;
}
export interface VerificationPayment {
  paymentId: string; name: string; status: string; statusLabel: string; inputs: VerificationField[]; canVerify: boolean; lastCheckedAt: string | null;
  missing: { key: string; label: string; required: boolean; why: string }[];
  integration: { available: boolean; bankId: string | null; bankName: string | null; mode: string; simulated: boolean; reason: string | null; banner?: string };
  latest: VerificationOut | null; history: VerificationOut[]; awaitingRequestId: string | null; nextAction: { type: string; label: string; text: string };
  latestSimulated: boolean; anyProductionConfirmation: boolean;
}
export interface BankResponseOut {
  id: string; body: string; createdAt: string; receivedAt: string | null; from: string | null; filename: string | null; association: "reference_match" | "officer_selected" | "unlinked";
  needsReview: boolean; associationNote: string | null; summary: { text: string; start: number; end: number }[]; state: string; simulated: boolean; requestId: string | null;
  provenance: string; authenticated: false;
}
export interface BankRequestOut {
  id: string; caseId: string; state: "approved" | "queued" | "sent" | "failed" | "response_received"; stateLabel: string; delivery: "simulated" | "email" | "manual"; deliveryLabel: string;
  simulated: boolean; banner: string | null; bankId: string; bankName: string; to: string; subject: string; body: string; attachments: { id: string; filename: string }[];
  responseRef: string; paymentIds: string[]; attempts: number; lastError: string | null; approvedBy: string | null; approvedAt: string; sentAt: string | null; sentNote: string | null;
  canSend: boolean; canMarkSent: boolean; replies: BankResponseOut[]; messageId: string | null;
}
export interface VerificationSection {
  payments: VerificationPayment[]; requests: BankRequestOut[]; unlinkedResponses: BankResponseOut[]; statuses: Record<string, string>; simulatedBanner: string; notices: string[];
}

export const bank = {
  section: (caseId: string) => request<VerificationSection>("GET", `/api/officer/cases/${caseId}/verification`),
  verify: (caseId: string, paymentId: string) => post<{ verification: VerificationOut; duplicate: boolean; section: VerificationSection }>(`/api/officer/cases/${caseId}/verification/verify`, { paymentId }),
  send: (caseId: string, requestId: string) => post<VerificationSection>(`/api/officer/cases/${caseId}/bank-requests/${requestId}/send`),
  markSent: (caseId: string, requestId: string) => post<VerificationSection>(`/api/officer/cases/${caseId}/bank-requests/${requestId}/mark-sent`),
  attach: (caseId: string, body: { text: string; requestId?: string; fromAddress?: string; filename?: string; receivedAt?: string }) =>
    post<{ response: BankResponseOut; section: VerificationSection }>(`/api/officer/cases/${caseId}/bank-responses`, body),
};
