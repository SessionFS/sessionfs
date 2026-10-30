import { afterEach, describe, expect, it, vi } from 'vitest';
import { createReviewLoopApi } from './reviewLoop';

afterEach(() => vi.unstubAllGlobals());

function stub(body: unknown, ok = true, status = 200) {
  const f = vi.fn().mockResolvedValue({ ok, status, json: async () => body, text: async () => JSON.stringify(body) });
  vi.stubGlobal('fetch', f);
  return f;
}

describe('review loop api writes', () => {
  it('PATCHes a loop with only the given caps', async () => {
    const f = stub({ id: 'tk_p', title: 't', children: [] });
    await createReviewLoopApi('https://x.test', 'k').updateLoop('proj 1', 'tk_p', { token_budget: 5, max_review_rounds: null });
    const [url, init] = f.mock.calls[0];
    expect(url).toBe('https://x.test/api/v1/projects/proj%201/loops/tk_p');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body)).toEqual({ token_budget: 5, max_review_rounds: null });
    expect(init.headers.Authorization).toBe('Bearer k');
  });

  it('POSTs the close route and the seed route without a body', async () => {
    const f = stub({});
    const api = createReviewLoopApi('https://x.test', 'k');
    await api.closeLoopItem('p', 'tk_p', 'tk_c');
    await api.seedChecklist('p');
    expect(f.mock.calls[0][0]).toBe('https://x.test/api/v1/projects/p/loops/tk_p/children/tk_c/close');
    expect(f.mock.calls[0][1].method).toBe('POST');
    expect(f.mock.calls[0][1].body).toBeUndefined();
    expect(f.mock.calls[1][0]).toBe('https://x.test/api/v1/projects/p/loop-checklist/seed');
  });

  it('PATCHes a checklist item status and lists them', async () => {
    const f = stub([]);
    const api = createReviewLoopApi('https://x.test', 'k');
    await api.patchChecklistItem('p', 'lci_1', 'active');
    await api.listChecklist('p');
    expect(f.mock.calls[0][0]).toBe('https://x.test/api/v1/projects/p/loop-checklist/lci_1');
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ status: 'active' });
    expect(f.mock.calls[1][0]).toBe('https://x.test/api/v1/projects/p/loop-checklist');
  });

  it('throws an ApiError carrying the status on failure', async () => {
    stub({ detail: 'nope' }, false, 409);
    await expect(createReviewLoopApi('https://x.test', 'k').closeLoopItem('p', 'a', 'b')).rejects.toMatchObject({ status: 409 });
  });
});
