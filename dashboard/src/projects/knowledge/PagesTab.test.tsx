import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApiClient } from '../../api/client';
import PagesTab from './PagesTab';

/**
 * The wiki page "Publish" switch flips only the Confluence opt-in through
 * PATCH /pages/{slug} with `{publish}`; it never rewrites the page content.
 * The real API client runs against a stubbed fetch so the wire request is
 * what gets asserted.
 */

const mockAuth = vi.fn();
const addToast = vi.fn();

vi.mock('../../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

const PAGE = {
  id: 'page_1',
  slug: 'architecture',
  title: 'Architecture',
  page_type: 'article',
  content: '# Architecture\n\nAPI, worker and dashboard.',
  word_count: 120,
  entry_count: 4,
  auto_generated: false,
  updated_at: '2026-10-20T10:00:00Z',
  publish: false,
};

const fetchMock = vi.fn();

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
  );
}

beforeEach(() => {
  addToast.mockReset();
  fetchMock.mockReset();
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    const method = init?.method ?? 'GET';
    if (method === 'GET' && path === '/api/v1/projects/proj_a/pages') return json([PAGE]);
    if (method === 'GET' && path === '/api/v1/projects/proj_a/pages/architecture') {
      return json({ ...PAGE, backlinks: [] });
    }
    if (method === 'PATCH' && path === '/api/v1/projects/proj_a/pages/architecture') {
      return json({ slug: 'architecture', publish: true });
    }
    return json({ error: { code: '404', message: 'not found' } }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  mockAuth.mockReturnValue({
    auth: { apiKey: 'sk_test', baseUrl: 'http://test.api', client: createApiClient('http://test.api', 'sk_test') },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderTab() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <PagesTab
        projectId="proj_a"
        canEdit
        initialSlug="architecture"
        confluence={{ orgId: 'org_x', isEnterprise: true }}
      />
    </QueryClientProvider>,
  );
}

describe('PagesTab publish toggle', () => {
  it('sends only {publish} through PATCH, never the page content', async () => {
    const user = userEvent.setup();
    renderTab();

    const toggle = await screen.findByRole('switch', { name: 'Publish' });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    await user.click(toggle);

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'PATCH')).toBe(true),
    );
    const writes = fetchMock.mock.calls.filter(([, init]) => {
      const method = (init as RequestInit | undefined)?.method ?? 'GET';
      return method !== 'GET';
    });
    expect(writes).toHaveLength(1);
    const [url, init] = writes[0] as [string, RequestInit];
    expect(new URL(url).pathname).toBe('/api/v1/projects/proj_a/pages/architecture');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({ publish: true });

    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith('success', 'This page will publish to Confluence.'),
    );
  });

  it('hides the toggle for read-only members', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={qc}>
        <PagesTab
          projectId="proj_a"
          canEdit={false}
          initialSlug="architecture"
          confluence={{ orgId: 'org_x', isEnterprise: true }}
        />
      </QueryClientProvider>,
    );
    expect(await screen.findByText('API, worker and dashboard.')).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: 'Publish' })).not.toBeInTheDocument();
  });
});
