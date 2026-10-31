import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Layout from '../components/Layout';
import HelpPage from '../help/HelpPage';
import { axe } from '../test/axe';

// Same mocking approach as components/Layout.test.tsx: the shell is audited
// with its data hooks stubbed so no provider tree is needed.
const { useMeMock } = vi.hoisted(() => ({ useMeMock: vi.fn() }));

vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ logout: vi.fn() }) }));
vi.mock('../hooks/useHandoffs', () => ({
  useHandoffInbox: () => ({ data: { handoffs: [{ status: 'pending' }] } }),
}));
vi.mock('../hooks/useMe', () => ({ useMe: useMeMock }));
vi.mock('../transfers/useTransfers', () => ({
  useTransfers: () => ({ data: { transfers: [] }, isLoading: false, error: null }),
  useMyOrgs: () => ({ data: [], isLoading: false }),
}));
vi.mock('../invites/useInvites', () => ({
  useMyInvites: () => ({ data: { invites: [] }, isLoading: false, error: null }),
}));
vi.mock('../components/SearchBar', () => ({
  default: () => <input aria-label="Search sessions" />,
}));
vi.mock('../hooks/useTicketNotifications', () => ({
  useTicketNotificationUnreadCount: () => ({ data: { count: 3, etag: '' } }),
}));
vi.mock('../hooks/useReviewLoop', () => ({
  useNeedsYouCount: () => ({ data: { count: 2 } }),
}));
vi.mock('../helper/HelperProvider', () => ({
  useHelper: () => ({ openHelper: vi.fn() }),
}));

beforeEach(() => {
  useMeMock.mockReturnValue({
    data: {
      tier: 'admin',
      email: 'admin@example.test',
      org_id: 'org_1',
      org_name: 'Test Org',
      org_role: 'admin',
      effective_tier: 'enterprise',
      default_org_id: 'org_1',
    },
  });
});

describe('Layout shell and Help page', () => {
  it('Layout sidebar and top bar have no axe violations', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<h1>Sessions</h1>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByRole('heading', { name: 'Sessions' });
    expect(await axe(container)).toHaveNoViolations();
  });

  it('Help page has no axe violations', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    const { container } = render(
      <MemoryRouter>
        <main>
          <HelpPage />
        </main>
      </MemoryRouter>,
    );
    await waitFor(() => expect(container.querySelector('h1')).toBeTruthy());
    expect(await axe(container)).toHaveNoViolations();
  }, 20000);
});
