/**
 * P2-1 regression: DELETE routes answer 204 with no body; the shared request()
 * helper must resolve (not throw on resp.json()).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApiClient } from './client';

describe('createApiClient no-content responses', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('resolves a 204 DELETE without parsing a body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);
    const client = createApiClient('https://api.example', 'sk-test');
    await expect(client.deletePersonalSkillVersion('spk_1', 'skv_1')).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.example/api/v1/users/me/skills/packages/spk_1/versions/skv_1',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('still parses JSON bodies and surfaces errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200, headers: { 'content-type': 'application/json' } })));
    const client = createApiClient('https://api.example', 'sk-test');
    await expect(client.health()).resolves.toEqual({ status: 'ok' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"error":{"code":"version_referenced"}}', { status: 409 })));
    await expect(client.deletePersonalSkillVersion('spk_1', 'skv_1')).rejects.toMatchObject({ status: 409 });
  });
});
