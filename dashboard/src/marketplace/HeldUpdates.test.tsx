import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import HeldUpdates, { HeldUpdateQueues } from './HeldUpdates';

type DisclosureMock = { data: unknown; isLoading: boolean; error: unknown; isFetching?: boolean; refetch?: () => void };

const { hooks, CARD } = vi.hoisted(() => ({
  CARD: {
    version_id: 'skv_2', content_hash: 'sha256:c2', capability_digest: 'sha256:cap2', capabilities: ['instructions', 'network'],
    provenance: { publisher_verified: false, attested: false }, scan: { ruleset: 'r1', finding_count: 0 },
    unicode: { flagged_files: 0, findings: [] }, shell_commands: [], network_endpoints: ['new.example.com'], env_names: [],
    mcp_servers: { local: [], remote: [], declared: false }, allowed_tools: [], persistence_targets: [],
    executable_extensions: [], tools: {}, consent_required: ['instructions'], consent_rule_version: 2,
    disclosure_digest: 'sha256:card2',
  },
  hooks: {
    useHeldUpdates: vi.fn(),
    useMarketplaceDisclosure: vi.fn(),
    advance: { mutateAsync: vi.fn(), isPending: false },
    addToast: vi.fn(),
  },
}));

vi.mock('../hooks/useMarketplaceDisclosure', () => ({
  useHeldUpdates: (orgId: string) => hooks.useHeldUpdates(orgId),
  useMarketplaceDisclosure: (versionId: string, tools: string[], scope: string): DisclosureMock =>
    hooks.useMarketplaceDisclosure(versionId, tools, scope),
  useAdvanceAssignment: () => hooks.advance,
}));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));

const HELD = {
  assignment: {
    id: 'osa_1', org_id: 'org_1', version_id: 'skv_1', content_hash: 'sha256:c1', target_type: 'org', target_id: '*',
    target_user_id: null, mode: 'required', target_tools: ['codex'], policy: {}, created_at: '2026-10-01T00:00:00Z',
  },
  package_id: 'spk_1', name: 'kit', from_version_id: 'skv_1', from_version: '1.0.0', to_version_id: 'skv_2',
  to_version: '2.0.0', to_content_hash: 'sha256:c2', update_class: 'widening',
  widens: [{ kind: 'new_network_endpoint', item: 'new.example.com' }], code_changes: [], held: true,
  requires: 'approve_widening',
  update_digest: 'sha256:upd0',
  // The exact build_diff() object the update_digest binds (nested under `diff`).
  diff: {
    update_class: 'widening', baseline_known: true, baseline_digest: 'sha256:cap1',
    widens: [{ kind: 'new_network_endpoint', item: 'new.example.com' }], narrows: [], code_changes: [],
    changed_requirements: [], capability_digest_changed: true,
    files: { added: ['skills/kit/SKILL.md'], removed: [], changed: ['skills/kit/scripts/run.sh'] },
    scan_delta: { old_max_severity: 'medium', new_max_severity: 'high', old_finding_count: 1, new_finding_count: 2, old_ruleset: '2026.9.1', new_ruleset: '2026.10.1' },
    unicode_delta: { new_findings: [] },
    provenance_delta: { old: { publisher_verified: false, attested: false }, new: { publisher_verified: true, attested: false } },
    consent: { personal: 'explicit_reconsent', org_required: 'never_auto_apply', description: 'The update asks for more access: it always stops and requires explicit re-consent.' },
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useHeldUpdates.mockReturnValue({
    data: { pages: [{ items: [HELD], count: 1, has_more: false, next_cursor: null }] },
    isLoading: false, error: null, hasNextPage: false,
  });
  hooks.useMarketplaceDisclosure.mockReturnValue({ data: CARD, isLoading: false, error: null, isFetching: false, refetch: vi.fn() });
  hooks.advance.mutateAsync.mockResolvedValue({});
});

describe('HeldUpdates', () => {
  it('shows the diff and the target disclosure and advances with a digest-bound acknowledgement', async () => {
    const user = userEvent.setup();
    render(<HeldUpdates orgId="org_1" />);
    expect(hooks.useMarketplaceDisclosure).toHaveBeenCalledWith('skv_2', ['codex'], 'user');
    expect(screen.getByText('asks for more access')).toBeInTheDocument();
    expect(screen.getByText('+ new_network_endpoint: new.example.com')).toBeInTheDocument();
    // The nested diff renders every changed file path and every delta leaf.
    expect(screen.getByText('+ skills/kit/SKILL.md')).toBeInTheDocument();
    expect(screen.getByText('~ skills/kit/scripts/run.sh')).toBeInTheDocument();
    expect(screen.getByText('scan_delta.new_ruleset = 2026.10.1')).toBeInTheDocument();
    expect(screen.getByText('provenance_delta.new.publisher_verified = true')).toBeInTheDocument();
    expect(screen.getByTestId('disclosure-card')).toBeInTheDocument();
    const button = screen.getByRole('button', { name: 'Advance' });
    expect(button).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: 'Acknowledge the update of kit' }));
    expect(button).toBeEnabled();
    await user.click(button);
    expect(hooks.advance.mutateAsync).toHaveBeenCalledWith({
      assignmentId: 'osa_1',
      input: {
        to_version_id: 'skv_2', content_hash: 'sha256:c2', approve_widening: true,
        capability_digest: 'sha256:cap2', consent_rule_version: 2, disclosure_digest: 'sha256:card2',
        update_digest: 'sha256:upd0',
      },
    });
  });

  it('clears the acknowledgement and reloads when the disclosure changed', async () => {
    const user = userEvent.setup();
    const refetch = vi.fn();
    hooks.useMarketplaceDisclosure.mockReturnValue({ data: CARD, isLoading: false, error: null, isFetching: false, refetch });
    hooks.advance.mutateAsync.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ error: {
      code: 'disclosure_changed', message: 'changed', details: {},
    } })));
    render(<HeldUpdates orgId="org_1" />);
    const checkbox = screen.getByRole('checkbox', { name: 'Acknowledge the update of kit' });
    await user.click(checkbox);
    await user.click(screen.getByRole('button', { name: 'Advance' }));
    expect(refetch).toHaveBeenCalled();
    expect(checkbox).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Advance' })).toBeDisabled();
  });

  it('binds the advance to the reviewed update digest', async () => {
    const user = userEvent.setup();
    hooks.useHeldUpdates.mockReturnValue({
      data: { pages: [{ items: [{ ...HELD, update_digest: 'sha256:upd1' }], count: 1, has_more: false, next_cursor: null }] },
      isLoading: false, error: null, hasNextPage: false,
    });
    render(<HeldUpdates orgId="org_1" />);
    await user.click(screen.getByRole('checkbox', { name: 'Acknowledge the update of kit' }));
    await user.click(screen.getByRole('button', { name: 'Advance' }));
    expect(hooks.advance.mutateAsync.mock.calls[0][0].input.update_digest).toBe('sha256:upd1');
  });

  it.each(['reapproval_required', 'assignment_changed', 'assignment_revoked', 'assignment_superseded'])(
    'refetches the queue and clears the acknowledgement on %s',
    async (code) => {
      const user = userEvent.setup();
      const refetch = vi.fn();
      hooks.useHeldUpdates.mockReturnValue({
        data: { pages: [{ items: [{ ...HELD, update_digest: 'sha256:upd1' }], count: 1, has_more: false, next_cursor: null }] },
        isLoading: false, error: null, hasNextPage: false, refetch,
      });
      hooks.advance.mutateAsync.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ error: {
        code, message: 'stale', details: { update_class: 'widening', update_digest: 'sha256:upd2', diff_changed: true },
      } })));
      render(<HeldUpdates orgId="org_1" />);
      const checkbox = screen.getByRole('checkbox', { name: 'Acknowledge the update of kit' });
      await user.click(checkbox);
      await user.click(screen.getByRole('button', { name: 'Advance' }));
      expect(refetch).toHaveBeenCalled();
      expect(checkbox).not.toBeChecked();
      expect(screen.getByRole('button', { name: 'Advance' })).toBeDisabled();
      expect(hooks.addToast).toHaveBeenCalledWith('error', expect.stringMatching(/refreshed/));
    },
  );

  it('fails closed when the held update carries no diff', () => {
    const { diff: _dropped, ...noDiff } = HELD;
    void _dropped;
    hooks.useHeldUpdates.mockReturnValue({
      data: { pages: [{ items: [{ ...noDiff, update_digest: 'sha256:upd1' }], count: 1, has_more: false, next_cursor: null }] },
      isLoading: false, error: null, hasNextPage: false,
    });
    render(<HeldUpdates orgId="org_1" />);
    expect(screen.getByTestId('held-update-diff-missing')).toHaveTextContent(/update diff is unavailable/);
    expect(screen.queryByRole('checkbox', { name: 'Acknowledge the update of kit' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Advance' })).toBeDisabled();
  });

  it('fails closed when the held update carries no digest', () => {
    const { update_digest: _dropped, ...noDigest } = HELD;
    void _dropped;
    hooks.useHeldUpdates.mockReturnValue({
      data: { pages: [{ items: [noDigest], count: 1, has_more: false, next_cursor: null }] },
      isLoading: false, error: null, hasNextPage: false,
    });
    render(<HeldUpdates orgId="org_1" />);
    expect(screen.getByTestId('held-update-digest-missing')).toHaveTextContent(/update digest is unavailable/);
    expect(screen.getByRole('button', { name: 'Advance' })).toBeDisabled();
  });

  it('offers an org selector when the viewer administers several orgs', async () => {
    const user = userEvent.setup();
    render(<HeldUpdateQueues orgs={[{ org_id: 'org_1', name: 'Acme' }, { org_id: 'org_2', name: 'Beta' }]} />);
    expect(hooks.useHeldUpdates).toHaveBeenLastCalledWith('org_1');
    await user.click(screen.getByRole('combobox', { name: 'Organization for updates to review' }));
    await user.click(screen.getByRole('option', { name: 'Beta' }));
    expect(hooks.useHeldUpdates).toHaveBeenLastCalledWith('org_2');
  });

  it('shows no selector for a single administered org', () => {
    render(<HeldUpdateQueues orgs={[{ org_id: 'org_1', name: 'Acme' }]} />);
    expect(screen.queryByRole('combobox', { name: 'Organization for updates to review' })).not.toBeInTheDocument();
    expect(hooks.useHeldUpdates).toHaveBeenLastCalledWith('org_1');
  });

  it('renders an empty queue', () => {
    hooks.useHeldUpdates.mockReturnValue({
      data: { pages: [{ items: [], count: 0, has_more: false, next_cursor: null }] }, isLoading: false, error: null,
    });
    render(<HeldUpdates orgId="org_1" />);
    expect(screen.getByText('No assigned package has a newer version.')).toBeInTheDocument();
  });
});
