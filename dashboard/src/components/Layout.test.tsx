import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import Layout from './Layout';

// ── Mutable mocks (hoisted so vi.mock can reference them) ──
const { mockLogout, useMeMock } = vi.hoisted(() => ({
  mockLogout: vi.fn(),
  useMeMock: vi.fn(),
}));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    logout: mockLogout,
  }),
}));

vi.mock('../hooks/useHandoffs', () => ({
  useHandoffInbox: () => ({
    data: {
      handoffs: [{ status: 'pending' }, { status: 'claimed' }],
    },
  }),
}));

vi.mock('../hooks/useMe', () => ({
  useMe: useMeMock,
}));

// Phase 4 Round 3 added a Transfers nav link with a pending-count
// badge. The hook needs AuthProvider context; stub it here so this
// suite stays focused on the layout shell.
vi.mock('../transfers/useTransfers', () => ({
  useTransfers: () => ({ data: { transfers: [] }, isLoading: false, error: null }),
}));

// v0.10.22 — Invites nav link reads from useMyInvites; stub here so the
// layout shell test doesn't need a QueryClientProvider just for the
// pending-count badge.
vi.mock('../invites/useInvites', () => ({
  useMyInvites: () => ({ data: { invites: [] }, isLoading: false, error: null }),
}));

vi.mock('./SearchBar', () => ({
  default: () => <div data-testid="search-bar">Search</div>,
}));

vi.mock('./ThemeToggle', () => ({
  default: () => <button type="button">Theme</button>,
}));

vi.mock('./Badge', () => ({
  Badge: ({ label }: { label: string }) => <span>{label}</span>,
}));

// ── Helpers ──

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<div>Dashboard home</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

/** Click the avatar trigger button to open the account menu. */
async function openAccountMenu() {
  const trigger = document.querySelector('[aria-haspopup="menu"]') as HTMLElement;
  await userEvent.click(trigger);
  // Wait for the menu to appear
  await waitFor(() => {
    expect(screen.getByRole('menu', { name: 'Account menu' })).toBeInTheDocument();
  });
}

// ── Default mock: admin user with org ──
beforeEach(() => {
  vi.clearAllMocks();
  useMeMock.mockReturnValue({
    data: {
      tier: 'admin',
      email: 'admin@sessionfs.dev',
      default_org_id: 'org_123',
    },
  });
});

describe('Layout', () => {
  it('renders the branded shell copy and SessionFS home link', () => {
    renderLayout();

    expect(screen.getByLabelText('SessionFS home')).toBeInTheDocument();
    expect(screen.getAllByText(/memory layer for ai coding agents/i).length).toBeGreaterThan(0);
    expect(screen.getByText('Dashboard home')).toBeInTheDocument();
  });

  // ── Account menu tests ──

  describe('account menu', () => {
    it('shows all sections for an org user (identity, org items, personal, logout)', async () => {
      useMeMock.mockReturnValue({
        data: {
          tier: 'team',
          email: 'team@acme.corp',
          default_org_id: 'org_123',
        },
      });
      renderLayout();
      await openAccountMenu();

      // Identity header (non-interactive — rendered as div, not menuitem)
      expect(screen.getByText('team@acme.corp')).toBeInTheDocument();

      // Organization section
      expect(screen.getByRole('menuitem', { name: 'Organization' })).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: 'Invites' })).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: 'Billing' })).toBeInTheDocument();

      // Personal section
      expect(screen.getByRole('menuitem', { name: 'Settings' })).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: 'Help' })).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: /Theme:/ })).toBeInTheDocument();

      // Admin link NOT present for team tier
      expect(screen.queryByRole('menuitem', { name: 'Admin' })).not.toBeInTheDocument();

      // Logout
      expect(screen.getByRole('menuitem', { name: 'Logout' })).toBeInTheDocument();
    });

    it('shows Admin item for admin-tier users', async () => {
      renderLayout(); // default mock: admin tier
      await openAccountMenu();

      expect(screen.getByRole('menuitem', { name: 'Admin' })).toBeInTheDocument();
    });

    it('omits org section for a solo user with no default_org_id', async () => {
      useMeMock.mockReturnValue({
        data: {
          tier: 'free',
          email: 'solo@sessionfs.dev',
          // no default_org_id
        },
      });
      renderLayout();
      await openAccountMenu();

      // Org items should be absent
      expect(screen.queryByRole('menuitem', { name: 'Organization' })).not.toBeInTheDocument();
      expect(screen.queryByRole('menuitem', { name: 'Invites' })).not.toBeInTheDocument();
      expect(screen.queryByRole('menuitem', { name: 'Billing' })).not.toBeInTheDocument();

      // Personal items still present
      expect(screen.getByRole('menuitem', { name: 'Settings' })).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: 'Help' })).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: 'Logout' })).toBeInTheDocument();
    });

    it('calls logout when Logout menuitem is clicked', async () => {
      renderLayout();
      await openAccountMenu();

      await userEvent.click(screen.getByRole('menuitem', { name: 'Logout' }));
      expect(mockLogout).toHaveBeenCalledTimes(1);
    });

    it('sets aria-expanded on the trigger button', async () => {
      renderLayout();
      const trigger = document.querySelector('[aria-haspopup="menu"]') as HTMLElement;
      expect(trigger).toBeInTheDocument();
      expect(trigger.getAttribute('aria-expanded')).toBe('false');

      await openAccountMenu();
      expect(trigger.getAttribute('aria-expanded')).toBe('true');

      await userEvent.keyboard('{Escape}');
      await waitFor(() => {
        expect(trigger.getAttribute('aria-expanded')).toBe('false');
      });
    });

    it('closes on Escape key', async () => {
      renderLayout();
      await openAccountMenu();

      await userEvent.keyboard('{Escape}');
      await waitFor(() => {
        expect(screen.queryByRole('menu', { name: 'Account menu' })).not.toBeInTheDocument();
      });
    });

    it('supports keyboard navigation (ArrowDown + Enter to select Settings)', async () => {
      renderLayout();
      await openAccountMenu();

      // The first non-header, non-separator item is "Organization" (for the default admin+org mock).
      // ArrowDown to "Settings" — it's after org items + separator + Settings.
      // Count the enabled items order: Organization(0), Invites(1), Billing(2), Settings(3), Help(4), Theme(5), Admin(6), Logout(7)
      // Press ArrowDown 4 times to reach Settings (index 3)
      await userEvent.keyboard('{ArrowDown}'); // Organization
      await userEvent.keyboard('{ArrowDown}'); // Invites
      await userEvent.keyboard('{ArrowDown}'); // Billing
      await userEvent.keyboard('{ArrowDown}'); // Settings (active)
      await userEvent.keyboard('{Enter}');      // Select Settings

      // After Enter, the menu should close (item was selected)
      await waitFor(() => {
        expect(screen.queryByRole('menu', { name: 'Account menu' })).not.toBeInTheDocument();
      });
    });

  });
});
