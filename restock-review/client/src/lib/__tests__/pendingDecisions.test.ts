/**
 * The gap that made a real approval look broken.
 *
 * Submitting decisions triggers a job that takes one to two and a half minutes and writes
 * nothing until it finishes. The list page had no way to know, so the quote sat there looking
 * untouched -- and a real decision got submitted twice, two minutes apart.
 *
 * Vitest runs in a node environment here, so storage is stubbed rather than switching the whole
 * suite to jsdom for one module. What is under test is our logic, not the browser's.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

function stubStorage(): void {
  const store = new Map<string, string>();
  vi.stubGlobal('sessionStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
}

stubStorage();

const { clearSubmitted, isSubmitted, markSubmitted, submittedQuoteIds } = await import(
  '../pendingDecisions'
);

describe('pendingDecisions', () => {
  beforeEach(() => sessionStorage.clear());

  it('remembers a submitted quote so another page can explain the delay', () => {
    markSubmitted('QT-1', 123);
    expect(isSubmitted('QT-1')).toBe(true);
  });

  it('forgets it once the run has been applied', () => {
    markSubmitted('QT-1', 123);
    clearSubmitted('QT-1');
    expect(isSubmitted('QT-1')).toBe(false);
  });

  it('does not record the same quote twice', () => {
    markSubmitted('QT-1', 1);
    markSubmitted('QT-1', 2);
    expect(submittedQuoteIds().size).toBe(1);
  });

  it('expires a stale claim rather than insisting forever', () => {
    // A badge still saying "applying" after a lunch break is a worse lie than the silence it
    // replaced, so anything older than ten minutes is dropped on read.
    sessionStorage.setItem(
      'restock.pendingDecisions',
      JSON.stringify([{ quoteId: 'QT-OLD', submittedAt: Date.now() - 11 * 60 * 1000 }]),
    );
    expect(isSubmitted('QT-OLD')).toBe(false);
  });

  it('keeps a claim that is still fresh', () => {
    sessionStorage.setItem(
      'restock.pendingDecisions',
      JSON.stringify([{ quoteId: 'QT-NEW', submittedAt: Date.now() - 30 * 1000 }]),
    );
    expect(isSubmitted('QT-NEW')).toBe(true);
  });

  it('survives unreadable storage instead of taking the page down with it', () => {
    sessionStorage.setItem('restock.pendingDecisions', 'not json');
    expect(submittedQuoteIds().size).toBe(0);
  });
});
