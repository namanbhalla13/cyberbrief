/** Where a citizen's unsent work is kept in this browser tab.
 *
 * Everything is keyed by the signed-in account, so a second person using the same tab can never be handed the first person's
 * text or report reference. The keys are also wiped on sign-out. The server-side draft is never touched here: this is only the
 * pointer to it and the words typed but not yet saved.
 */
import type { SessionOut } from "./api";

const PREFIXES = ["cb-draft-", "cb-answers-"];

const read = (k: string): string | null => { try { return window.sessionStorage.getItem(k); } catch { return null; } };
const write = (k: string, v: string | null) => {
  try { if (v === null) window.sessionStorage.removeItem(k); else window.sessionStorage.setItem(k, v); } catch { /* storage may be blocked; the report itself is on the server */ }
};

/** Who this tab is signed in as; empty when nobody is, so nothing is ever read or written for an anonymous visitor. */
export const scopeOf = (session: SessionOut | null): string => (session?.authenticated && session.accountRef ? session.accountRef : "");

const OWNER_KEY = "cb-draft-owner";

/** Called whenever the tab learns who is signed in. If it is not who the stored drafts belong to, they are forgotten before anything can show them. */
export function adoptOwner(scope: string) {
  const owner = read(OWNER_KEY);
  if (owner !== null && owner !== scope) clearLocalDrafts();
  write(OWNER_KEY, scope || null);
}

export const draftStore = {
  caseId: (scope: string) => (scope ? read(`cb-draft-case:${scope}`) : null),
  setCaseId: (scope: string, id: string | null) => { if (scope) write(`cb-draft-case:${scope}`, id); },
  story: (scope: string, caseId: string | null) => (scope ? read(`cb-draft-story:${scope}:${caseId ?? "new"}`) : null),
  setStory: (scope: string, caseId: string | null, text: string | null) => { if (scope) write(`cb-draft-story:${scope}:${caseId ?? "new"}`, text); },
};

/** Forgets every locally kept draft pointer, typed-but-unsent text and reply draft. Called on sign-out. */
export function clearLocalDrafts() {
  try {
    const doomed: string[] = [];
    for (let i = 0; i < window.sessionStorage.length; i++) {
      const k = window.sessionStorage.key(i);
      if (k && PREFIXES.some((p) => k.startsWith(p))) doomed.push(k);
    }
    doomed.forEach((k) => window.sessionStorage.removeItem(k));
  } catch { /* nothing to clear if storage is blocked */ }
}
