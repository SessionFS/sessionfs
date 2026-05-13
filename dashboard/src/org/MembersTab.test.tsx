/**
 * UI coverage for v0.10.0 Phase 3b MembersTab. Hook internals are
 * mocked at `./useOrgMembers` so the tests focus on the UI surface
 * — listing, invite form, role-change buttons, and especially the
 * data-stays / access-revoked confirmation modal mandated by the
 * CEO directive (KB entry 230 #3).
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import MembersTab from './MembersTab';

const { hooks, meHook, toastHook, toastApi } = vi.hoisted(() => {
  const toastApi = {
    addToast: vi.fn(),
    removeToast: vi.fn(),
    toasts: [],
  };
  return {
    hooks: {
      useOrgMembers: vi.fn(),
      useInviteMember: vi.fn(),
      useChangeMemberRole: vi.fn(),
      useRemoveMember: vi.fn(),
    },
    meHook: { useMe: vi.fn() },
    toastHook: { useToast: vi.fn() },
    toastApi,
  };
});

vi.mock('./useOrgMembers', () => hooks);
vi.mock('../hooks/useMe', () => meHook);
vi.mock('../hooks/useToast', () => toastHook);

function makeMutation(extra: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    isPending: false,
    isError: false,
    error: null,
    ...extra,
  };
}

function defaultMembers() {
  return {
    org_id: 'org_test',
    members: [
      {
        user_id: 'u_alice',
        email: 'alice@example.com',
        display_name: 'Alice',
        role: 'admin',
        joined_at: '2026-04-10T12:00:00Z',
      },
      {
        user_id: 'u_bob',
        email: 'bob@example.com',
        display_name: 'Bob',
        role: 'member',
        joined_at: '2026-04-11T12:00:00Z',
      },
    ],
    seats_used: 2,
    seats_limit: 10,
    current_user_role: 'admin',
  };
}

beforeEach(() => {
  for (const h of Object.values(hooks)) h.mockReset();
  meHook.useMe.mockReset();
  toastHook.useToast.mockReset();
  toastApi.addToast.mockReset();
  toastApi.removeToast.mockReset();

  hooks.useOrgMembers.mockReturnValue({
    data: defaultMembers(),
    isLoading: false,
    error: null,
  });
  hooks.useInviteMember.mockReturnValue(makeMutation());
  hooks.useChangeMemberRole.mockReturnValue(makeMutation());
  hooks.useRemoveMember.mockReturnValue(makeMutation());
  // Default: viewer is alice (an admin, so self == admin row by default)
  meHook.useMe.mockReturnValue({
    data: { user_id: 'u_alice', email: 'alice@example.com' },
  });
  toastHook.useToast.mockReturnValue(toastApi);
});

describe('MembersTab', () => {
  it('shows a loading state while members fetch', () => {
    hooks.useOrgMembers.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });
    render(<MembersTab orgId="org_test" />);
    expect(screen.getByText(/loading members/i)).toBeInTheDocument();
  });

  it('shows an error state when members fail to load', () => {
    hooks.useOrgMembers.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('boom'),
    });
    render(<MembersTab orgId="org_test" />);
    expect(screen.getByRole('alert')).toHaveTextContent(/boom/i);
  });

  it('renders the member table with roles and seat count', () => {
    render(<MembersTab orgId="org_test" />);
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(
      /members \(2 \/ 10\)/i,
    );
    expect(screen.getByText('alice@example.com')).toBeInTheDocument();
    expect(screen.getByText('bob@example.com')).toBeInTheDocument();
    expect(screen.getByLabelText(/role: admin/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/role: member/i)).toBeInTheDocument();
  });

  it('hides invite form and action buttons for non-admin viewers', () => {
    hooks.useOrgMembers.mockReturnValue({
      data: { ...defaultMembers(), current_user_role: 'member' },
      isLoading: false,
      error: null,
    });
    render(<MembersTab orgId="org_test" />);
    expect(screen.queryByRole('form', { name: /invite member/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^promote/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^remove/i })).toBeNull();
  });

  it('admin can submit the invite form and the mutation receives the email + role', async () => {
    const invite = makeMutation();
    hooks.useInviteMember.mockReturnValue(invite);

    render(<MembersTab orgId="org_test" />);
    const user = userEvent.setup();

    const form = screen.getByRole('form', { name: /invite member/i });
    await user.type(within(form).getByLabelText(/^email/i), 'new@example.com');
    await user.selectOptions(within(form).getByLabelText(/^role/i), 'admin');
    await user.click(within(form).getByRole('button', { name: /send invite/i }));

    expect(invite.mutate).toHaveBeenCalledWith(
      { email: 'new@example.com', role: 'admin' },
      expect.any(Object),
    );
  });

  it('promote button fires the role mutation with role=admin', async () => {
    const changeRole = makeMutation();
    hooks.useChangeMemberRole.mockReturnValue(changeRole);

    render(<MembersTab orgId="org_test" />);
    const user = userEvent.setup();

    await user.click(
      screen.getByRole('button', { name: /promote bob@example.com to admin/i }),
    );
    expect(changeRole.mutate).toHaveBeenCalledWith(
      { userId: 'u_bob', role: 'admin' },
      expect.any(Object),
    );
  });

  it('demote button is disabled when the target is the last admin', () => {
    // alice is the only admin (default), demoting her would leave 0.
    render(<MembersTab orgId="org_test" />);
    const demoteBtn = screen.getByRole('button', {
      name: /demote alice@example.com to member/i,
    });
    expect(demoteBtn).toBeDisabled();
    expect(demoteBtn).toHaveAttribute(
      'title',
      expect.stringMatching(/last admin/i),
    );
  });

  it('demote button is enabled when there are multiple admins', () => {
    hooks.useOrgMembers.mockReturnValue({
      data: {
        ...defaultMembers(),
        members: [
          ...defaultMembers().members.map((m) =>
            m.user_id === 'u_bob' ? { ...m, role: 'admin' } : m,
          ),
        ],
      },
      isLoading: false,
      error: null,
    });
    render(<MembersTab orgId="org_test" />);
    expect(
      screen.getByRole('button', { name: /demote bob@example.com to member/i }),
    ).not.toBeDisabled();
  });

  it('remove button opens the data-stays confirmation modal with mandatory wording', async () => {
    render(<MembersTab orgId="org_test" />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole('button', { name: /remove bob@example.com/i }),
    );

    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByText(/remove bob@example\.com\?/i)).toBeInTheDocument();
    // CEO-mandated wording (KB entry 230 #3):
    expect(within(dialog).getByText(/access will be revoked\. data stays/i))
      .toBeInTheDocument();
    expect(within(dialog).getByText(/sessions stay/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/auto-transfer/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/knowledge-base entries stay/i))
      .toBeInTheDocument();
    expect(within(dialog).getByText(/pending transfers/i)).toBeInTheDocument();
  });

  it('confirming the remove modal fires the remove mutation', async () => {
    const remove = makeMutation();
    hooks.useRemoveMember.mockReturnValue(remove);

    render(<MembersTab orgId="org_test" />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole('button', { name: /remove bob@example.com/i }),
    );
    await user.click(screen.getByRole('button', { name: /confirm remove member/i }));

    expect(remove.mutate).toHaveBeenCalledWith(
      { userId: 'u_bob' },
      expect.any(Object),
    );
  });

  it('cancel button closes the remove modal without firing the mutation', async () => {
    const remove = makeMutation();
    hooks.useRemoveMember.mockReturnValue(remove);

    render(<MembersTab orgId="org_test" />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole('button', { name: /remove bob@example.com/i }),
    );
    await user.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(remove.mutate).not.toHaveBeenCalled();
  });

  it('disables all action buttons while remove is in flight', () => {
    hooks.useRemoveMember.mockReturnValue(makeMutation({ isPending: true }));
    render(<MembersTab orgId="org_test" />);
    // Member-row remove button disabled while in flight.
    expect(
      screen.getByRole('button', { name: /remove bob@example.com/i }),
    ).toBeDisabled();
  });

  it('invite button is disabled when the email field is empty', () => {
    render(<MembersTab orgId="org_test" />);
    expect(screen.getByRole('button', { name: /send invite/i })).toBeDisabled();
  });

  it('remove button is disabled on the viewer\'s own row', () => {
    // Default viewer is alice. Promote bob to admin so alice is not the last
    // admin (rules out the wouldDemoteLastAdmin confound for the demote check).
    hooks.useOrgMembers.mockReturnValue({
      data: {
        ...defaultMembers(),
        members: defaultMembers().members.map((m) =>
          m.user_id === 'u_bob' ? { ...m, role: 'admin' } : m,
        ),
      },
      isLoading: false,
      error: null,
    });
    render(<MembersTab orgId="org_test" />);
    const removeSelf = screen.getByRole('button', {
      name: /remove alice@example.com/i,
    });
    expect(removeSelf).toBeDisabled();
    expect(removeSelf).toHaveAttribute(
      'title',
      expect.stringMatching(/yourself/i),
    );
  });

  it('demote button is disabled on the viewer\'s own row even with multiple admins', () => {
    hooks.useOrgMembers.mockReturnValue({
      data: {
        ...defaultMembers(),
        members: defaultMembers().members.map((m) =>
          m.user_id === 'u_bob' ? { ...m, role: 'admin' } : m,
        ),
      },
      isLoading: false,
      error: null,
    });
    render(<MembersTab orgId="org_test" />);
    const demoteSelf = screen.getByRole('button', {
      name: /demote alice@example.com to member/i,
    });
    expect(demoteSelf).toBeDisabled();
    expect(demoteSelf).toHaveAttribute(
      'title',
      expect.stringMatching(/own role/i),
    );
  });

  it('shows an error toast when invite mutation fails', async () => {
    const invite = makeMutation();
    invite.mutate.mockImplementation((_body, opts) =>
      opts?.onError?.(new Error('Seat limit reached')),
    );
    hooks.useInviteMember.mockReturnValue(invite);

    render(<MembersTab orgId="org_test" />);
    const user = userEvent.setup();
    const form = screen.getByRole('form', { name: /invite member/i });
    await user.type(within(form).getByLabelText(/^email/i), 'new@example.com');
    await user.click(within(form).getByRole('button', { name: /send invite/i }));

    expect(toastApi.addToast).toHaveBeenCalledWith(
      'error',
      expect.stringMatching(/seat limit reached/i),
    );
  });

  it('shows an error toast when remove mutation fails', async () => {
    const remove = makeMutation();
    remove.mutate.mockImplementation((_body, opts) =>
      opts?.onError?.(new Error('cannot remove last admin')),
    );
    hooks.useRemoveMember.mockReturnValue(remove);

    render(<MembersTab orgId="org_test" />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole('button', { name: /remove bob@example.com/i }),
    );
    await user.click(screen.getByRole('button', { name: /confirm remove member/i }));

    expect(toastApi.addToast).toHaveBeenCalledWith(
      'error',
      expect.stringMatching(/cannot remove last admin/i),
    );
  });

  it('shows a success toast when invite mutation succeeds', async () => {
    const invite = makeMutation();
    invite.mutate.mockImplementation((_body, opts) => opts?.onSuccess?.());
    hooks.useInviteMember.mockReturnValue(invite);

    render(<MembersTab orgId="org_test" />);
    const user = userEvent.setup();
    const form = screen.getByRole('form', { name: /invite member/i });
    await user.type(within(form).getByLabelText(/^email/i), 'new@example.com');
    await user.click(within(form).getByRole('button', { name: /send invite/i }));

    expect(toastApi.addToast).toHaveBeenCalledWith(
      'success',
      expect.stringMatching(/invitation sent to new@example\.com/i),
    );
  });
});
