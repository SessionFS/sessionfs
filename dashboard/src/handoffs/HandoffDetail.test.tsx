import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import HandoffDetail from './HandoffDetail';

/**
 * UI coverage for HandoffDetail status transitions. Tests render the
 * component with mocked hooks for each of the core states:
 *   - pending (recipient sees the Claim button)
 *   - claimed (no claim button, "View full session" link shown)
 *   - expired (stepper shows expired state, no claim button)
 * Plus the claim mutation happy path and error surface.
 */

const mockUseHandoff = vi.fn();
const mockUseHandoffSummary = vi.fn();
const mockAuth = vi.fn();
const mockClaimHandoff = vi.fn();

vi.mock('../hooks/useHandoffs', () => ({
  useHandoff: (...args: unknown[]) => mockUseHandoff(...args),
  useHandoffSummary: (...args: unknown[]) => mockUseHandoffSummary(...args),
}));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

function renderHandoff(handoffId = 'ho_test123') {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/handoffs/${handoffId}`]}>
        <Routes>
          <Route path="/handoffs/:id" element={<HandoffDetail />} />
          <Route path="/handoffs" element={<div>handoffs list</div>} />
          <Route path="/sessions/:id" element={<div>session page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function baseHandoff(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ho_test123',
    sender_email: 'alice@example.com',
    recipient_email: 'bob@example.com',
    status: 'pending',
    session_id: 'ses_source123',
    recipient_session_id: null,
    session_title: 'Debugging auth middleware',
    session_tool: 'claude-code',
    session_model_id: 'claude-sonnet-4-5',
    session_message_count: 42,
    session_total_tokens: 15000,
    message: 'Here you go — see the notes in the last assistant turn.',
    created_at: '2026-04-10T12:00:00Z',
    claimed_at: null,
    ...overrides,
  };
}

describe('HandoffDetail', () => {
  beforeEach(() => {
    mockUseHandoff.mockReset();
    mockUseHandoffSummary.mockReset();
    mockAuth.mockReset();
    mockClaimHandoff.mockReset();

    mockAuth.mockReturnValue({
      auth: {
        apiKey: 'sk_test',
        baseUrl: 'http://test.api',
        client: {
          claimHandoff: mockClaimHandoff,
        },
      },
    });
    mockUseHandoffSummary.mockReturnValue({ data: null });
  });

  it('shows a loading placeholder while the handoff is fetching', () => {
    mockUseHandoff.mockReturnValue({ data: undefined, isLoading: true, error: null });
    renderHandoff();
    expect(screen.getByText(/loading handoff/i)).toBeInTheDocument();
  });

  it('renders an error state when the handoff fetch fails', () => {
    mockUseHandoff.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('Not found'),
    });
    renderHandoff();
    expect(screen.getByText(/failed to load handoff/i)).toBeInTheDocument();
    expect(screen.getByText(/not found/i)).toBeInTheDocument();
  });

  it('pending handoff shows the Claim button for the recipient', async () => {
    mockUseHandoff.mockReturnValue({
      data: baseHandoff({ status: 'pending' }),
      isLoading: false,
      error: null,
    });

    renderHandoff();

    expect(screen.getByRole('button', { name: /claim this handoff/i })).toBeInTheDocument();
    // Claimed-only UI elements must NOT be present
    expect(screen.queryByRole('link', { name: /view full session/i })).not.toBeInTheDocument();
    // Session details render
    expect(screen.getByText('Debugging auth middleware')).toBeInTheDocument();
    expect(screen.getByText('alice@example.com')).toBeInTheDocument();
    expect(screen.getByText('bob@example.com')).toBeInTheDocument();
  });

  it('claimed handoff shows the "View full session" link and no claim button', () => {
    mockUseHandoff.mockReturnValue({
      data: baseHandoff({
        status: 'claimed',
        recipient_session_id: 'ses_recipient456',
        claimed_at: '2026-04-10T13:00:00Z',
      }),
      isLoading: false,
      error: null,
    });

    renderHandoff();

    expect(screen.queryByRole('button', { name: /claim this handoff/i })).not.toBeInTheDocument();

    const viewLink = screen.getByRole('link', { name: /view full session/i });
    expect(viewLink).toBeInTheDocument();
    // Link should point at the RECIPIENT's session copy, not the sender's
    expect(viewLink.getAttribute('href')).toContain('ses_recipient456');
  });

  it('expired handoff stepper shows expired state and no claim button', () => {
    mockUseHandoff.mockReturnValue({
      data: baseHandoff({ status: 'expired' }),
      isLoading: false,
      error: null,
    });

    renderHandoff();

    expect(screen.queryByRole('button', { name: /claim this handoff/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /view full session/i })).not.toBeInTheDocument();
    // The stepper relabels "Claimed" → "Expired" for expired handoffs
    expect(screen.getByText('Expired')).toBeInTheDocument();
  });

  it('clicking Claim this handoff calls the client claim method', async () => {
    mockUseHandoff.mockReturnValue({
      data: baseHandoff({ status: 'pending' }),
      isLoading: false,
      error: null,
    });
    mockClaimHandoff.mockResolvedValue({
      id: 'ho_test123',
      status: 'claimed',
      recipient_session_id: 'ses_recipient456',
    });

    renderHandoff();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /claim this handoff/i }));

    await waitFor(() => {
      expect(mockClaimHandoff).toHaveBeenCalledWith('ho_test123');
    });
  });

  it('claim mutation failure surfaces an error message', async () => {
    mockUseHandoff.mockReturnValue({
      data: baseHandoff({ status: 'pending' }),
      isLoading: false,
      error: null,
    });
    mockClaimHandoff.mockRejectedValue(new Error('Already claimed'));

    renderHandoff();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /claim this handoff/i }));

    await waitFor(() => {
      expect(screen.getByText(/failed to claim/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/already claimed/i)).toBeInTheDocument();
  });

  it('displays the CLI pull command with the handoff id', () => {
    mockUseHandoff.mockReturnValue({
      data: baseHandoff({ id: 'ho_pull_test_999' }),
      isLoading: false,
      error: null,
    });
    renderHandoff('ho_pull_test_999');
    expect(screen.getByText(/sfs pull-handoff ho_pull_test_999/)).toBeInTheDocument();
  });

  it('sender message renders when present and is omitted when null', () => {
    // With message
    mockUseHandoff.mockReturnValue({
      data: baseHandoff({ message: 'read the auth notes' }),
      isLoading: false,
      error: null,
    });
    const { unmount } = renderHandoff();
    expect(screen.getByText(/read the auth notes/i)).toBeInTheDocument();
    unmount();

    // Without message
    mockUseHandoff.mockReturnValue({
      data: baseHandoff({ message: null }),
      isLoading: false,
      error: null,
    });
    renderHandoff();
    expect(screen.queryByText(/message from alice/i)).not.toBeInTheDocument();
  });
});
