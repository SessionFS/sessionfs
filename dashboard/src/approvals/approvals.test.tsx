import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  type ApprovalPolicy,
  type ApprovalPolicyList,
  type ApprovalQueueItem,
  type ApprovalRequestDetail,
  type TicketApproval,
  type TicketNotification,
} from '../api/client';
import { axe } from '../test/axe';
import TicketInbox from '../notifications/TicketInbox';
import { TrustSeal } from '../projects/loop/TrustSeal';
import ApprovalsPage from './ApprovalsPage';
import PoliciesSection from './PoliciesSection';
import { buildPolicyBody, emptyForm, validateForm } from './policyForm';
import { summarizeFilter, summarizeRequirements } from './format';

const { client, toast } = vi.hoisted(() => ({
  client: {
    listApprovalPolicies: vi.fn(),
    createApprovalPolicy: vi.fn(),
    updateApprovalPolicy: vi.fn(),
    deactivateApprovalPolicy: vi.fn(),
    listMyApprovals: vi.fn(),
    getApprovalRequest: vi.fn(),
    decideApprovalRequest: vi.fn(),
    listProjects: vi.fn(),
    listTicketNotifications: vi.fn(),
    markTicketNotificationsRead: vi.fn(),
  },
  toast: { addToast: vi.fn() },
}));

vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ auth: { client, apiKey: 'k', baseUrl: '' } }) }));
vi.mock('../hooks/useToast', () => ({ useToast: () => toast }));
vi.mock('../org/useOrgMembers', () => ({
  useOrgMembers: () => ({
    data: {
      members: [
        { user_id: 'u_ada', email: 'ada@acme.test', display_name: 'Ada', role: 'admin' },
        { user_id: 'u_bo', email: 'bo@acme.test', display_name: null, role: 'member' },
      ],
    },
  }),
}));

const ORG = 'org_1';
const apiErr = (status: number, code: string, message: string) =>
  new ApiError(status, JSON.stringify({ detail: { error: code, code, message } }));

function policy(over: Partial<ApprovalPolicy> = {}): ApprovalPolicy {
  return {
    id: 'ap_1',
    lineage_id: 'ap_1',
    name: 'Auth changes',
    description: 'Security signs off',
    target_kind: 'merge',
    requirements: [{ who: 'org_role', role: 'admin', count: 1 }],
    filter: { project_ids: ['proj_pay'], finding_harm_any: ['data-exposed'] },
    blocking: true,
    active: true,
    version: 2,
    ...over,
  };
}

function list(over: Partial<ApprovalPolicyList> = {}): ApprovalPolicyList {
  return { policies: [policy()], can_manage: true, plan_includes_approvals: true, ...over };
}

function renderAt(ui: React.ReactElement, url = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const detail = (over: Partial<ApprovalRequestDetail> = {}): ApprovalRequestDetail => ({
  id: 'ar_1',
  policy: {
    id: 'ap_1',
    name: 'Auth changes',
    version: 2,
    blocking: true,
    requirements: [{ who: 'org_role', role: 'admin', count: 1 }],
  },
  org_id: ORG,
  project_id: 'proj_pay',
  target_kind: 'merge',
  target_id: 'tk_123',
  target_fingerprint: 'a'.repeat(40),
  state: 'pending',
  requested_at: '2026-10-31T10:00:00Z',
  expires_at: '2026-11-14T10:00:00Z',
  decided_at: null,
  approved_count: 0,
  required_count: 1,
  can_decide: true,
  decisions: [],
  ...over,
});

const queueItem = (over: Partial<ApprovalQueueItem> = {}): ApprovalQueueItem => ({
  id: 'ar_1',
  policy_id: 'ap_1',
  policy_name: 'Auth changes',
  org_id: ORG,
  project_id: 'proj_pay',
  target_kind: 'merge',
  target_id: 'tk_123',
  target_title: 'Rotate session tokens',
  target_fingerprint: 'a'.repeat(40),
  state: 'pending',
  requested_at: '2026-10-31T10:00:00Z',
  expires_at: '2026-11-14T10:00:00Z',
  approved_count: 0,
  required_count: 1,
  ...over,
});

beforeEach(() => {
  for (const f of Object.values(client)) f.mockReset();
  toast.addToast.mockReset();
  client.listApprovalPolicies.mockResolvedValue(list());
  client.listProjects.mockResolvedValue([
    { id: 'proj_pay', name: 'Payments', org_id: ORG },
    { id: 'proj_id', name: 'Identity', org_id: ORG },
    { id: 'proj_x', name: 'Elsewhere', org_id: 'org_other' },
  ]);
  client.listMyApprovals.mockResolvedValue({ requests: [queueItem()], count: 1 });
  client.getApprovalRequest.mockResolvedValue(detail());
  client.decideApprovalRequest.mockResolvedValue(detail({ state: 'approved', approved_count: 1 }));
});

describe('policy form helpers', () => {
  it('summarises requirements and filters', () => {
    expect(summarizeRequirements([{ who: 'org_role', role: 'admin', count: 2 }])).toBe('2 org admin or owners');
    expect(summarizeRequirements([{ who: 'users', user_ids: ['a', 'b'], count: 1 }], (id) => id.toUpperCase())).toBe(
      '1 of A, B',
    );
    expect(summarizeFilter({ priority_at_least: 'high', finding_harm_any: ['crash'] })).toBe(
      'Priority high or above · Harm: crash',
    );
  });

  it('validates name, counts and classes', () => {
    const f = emptyForm();
    f.requirements[0].count = '0';
    f.checklistClasses = 'Bad Class';
    const errs = validateForm(f);
    expect(errs.name).toBeTruthy();
    expect(errs.rows?.[f.requirements[0].key]).toMatch(/1 to 20/);
    expect(errs.checklistClasses).toMatch(/Bad/);
  });

  it('builds only the filter keys that belong to the target', () => {
    const f = emptyForm();
    f.name = '  KB gate ';
    f.targetKind = 'kb_promote';
    f.entryTypes = ['decision'];
    f.entityRefPrefixes = 'docs/compliance/\ndocs/compliance/\n';
    f.priority = 'high';
    expect(buildPolicyBody(f)).toEqual({
      name: 'KB gate',
      description: '',
      target_kind: 'kb_promote',
      filter: { entry_type_any: ['decision'], entity_ref_prefix_any: ['docs/compliance/'] },
      requirements: [{ who: 'org_role', role: 'admin', count: 1 }],
      blocking: true,
    });
  });
});

describe('PoliciesSection', () => {
  it('lists policies with target, requirements, mode and version', async () => {
    renderAt(<PoliciesSection orgId={ORG} />);
    const row = await screen.findByTestId('policy-ap_1');
    expect(row).toHaveTextContent('Auth changes');
    expect(row).toHaveTextContent('Payments');
    expect(row).toHaveTextContent('Merge');
    expect(row).toHaveTextContent('1 org admin or owner');
    expect(row).toHaveTextContent('Blocking');
    expect(row).toHaveTextContent('v2');
  });

  it('shows validation errors and sends nothing when the form is invalid', async () => {
    const user = userEvent.setup();
    renderAt(<PoliciesSection orgId={ORG} />);
    await user.click(await screen.findByRole('button', { name: 'New policy' }));
    const dlg = await screen.findByRole('dialog', { name: 'New approval policy' });
    await user.click(within(dlg).getByRole('button', { name: 'Create policy' }));
    expect(await within(dlg).findByText('Give the policy a name.')).toBeInTheDocument();
    expect(client.createApprovalPolicy).not.toHaveBeenCalled();
  });

  it('creates a policy with the structured body', async () => {
    client.createApprovalPolicy.mockResolvedValue(policy());
    const user = userEvent.setup();
    renderAt(<PoliciesSection orgId={ORG} />);
    await user.click(await screen.findByRole('button', { name: 'New policy' }));
    const dlg = await screen.findByRole('dialog', { name: 'New approval policy' });
    await user.type(within(dlg).getByLabelText('Name'), 'Auth gate');
    await user.click(within(dlg).getByRole('checkbox', { name: 'Payments' }));
    expect(within(dlg).queryByRole('checkbox', { name: 'Elsewhere' })).not.toBeInTheDocument();
    await user.click(within(dlg).getByRole('checkbox', { name: 'Data exposed' }));
    await user.click(within(dlg).getByRole('button', { name: /Priority at least/ }));
    await user.click(screen.getByRole('option', { name: 'high' }));
    // Switch the requirement to named people.
    const req = within(dlg).getByRole('group', { name: 'Requirement 1' });
    await user.click(within(req).getByRole('button', { name: /Approver/ }));
    await user.click(screen.getByRole('option', { name: 'Named people' }));
    await user.click(within(dlg).getByRole('checkbox', { name: 'Ada (ada@acme.test)' }));
    await user.click(within(dlg).getByRole('button', { name: 'Create policy' }));
    await waitFor(() => expect(client.createApprovalPolicy).toHaveBeenCalled());
    expect(client.createApprovalPolicy).toHaveBeenCalledWith(ORG, {
      name: 'Auth gate',
      description: '',
      target_kind: 'merge',
      filter: { project_ids: ['proj_pay'], priority_at_least: 'high', finding_harm_any: ['data-exposed'] },
      requirements: [{ who: 'users', user_ids: ['u_ada'], count: 1 }],
      blocking: true,
    });
  });

  it('rejects a user count above the people picked', async () => {
    const user = userEvent.setup();
    renderAt(<PoliciesSection orgId={ORG} />);
    await user.click(await screen.findByRole('button', { name: 'New policy' }));
    const dlg = await screen.findByRole('dialog', { name: 'New approval policy' });
    await user.type(within(dlg).getByLabelText('Name'), 'Two of one');
    const req = within(dlg).getByRole('group', { name: 'Requirement 1' });
    await user.click(within(req).getByRole('button', { name: /Approver/ }));
    await user.click(screen.getByRole('option', { name: 'Named people' }));
    await user.click(within(dlg).getByRole('checkbox', { name: 'Ada (ada@acme.test)' }));
    const count = within(req).getByLabelText('Approvals needed');
    await user.clear(count);
    await user.type(count, '2');
    await user.click(within(dlg).getByRole('button', { name: 'Create policy' }));
    expect(await within(dlg).findByText('Count cannot exceed the number of people picked.')).toBeInTheDocument();
    expect(client.createApprovalPolicy).not.toHaveBeenCalled();
  });

  it('edits as a new version and shows a server error', async () => {
    client.updateApprovalPolicy.mockRejectedValue(
      apiErr(422, 'approver_not_eligible', 'Every named approver must be an active member.'),
    );
    const user = userEvent.setup();
    renderAt(<PoliciesSection orgId={ORG} />);
    await user.click(await screen.findByRole('button', { name: 'Edit Auth changes' }));
    const dlg = await screen.findByRole('dialog', { name: 'Edit approval policy' });
    expect(dlg).toHaveTextContent('Saving creates version 3');
    expect(within(dlg).getByLabelText('Name')).toHaveValue('Auth changes');
    await user.click(within(dlg).getByRole('button', { name: 'Save new version' }));
    await waitFor(() => expect(client.updateApprovalPolicy).toHaveBeenCalled());
    expect(client.updateApprovalPolicy.mock.calls[0][1]).toBe('ap_1');
    expect(await within(dlg).findByRole('alert')).toHaveTextContent('Every named approver');
  });

  it('disables a policy after confirmation', async () => {
    client.deactivateApprovalPolicy.mockResolvedValue(policy({ active: false }));
    const user = userEvent.setup();
    renderAt(<PoliciesSection orgId={ORG} />);
    await user.click(await screen.findByRole('button', { name: 'Disable Auth changes' }));
    const dlg = await screen.findByRole('dialog', { name: 'Disable this policy?' });
    expect(client.deactivateApprovalPolicy).not.toHaveBeenCalled();
    await user.click(within(dlg).getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(client.deactivateApprovalPolicy).toHaveBeenCalledWith(ORG, 'ap_1'));
  });

  it('is read-only for a non-admin', async () => {
    client.listApprovalPolicies.mockResolvedValue(
      list({ can_manage: false, policies: [{ ...policy(), filter: undefined, description: undefined }] }),
    );
    renderAt(<PoliciesSection orgId={ORG} />);
    expect(await screen.findByTestId('policy-ap_1')).toHaveTextContent('Auth changes');
    expect(screen.queryByRole('button', { name: 'New policy' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit|Disable/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Only organization owners and admins can change policies/)).toBeInTheDocument();
  });

  it('shows an upgrade note and no create form without the plan', async () => {
    client.listApprovalPolicies.mockResolvedValue(list({ plan_includes_approvals: false }));
    renderAt(<PoliciesSection orgId={ORG} />);
    expect(await screen.findByText('Approval policies are an Enterprise feature')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'New policy' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit Auth changes' })).not.toBeInTheDocument();
    // Disabling still works so an enforced gate can be cleared.
    expect(screen.getByRole('button', { name: 'Disable Auth changes' })).toBeInTheDocument();
  });

  it('has no axe violations with the dialog open', async () => {
    const user = userEvent.setup();
    renderAt(<PoliciesSection orgId={ORG} />);
    await user.click(await screen.findByRole('button', { name: 'New policy' }));
    await screen.findByRole('dialog', { name: 'New approval policy' });
    expect(await axe(document.body)).toHaveNoViolations();
  });
});

describe('ApprovalsPage', () => {
  it('lists the queue with policy, counts and expiry', async () => {
    renderAt(<ApprovalsPage />, '/approvals');
    const row = await screen.findByTestId('approval-ar_1');
    expect(row).toHaveTextContent('Rotate session tokens');
    expect(row).toHaveTextContent('Auth changes');
    expect(row).toHaveTextContent('0 of 1 approved');
    expect(row).toHaveTextContent('Pending');
    expect(within(row).getByRole('link', { name: 'Open target' })).toHaveAttribute(
      'href',
      '/projects/proj_pay?tab=work&sub=tickets&ticket=tk_123',
    );
  });

  it('shows an empty state', async () => {
    client.listMyApprovals.mockResolvedValue({ requests: [], count: 0 });
    renderAt(<ApprovalsPage />, '/approvals');
    expect(await screen.findByText('Nothing is waiting for your approval.')).toBeInTheDocument();
  });

  it('opens the drawer from the deep link and requires a reason', async () => {
    const user = userEvent.setup();
    renderAt(<ApprovalsPage />, '/approvals?request=ar_1');
    const dlg = await screen.findByRole('dialog', { name: 'Auth changes' });
    expect(client.getApprovalRequest).toHaveBeenCalledWith('ar_1');
    expect(dlg).toHaveTextContent('Policy v2');
    expect(dlg).toHaveTextContent('1 org admin or owner');
    expect(dlg).toHaveTextContent('No decisions yet.');
    await user.click(within(dlg).getByRole('button', { name: 'Approve' }));
    expect(await within(dlg).findByText(/Give a reason/)).toBeInTheDocument();
    expect(client.decideApprovalRequest).not.toHaveBeenCalled();
  });

  it('approves with a reason', async () => {
    const user = userEvent.setup();
    renderAt(<ApprovalsPage />, '/approvals?request=ar_1');
    const dlg = await screen.findByRole('dialog', { name: 'Auth changes' });
    await user.type(within(dlg).getByLabelText('Reason'), ' Looks right ');
    await user.click(within(dlg).getByRole('button', { name: 'Approve' }));
    await waitFor(() =>
      expect(client.decideApprovalRequest).toHaveBeenCalledWith('ar_1', { decision: 'approve', reason: 'Looks right' }),
    );
    expect(toast.addToast).toHaveBeenCalledWith('success', 'Approved');
  });

  it('rejects with a reason and shows a server error', async () => {
    client.decideApprovalRequest.mockRejectedValue(
      apiErr(403, 'self_approval_forbidden', 'You cannot approve your own work.'),
    );
    const user = userEvent.setup();
    renderAt(<ApprovalsPage />, '/approvals?request=ar_1');
    const dlg = await screen.findByRole('dialog', { name: 'Auth changes' });
    await user.type(within(dlg).getByLabelText('Reason'), 'No');
    await user.click(within(dlg).getByRole('button', { name: 'Reject' }));
    expect(await within(dlg).findByText('You cannot approve your own work.')).toBeInTheDocument();
    expect(client.decideApprovalRequest).toHaveBeenCalledWith('ar_1', { decision: 'reject', reason: 'No' });
  });

  it('hides the decision form when the viewer cannot decide, and lists decisions', async () => {
    client.getApprovalRequest.mockResolvedValue(
      detail({
        can_decide: false,
        decisions: [
          {
            id: 'ad_1',
            approver_user_id: 'u_ada',
            approver_name: 'Ada',
            requirement_index: 0,
            decision: 'reject',
            reason: 'Needs a test',
            decided_at: '2026-10-31T11:00:00Z',
          },
        ],
      }),
    );
    renderAt(<ApprovalsPage />, '/approvals?request=ar_1');
    const dlg = await screen.findByRole('dialog', { name: 'Auth changes' });
    expect(dlg).toHaveTextContent('Ada');
    expect(dlg).toHaveTextContent('Needs a test');
    expect(within(dlg).queryByLabelText('Reason')).not.toBeInTheDocument();
    expect(within(dlg).queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('shows an unavailable request', async () => {
    client.getApprovalRequest.mockRejectedValue(apiErr(404, 'not_found', 'Approval request not found'));
    renderAt(<ApprovalsPage />, '/approvals?request=ar_gone');
    expect(await screen.findByRole('alert')).toHaveTextContent('Approval request not found');
  });

  it('has no axe violations with the drawer open', async () => {
    renderAt(<ApprovalsPage />, '/approvals?request=ar_1');
    await screen.findByRole('dialog', { name: 'Auth changes' });
    expect(await axe(document.body)).toHaveNoViolations();
  });
});

describe('readiness seal', () => {
  const pending: TicketApproval = {
    request_id: 'ar_9',
    policy_id: 'ap_1',
    policy_name: 'Auth changes',
    state: 'pending',
    blocking: true,
    approved_count: 0,
    required_count: 2,
  };

  it('says awaiting approval with counts and links to the request', () => {
    renderAt(
      <TrustSeal
        readiness={{
          ready: false,
          reasons: ['Waiting on approval: Auth changes (0 of 2)'],
          verdict: null,
          approvals: [pending],
        }}
      />,
    );
    const line = screen.getByRole('list', { name: 'Awaiting approval' });
    expect(line).toHaveTextContent('Awaiting approval: Auth changes (0 of 2)');
    expect(within(line).getByRole('link', { name: 'Auth changes' })).toHaveAttribute('href', '/approvals?request=ar_9');
    // The server's own restatement is not repeated.
    expect(screen.queryByText(/Waiting on approval/)).not.toBeInTheDocument();
  });

  it('shows advisory warnings quietly on a ready change', () => {
    renderAt(
      <TrustSeal
        readiness={{ ready: true, reasons: [], verdict: null, warnings: ['Advisory: Style review not approved (0 of 1)'] }}
      />,
    );
    expect(screen.getByText('Ready')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Approval notes' })).toHaveTextContent('Style review');
  });
});

describe('Inbox approval rows', () => {
  it('links an approval notification to the request drawer', async () => {
    const row: TicketNotification = {
      id: 7,
      subject_kind: 'approval',
      subject: { kind: 'approval', id: 'ar_7', title: 'Approval needed: Rotate session tokens' },
      ticket_id: null,
      project_id: null,
      ticket_title: null,
      kind: 'approval_requested',
      summary: 'Auth changes needs your approval',
      actor_type: 'user',
      actor_user_id: null,
      service_key_name: null,
      event_count: 1,
      created_at: '2026-10-31T10:00:00Z',
      updated_at: '2026-10-31T10:00:00Z',
      read_at: null,
    };
    client.listTicketNotifications.mockResolvedValue({ items: [row], next_before: null });
    renderAt(<TicketInbox />, '/inbox');
    const link = await screen.findByRole('link', { name: 'Approval needed: Rotate session tokens' });
    expect(link).toHaveAttribute('href', '/approvals?request=ar_7');
    const card = screen.getByTestId('notification-7');
    expect(card).toHaveTextContent('Approval');
    expect(card).toHaveTextContent('Auth changes needs your approval');
  });
});
