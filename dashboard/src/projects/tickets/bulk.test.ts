import { describe, expect, it } from 'vitest';
import type { Ticket } from '../../api/client';
import { bulkSummary, bulkTargets, runBounded } from './bulk';

const t = (id: string, status: string, extra: Partial<Ticket> = {}) =>
  ({ id, status, kind: 'task', created_by_user_id: 'u1', ...extra }) as unknown as Ticket;

describe('runBounded', () => {
  it('never has more than the limit in flight and reports in input order', async () => {
    let inFlight = 0;
    let peak = 0;
    const items = Array.from({ length: 10 }, (_, i) => i);
    const result = await runBounded(items, 4, async (i) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, (10 - i) * 2));
      inFlight--;
      if (i % 3 === 0) throw new Error(`bad ${i}`);
    });
    expect(peak).toBe(4);
    expect(result.ok).toEqual([1, 2, 4, 5, 7, 8]);
    expect(result.failed.map((f) => f.item)).toEqual([0, 3, 6, 9]);
  });

  it('handles an empty list', async () => {
    expect(await runBounded([], 4, async () => undefined)).toEqual({ ok: [], failed: [] });
  });
});

describe('bulkTargets', () => {
  const editor = { canEdit: true, userId: 'u9' };
  const reader = { canEdit: false, userId: 'u1' };

  it('approves only Suggested and dismisses only Suggested or Open', () => {
    const sel = [t('a', 'suggested'), t('b', 'open'), t('c', 'in_progress'), t('d', 'review')];
    const r = bulkTargets(sel, editor);
    expect(r.approve.map((x) => x.id)).toEqual(['a']);
    expect(r.dismiss.map((x) => x.id)).toEqual(['a', 'b']);
    expect(r.assign.map((x) => x.id)).toEqual(['a', 'b', 'c', 'd']);
    expect(r.approveReason).toBeUndefined();
  });

  it('says why an action reaches none of the selection', () => {
    const r = bulkTargets([t('c', 'in_progress')], editor);
    expect(r.approveReason).toBe('Only Suggested tickets can be approved');
    expect(r.dismissReason).toBe('Only Suggested or Open tickets can be dismissed');
  });

  it('follows the viewer rule: a reader never approves and dismisses only their own tickets', () => {
    const sel = [t('a', 'suggested'), t('b', 'open', { created_by_user_id: 'u2' })];
    const r = bulkTargets(sel, reader);
    expect(r.approve).toEqual([]);
    expect(r.approveReason).toBe('Only project editors can approve tickets');
    expect(r.dismiss.map((x) => x.id)).toEqual(['a']);
    const none = bulkTargets([t('b', 'open', { created_by_user_id: 'u2' })], reader);
    expect(none.dismissReason).toBe('You can dismiss only tickets you created');
  });

  it('leaves out tickets the viewer may not reassign', () => {
    const r = bulkTargets([t('a', 'open', { created_by_user_id: 'u2' })], { canEdit: false, userId: 'u1' });
    expect(r.assign).toEqual([]);
    expect(r.assignReason).toBe('You can’t reassign these tickets');
  });
});

describe('bulkSummary', () => {
  const ok = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `t${i}` }));
  it('summarises success, partial failure and total failure in one line', () => {
    expect(bulkSummary('approve', { ok: ok(5), failed: [] })).toEqual({ tone: 'success', text: 'Approved 5 tickets' });
    expect(bulkSummary('follow', { ok: ok(1), failed: [] }).text).toBe('Following 1 ticket');
    expect(
      bulkSummary('approve', { ok: ok(5), failed: [{ item: { id: 'x' }, error: new Error('Ticket changed') }] }),
    ).toEqual({ tone: 'error', text: 'Approved 5 · 1 failed: Ticket changed' });
    expect(
      bulkSummary('dismiss', {
        ok: [],
        failed: [
          { item: { id: 'x' }, error: new Error('Forbidden') },
          { item: { id: 'y' }, error: new Error('Other') },
        ],
      }).text,
    ).toBe('Couldn’t dismiss 2 tickets: Forbidden (and 1 more)');
  });
});
