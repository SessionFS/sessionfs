import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import Layout from './Layout';

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    logout: vi.fn(),
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
  useMe: () => ({
    data: {
      tier: 'admin',
      email: 'admin@sessionfs.dev',
    },
  }),
}));

// Phase 4 Round 3 added a Transfers nav link with a pending-count
// badge. The hook needs AuthProvider context; stub it here so this
// suite stays focused on the layout shell.
vi.mock('../transfers/useTransfers', () => ({
  useTransfers: () => ({ data: { transfers: [] }, isLoading: false, error: null }),
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

describe('Layout', () => {
  it('renders the branded shell copy and SessionFS home link', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<div>Dashboard home</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByLabelText('SessionFS home')).toBeInTheDocument();
    expect(screen.getAllByText(/memory layer for ai coding agents/i).length).toBeGreaterThan(0);
    expect(screen.getByText('Dashboard home')).toBeInTheDocument();
  });
});
