import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type HelperAction } from '../api/client';
import { axe } from '../test/axe';
import type { TurnAction } from './actions';

const { useAuthMock } = vi.hoisted(() => ({ useAuthMock: vi.fn() }));
vi.mock('../auth/AuthContext', () => ({ useAuth: useAuthMock }));

import HelperActionCard from './HelperActionCard';

const ID = 'ha_0123456789abcdef01234567';
const inMinutes = (m: number) => new Date(Date.now() + m * 60_000).toISOString();

const TRASH: TurnAction = {
  id: ID,
  kind: 'trash_sessions',
  summary: 'Move 2 session(s) to Trash',
  expires_at: inMinutes(14),
  targets: [
    { id: 'ses_a', name: 'Fix webhook retry', change: 'will_change', before: 'active', after: 'trashed', tool: 'claude-code', created_at: '2026-10-01T10:00:00Z' },
    { id: 'ses_b', name: 'Scratch notes', change: 'will_change', before: 'active', after: 'trashed' },
    { id: 'ses_c', name: 'Old spike', change: 'skipped', reason: 'already_trashed' },
  ],
};

const NARRATIVE: TurnAction = {
  id: ID,
  kind: 'set_auto_narrative',
  summary: 'Set auto-narrative on for 1 project(s)',
  expires_at: inMinutes(14),
  targets: [
    { id: 'proj_1', name: 'Acme', change: 'will_change', before: false, after: true },
    { id: 'proj_2', name: 'Docs', change: 'unchanged', before: true, after: true },
  ],
};

function record(a: TurnAction, patch: Partial<HelperAction>): HelperAction {
  return { ...a, status: 'pending', params: {}, result: {}, created_at: null, decided_at: null, ...patch };
}

const client = {
  getHelperAction: vi.fn(),
  confirmHelperAction: vi.fn(),
  cancelHelperAction: vi.fn(),
};

function renderCard(action: TurnAction, qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HelperActionCard action={action} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, qc };
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ auth: { client } });
});

describe('HelperActionCard', () => {
  it('labels a project skipped by the plan gate', () => {
    renderCard({
      ...NARRATIVE,
      targets: [
        ...NARRATIVE.targets,
        { id: 'proj_3', name: 'Free repo', change: 'skipped', reason: 'upgrade_required' },
      ],
    });
    const list = screen.getByRole('list', { name: /Targets of/ });
    expect(within(list).getByText(/Needs a plan upgrade/)).toBeInTheDocument();
  });

  it('renders a pending proposal from the stored preview', () => {
    renderCard(TRASH);
    expect(screen.getByRole('heading', { name: 'Move 2 sessions to Trash' })).toBeInTheDocument();
    expect(screen.getByText('Awaiting confirmation')).toBeInTheDocument();
    expect(screen.getByText('2 will change · 1 skipped')).toBeInTheDocument();
    const list = screen.getByRole('list', { name: /Targets of/ });
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);
    expect(within(list).getByText(/claude-code · Oct 1, 2026/)).toBeInTheDocument();
    expect(within(list).getByText(/Already in Trash/)).toBeInTheDocument();
    expect(screen.getByText(/Expires in 1[34] min/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Move to Trash' })).toBeInTheDocument();
    expect(client.getHelperAction).not.toHaveBeenCalled();
  });

  it('confirm calls the endpoint, shows per-target results and invalidates caches', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(TRASH, {
        status: 'confirmed',
        result: { targets: [{ id: 'ses_a', ok: true, error: null }, { id: 'ses_b', ok: false, error: 'not_owned' }] },
      }),
    );
    const { qc } = renderCard(TRASH);
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    await userEvent.click(screen.getByRole('button', { name: 'Move to Trash' }));
    expect(client.confirmHelperAction).toHaveBeenCalledWith(ID);
    expect(await screen.findByText('Partly applied')).toBeInTheDocument();
    expect(screen.getByText('In Trash')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText(/Not your session/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Trash' })).toHaveAttribute('href', '/?view=trash');
    expect(screen.queryByRole('button', { name: 'Move to Trash' })).not.toBeInTheDocument();
    const keys = invalidate.mock.calls.map((c) => (c[0] as { queryKey: string[] }).queryKey[0]);
    expect(keys).toEqual(expect.arrayContaining(['projects', 'project', 'sessions']));
  });

  it('cancel calls the endpoint and shows the cancelled state', async () => {
    client.cancelHelperAction.mockResolvedValue(record(NARRATIVE, { status: 'cancelled' }));
    renderCard(NARRATIVE);
    expect(screen.getByRole('heading', { name: 'Turn Auto Narrative on for 1 project' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(client.cancelHelperAction).toHaveBeenCalledWith(ID);
    expect(await screen.findByText('Cancelled — nothing was changed.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('a 409 action_expired on confirm shows the expired state', async () => {
    client.confirmHelperAction.mockRejectedValue(
      new ApiError(409, JSON.stringify({ error: { code: 'action_expired', message: 'expired' } })),
    );
    renderCard(NARRATIVE);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(await screen.findByText('Expired')).toBeInTheDocument();
    expect(screen.getByText(/Expired before it was confirmed/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('other errors show an alert with retry', async () => {
    client.confirmHelperAction
      .mockRejectedValueOnce(new ApiError(500, 'boom'))
      .mockResolvedValueOnce(
        record(NARRATIVE, { status: 'confirmed', result: { targets: [{ id: 'proj_1', ok: true, error: null }] } }),
      );
    renderCard(NARRATIVE);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Couldn’t apply the change')).toBeInTheDocument();
    await userEvent.click(within(alert).getByRole('button', { name: /Retry/ }));
    expect(await screen.findByText('Done')).toBeInTheDocument();
    expect(client.confirmHelperAction).toHaveBeenCalledTimes(2);
  });

  it('a card restored from history reads its status from the server', async () => {
    client.getHelperAction.mockResolvedValue(
      record(NARRATIVE, { status: 'expired', params: { project_ids: ['proj_1'], enabled: true } }),
    );
    renderCard({ id: ID, kind: 'set_auto_narrative', summary: '', targets: [], expires_at: null, fromHistory: true });
    expect(screen.getByText(/Checking this action/)).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Turn Auto Narrative on for 1 project' })).toBeInTheDocument();
    expect(client.getHelperAction).toHaveBeenCalledWith(ID);
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('a pending card whose clock ran out shows as expired', () => {
    renderCard({ ...NARRATIVE, expires_at: inMinutes(-1) });
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('has no axe violations pending and after confirm', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(TRASH, { status: 'confirmed', result: { targets: [{ id: 'ses_a', ok: true, error: null }, { id: 'ses_b', ok: true, error: null }] } }),
    );
    const { container } = renderCard(TRASH);
    expect(await axe(container)).toHaveNoViolations();
    await userEvent.click(screen.getByRole('button', { name: 'Move to Trash' }));
    await waitFor(() => expect(screen.getByText('Done')).toBeInTheDocument());
    expect(await axe(container)).toHaveNoViolations();
  });
});
