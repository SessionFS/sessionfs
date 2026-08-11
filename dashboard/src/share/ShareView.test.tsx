import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import ShareView from './ShareView';

// ── Helpers ──────────────────────────────────────────────────────────

function mockFetchResponse(status: number, body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

function renderShareView(token: string) {
  return render(
    <MemoryRouter initialEntries={[`/s/${token}`]}>
      <Routes>
        <Route path="/s/:token" element={<ShareView />} />
      </Routes>
    </MemoryRouter>,
  );
}

const CLEAN_SHARE_DATA = {
  title: 'My Debug Session',
  tool: 'claude-code',
  created_at: '2026-08-01T12:00:00Z',
  message_count: 2,
  messages: [
    { role: 'user', content: [{ type: 'text', text: 'Help me debug this.' }] },
    { role: 'assistant', content: [{ type: 'text', text: 'Sure! Let me look.' }] },
  ],
  owner_display_name: 'Test User',
};

// ── Tests ────────────────────────────────────────────────────────────

describe('ShareView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the transcript when data loads successfully', async () => {
    vi.stubGlobal('fetch', mockFetchResponse(200, CLEAN_SHARE_DATA));

    renderShareView('test-token-123');

    await waitFor(() => {
      expect(screen.getByText('My Debug Session')).toBeInTheDocument();
    });

    expect(screen.getByText('Claude Code')).toBeInTheDocument();
    expect(screen.getByText('2 messages')).toBeInTheDocument();
    expect(screen.getByText('Shared by Test User')).toBeInTheDocument();
    expect(screen.getByText('Help me debug this.')).toBeInTheDocument();
    expect(screen.getByText('Sure! Let me look.')).toBeInTheDocument();
    expect(screen.getByText(/Memory Layer For AI Agents/)).toBeInTheDocument();
  });

  it('shows password prompt when link requires password', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: 'Password required' }),
      }),
    );

    renderShareView('password-link');

    await waitFor(() => {
      expect(screen.getByText('Password Required')).toBeInTheDocument();
    });

    expect(screen.getByPlaceholderText('Enter password')).toBeInTheDocument();
    expect(screen.getByText('View Session')).toBeInTheDocument();
  });

  it('shows error for wrong password', async () => {
    let callCount = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve({
            ok: false,
            status: 401,
            json: () => Promise.resolve({ detail: 'Password required' }),
          });
        }
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.resolve({ detail: 'Invalid password' }),
        });
      }),
    );

    renderShareView('password-link');

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Enter password')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText('Enter password'), 'wrong');
    await user.click(screen.getByText('View Session'));

    await waitFor(() => {
      expect(screen.getByText('Invalid password')).toBeInTheDocument();
    });
  });

  it('shows dead-link state for 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ detail: 'Share link not found' }),
      }),
    );

    renderShareView('dead-link');

    await waitFor(() => {
      expect(screen.getByText('Link Not Found')).toBeInTheDocument();
    });

    expect(
      screen.getByText(/This share link may have been revoked, expired, or never existed/),
    ).toBeInTheDocument();
  });

  it('shows blocked state for 451 (DLP)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 451,
        json: () => Promise.resolve({ detail: 'Sensitive content' }),
      }),
    );

    renderShareView('blocked-link');

    await waitFor(() => {
      expect(screen.getByText('Content Blocked')).toBeInTheDocument();
    });

    expect(
      screen.getByText(/This shared session contains sensitive content/),
    ).toBeInTheDocument();
  });

  it('sets document title and OG meta tags', async () => {
    vi.stubGlobal('fetch', mockFetchResponse(200, CLEAN_SHARE_DATA));

    renderShareView('test-token-og');

    await waitFor(() => {
      expect(screen.getByText('My Debug Session')).toBeInTheDocument();
    });

    expect(document.title).toBe('My Debug Session — SessionFS');

    // OG meta tags
    const ogTitle = document.querySelector('meta[property="og:title"]');
    expect(ogTitle).toBeTruthy();
    expect(ogTitle!.getAttribute('content')).toBe('My Debug Session — SessionFS');

    const ogDesc = document.querySelector('meta[property="og:description"]');
    expect(ogDesc).toBeTruthy();
    expect(ogDesc!.getAttribute('content')).toContain('claude-code');

    const ogType = document.querySelector('meta[property="og:type"]');
    expect(ogType).toBeTruthy();
    expect(ogType!.getAttribute('content')).toBe('website');
  });

  it('renders without owner name when display_name is null', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetchResponse(200, {
        ...CLEAN_SHARE_DATA,
        owner_display_name: null,
      }),
    );

    renderShareView('no-owner');

    await waitFor(() => {
      expect(screen.getByText('My Debug Session')).toBeInTheDocument();
    });

    expect(screen.queryByText(/Shared by/)).toBeNull();
  });

  it('shows empty state when session has no messages', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetchResponse(200, {
        ...CLEAN_SHARE_DATA,
        messages: [],
        message_count: 0,
      }),
    );

    renderShareView('empty-session');

    await waitFor(() => {
      expect(screen.getByText('This session contains no messages.')).toBeInTheDocument();
    });
  });
});
