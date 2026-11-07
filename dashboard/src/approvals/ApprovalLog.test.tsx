import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ApprovalLogRow } from '../api/client';
import { axe } from '../test/axe';
import GovernanceSection from './GovernanceSection';

const { client } = vi.hoisted(() => ({
  client: {
    listApprovalPolicies: vi.fn(),
    listProjects: vi.fn(),
    getApprovalLog: vi.fn(),
    exportApprovalLogCsv: vi.fn(),
  },
}));

vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ auth: { client, apiKey: 'k', baseUrl: '' } }) }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));
vi.mock('../org/useOrgMembers', () => ({
  useOrgMembers: () => ({
    data: { members: [{ user_id: 'u_ada', email: 'ada@acme.test', display_name: 'Ada', role: 'admin' }] },
  }),
}));

const ORG = 'org_1';
const row = (n: number, over: Partial<ApprovalLogRow> = {}): ApprovalLogRow => ({
  decision_id: `ad_${n}`,
  decided_at: '2026-11-01T10:00:00Z',
  decision: 'approve',
  approver_user_id: 'u_ada',
  approver_name: 'Ada',
  reason: 'Checked the token handling.',
  request_id: `ar_${n}`,
  request_state: 'approved',
  policy_id: 'ap_1',
  policy_name: 'Auth changes',
  policy_version: 2,
  project_id: 'proj_pay',
  target_kind: 'merge',
  target_id: 'tk_1',
  target_fingerprint: 'a'.repeat(40),
  requested_at: '2026-10-31T10:00:00Z',
  ...over,
});

function renderIt(isAdmin: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <GovernanceSection orgId={ORG} isAdmin={isAdmin} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  for (const f of Object.values(client)) f.mockReset();
  client.listApprovalPolicies.mockResolvedValue({
    policies: [{ id: 'ap_1', lineage_id: 'ap_1', name: 'Auth changes', target_kind: 'merge', requirements: [], blocking: true, active: true, version: 2, filter: {} }],
    can_manage: true,
    plan_includes_approvals: true,
  });
  client.listProjects.mockResolvedValue([{ id: 'proj_pay', name: 'Payments', org_id: ORG }]);
  client.getApprovalLog.mockResolvedValue({ items: [row(1)], next_before: null });
  URL.createObjectURL = vi.fn(() => 'blob:csv');
  URL.revokeObjectURL = vi.fn();
});

describe('Governance approval log', () => {
  it('hides the log tab from non-admins', async () => {
    renderIt(false);
    await screen.findByText('Approval policies');
    expect(screen.queryByRole('tab', { name: 'Approval log' })).toBeNull();
    expect(client.getApprovalLog).not.toHaveBeenCalled();
  });

  it('shows decisions for admins and has no axe violations', async () => {
    const { container } = renderIt(true);
    await userEvent.click(await screen.findByRole('tab', { name: 'Approval log' }));
    expect(await screen.findByTestId('log-ad_1')).toHaveTextContent('Payments');
    expect(screen.getByTestId('log-ad_1')).toHaveTextContent('Auth changes');
    expect(await axe(container)).toHaveNoViolations();
  });

  it('applies filters and follows next_before', async () => {
    client.getApprovalLog.mockImplementation(async (_o: string, _f: unknown, before?: string | null) =>
      before ? { items: [row(2, { decision: 'reject', request_state: 'rejected' })], next_before: null }
        : { items: [row(1)], next_before: '2026-11-01T10:00:00+00:00|ad_1' });
    renderIt(true);
    await userEvent.click(await screen.findByRole('tab', { name: 'Approval log' }));
    await screen.findByTestId('log-ad_1');
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByTestId('log-ad_2');
    expect(client.getApprovalLog).toHaveBeenLastCalledWith(ORG, {}, '2026-11-01T10:00:00+00:00|ad_1');
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();

    await userEvent.type(screen.getByLabelText('From'), '2026-11-01');
    await waitFor(() =>
      expect(client.getApprovalLog).toHaveBeenLastCalledWith(
        ORG, expect.objectContaining({ since: '2026-11-01T00:00:00Z' }), null),
    );
  });

  it('exports CSV with the auth header call and offers a blob link, never window.open', async () => {
    const open = vi.spyOn(window, 'open');
    client.exportApprovalLogCsv.mockResolvedValue({ blob: new Blob(['a,b']), truncated: true });
    renderIt(true);
    await userEvent.click(await screen.findByRole('tab', { name: 'Approval log' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Export CSV' }));
    const link = await screen.findByRole('link', { name: /Download approval-log/ });
    expect(link).toHaveAttribute('href', 'blob:csv');
    expect(link).toHaveAttribute('download', `approval-log-${ORG}.csv`);
    expect(screen.getByText(/stops at 10,000 rows/)).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
  });

  it('reports an export failure', async () => {
    client.exportApprovalLogCsv.mockRejectedValue(new Error('boom'));
    renderIt(true);
    await userEvent.click(await screen.findByRole('tab', { name: 'Approval log' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Export CSV' }));
    expect(await screen.findByText(/Could not export the log/)).toBeInTheDocument();
  });
});
