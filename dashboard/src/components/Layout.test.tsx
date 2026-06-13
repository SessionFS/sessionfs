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
    it('is identity + logout only — navigation lives in the sidebar, not duplicated here', async () => {
      useMeMock.mockReturnValue({
        data: {
          tier: 'team',
          email: 'team@acme.corp',
          default_org_id: 'org_123',
        },
      });
      renderLayout();
      await openAccountMenu();

      // Identity header (non-interactive) + Logout are the only menu contents.
      expect(screen.getByText('team@acme.corp')).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: 'Logout' })).toBeInTheDocument();

      // Navigation must NOT be duplicated in the account menu (it's in the sidebar).
      for (const name of ['Organization', 'Invites', 'Billing', 'Settings', 'Help', 'Admin']) {
        expect(screen.queryByRole('menuitem', { name })).not.toBeInTheDocument();
      }
      expect(screen.queryByRole('menuitem', { name: /Theme:/ })).not.toBeInTheDocument();
    });

    it('shows the same minimal menu for a solo user (no org duplication either)', async () => {
      useMeMock.mockReturnValue({
        data: {
          tier: 'free',
          email: 'solo@sessionfs.dev',
          // no default_org_id
        },
      });
      renderLayout();
      await openAccountMenu();

      expect(screen.getByText('solo@sessionfs.dev')).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: 'Logout' })).toBeInTheDocument();
      for (const name of ['Organization', 'Invites', 'Billing', 'Settings', 'Help', 'Admin']) {
        expect(screen.queryByRole('menuitem', { name })).not.toBeInTheDocument();
      }
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

    it('supports keyboard navigation (ArrowDown + Enter selects the only item, Logout)', async () => {
      renderLayout();
      await openAccountMenu();

      // Logout is the single enabled menuitem now (identity header is non-interactive).
      await userEvent.keyboard('{ArrowDown}'); // Logout (active)
      await userEvent.keyboard('{Enter}');      // Select Logout

      expect(mockLogout).toHaveBeenCalled();
      await waitFor(() => {
        expect(screen.queryByRole('menu', { name: 'Account menu' })).not.toBeInTheDocument();
      });
    });

  });

  // ── Sidebar tests ──

  describe('sidebar', () => {
    it('renders nav with aria-label="Primary" inside an aside', () => {
      renderLayout();
      // Sidebar + mobile drawer each have a <nav aria-label="Primary">
      const navs = screen.getAllByRole('navigation', { name: 'Primary' });
      expect(navs.length).toBeGreaterThanOrEqual(1);
      // At least one is inside an <aside> (the desktop sidebar)
      expect(navs.some((n) => n.closest('aside'))).toBe(true);
    });

    it('renders all ungrouped nav items (Sessions, Projects, Handoffs)', () => {
      renderLayout();
      // In jsdom both sidebar and mobile drawer are visible simultaneously
      // (no CSS media-query eval). Use getAllByRole to tolerate duplicates.
      expect(screen.getAllByRole('link', { name: /Sessions/ }).length).toBeGreaterThan(0);
      expect(screen.getAllByRole('link', { name: /Projects/ }).length).toBeGreaterThan(0);
      expect(screen.getAllByRole('link', { name: /Handoffs/ }).length).toBeGreaterThan(0);
    });

    it('renders ORGANIZATION group label text', () => {
      renderLayout();
      // The uppercase group label appears in the sidebar (text-micro, uppercase)
      const labels = screen.getAllByText('Organization');
      expect(labels.length).toBeGreaterThanOrEqual(1);
    });

    it('hides org-only Organization nav link for solo users', () => {
      useMeMock.mockReturnValue({
        data: { tier: 'free', email: 'solo@sessionfs.dev' },
      });
      renderLayout();
      // The Organization link (to /settings/organization) should be absent
      const links = screen.queryAllByRole('link', { name: 'Organization' });
      expect(links.length).toBe(0);
    });

    it('shows org chip when user has default_org_id', () => {
      renderLayout();
      const chip = screen.getByText('org_123');
      const chipLink = chip.closest('a');
      expect(chipLink).toBeInTheDocument();
      expect(chipLink).toHaveAttribute('href', '/settings/organization');
    });

    it('hides org chip for solo users', () => {
      useMeMock.mockReturnValue({
        data: { tier: 'free', email: 'solo@sessionfs.dev' },
      });
      renderLayout();
      expect(screen.queryByText('org_123')).not.toBeInTheDocument();
    });

    it('renders bottom-pinned Settings, Help, and Admin (admin user)', () => {
      renderLayout();
      // Sidebar + mobile drawer both render links in jsdom
      expect(screen.getAllByRole('link', { name: /Settings/ }).length).toBeGreaterThan(0);
      expect(screen.getAllByRole('link', { name: /Help/ }).length).toBeGreaterThan(0);
      expect(screen.getAllByRole('link', { name: /Admin/ }).length).toBeGreaterThan(0);
    });

    it('hides Admin from bottom items for non-admin users', () => {
      useMeMock.mockReturnValue({
        data: { tier: 'team', email: 'team@acme.corp', default_org_id: 'org_123' },
      });
      renderLayout();
      expect(screen.queryByRole('link', { name: /Admin/ })).not.toBeInTheDocument();
    });

    it('renders SearchBar (via mock)', () => {
      renderLayout();
      expect(screen.getByTestId('search-bar')).toBeInTheDocument();
    });

    describe('collapse', () => {
      it('reads collapsed state from localStorage on mount', () => {
        localStorage.setItem('sfs-sidebar-collapsed', 'true');
        renderLayout();
        expect(screen.getByLabelText('Expand sidebar')).toBeInTheDocument();
        localStorage.removeItem('sfs-sidebar-collapsed');
      });

      it('defaults to expanded when no localStorage key', () => {
        localStorage.removeItem('sfs-sidebar-collapsed');
        renderLayout();
        expect(screen.getByLabelText('Collapse sidebar')).toBeInTheDocument();
      });

      it('persists collapsed state to localStorage on toggle', async () => {
        localStorage.removeItem('sfs-sidebar-collapsed');
        renderLayout();
        const btn = screen.getByLabelText('Collapse sidebar');
        await userEvent.click(btn);
        expect(localStorage.getItem('sfs-sidebar-collapsed')).toBe('true');
        expect(screen.getByLabelText('Expand sidebar')).toBeInTheDocument();
        localStorage.removeItem('sfs-sidebar-collapsed');
      });
    });
  });
});
