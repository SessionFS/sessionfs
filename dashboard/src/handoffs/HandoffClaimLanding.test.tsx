import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import HandoffClaimLanding from './HandoffClaimLanding';

const { mockNavigate } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});
vi.mock('../auth/apiBase', () => ({ resolveApiBaseUrl: () => 'https://api.test' }));

const MOCK_PREVIEW = {
  title: 'Test Fix Session',
  sender_email: 'alice@example.com',
  tool: 'claude-code',
  message_count: 5,
  status: 'pending',
  expires_at: '2026-09-15T00:00:00Z',
  preview_messages: [
    { role: 'user', text: 'Please fix the bug in auth.py', index: 0 },
    { role: 'assistant', text: 'I found the issue — the token expiry check was inverted.', index: 1 },
  ],
};

function setHash(hash: string) {
  window.history.replaceState(null, '', `/handoffs/claim/hnd_test${hash}`);
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/handoffs/claim/:id" element={<HandoffClaimLanding />} />
        <Route path="/login" element={<div data-testid="login-page">Login Page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('HandoffClaimLanding', () => {
  beforeEach(() => {
    mockNavigate.mockReset();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    setHash('');
  });

  it('reads the token from the fragment, fetches preview, renders card', async () => {
    setHash('#t=hpr_validtoken123');
    const replaceSpy = vi.spyOn(window.history, 'replaceState');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => MOCK_PREVIEW,
    });
    vi.stubGlobal('fetch', fetchMock);

    renderAt('/handoffs/claim/hnd_test#t=hpr_validtoken123');

    // Fragment cleared immediately (F2).
    expect(replaceSpy).toHaveBeenCalled();

    await waitFor(() => {
      expect(screen.getByText('Test Fix Session')).toBeTruthy();
    });
    expect(screen.getByText(/alice@example.com/)).toBeTruthy();

    // Check fetch was called with correct URL.
    const [url] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/v1/handoffs/hnd_test/preview');
    expect(url).toContain('token=hpr_validtoken123');
  });

  it('shows dead-link state when token is missing from fragment', async () => {
    setHash(''); // no #t= param
    const replaceSpy = vi.spyOn(window.history, 'replaceState');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    renderAt('/handoffs/claim/hnd_test');

    expect(replaceSpy).toHaveBeenCalled();

    await waitFor(() => {
      expect(screen.getByText(/missing the preview token/i)).toBeTruthy();
    });
    // fetch should never have been called.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows dead-link state on 404 response', async () => {
    setHash('#t=hpr_expiredtoken');
    const replaceSpy = vi.spyOn(window.history, 'replaceState');
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    vi.stubGlobal('fetch', fetchMock);

    renderAt('/handoffs/claim/hnd_test#t=hpr_expiredtoken');

    expect(replaceSpy).toHaveBeenCalled();

    await waitFor(() => {
      expect(screen.getByText(/no longer valid/i)).toBeTruthy();
    });
  });

  it('shows dead-link state on network error', async () => {
    setHash('#t=hpr_nettest');
    vi.spyOn(window.history, 'replaceState');
    const fetchMock = vi.fn().mockRejectedValue(new Error('Network error'));
    vi.stubGlobal('fetch', fetchMock);

    renderAt('/handoffs/claim/hnd_test#t=hpr_nettest');

    await waitFor(() => {
      expect(screen.getByText(/could not reach the server/i)).toBeTruthy();
    });
  });

  it('shows claim CTA for pending handoffs', async () => {
    setHash('#t=hpr_pending');
    vi.spyOn(window.history, 'replaceState');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => MOCK_PREVIEW,
    }));

    renderAt('/handoffs/claim/hnd_test#t=hpr_pending');

    await waitFor(() => {
      expect(screen.getByText(/sign in.*create account.*claim/i)).toBeTruthy();
    });
  });

  it('claim CTA routes to /login', async () => {
    setHash('#t=hpr_test');
    vi.spyOn(window.history, 'replaceState');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => MOCK_PREVIEW,
    }));

    renderAt('/handoffs/claim/hnd_test#t=hpr_test');

    await waitFor(() => {
      const cta = screen.getByText(/sign in.*create account.*claim/i);
      expect(cta.closest('a')?.getAttribute('href')).toBe('/login');
    });
  });

  it('renders preview messages with role labels', async () => {
    setHash('#t=hpr_msgs');
    vi.spyOn(window.history, 'replaceState');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        ...MOCK_PREVIEW,
        preview_messages: [
          { role: 'user', text: 'Fix the bug', index: 0 },
          { role: 'assistant', text: 'Done', index: 1 },
        ],
      }),
    }));

    renderAt('/handoffs/claim/hnd_test#t=hpr_msgs');

    await waitFor(() => {
      expect(screen.getByText(/fix the bug/i)).toBeTruthy();
      expect(screen.getByText(/Done/)).toBeTruthy();
    });
  });

  it('shows loading state initially', () => {
    setHash('#t=hpr_loading');
    vi.spyOn(window.history, 'replaceState');
    // Return a promise that never resolves so we stay in loading state.
    vi.stubGlobal('fetch', () => new Promise(() => {}));

    renderAt('/handoffs/claim/hnd_test#t=hpr_loading');

    expect(screen.getByText(/loading handoff preview/i)).toBeTruthy();
  });
});
