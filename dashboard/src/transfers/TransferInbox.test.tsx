/**
 * Tests for the v0.10.0 Phase 4 TransferInbox. Hooks are mocked at
 * `./useTransfers` (and `../hooks/useToast`) so this suite focuses on
 * the UI surface: list rendering, accept/reject/cancel wiring, and
 * empty / loading / error states.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import TransferInbox from './TransferInbox';

const { hooks, toastHook, toastApi } = vi.hoisted(() => {
  const toastApi = { addToast: vi.fn(), removeToast: vi.fn(), toasts: [] };
  return {
    hooks: {
      useTransfers: vi.fn(),
      useAcceptTransfer: vi.fn(),
      useRejectTransfer: vi.fn(),
      useCancelTransfer: vi.fn(),
    },
    toastHook: { useToast: vi.fn() },
    toastApi,
  };
});

vi.mock('./useTransfers', () => hooks);
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

function incomingTransfer(overrides: Record<string, unknown> = {}) {
  return {
    id: 'xfer_in1',
    project_id: 'proj_x',
    project_git_remote_snapshot: 'github.com/acme/x',
    project_name_snapshot: 'acme-x',
    initiated_by: 'u_initiator',
    target_user_id: 'u_me',
    from_scope: 'org_alpha',
    to_scope: 'org_beta',
    state: 'pending',
    accepted_by: null,
    created_at: '2026-05-12T00:00:00Z',
    accepted_at: null,
    updated_at: '2026-05-12T00:00:00Z',
    ...overrides,
  };
}

function outgoingTransfer(overrides: Record<string, unknown> = {}) {
  return {
    ...incomingTransfer(),
    id: 'xfer_out1',
    initiated_by: 'u_me',
    target_user_id: 'u_other',
    ...overrides,
  };
}

beforeEach(() => {
  for (const h of Object.values(hooks)) h.mockReset();
  toastHook.useToast.mockReset();
  toastApi.addToast.mockReset();

  hooks.useTransfers.mockImplementation((direction: string) => ({
    data: { transfers: direction === 'incoming' ? [incomingTransfer()] : [outgoingTransfer()] },
    isLoading: false,
    error: null,
  }));
  hooks.useAcceptTransfer.mockReturnValue(makeMutation());
  hooks.useRejectTransfer.mockReturnValue(makeMutation());
  hooks.useCancelTransfer.mockReturnValue(makeMutation());
  toastHook.useToast.mockReturnValue(toastApi);
});

describe('TransferInbox', () => {
  it('shows a loading state while either query is pending', () => {
    hooks.useTransfers.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });
    render(<TransferInbox />);
    expect(screen.getByText(/loading transfers/i)).toBeInTheDocument();
  });

  it('renders incoming and outgoing rows with scope labels and counts', () => {
    render(<TransferInbox />);
    expect(screen.getByRole('heading', { level: 3, name: /incoming \(1\)/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 3, name: /outgoing \(1\)/i })).toBeInTheDocument();
    expect(screen.getByTestId('incoming-xfer_in1')).toHaveTextContent(/acme-x/);
    expect(screen.getByTestId('outgoing-xfer_out1')).toBeInTheDocument();
  });

  it('renders empty states when there are no transfers', () => {
    hooks.useTransfers.mockReturnValue({
      data: { transfers: [] },
      isLoading: false,
      error: null,
    });
    render(<TransferInbox />);
    expect(screen.getByText(/no pending incoming transfers/i)).toBeInTheDocument();
    expect(screen.getByText(/no pending outgoing transfers/i)).toBeInTheDocument();
  });

  it('accept button fires the accept mutation and surfaces a success toast', async () => {
    const accept = makeMutation();
    accept.mutate.mockImplementation((_body: unknown, opts: { onSuccess?: () => void }) =>
      opts?.onSuccess?.(),
    );
    hooks.useAcceptTransfer.mockReturnValue(accept);

    render(<TransferInbox />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /accept transfer xfer_in1/i }));

    expect(accept.mutate).toHaveBeenCalledWith(
      { transferId: 'xfer_in1' },
      expect.any(Object),
    );
    expect(toastApi.addToast).toHaveBeenCalledWith(
      'success',
      expect.stringMatching(/accepted transfer of acme-x/i),
    );
  });

  it('reject button fires the reject mutation', async () => {
    const reject = makeMutation();
    hooks.useRejectTransfer.mockReturnValue(reject);

    render(<TransferInbox />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /reject transfer xfer_in1/i }));

    expect(reject.mutate).toHaveBeenCalledWith(
      { transferId: 'xfer_in1' },
      expect.any(Object),
    );
  });

  it('cancel button on outgoing row fires the cancel mutation', async () => {
    const cancel = makeMutation();
    hooks.useCancelTransfer.mockReturnValue(cancel);

    render(<TransferInbox />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /cancel transfer xfer_out1/i }));

    expect(cancel.mutate).toHaveBeenCalledWith(
      { transferId: 'xfer_out1' },
      expect.any(Object),
    );
  });

  it('surfaces an error toast when accept fails', async () => {
    const accept = makeMutation();
    accept.mutate.mockImplementation((_body: unknown, opts: { onError?: (e: Error) => void }) =>
      opts?.onError?.(new Error('You no longer have standing')),
    );
    hooks.useAcceptTransfer.mockReturnValue(accept);

    render(<TransferInbox />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /accept transfer xfer_in1/i }));

    expect(toastApi.addToast).toHaveBeenCalledWith(
      'error',
      expect.stringMatching(/no longer have standing/i),
    );
  });

  it('disables all action buttons while any mutation is in flight', () => {
    hooks.useAcceptTransfer.mockReturnValue(makeMutation({ isPending: true }));
    render(<TransferInbox />);
    expect(screen.getByRole('button', { name: /accept transfer xfer_in1/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /reject transfer xfer_in1/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /cancel transfer xfer_out1/i })).toBeDisabled();
  });

  it('renders a deleted-project marker when project_id is null', () => {
    hooks.useTransfers.mockImplementation((direction: string) => ({
      data: {
        transfers:
          direction === 'incoming'
            ? [incomingTransfer({ project_id: null, project_name_snapshot: 'gone' })]
            : [],
      },
      isLoading: false,
      error: null,
    }));
    render(<TransferInbox />);
    expect(screen.getByText(/\(deleted\)/i)).toBeInTheDocument();
  });
});
