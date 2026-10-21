import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import OrgUsagePage from './OrgUsagePage';
import PublisherAnalyticsPage from './PublisherAnalyticsPage';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    usePublisherAnalytics: vi.fn(),
    useUpdatePackageSettings: vi.fn(),
    useMarketplaceCategories: vi.fn(),
    useOrgSkillUsage: vi.fn(),
    useMe: vi.fn(),
    useMyOrgs: vi.fn(),
    addToast: vi.fn(),
  },
}));

vi.mock('../hooks/useMarketplace', () => ({
  MARKETPLACE_TOOLS: ['codex'],
  usePublisherAnalytics: hooks.usePublisherAnalytics,
  useUpdatePackageSettings: hooks.useUpdatePackageSettings,
  useMarketplaceCategories: hooks.useMarketplaceCategories,
  useOrgSkillUsage: hooks.useOrgSkillUsage,
}));
vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../transfers/useTransfers', () => ({ useMyOrgs: hooks.useMyOrgs }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));

const floor = (count: number | null) => ({ count, display: count == null ? '<10' : String(count) });

const analytics = {
  publisher_id: 'pub_1', namespace: 'acme', window_days: 30, since: '2026-09-21', k_floor: 10,
  totals: {
    installs: floor(42), active_installs: floor(30), uninstalls: floor(null), uninstall_rate: 0.119,
    translation_failures: floor(null), service_installs: floor(0),
  },
  by_tool: [{ tool: 'codex', installs: floor(42), active: floor(30), uninstalls: floor(null), uninstall_rate: 0.119, translation_failures: floor(null) }],
  by_day: [{ day: '2026-10-19', installs: floor(12) }, { day: '2026-10-20', installs: floor(null) }],
  packages: [{
    package_id: 'spk_1', namespace: 'acme', name: 'lint', display_name: 'Lint', current_version_id: 'skv_1',
    current_version: '1.2.0', installs: floor(42), active_installs: floor(30), uninstalls: floor(null),
    uninstall_rate: 0.119, translation_failures: floor(null), installs_by_tool: {}, translation_failures_by_tool: {},
    installs_by_version: [{ version_id: 'skv_1', version: '1.2.0', installs: floor(40) }],
    works_in: [], works_in_regressions: ['cursor'], stale: true, unmaintained: false, deprecated: false,
    categories: ['review'], deprecation_message: null, successor_package_id: null, readme_path: null,
    scan: { max_severity: 'low', counts: {} }, open_reports: 2,
  }],
};

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/marketplace/publishers/:publisherId/analytics" element={<PublisherAnalyticsPage />} />
        <Route path="/marketplace/usage" element={<OrgUsagePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  hooks.usePublisherAnalytics.mockReturnValue({ data: analytics, isLoading: false, error: null });
  hooks.useUpdatePackageSettings.mockReturnValue({ mutateAsync: vi.fn().mockResolvedValue({}), isPending: false });
  hooks.useMarketplaceCategories.mockReturnValue({ data: { items: [
    { slug: 'review', display_name: 'Code review' }, { slug: 'testing', display_name: 'Testing' },
  ] } });
  hooks.useMe.mockReturnValue({ data: { user_id: 'u1' } });
  hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'admin' }], isLoading: false });
  hooks.useOrgSkillUsage.mockReturnValue({
    data: { org_id: 'org_1', window_days: 30, since: '2026-09-21', stale_or_deprecated: ['spk_1'], items: [{
      package_id: 'spk_1', namespace: 'acme', name: 'lint', display_name: 'Lint', installs: 3, active: 2,
      uninstalls: 1, translation_failures: 0, by_tool: [{ tool: 'codex', installs: 3, active: 2, uninstalls: 1, translation_failures: 0 }],
      stale: false, unmaintained: false, deprecated: true,
    }] },
    isLoading: false,
    error: null,
  });
});

describe('PublisherAnalyticsPage', () => {
  it('shows floored totals, tool rows, regressions and reports', () => {
    renderAt('/marketplace/publishers/pub_1/analytics');
    expect(hooks.usePublisherAnalytics).toHaveBeenLastCalledWith('pub_1', 30);
    expect(screen.getAllByText('42').length).toBeGreaterThan(0);
    expect(screen.getAllByText('11.9%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('<10').length).toBeGreaterThan(0);
    expect(screen.getByText('cursor')).toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Installs per day' })).toBeInTheDocument();
  });

  it('edits package settings starting from the current categories', async () => {
    const user = userEvent.setup();
    const mutateAsync = vi.fn().mockResolvedValue({});
    hooks.useUpdatePackageSettings.mockReturnValue({ mutateAsync, isPending: false });
    renderAt('/marketplace/publishers/pub_1/analytics');
    await user.click(screen.getByRole('button', { name: 'Settings…' }));
    expect(screen.getByLabelText('Code review')).toBeChecked();
    await user.click(screen.getByLabelText('Testing'));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(mutateAsync).toHaveBeenCalledWith({
      packageId: 'spk_1',
      body: { categories: ['review', 'testing'], deprecated: false, readme_path: null },
    });
  });

  it('explains the tier gate', () => {
    hooks.usePublisherAnalytics.mockReturnValue({
      data: undefined, isLoading: false,
      error: new ApiError(403, JSON.stringify({ error: { code: 'upgrade_required', message: 'Starter tier' } })),
    });
    renderAt('/marketplace/publishers/pub_1/analytics');
    expect(screen.getByText('Publisher analytics need a paid plan')).toBeInTheDocument();
  });
});

describe('OrgUsagePage', () => {
  it('lists the org members usage with status badges', () => {
    renderAt('/marketplace/usage');
    expect(hooks.useOrgSkillUsage).toHaveBeenLastCalledWith('org_1', 30);
    expect(screen.getByText('acme/lint')).toBeInTheDocument();
    expect(screen.getByText('deprecated')).toBeInTheDocument();
    expect(screen.getByText('codex (3)')).toBeInTheDocument();
  });

  it('explains when the viewer administers no org', () => {
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'member' }], isLoading: false });
    hooks.useOrgSkillUsage.mockReturnValue({ data: undefined, isLoading: false, error: null });
    renderAt('/marketplace/usage');
    expect(screen.getByText('No organization to show')).toBeInTheDocument();
  });
});
