/**
 * GitHub #92 — the represented-persona deep link mounts a secondary org's
 * personas. Verifies the mount passes the right orgId, resolves the admin
 * flag and member list for the OrgPersonasSection, and links back to the
 * primary org.
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import SecondaryOrgPage from './SecondaryOrgPage';

const { mockUseOrgMembers, mockUseMe, mockUseMyOrgs } = vi.hoisted(() => ({
  mockUseOrgMembers: vi.fn(),
  mockUseMe: vi.fn(),
  mockUseMyOrgs: vi.fn(),
}));

vi.mock('./useOrgMembers', () => ({
  useOrgMembers: (orgId: string) => mockUseOrgMembers(orgId),
}));

vi.mock('../hooks/useMe', () => ({
  useMe: () => mockUseMe(),
}));

vi.mock('../transfers/useTransfers', () => ({
  useMyOrgs: () => mockUseMyOrgs(),
}));

vi.mock('./OrgPersonasSection', () => ({
  default: ({
    orgId,
    isAdmin,
    myUserId,
    members,
  }: {
    orgId: string;
    isAdmin: boolean;
    myUserId?: string;
    members: { user_id: string; email: string }[];
  }) => (
    <div data-testid="org-personas-stub" data-org-id={orgId} data-is-admin={String(isAdmin)} data-my-user-id={myUserId ?? ''}>
      {members.map((m) => m.email).join(',')}
    </div>
  ),
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/settings/organization?tab=personas&org=org_2']}>
      <SecondaryOrgPage orgId="org_2" />
    </MemoryRouter>,
  );
}

describe('SecondaryOrgPage', () => {
  it('mounts OrgPersonasSection for the secondary org with resolved role and members', () => {
    mockUseOrgMembers.mockReturnValue({
      data: {
        org_id: 'org_2',
        current_user_role: 'admin',
        members: [
          { user_id: 'u_1', email: 'kelvin@example.com', display_name: null, role: 'member', joined_at: null },
        ],
      },
      isLoading: false,
    });
    mockUseMe.mockReturnValue({ data: { user_id: 'u_9' } });
    mockUseMyOrgs.mockReturnValue({
      data: [{ org_id: 'org_2', name: 'Globex', role: 'admin' }],
    });

    renderPage();

    const stub = screen.getByTestId('org-personas-stub');
    expect(stub).toHaveAttribute('data-org-id', 'org_2');
    expect(stub).toHaveAttribute('data-is-admin', 'true');
    expect(stub).toHaveAttribute('data-my-user-id', 'u_9');
    expect(stub).toHaveTextContent('kelvin@example.com');
    expect(screen.getByRole('heading', { name: /globex/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /back to your organization/i })).toHaveAttribute(
      'href',
      '/settings/organization',
    );
  });

  it('treats a secondary-org member as non-admin', () => {
    mockUseOrgMembers.mockReturnValue({
      data: {
        org_id: 'org_2',
        current_user_role: 'member',
        members: [{ user_id: 'u_1', email: 'kelvin@example.com', display_name: null, role: 'member', joined_at: null }],
      },
      isLoading: false,
    });
    mockUseMe.mockReturnValue({ data: { user_id: 'u_1' } });
    mockUseMyOrgs.mockReturnValue({ data: [{ org_id: 'org_2', name: 'Globex', role: 'member' }] });

    renderPage();

    expect(screen.getByTestId('org-personas-stub')).toHaveAttribute('data-is-admin', 'false');
  });

  it('shows a loading state until members resolve', () => {
    mockUseOrgMembers.mockReturnValue({ data: undefined, isLoading: true });
    mockUseMe.mockReturnValue({ data: undefined });
    mockUseMyOrgs.mockReturnValue({ data: undefined });

    renderPage();

    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });
  it('shows an error instead of loading forever when the org cannot be loaded', () => {
    mockUseOrgMembers.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    mockUseMe.mockReturnValue({ data: { user_id: 'u_9' } });
    mockUseMyOrgs.mockReturnValue({ data: [] });
    renderPage();
    expect(screen.getByText(/don.t have access to this organization/i)).toBeInTheDocument();
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
    expect(screen.queryByTestId('org-personas-stub')).not.toBeInTheDocument();
  });
});
