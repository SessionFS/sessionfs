import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MarketplaceTab from './MarketplaceTab';
import { ApiError } from '../api/client';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useSkillInstallRequests: vi.fn(),
    approve: { mutateAsync: vi.fn(), isPending: false },
    challenge: { mutateAsync: vi.fn(), isPending: false },
    revoke: { mutateAsync: vi.fn(), isPending: false },
    cancel: { mutateAsync: vi.fn(), isPending: false },
    useMe: vi.fn(),
    addToast: vi.fn(),
  },
}));

vi.mock('../hooks/useMarketplace', () => ({
  useSkillInstallRequests: hooks.useSkillInstallRequests,
  useApproveSkillInstallRequest: () => hooks.approve,
  useChallengeSkillInstallRequest: () => hooks.challenge,
  useRevokeSkillInstallRequest: () => hooks.revoke,
  useCancelSkillInstallRequest: () => hooks.cancel,
}));
vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'pro', org_role: null } });
  hooks.challenge.mutateAsync.mockResolvedValue({ id: 'sir_1', status: 'challenge_sent', delivery: 'email', expires_at: '2026-09-01T00:10:00Z' });
  hooks.useSkillInstallRequests.mockReturnValue({
    data: {
      can_approve: true,
      items: [{
        id: 'sir_1', project_id: 'prj_1', version_id: 'skv_1', content_hash: `sha256:${'a'.repeat(64)}`,
        requested_by_user_id: 'user_1', requested_by_session_id: null, target_user_id: 'user_1',
        target_device_id: null, target_scope: 'project', target_tools: ['codex'],
        requested_capabilities: ['instructions'], requested_via: 'api', self_approval_requires_confirmation: true,
        approval_challenge_pending: false, status: 'pending', approved_by_user_id: null,
        decided_at: null, created_at: '2026-09-01T00:00:00Z',
      }],
    },
    isLoading: false,
    error: null,
  });
});

describe('MarketplaceTab', () => {
  it('requires the emailed confirmation code when the approver filed the request', async () => {
    const user = userEvent.setup();
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getByText(`sha256:${'a'.repeat(64)}`)).toBeInTheDocument();
    const approveButton = screen.getByRole('button', { name: 'Approve exact hash' });
    expect(approveButton).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Send confirmation code' }));
    expect(hooks.challenge.mutateAsync).toHaveBeenCalledWith('sir_1');
    expect(hooks.addToast).toHaveBeenCalledWith('success', expect.stringMatching(/Confirmation code sent/));
    await user.type(screen.getByLabelText('Confirmation code'), 'ab12cd34');
    expect(approveButton).toBeEnabled();
    await user.click(approveButton);
    expect(hooks.approve.mutateAsync).toHaveBeenCalledWith({
      requestId: 'sir_1',
      approvedCapabilities: [],
      confirmationCode: 'ab12cd34',
    });
  });

  it('lets a different owner/admin approve without a code', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.queryByRole('button', { name: 'Send confirmation code' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Approve exact hash' }));
    expect(hooks.approve.mutateAsync).toHaveBeenCalledWith({
      requestId: 'sir_1',
      approvedCapabilities: [],
      confirmationCode: undefined,
    });
  });

  it('requires an explicit confirmation per elevated capability before approving', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    const base = hooks.useSkillInstallRequests();
    hooks.useSkillInstallRequests.mockReturnValue({
      ...base,
      data: {
        ...base.data,
        items: [{ ...base.data.items[0], requested_capabilities: ['instructions', 'scripts', 'hooks'] }],
      },
    });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getByLabelText('Requested capabilities')).toHaveTextContent('scripts');
    expect(screen.getByLabelText('Requested capabilities')).toHaveTextContent('hooks');
    const approveButton = screen.getByRole('button', { name: 'Approve exact hash' });
    expect(approveButton).toBeDisabled();
    await user.click(screen.getByLabelText('Confirm scripts'));
    expect(approveButton).toBeDisabled();
    await user.click(screen.getByLabelText('Confirm hooks'));
    expect(approveButton).toBeEnabled();
    await user.click(approveButton);
    expect(hooks.approve.mutateAsync).toHaveBeenCalledWith({
      requestId: 'sir_1',
      approvedCapabilities: ['scripts', 'hooks'],
      confirmationCode: undefined,
    });
  });

  it('labels MCP-filed requests and still gates self-approval on the code', () => {
    const base = hooks.useSkillInstallRequests();
    hooks.useSkillInstallRequests.mockReturnValue({
      ...base,
      data: { ...base.data, items: [{ ...base.data.items[0], requested_via: 'mcp' }] },
    });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getByText('filed via MCP')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send confirmation code' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve exact hash' })).toBeDisabled();
  });

  it('does not offer approval to a non-admin project member', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_2', tier: 'pro', org_role: 'member' } });
    hooks.useSkillInstallRequests.mockReturnValue({
      ...hooks.useSkillInstallRequests(),
      data: { ...hooks.useSkillInstallRequests().data, can_approve: false },
    });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.queryByRole('button', { name: 'Approve exact hash' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
  });

  it('shows the concrete hook events and pre-approved tools next to the hooks checkbox', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    const base = hooks.useSkillInstallRequests();
    hooks.useSkillInstallRequests.mockReturnValue({
      ...base,
      data: {
        ...base.data,
        items: [{
          ...base.data.items[0],
          requested_capabilities: ['instructions', 'hooks'],
          hook_events: ['PreToolUse', 'Stop'],
          allowed_tools: ['allowed-tools:Bash'],
        }],
      },
    });
    render(<MarketplaceTab projectId="prj_1" />);
    const detail = screen.getByTestId('capability-detail-hooks');
    expect(detail).toHaveTextContent('hooks: PreToolUse, Stop');
    expect(detail).toHaveTextContent('pre-approved: allowed-tools:Bash');
  });

  it('surfaces a confirmation-code delivery failure distinctly', async () => {
    const user = userEvent.setup();
    hooks.challenge.mutateAsync.mockRejectedValue(new ApiError(503, JSON.stringify({ error: { code: 'confirmation_delivery_failed', message: 'x' } })));
    render(<MarketplaceTab projectId="prj_1" />);
    await user.click(screen.getByRole('button', { name: 'Send confirmation code' }));
    expect(hooks.addToast).toHaveBeenCalledWith('error', expect.stringMatching(/could not be delivered/));
  });

  it('offers revoke on approved grants and relays the applied-state warning', async () => {
    const user = userEvent.setup();
    hooks.revoke.mutateAsync.mockResolvedValue({ warning: 'Device state was not touched.' });
    const base = hooks.useSkillInstallRequests();
    hooks.useSkillInstallRequests.mockReturnValue({
      ...base,
      data: { ...base.data, items: [{ ...base.data.items[0], status: 'applied' }] },
    });
    render(<MarketplaceTab projectId="prj_1" />);
    await user.click(screen.getByRole('button', { name: 'Revoke grant' }));
    expect(hooks.revoke.mutateAsync).toHaveBeenCalledWith('sir_1');
    expect(hooks.addToast).toHaveBeenCalledWith('success', expect.stringMatching(/Device state was not touched/));
  });

  it('renders a useful empty state', () => {
    hooks.useSkillInstallRequests.mockReturnValue({ data: { items: [], can_approve: true }, isLoading: false, error: null });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getByText('No install requests')).toBeInTheDocument();
  });
});
