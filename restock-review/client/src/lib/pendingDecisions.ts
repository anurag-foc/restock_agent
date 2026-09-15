/**
 * Quotes whose decisions have been submitted but not yet applied.
 *
 * Submitting does not write anything. It triggers the `restock_decision` job, which takes one
 * to two and a half minutes, and only then do the lines move off PENDING_APPROVAL. So for that
 * window the quote is genuinely still pending in the data and genuinely already decided in the
 * user's head.
 *
 * The detail page already polls and says so -- but the polling dies on unmount, and there is an
 * "All pending quotes" link on that same page inviting the reader to leave. Go back to the list
 * and the quote is sitting there exactly as before, with nothing to explain why. That is what
 * makes it read as broken, and it is why a real submission got sent twice: two job runs, two
 * minutes apart, for one decision.
 *
 * The honest fix is a status in the data between "pending" and "decided" -- but the decisions
 * endpoint deliberately does not write, so introducing one crosses a line this project drew on
 * purpose. This is the cheaper half: remember locally what we just submitted, so the list can
 * say "being applied" instead of silently contradicting the user.
 *
 * Scoped to the tab (sessionStorage) because that matches how long the claim stays true. It
 * does not follow the user to another browser, and it should not: a stale "applying..." badge
 * from yesterday would be a worse lie than the silence it replaces.
 */

const KEY = 'restock.pendingDecisions';

// Past this, assume the run finished (or failed) and stop claiming otherwise. The job's own
// ceiling is 15 minutes of polling; this is deliberately shorter than a work break, so nobody
// comes back from lunch to a badge that is still insisting.
const STALE_AFTER_MS = 10 * 60 * 1000;

export type PendingDecision = {
  quoteId: string;
  runId?: number;
  submittedAt: number;
};

function read(): PendingDecision[] {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const now = Date.now();
    return (parsed as PendingDecision[]).filter(
      (d) => d && typeof d.quoteId === 'string' && now - d.submittedAt < STALE_AFTER_MS,
    );
  } catch {
    // Private windows and cleared site data both throw here. A missing badge is a cosmetic
    // loss; a page that fails to render because storage was unavailable is not.
    return [];
  }
}

function write(entries: PendingDecision[]): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(entries));
  } catch {
    /* see read() */
  }
}

export function markSubmitted(quoteId: string, runId?: number): void {
  const others = read().filter((d) => d.quoteId !== quoteId);
  write([...others, { quoteId, runId, submittedAt: Date.now() }]);
}

export function clearSubmitted(quoteId: string): void {
  write(read().filter((d) => d.quoteId !== quoteId));
}

export function submittedQuoteIds(): Set<string> {
  return new Set(read().map((d) => d.quoteId));
}

export function isSubmitted(quoteId: string): boolean {
  return submittedQuoteIds().has(quoteId);
}
