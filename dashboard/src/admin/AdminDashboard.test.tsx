import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AdminDashboard from './AdminDashboard';

/**
 * UI coverage for AdminDashboard — the admin back-office with Users,
 * Licenses, and Activity tabs. Tests focus on the top-level dashboard's
 * flows: stats card rendering, tab switching, user search, and the
 * destructive action (delete user) dialog. Hook internals are mocked at
 * `../hooks/useAdmin` and sub-components (LicensesTab, ConfirmModal) are
 * replaced with stubs.
 */

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useAdminStats: vi.fn(),
    useAdminUsers: vi.fn(),
    useAdminActionLog: vi.fn(),
    useAdminChangeTier: vi.fn(),
    useAdminVerifyUser: vi.fn(),
    useAdminDeleteUser: vi.fn(),
  },
}));

vi.mock('../hooks/useAdmin', () => hooks);

vi.mock('./LicensesTab', () => ({
  default: () => <div data-testid="licenses-tab">Licenses tab content</div>,
}));

vi.mock('./ConfirmModal', () => ({
  default: ({
    open,
    title,
    onConfirm,
    onCancel,
  }: {
    open: boolean;
    title: string;
    onConfirm: () => void;
    onCancel: () => void;
  }) =>
    open ? (
      <div role="dialog" aria-label={title}>
        <p>{title}</p>
        <button onClick={onConfirm}>Confirm</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    ) : null,
}));

function makeMutation(overrides: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
    ...overrides,
  };
}

function renderPage() {
  return render(<AdminDashboard />);
}

function baseUser(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 'user_abc',
    email: 'alice@example.com',
    display_name: 'Alice',
    tier: 'pro',
    email_verified: true,
    is_active: true,
    created_at: '2026-03-01T12:00:00Z',
    session_count: 12,
    storage_used_bytes: 1024 * 1024 * 5,
    api_key_count: 1,
    ...overrides,
  };
}

describe('AdminDashboard', () => {
  beforeEach(() => {
    for (const h of Object.values(hooks)) h.mockReset();

    hooks.useAdminStats.mockReturnValue({
      data: {
        users: { total: 7, verified: 5 },
        sessions: {
          total: 151,
          total_size_bytes: 70_000_000,
          by_tool: { codex: 4, 'claude-code': 72, 'gemini-cli': 21, cursor: 54 },
        },
      },
      isLoading: false,
    });
    hooks.useAdminUsers.mockReturnValue({
      data: { users: [], total: 0 },
      isLoading: false,
      error: null,
    });
    hooks.useAdminActionLog.mockReturnValue({ data: { actions: [] } });
    hooks.useAdminChangeTier.mockReturnValue(makeMutation());
    hooks.useAdminVerifyUser.mockReturnValue(makeMutation());
    hooks.useAdminDeleteUser.mockReturnValue(makeMutation());
  });

  it('shows a loading indicator while stats are pending', () => {
    hooks.useAdminStats.mockReturnValue({ data: undefined, isLoading: true });
    renderPage();
    expect(screen.getByText(/loading stats/i)).toBeInTheDocument();
  });

  it('renders the three overview cards with numbers when stats load', () => {
    renderPage();
    // Users card
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText(/5 verified, 2 pending/i)).toBeInTheDocument();
    // Sessions card
    expect(screen.getByText('151')).toBeInTheDocument();
    expect(screen.getByText(/4 active tools/i)).toBeInTheDocument();
  });

  it('users tab renders the admin users table', () => {
    hooks.useAdminUsers.mockReturnValue({
      data: {
        users: [
          baseUser({ email: 'alice@example.com', tier: 'pro' }),
          baseUser({ id: 'user_xyz', email: 'bob@example.com', tier: 'team' }),
        ],
        total: 2,
      },
      isLoading: false,
      error: null,
    });
    renderPage();

    expect(screen.getByText('alice@example.com')).toBeInTheDocument();
    expect(screen.getByText('bob@example.com')).toBeInTheDocument();
  });

  it('typing in the search box calls useAdminUsers with the query', async () => {
    renderPage();
    const user = userEvent.setup();

    const searchInput = screen.getByPlaceholderText(/search by email/i);
    await user.type(searchInput, 'ayan');

    await waitFor(() => {
      // The hook should have been called with the trimmed search prefix.
      // React batching means the last call wins.
      const lastCall = hooks.useAdminUsers.mock.calls.at(-1);
      expect(lastCall?.[0]?.search).toBe('ayan');
    });
  });

  it('switching to the Licenses tab renders the LicensesTab stub', async () => {
    renderPage();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^licenses$/i }));

    expect(await screen.findByTestId('licenses-tab')).toBeInTheDocument();
  });

  it('switching to the Activity tab renders the activity panel', async () => {
    hooks.useAdminActionLog.mockReturnValue({
      data: {
        actions: [
          {
            id: 'act_1',
            admin_id: 'admin_xyz',
            action: 'change_tier',
            target_type: 'user',
            target_id: 'user_abc',
            details: JSON.stringify({ old_tier: 'free', new_tier: 'pro' }),
            created_at: '2026-04-10T12:00:00Z',
          },
        ],
      },
    });

    renderPage();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^activity$/i }));

    // After switching to Activity tab, a change_tier entry should appear
    await waitFor(() => {
      expect(screen.getByText(/change_tier/i)).toBeInTheDocument();
    });
  });

  it('delete user flow shows confirm dialog then calls deleteUser mutation', async () => {
    const deleteMut = makeMutation();
    hooks.useAdminDeleteUser.mockReturnValue(deleteMut);
    hooks.useAdminUsers.mockReturnValue({
      data: { users: [baseUser({ id: 'user_abc', email: 'target@example.com' })], total: 1 },
      isLoading: false,
      error: null,
    });
    renderPage();
    const user = userEvent.setup();

    // The user row has an ActionMenu (three-dot button) that opens a
    // dropdown with Manage / Verify Email / Delete User. Find the button
    // by its svg content — it's the only three-dot menu on a single-row
    // user table.
    const buttons = screen.getAllByRole('button');
    const actionBtn = buttons.find(
      (b) => b.querySelector('svg circle[cx="8"][cy="3"]'),
    );
    expect(actionBtn).toBeDefined();
    await user.click(actionBtn!);

    // The action buttons become visible — find Delete User
    const deleteBtn = await screen.findByRole('button', { name: /delete user/i });
    await user.click(deleteBtn);

    // Confirm dialog appears (from our stubbed ConfirmModal)
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();

    // Click Confirm
    await user.click(screen.getByRole('button', { name: /^confirm$/i }));

    await waitFor(() => {
      expect(deleteMut.mutate).toHaveBeenCalledWith({ userId: 'user_abc' });
    });
  });
});
