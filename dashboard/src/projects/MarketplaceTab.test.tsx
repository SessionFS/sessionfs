import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MarketplaceTab from './MarketplaceTab';
import { ApiError } from '../api/client';

type DisclosureMock = { data: unknown; isLoading: boolean; error: unknown; isFetching?: boolean; refetch?: () => void };

const { DISCLOSURE } = vi.hoisted(() => ({
  DISCLOSURE: {
    version_id: 'skv_1', content_hash: null, capability_digest: 'sha256:x', capabilities: ['instructions'],
    provenance: { publisher_verified: false, attested: false }, scan: {}, unicode: { flagged_files: 0, findings: [] },
    shell_commands: [], network_endpoints: [], env_names: [],
    mcp_servers: { local: [], remote: [], declared: false }, allowed_tools: [], persistence_targets: [],
    executable_extensions: [], tools: {}, consent_required: ['instructions'],
    disclosure_digest: 'sha256:card',
  },
}));

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useSkillInstallRequests: vi.fn(),
    approve: { mutateAsync: vi.fn(), isPending: false },
    challenge: { mutateAsync: vi.fn(), isPending: false },
    revoke: { mutateAsync: vi.fn(), isPending: false },
    cancel: { mutateAsync: vi.fn(), isPending: false },
    useRevocationIndex: vi.fn(() => ({ data: undefined as unknown })),
    useMe: vi.fn(),
    addToast: vi.fn(),
    useMarketplaceDisclosure: vi.fn((_versionId?: string, _tools?: string[], _scope?: string): DisclosureMock => ({ data: DISCLOSURE, isLoading: false, error: null })),
  },
}));

vi.mock('../hooks/useMarketplace', () => ({
  useSkillInstallRequests: hooks.useSkillInstallRequests,
  useApproveSkillInstallRequest: () => hooks.approve,
  useChallengeSkillInstallRequest: () => hooks.challenge,
  useRevokeSkillInstallRequest: () => hooks.revoke,
  useCancelSkillInstallRequest: () => hooks.cancel,
  useRevocationIndex: () => hooks.useRevocationIndex(),
}));
vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../hooks/useMarketplaceDisclosure', () => ({
  useMarketplaceDisclosure: (versionId?: string, tools?: string[], scope?: string) => hooks.useMarketplaceDisclosure(versionId, tools, scope),
}));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));

/** A build_diff() object as the server nests it under details.diff. */
function updateDiff(overrides: Record<string, unknown> = {}) {
  return {
    update_class: 'widening',
    baseline_known: false,
    widens: [{ kind: 'new_network_endpoint', item: 'sink.example.org' }],
    narrows: [],
    code_changes: [],
    changed_requirements: ['skill-scripts', 'skill-hooks'],
    capability_digest_changed: true,
    files: { added: [], removed: [], changed: [], unknown: true },
    scan_delta: { old_max_severity: null, new_max_severity: 'high', old_finding_count: null, new_finding_count: 1, old_ruleset: null, new_ruleset: '2026.10.1' },
    unicode_delta: { new_findings: [] },
    provenance_delta: { old: { publisher_verified: false, attested: false }, new: { publisher_verified: false, attested: false } },
    consent: { personal: 'explicit_reconsent', org_required: 'never_auto_apply', description: 'The update asks for more access: it always stops and requires explicit re-consent.' },
    ...overrides,
  };
}

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
      acknowledgements: { capabilityDigest: 'sha256:x', consentRuleVersion: 2, disclosureDigest: 'sha256:card' },
    });
  });

  it('badges a blocked version and withholds approval', () => {
    hooks.useRevocationIndex.mockReturnValueOnce({ data: { packages: new Map(), versions: new Map([['skv_1', 'blocked']]) } });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getByText('blocked')).toBeInTheDocument();
    expect(screen.getByText('This version is blocked; it can no longer be approved.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve exact hash' })).not.toBeInTheDocument();
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
      acknowledgements: { capabilityDigest: 'sha256:x', consentRuleVersion: 2, disclosureDigest: 'sha256:card' },
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
      acknowledgements: { capabilityDigest: 'sha256:x', consentRuleVersion: 2, disclosureDigest: 'sha256:card' },
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

  it('shows the install disclosure for the requested tools', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    hooks.useMarketplaceDisclosure.mockReturnValue({
      data: {
        version_id: 'skv_1', content_hash: null, capability_digest: 'sha256:x', capabilities: ['instructions', 'network'],
        provenance: { publisher_verified: false, attested: false }, scan: {}, unicode: { flagged_files: 0, findings: [] },
        shell_commands: [], network_endpoints: ['api.example.com'], env_names: [],
        mcp_servers: { local: [], remote: [], declared: false }, allowed_tools: [], persistence_targets: [],
        executable_extensions: [],
        tools: { codex: { delivery: 'sessionfs-emission', scope: 'project', summary: 'Writes 1 file.', writes: ['.codex/skills/kit/SKILL.md'], writes_total: 1, runs: { scripts: 0, hooks: [], mcp_local: 0 }, connects_to: [], auto_allowed: [], omitted: [], consent_required: [] } },
        consent_required: ['instructions'],
      },
      isLoading: false,
      error: null,
    });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(hooks.useMarketplaceDisclosure).toHaveBeenCalledWith('skv_1', ['codex'], 'project');
    expect(screen.getByTestId('disclosure-card')).toBeInTheDocument();
    expect(screen.getByText('Writes 1 file.')).toBeInTheDocument();
    expect(screen.getByText('api.example.com')).toBeInTheDocument();
  });

  it('asks for explicit re-approval when the update widens access', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    hooks.approve.mutateAsync.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ error: {
      code: 'reapproval_required', message: 'widens',
      details: {
        update_class: 'widening', widens: [{ kind: 'new_network_endpoint', item: 'sink.example.org' }], code_changes: [],
        diff: updateDiff(),
      },
    } })));
    render(<MarketplaceTab projectId="prj_1" />);
    await user.click(screen.getByRole('button', { name: 'Approve exact hash' }));
    expect(hooks.addToast).toHaveBeenCalledWith('error', expect.stringMatching(/re-approval/));
    expect(await screen.findByText(/more access than the version already approved/)).toBeInTheDocument();
    expect(screen.getByText(/new_network_endpoint: sink.example.org/)).toBeInTheDocument();
    const approveUpdate = screen.getByRole('button', { name: 'Approve update' });
    expect(approveUpdate).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: 'Acknowledge the update' }));
    hooks.approve.mutateAsync.mockResolvedValueOnce({});
    await user.click(approveUpdate);
    expect(hooks.approve.mutateAsync).toHaveBeenLastCalledWith({
      requestId: 'sir_1',
      approvedCapabilities: [],
      confirmationCode: undefined,
      acknowledgements: expect.objectContaining({ widening: true, capabilityDigest: 'sha256:x', consentRuleVersion: 2 }),
    });
  });

  it('fails closed when the reapproval carries no diff', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    hooks.approve.mutateAsync.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ error: {
      code: 'reapproval_required', message: 'widens',
      // No nested diff: there is nothing to acknowledge, so acknowledgement is impossible.
      details: { update_class: 'widening', widens: [{ kind: 'new_network_endpoint', item: 'sink.example.org' }], code_changes: [] },
    } })));
    render(<MarketplaceTab projectId="prj_1" />);
    await user.click(screen.getByRole('button', { name: 'Approve exact hash' }));
    expect(await screen.findByText(/more access than the version already approved/)).toBeInTheDocument();
    expect(screen.getByText(/update diff is unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: 'Acknowledge the update' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve update' })).toBeDisabled();
  });

  it('binds re-approval to the reviewed update digest and drops a stale diff', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    const refetch = vi.fn();
    hooks.useMarketplaceDisclosure.mockReturnValue({ data: DISCLOSURE, isLoading: false, error: null, refetch });
    const held = (digest: string, item: string, changed = false) => new ApiError(409, JSON.stringify({ error: {
      code: 'reapproval_required', message: 'widens',
      details: {
        update_class: 'widening', widens: [{ kind: 'new_network_endpoint', item }], code_changes: [],
        update_digest: digest, diff_changed: changed,
        diff: updateDiff({ widens: [{ kind: 'new_network_endpoint', item }] }),
      },
    } }));
    hooks.approve.mutateAsync.mockRejectedValueOnce(held('sha256:u1', 'one.example.org'));
    render(<MarketplaceTab projectId="prj_1" />);
    await user.click(screen.getByRole('button', { name: 'Approve exact hash' }));
    await user.click(screen.getByRole('checkbox', { name: 'Acknowledge the update' }));
    // The baseline moved meanwhile: the server returns the NEW diff.
    hooks.approve.mutateAsync.mockRejectedValueOnce(held('sha256:u2', 'two.example.org', true));
    await user.click(screen.getByRole('button', { name: 'Approve update' }));
    expect(hooks.approve.mutateAsync.mock.lastCall?.[0].acknowledgements.updateDigest).toBe('sha256:u1');
    expect(hooks.addToast).toHaveBeenLastCalledWith('error', expect.stringMatching(/changed since you reviewed/));
    expect(await screen.findByText(/new_network_endpoint: two.example.org/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Acknowledge the update' })).not.toBeChecked();
    await user.click(screen.getByRole('checkbox', { name: 'Acknowledge the update' }));
    // A disclosure change drops the cached diff entirely.
    hooks.approve.mutateAsync.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ error: {
      code: 'disclosure_changed', message: 'changed', details: {},
    } })));
    await user.click(screen.getByRole('button', { name: 'Approve update' }));
    expect(hooks.approve.mutateAsync.mock.lastCall?.[0].acknowledgements.updateDigest).toBe('sha256:u2');
    expect(refetch).toHaveBeenCalled();
    expect(screen.queryByText(/new_network_endpoint: two.example.org/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve exact hash' })).toBeInTheDocument();
  });

  it('blocks approval until the disclosure loads and shows its errors', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    hooks.useMarketplaceDisclosure.mockReturnValue({ data: undefined, isLoading: false, error: new Error('boom') });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getByRole('alert')).toHaveTextContent(/Install disclosure unavailable/);
    expect(screen.getByRole('button', { name: 'Approve exact hash' })).toBeDisabled();
    hooks.useMarketplaceDisclosure.mockReturnValue({ data: undefined, isLoading: true, error: null });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getAllByRole('button', { name: 'Approve exact hash' }).every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
  });

  it('asks for the disclosure in user scope for user and device grants', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    const base = hooks.useSkillInstallRequests();
    hooks.useSkillInstallRequests.mockReturnValue({
      ...base,
      data: { ...base.data, items: [{ ...base.data.items[0], target_scope: 'device', target_device_id: 'd1' }] },
    });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(hooks.useMarketplaceDisclosure).toHaveBeenCalledWith('skv_1', ['codex'], 'user');
  });

  it('reloads the disclosure when the server says it changed', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    const refetch = vi.fn();
    hooks.useMarketplaceDisclosure.mockReturnValue({ data: DISCLOSURE, isLoading: false, error: null, refetch });
    hooks.approve.mutateAsync.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ error: {
      code: 'disclosure_changed', message: 'changed', details: {},
    } })));
    render(<MarketplaceTab projectId="prj_1" />);
    await user.click(screen.getByRole('button', { name: 'Approve exact hash' }));
    expect(refetch).toHaveBeenCalled();
    expect(hooks.addToast).toHaveBeenCalledWith('error', expect.stringMatching(/disclosure changed/));
    // While the fresh disclosure loads, approval stays disabled.
    hooks.useMarketplaceDisclosure.mockReturnValue({ data: DISCLOSURE, isLoading: false, error: null, isFetching: true, refetch });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getAllByRole('button', { name: 'Approve exact hash' }).at(-1)).toBeDisabled();
  });

  it('clears confirmations on disclosure_changed and when the reviewed card changes', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_admin', tier: 'pro', org_role: 'admin' } });
    hooks.useMarketplaceDisclosure.mockReturnValue({ data: DISCLOSURE, isLoading: false, error: null, refetch: vi.fn() });
    const base = hooks.useSkillInstallRequests();
    hooks.useSkillInstallRequests.mockReturnValue({
      ...base,
      data: { ...base.data, items: [{ ...base.data.items[0], requested_capabilities: ['instructions', 'scripts'] }] },
    });
    hooks.approve.mutateAsync.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ error: {
      code: 'disclosure_changed', message: 'changed', details: {},
    } })));
    const { rerender } = render(<MarketplaceTab projectId="prj_1" />);
    const scripts = screen.getByRole('checkbox', { name: 'Confirm scripts' });
    await user.click(scripts);
    expect(scripts).toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Approve exact hash' }));
    expect(screen.getByRole('checkbox', { name: 'Confirm scripts' })).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Approve exact hash' })).toBeDisabled();

    // A new card digest (e.g. after a reload) also starts the review over.
    await user.click(screen.getByRole('checkbox', { name: 'Confirm scripts' }));
    expect(screen.getByRole('checkbox', { name: 'Confirm scripts' })).toBeChecked();
    hooks.useMarketplaceDisclosure.mockReturnValue({
      data: { ...DISCLOSURE, disclosure_digest: 'sha256:other' }, isLoading: false, error: null,
    });
    rerender(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getByRole('checkbox', { name: 'Confirm scripts' })).not.toBeChecked();
  });

  it('renders a useful empty state', () => {
    hooks.useSkillInstallRequests.mockReturnValue({ data: { items: [], can_approve: true }, isLoading: false, error: null });
    render(<MarketplaceTab projectId="prj_1" />);
    expect(screen.getByText('No install requests')).toBeInTheDocument();
  });
});
