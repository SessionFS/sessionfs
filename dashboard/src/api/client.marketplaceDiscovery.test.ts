/** Marketplace discovery client (marketplace v2 §9): search filters, README,
 *  package assets, publisher analytics, package settings and org usage. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApiClient } from './client';

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('marketplace discovery client', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('serializes every discovery filter', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ items: [], count: 0, has_more: false, next_cursor: null }));
    vi.stubGlobal('fetch', fetchMock);
    const client = createApiClient('https://api.example', 'sk-test');
    await client.searchMarketplace({
      q: 'lint', category: 'review', publisher: 'acme', verified: true, attested: false, license: 'MIT',
      max_severity: 'low', capability: 'scripts', works_in: 'codex', updated_within: 30,
      include_deprecated: false, sort: 'relevance', limit: 24,
    });
    const url = new URL(fetchMock.mock.calls[0][0]);
    expect(url.pathname).toBe('/api/v1/marketplace/packages');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      q: 'lint', limit: '24', category: 'review', publisher: 'acme', license: 'MIT', max_severity: 'low',
      capability: 'scripts', works_in: 'codex', sort: 'relevance', verified: 'true', attested: 'false',
      include_deprecated: 'false', updated_within: '30',
    });
  });

  it('calls the discovery endpoints', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(json({})));
    vi.stubGlobal('fetch', fetchMock);
    const client = createApiClient('https://api.example', 'sk-test');
    await client.getMarketplaceReadme('spk_1', 'skv_1');
    await client.getPublisherAnalytics('pub/1', 7);
    await client.updatePackageSettings('pub_1', 'spk_1', { categories: ['review'] });
    await client.getOrgSkillUsage('org_1', 90);
    await client.listMarketplaceCategories();
    fetchMock.mockResolvedValueOnce(new Response(new Blob(['png']), { status: 200 }));
    await client.getMarketplaceAsset('skv_1', 'assets/a b.png');
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls).toEqual([
      'https://api.example/api/v1/marketplace/packages/spk_1/readme?version_id=skv_1',
      'https://api.example/api/v1/publishers/pub%2F1/analytics?days=7',
      'https://api.example/api/v1/publishers/pub_1/packages/spk_1',
      'https://api.example/api/v1/orgs/org_1/skills/usage?days=90',
      'https://api.example/api/v1/marketplace/categories',
      'https://api.example/api/v1/marketplace/versions/skv_1/assets?path=assets%2Fa%20b.png',
    ]);
    expect(fetchMock.mock.calls[2][1].method).toBe('PATCH');
    expect(fetchMock.mock.calls[5][1].headers).toEqual({ Authorization: 'Bearer sk-test' });
  });
});
