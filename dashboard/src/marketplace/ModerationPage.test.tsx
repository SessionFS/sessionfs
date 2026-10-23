import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ModerationPage from './ModerationPage';
import { ApiError } from '../api/client';
import { RevocationFeedIncompleteError } from './moderationState';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useMe: vi.fn(),
    useMyOrgs: vi.fn(),
    useMarketplaceReports: vi.fn(),
    useRevocationIndex: vi.fn(),
    useMarketplacePackageInfo: vi.fn(),
    dismiss: { mutateAsync: vi.fn(), isPending: false },
    moderate: { mutateAsync: vi.fn(), isPending: false },
    addToast: vi.fn(),
  },
}));

vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../transfers/useTransfers', () => ({ useMyOrgs: hooks.useMyOrgs }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));
vi.mock('../hooks/useMarketplace', () => ({
  useMarketplaceReports: hooks.useMarketplaceReports,
  useRevocationIndex: hooks.useRevocationIndex,
  useMarketplacePackageInfo: hooks.useMarketplacePackageInfo,
  useDismissMarketplaceReport: () => hooks.dismiss,
  useModerateMarketplace: () => hooks.moderate,
}));

const report = {
  id: 'spr_1',
  package_id: 'spk_1',
  version_id: 'skv_1',
  reason: 'secrets',
  details: 'Contains <script>alert(1)</script> and an AWS key',
  status: 'open',
  created_at: '2026-10-14T10:00:00Z',
  resolved_at: null,
  reporter_user_id: 'user_r',
  resolved_by_user_id: null,
  resolution_note: null,
};

function reportPages(items: unknown[], extra: Record<string, unknown> = {}) {
  return {
    data: { pages: [{ items, count: items.length, has_more: false, next_cursor: null }], pageParams: [''] },
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    ...extra,
  };
}

function renderPage() {
  return render(<MemoryRouter><ModerationPage /></MemoryRouter>);
}

function apiError(status: number, error: Record<string, unknown>) {
  return new ApiError(status, JSON.stringify({ error }));
}

async function openReview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Review report spr_1' }));
  return screen.getByRole('dialog');
}

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'team', org_id: 'org_1', org_role: 'owner' }, isLoading: false });
  hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'owner' }], isLoading: false });
  hooks.useMarketplaceReports.mockReturnValue(reportPages([report]));
  hooks.useRevocationIndex.mockReturnValue({ data: { packages: new Map(), versions: new Map() } });
  hooks.useMarketplacePackageInfo.mockReturnValue({
    data: { display_name: 'Review Helper', collection: 'org', namespace: 'acme' },
    isLoading: false,
  });
  hooks.dismiss.mutateAsync.mockResolvedValue({ ...report, status: 'dismissed' });
  hooks.moderate.mutateAsync.mockResolvedValue({
    package_id: 'spk_1',
    version_id: 'skv_1',
    content_hash: null,
    state: 'yanked',
    previous_state: 'active',
    reason: 'leak',
    revocation_event_ids: [1],
    reports_resolved: 1,
    grants_revoked: {},
  });
});

describe('ModerationPage', () => {
  it('explains the permission boundary to members and never queries reports', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'pro', org_id: 'org_1', org_role: 'member' }, isLoading: false });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'member' }], isLoading: false });
    renderPage();
    expect(screen.getByRole('heading', { name: 'Moderation is limited to admins' })).toBeInTheDocument();
    expect(hooks.useMarketplaceReports).toHaveBeenCalledWith('open', false);
  });

  it('lets an org owner (not only role "admin") see the open queue', () => {
    renderPage();
    expect(screen.getByRole('heading', { name: 'Moderation queue' })).toBeInTheDocument();
    expect(hooks.useMarketplaceReports).toHaveBeenCalledWith('open', true);
    expect(screen.getByText('Leaks secrets or personal data')).toBeInTheDocument();
    expect(screen.getByText(/spk_1 · skv_1/)).toBeInTheDocument();
  });

  it('lets a platform admin with no org see the queue', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'admin' }, isLoading: false });
    hooks.useMyOrgs.mockReturnValue({ data: [], isLoading: false });
    renderPage();
    expect(screen.getByText(/Reports on public and personal listings/)).toBeInTheDocument();
  });

  it('shows yanked / blocked badges from the revocations feed', () => {
    hooks.useRevocationIndex.mockReturnValue({ data: { packages: new Map(), versions: new Map([['skv_1', 'blocked']]) } });
    renderPage();
    expect(screen.getByText('blocked')).toBeInTheDocument();
  });

  it('flags moderation state as incomplete when the feed was truncated', async () => {
    const user = userEvent.setup();
    hooks.useRevocationIndex.mockReturnValue({ data: undefined, error: new RevocationFeedIncompleteError() });
    renderPage();
    expect(screen.getByText('Moderation state is incomplete')).toBeInTheDocument();
    await openReview(user);
    expect(screen.getByText('unknown (revocations feed incomplete)')).toBeInTheDocument();
  });

  it('shows no incomplete notice for a fully read feed', () => {
    renderPage();
    expect(screen.queryByText('Moderation state is incomplete')).not.toBeInTheDocument();
  });

  it('switches the status filter', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('button', { name: 'Filter reports by status' }));
    await user.click(screen.getByRole('option', { name: 'All reports' }));
    expect(hooks.useMarketplaceReports).toHaveBeenLastCalledWith('all', true);
  });

  it('renders an empty queue and paginates', async () => {
    const user = userEvent.setup();
    hooks.useMarketplaceReports.mockReturnValue(reportPages([]));
    const first = renderPage();
    expect(screen.getByRole('heading', { name: 'Nothing to review' })).toBeInTheDocument();
    first.unmount();

    const fetchNextPage = vi.fn();
    hooks.useMarketplaceReports.mockReturnValue(reportPages([report], { hasNextPage: true, fetchNextPage }));
    renderPage();
    await user.click(screen.getByRole('button', { name: 'Load more' }));
    expect(fetchNextPage).toHaveBeenCalledTimes(1);
  });

  it('shows report details as inert text with the package it targets', async () => {
    const user = userEvent.setup();
    hooks.useMarketplaceReports.mockReturnValue(reportPages([{ ...report, scope_kind: 'org', scope_org_id: 'org_1' }]));
    renderPage();
    const dialog = await openReview(user);
    expect(within(dialog).getByText('org scope')).toBeInTheDocument();
    expect(within(dialog).getByText('org_1')).toBeInTheDocument();
    expect(within(dialog).getByText('Contains <script>alert(1)</script> and an AWS key')).toBeInTheDocument();
    expect(within(dialog).getByText('Review Helper')).toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
  });

  it('dismisses a report only with a note', async () => {
    const user = userEvent.setup();
    renderPage();
    const dialog = await openReview(user);
    const button = within(dialog).getByRole('button', { name: 'Dismiss report' });
    expect(button).toBeDisabled();
    await user.type(within(dialog).getByLabelText('Dismissal note (required)'), 'Example key, not real');
    await user.click(button);
    expect(hooks.dismiss.mutateAsync).toHaveBeenCalledWith({ reportId: 'spr_1', note: 'Example key, not real' });
    expect(hooks.addToast).toHaveBeenCalledWith('success', 'Report dismissed.');
  });

  it('yanks the reported version after a reason and an explicit confirmation', async () => {
    const user = userEvent.setup();
    renderPage();
    const dialog = await openReview(user);
    const trigger = within(dialog).getByRole('button', { name: 'Yank…' });
    expect(trigger).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/^Reason \(required/), 'Leaked credential');
    await user.click(trigger);
    const confirm = within(dialog).getByRole('alertdialog');
    expect(within(confirm).getByText(/Hides it from search, preview, download/)).toBeInTheDocument();
    expect(hooks.moderate.mutateAsync).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole('button', { name: 'Confirm yank' }));
    expect(hooks.moderate.mutateAsync).toHaveBeenCalledWith({
      kind: 'versions',
      targetId: 'skv_1',
      action: 'yank',
      reason: 'Leaked credential',
    });
    expect(hooks.addToast).toHaveBeenCalledWith('success', expect.stringMatching(/1 report\(s\) resolved/));
  });

  it('blocks the whole package and warns that grants are revoked', async () => {
    const user = userEvent.setup();
    renderPage();
    const dialog = await openReview(user);
    await user.click(within(dialog).getByRole('button', { name: /Target/ }));
    await user.click(within(dialog).getByRole('option', { name: /Whole package/ }));
    await user.click(within(dialog).getByRole('button', { name: /Action/ }));
    await user.click(within(dialog).getByRole('option', { name: /Block/ }));
    await user.type(within(dialog).getByLabelText(/^Reason \(required/), 'Malware');
    await user.click(within(dialog).getByRole('button', { name: 'Block…' }));
    const confirm = within(dialog).getByRole('alertdialog');
    expect(within(confirm).getByText(/revokes every install request/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole('button', { name: 'Confirm block' }));
    expect(hooks.moderate.mutateAsync).toHaveBeenCalledWith({ kind: 'packages', targetId: 'spk_1', action: 'block', reason: 'Malware' });
  });

  it('explains a 403 moderation_forbidden in plain language', async () => {
    const user = userEvent.setup();
    hooks.moderate.mutateAsync.mockRejectedValue(apiError(403, {
      code: 'moderation_forbidden',
      message: "Only a platform admin can restore a platform admin's takedown.",
      details: {},
    }));
    renderPage();
    const dialog = await openReview(user);
    await user.click(within(dialog).getByRole('button', { name: /Action/ }));
    await user.click(within(dialog).getByRole('option', { name: 'Restore' }));
    await user.type(within(dialog).getByLabelText(/^Reason \(required/), 'False positive');
    await user.click(within(dialog).getByRole('button', { name: 'Restore…' }));
    await user.click(within(dialog).getByRole('button', { name: 'Confirm restore' }));
    expect(await within(dialog).findByText("Only a platform admin can restore a platform admin's takedown.")).toBeInTheDocument();
    expect(within(dialog).getByText(/Public listings need a platform admin/)).toBeInTheDocument();
  });

  it('restoring a quarantine with several critical findings asks to clear each one', async () => {
    const user = userEvent.setup();
    const findings = [
      { rule_id: 'encoded_exec.decode_pipe', path: 'skills/a/SKILL.md', line: 9, line_sha: 'a'.repeat(64) },
      { rule_id: 'fetch_and_run.pipe_to_interpreter', path: 'skills/a/SKILL.md', line: 4, line_sha: 'b'.repeat(64) },
    ];
    hooks.moderate.mutateAsync.mockRejectedValueOnce(apiError(409, {
      code: 'findings_required',
      message: 'Restoring this quarantine clears its critical scan findings as false positives.',
      details: { current_state: 'quarantined', findings },
    }));
    renderPage();
    const dialog = await openReview(user);
    await user.click(within(dialog).getByRole('button', { name: /Action/ }));
    await user.click(within(dialog).getByRole('option', { name: 'Restore' }));
    await user.type(within(dialog).getByLabelText(/^Reason \(required/), 'Vendor installer');
    await user.click(within(dialog).getByRole('button', { name: 'Restore…' }));
    await user.click(within(dialog).getByRole('button', { name: 'Confirm restore' }));
    expect(hooks.moderate.mutateAsync).toHaveBeenLastCalledWith(expect.not.objectContaining({ findings: expect.anything() }));
    const list = await within(dialog).findByRole('region', { name: 'Critical findings to clear' });
    expect(within(list).getByText(/encoded_exec.decode_pipe — skills\/a\/SKILL.md:9/)).toBeInTheDocument();
    expect(within(dialog).getByText('Restoring this quarantine clears its critical scan findings.')).toBeInTheDocument();
    await user.click(within(list).getByRole('button', { name: 'Restore and clear these 2 findings' }));
    expect(hooks.moderate.mutateAsync).toHaveBeenLastCalledWith({
      kind: 'versions',
      targetId: 'skv_1',
      action: 'restore',
      reason: 'Vendor installer',
      findings: findings.map(({ rule_id, path, line_sha }) => ({ rule_id, path, line_sha })),
    });
  });

  it('discards findings to clear when the action or target changes', async () => {
    const user = userEvent.setup();
    const findings = [
      { rule_id: 'encoded_exec.decode_pipe', path: 'skills/a/SKILL.md', line: 9, line_sha: 'a'.repeat(64) },
    ];
    hooks.moderate.mutateAsync.mockRejectedValueOnce(apiError(409, {
      code: 'findings_required',
      message: 'Restoring this quarantine clears its critical scan findings as false positives.',
      details: { current_state: 'quarantined', findings },
    }));
    renderPage();
    const dialog = await openReview(user);
    await user.click(within(dialog).getByRole('button', { name: /Action/ }));
    await user.click(within(dialog).getByRole('option', { name: 'Restore' }));
    await user.type(within(dialog).getByLabelText(/^Reason \(required/), 'Vendor installer');
    await user.click(within(dialog).getByRole('button', { name: 'Restore…' }));
    await user.click(within(dialog).getByRole('button', { name: 'Confirm restore' }));
    await within(dialog).findByRole('region', { name: 'Critical findings to clear' });
    // Switching to Block hides the findings: "Restore and clear" cannot run
    // Block (or anything else) without its own confirmation.
    await user.click(within(dialog).getByRole('button', { name: /Action/ }));
    await user.click(within(dialog).getByRole('option', { name: /^Block/ }));
    expect(within(dialog).queryByRole('region', { name: 'Critical findings to clear' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: /Restore and clear/ })).not.toBeInTheDocument();
    // Switching back does not resurrect them either.
    await user.click(within(dialog).getByRole('button', { name: /Action/ }));
    await user.click(within(dialog).getByRole('option', { name: 'Restore' }));
    expect(within(dialog).queryByRole('region', { name: 'Critical findings to clear' })).not.toBeInTheDocument();
    expect(hooks.moderate.mutateAsync).toHaveBeenCalledTimes(1);
  });

  it('discards findings to clear when the target changes', async () => {
    const user = userEvent.setup();
    hooks.moderate.mutateAsync.mockRejectedValueOnce(apiError(409, {
      code: 'findings_required',
      message: 'Restoring this quarantine clears its critical scan findings as false positives.',
      details: {
        current_state: 'quarantined',
        findings: [{ rule_id: 'encoded_exec.decode_pipe', path: 'skills/a/SKILL.md', line: 9, line_sha: 'a'.repeat(64) }],
      },
    }));
    renderPage();
    const dialog = await openReview(user);
    await user.click(within(dialog).getByRole('button', { name: /Action/ }));
    await user.click(within(dialog).getByRole('option', { name: 'Restore' }));
    await user.type(within(dialog).getByLabelText(/^Reason \(required/), 'Vendor installer');
    await user.click(within(dialog).getByRole('button', { name: 'Restore…' }));
    await user.click(within(dialog).getByRole('button', { name: 'Confirm restore' }));
    await within(dialog).findByRole('region', { name: 'Critical findings to clear' });
    await user.click(within(dialog).getByRole('button', { name: /Target/ }));
    await user.click(within(dialog).getByRole('option', { name: /Whole package/ }));
    expect(within(dialog).queryByRole('button', { name: /Restore and clear/ })).not.toBeInTheDocument();
    expect(hooks.moderate.mutateAsync).toHaveBeenCalledTimes(1);
  });

  it('explains 409 already_revoked with the current state', async () => {
    const user = userEvent.setup();
    hooks.moderate.mutateAsync.mockRejectedValue(apiError(409, {
      code: 'already_revoked',
      message: 'The target is already yanked.',
      details: { current_state: 'yanked' },
    }));
    renderPage();
    const dialog = await openReview(user);
    await user.type(within(dialog).getByLabelText(/^Reason \(required/), 'dup');
    await user.click(within(dialog).getByRole('button', { name: 'Yank…' }));
    await user.click(within(dialog).getByRole('button', { name: 'Confirm yank' }));
    expect(await within(dialog).findByText('The target is already yanked.')).toBeInTheDocument();
    expect(within(dialog).getByText('Current state: yanked.')).toBeInTheDocument();
  });

  it('hides the dismiss form on resolved reports and shows the resolution', async () => {
    const user = userEvent.setup();
    hooks.useMarketplaceReports.mockReturnValue(reportPages([{
      ...report, status: 'actioned', resolution_note: 'yanked: leak', resolved_at: '2026-10-14T11:00:00Z',
    }]));
    renderPage();
    const dialog = await openReview(user);
    expect(within(dialog).queryByRole('button', { name: 'Dismiss report' })).not.toBeInTheDocument();
    expect(within(dialog).getByText('yanked: leak')).toBeInTheDocument();
  });
});
