/**
 * Marketplace v2 client layer: multipart publish (browser-set boundary, no
 * JSON Content-Type), report / dismiss / moderation routes and the
 * revocations feed.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, createApiClient } from './client';

function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });
}

describe('marketplace publish + moderation client', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('uploads a personal package as multipart without forcing a Content-Type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ package_id: 'spk_1' }, 201));
    vi.stubGlobal('fetch', fetchMock);
    const client = createApiClient('https://api.example', 'sk-test');
    const file = new File([new Uint8Array(4)], 'pkg.tar.gz');
    await client.publishSkillPackage({ target: { kind: 'personal' }, file, namespace: 'tools' });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.example/api/v1/users/me/skills/packages');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ Authorization: 'Bearer sk-test' });
    const form = init.body as FormData;
    expect((form.get('package') as File).name).toBe('pkg.tar.gz');
    expect(form.get('namespace')).toBe('tools');
    expect(form.has('collection')).toBe(false);
    expect(form.has('visibility')).toBe(false);
  });

  it('posts org publishes with visibility and keeps Retry-After on errors', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ error: { code: 'publish_busy', message: 'busy', details: {} } }, 503, { 'retry-after': '7' }));
    vi.stubGlobal('fetch', fetchMock);
    const client = createApiClient('https://api.example', 'sk-test');
    const error = await client.publishSkillPackage({
      target: { kind: 'org', orgId: 'org/1', visibility: 'public' },
      file: new Blob([new Uint8Array(1)]),
    }).catch((e) => e);
    expect(fetchMock.mock.calls[0][0]).toBe('https://api.example/api/v1/orgs/org%2F1/skills/packages');
    expect((fetchMock.mock.calls[0][1].body as FormData).get('visibility')).toBe('public');
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe('publish_busy');
    expect(error.details.retry_after).toBe('7');
  });

  it('hits the report, queue, dismiss, moderation and feed routes', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(json({})));
    vi.stubGlobal('fetch', fetchMock);
    const client = createApiClient('https://api.example', 'sk-test');
    await client.reportMarketplacePackage('spk_1', { reason: 'spam', version_id: 'skv_1' });
    await client.listMarketplaceReports({ status: 'open', limit: 50, cursor: 'c1' });
    await client.dismissMarketplaceReport('spr_1', 'not real');
    await client.moderateMarketplace('versions', 'skv_1', 'block', 'malware');
    await client.listMarketplaceRevocations({ since: '2026-01-01T00:00:00+00:00', limit: 500 });
    const calls = fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET', init?.body]);
    expect(calls).toEqual([
      ['https://api.example/api/v1/marketplace/packages/spk_1/reports', 'POST', JSON.stringify({ reason: 'spam', version_id: 'skv_1' })],
      ['https://api.example/api/v1/marketplace/reports?status=open&limit=50&cursor=c1', 'GET', undefined],
      ['https://api.example/api/v1/marketplace/reports/spr_1/dismiss', 'POST', JSON.stringify({ note: 'not real' })],
      ['https://api.example/api/v1/marketplace/versions/skv_1/block', 'POST', JSON.stringify({ reason: 'malware' })],
      ['https://api.example/api/v1/marketplace/revocations?since=2026-01-01T00%3A00%3A00%2B00%3A00&limit=500', 'GET', undefined],
    ]);
  });
});
