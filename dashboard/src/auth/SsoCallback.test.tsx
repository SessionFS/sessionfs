import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import SsoCallback from './SsoCallback';

const { mockLogin, mockNavigate } = vi.hoisted(() => ({
  mockLogin: vi.fn(),
  mockNavigate: vi.fn(),
}));

vi.mock('./AuthContext', () => ({ useAuth: () => ({ login: mockLogin }) }));
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});
vi.mock('./apiBase', () => ({ resolveApiBaseUrl: () => 'https://api.test' }));

function setHash(hash: string) {
  window.history.replaceState(null, '', `/sso/callback${hash}`);
}

describe('SsoCallback', () => {
  beforeEach(() => {
    mockLogin.mockReset();
    mockNavigate.mockReset();
    mockLogin.mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    setHash('');
  });

  it('exchanges the code (with credentials), clears the fragment, logs in', async () => {
    setHash('#code=one-time-abc');
    const replaceSpy = vi.spyOn(window.history, 'replaceState');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ api_key: 'sk_minted' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<MemoryRouter><SsoCallback /></MemoryRouter>);

    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith('https://api.test', 'sk_minted'));
    // POSTs to /exchange with credentials + the code from the fragment.
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.test/api/v1/auth/sso/exchange');
    expect(opts.method).toBe('POST');
    expect(opts.credentials).toBe('include');
    expect(JSON.parse(opts.body)).toEqual({ code: 'one-time-abc' });
    // Fragment wiped from the URL (F2).
    expect(replaceSpy).toHaveBeenCalled();
    expect(window.location.hash).toBe('');
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true }));
  });

  it('exchanges against the saved API base from /start (custom Server URL)', async () => {
    setHash('#code=abc');
    sessionStorage.setItem('sfs_sso_api_base', 'https://selfhosted.example.com');
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ api_key: 'k' }) });
    vi.stubGlobal('fetch', fetchMock);
    render(<MemoryRouter><SsoCallback /></MemoryRouter>);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][0]).toBe('https://selfhosted.example.com/api/v1/auth/sso/exchange');
    // Consumed the saved base so it can't leak into a later flow.
    expect(sessionStorage.getItem('sfs_sso_api_base')).toBeNull();
  });

  it('shows a message for a pending_confirmation redirect and does not exchange', async () => {
    setHash('#error=pending_confirmation');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<MemoryRouter><SsoCallback /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByRole('alert').textContent).toMatch(/explicit confirmation/i);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it('shows an error when no code is present', async () => {
    setHash('');
    vi.stubGlobal('fetch', vi.fn());
    render(<MemoryRouter><SsoCallback /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it('surfaces a generic error when the exchange fails (does not log in)', async () => {
    setHash('#code=expired');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 400, json: async () => ({}) }));
    render(<MemoryRouter><SsoCallback /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(mockLogin).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
