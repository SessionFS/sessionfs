/**
 * Tests for the v0.10.0 Phase 4 TransferPanel. Hooks at
 * `../transfers/useTransfers` and `../hooks/useToast` are mocked so the
 * suite focuses on the UI surface: destination filtering, initiate vs
 * pending-cancel branches, and toast wiring.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import TransferPanel from './TransferPanel';

const { hooks, toastHook, toastApi } = vi.hoisted(() => {
  const toastApi = { addToast: vi.fn(), removeToast: vi.fn(), toasts: [] };
  return {
    hooks: {
      useTransfers: vi.fn(),
      useInitiateTransfer: vi.fn(),
      useCancelTransfer: vi.fn(),
    },
    toastHook: { useToast: vi.fn() },
    toastApi,
  };
});

vi.mock('../transfers/useTransfers', () => hooks);
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

const ORGS = [
  { org_id: 'org_alpha', name: 'Alpha Co' },
  { org_id: 'org_beta', name: 'Beta Co' },
];

beforeEach(() => {
  for (const h of Object.values(hooks)) h.mockReset();
  toastHook.useToast.mockReset();
  toastApi.addToast.mockReset();

  hooks.useTransfers.mockReturnValue({
    data: { transfers: [] },
    isLoading: false,
    error: null,
  });
  hooks.useInitiateTransfer.mockReturnValue(makeMutation());
  hooks.useCancelTransfer.mockReturnValue(makeMutation());
  toastHook.useToast.mockReturnValue(toastApi);
});

describe('TransferPanel', () => {
  it('lists destinations excluding the current scope (personal source)', () => {
    render(
      <TransferPanel
        projectId="proj_x"
        currentScope="personal"
        availableOrgs={ORGS}
      />,
    );
    const select = screen.getByLabelText(/transfer destination/i);
    const options = Array.from(select.querySelectorAll('option')).map((o) => o.value);
    // Personal is excluded (it's the current scope), but both orgs appear.
    expect(options).toEqual(['org_alpha', 'org_beta']);
  });

  it('lists destinations excluding the current org scope', () => {
    render(
      <TransferPanel
        projectId="proj_x"
        currentScope="org_alpha"
        availableOrgs={ORGS}
      />,
    );
    const select = screen.getByLabelText(/transfer destination/i);
    const options = Array.from(select.querySelectorAll('option')).map((o) => o.value);
    expect(options).toEqual(['personal', 'org_beta']);
  });

  it('shows an empty-destinations message when no candidates are available', () => {
    render(
      <TransferPanel
        projectId="proj_x"
        currentScope="personal"
        availableOrgs={[]}
      />,
    );
    expect(screen.getByText(/no transfer destinations available/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/transfer destination/i)).toBeNull();
  });

  it('initiate fires the mutation with the chosen destination', async () => {
    const initiate = makeMutation();
    hooks.useInitiateTransfer.mockReturnValue(initiate);

    render(
      <TransferPanel
        projectId="proj_x"
        currentScope="personal"
        availableOrgs={ORGS}
      />,
    );
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText(/transfer destination/i), 'org_beta');
    await user.click(screen.getByRole('button', { name: /initiate transfer/i }));

    expect(initiate.mutate).toHaveBeenCalledWith(
      { to: 'org_beta' },
      expect.any(Object),
    );
  });

  it('surfaces a "moved" toast when the server auto-accepts', async () => {
    const initiate = makeMutation();
    initiate.mutate.mockImplementation(
      (_body: unknown, opts: { onSuccess?: (t: { state: string; to_scope: string }) => void }) =>
        opts?.onSuccess?.({ state: 'accepted', to_scope: 'org_beta' }),
    );
    hooks.useInitiateTransfer.mockReturnValue(initiate);

    render(
      <TransferPanel
        projectId="proj_x"
        currentScope="personal"
        availableOrgs={ORGS}
      />,
    );
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText(/transfer destination/i), 'org_beta');
    await user.click(screen.getByRole('button', { name: /initiate transfer/i }));

    expect(toastApi.addToast).toHaveBeenCalledWith(
      'success',
      expect.stringMatching(/project moved to beta co/i),
    );
  });

  it('shows the pending-cancel branch when an outgoing pending row exists for this project', async () => {
    hooks.useTransfers.mockReturnValue({
      data: {
        transfers: [
          {
            id: 'xfer_99',
            project_id: 'proj_x',
            project_git_remote_snapshot: null,
            project_name_snapshot: 'proj_x',
            initiated_by: 'u_me',
            target_user_id: 'u_admin',
            from_scope: 'personal',
            to_scope: 'org_beta',
            state: 'pending',
            accepted_by: null,
            created_at: '2026-05-12T00:00:00Z',
            accepted_at: null,
            updated_at: '2026-05-12T00:00:00Z',
          },
        ],
      },
      isLoading: false,
      error: null,
    });
    const cancel = makeMutation();
    hooks.useCancelTransfer.mockReturnValue(cancel);

    render(
      <TransferPanel
        projectId="proj_x"
        currentScope="personal"
        availableOrgs={ORGS}
      />,
    );
    // The destination form is hidden while a pending transfer exists.
    expect(screen.queryByLabelText(/transfer destination/i)).toBeNull();
    expect(screen.getByText(/pending transfer to beta co/i)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /cancel pending transfer/i }));
    expect(cancel.mutate).toHaveBeenCalledWith(
      { transferId: 'xfer_99' },
      expect.any(Object),
    );
  });

  it('surfaces an error toast when initiate fails', async () => {
    const initiate = makeMutation();
    initiate.mutate.mockImplementation(
      (_body: unknown, opts: { onError?: (e: Error) => void }) =>
        opts?.onError?.(new Error('Destination org has no admin to accept the transfer')),
    );
    hooks.useInitiateTransfer.mockReturnValue(initiate);

    render(
      <TransferPanel
        projectId="proj_x"
        currentScope="personal"
        availableOrgs={ORGS}
      />,
    );
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText(/transfer destination/i), 'org_beta');
    await user.click(screen.getByRole('button', { name: /initiate transfer/i }));

    expect(toastApi.addToast).toHaveBeenCalledWith(
      'error',
      expect.stringMatching(/no admin to accept/i),
    );
  });
});
