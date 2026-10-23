import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MarketplaceNav from './MarketplaceNav';

const hooks = vi.hoisted(() => ({ useMe: vi.fn(), useMyOrgs: vi.fn() }));
vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../transfers/useTransfers', () => ({ useMyOrgs: hooks.useMyOrgs }));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <MarketplaceNav />
    </MemoryRouter>,
  );
}

function names() {
  return screen.getAllByRole('link').map((l) => l.textContent);
}

beforeEach(() => {
  hooks.useMe.mockReturnValue({ data: { user_id: 'u1', tier: 'free', effective_tier: 'free' } });
  hooks.useMyOrgs.mockReturnValue({ data: [], isLoading: false });
});

describe('MarketplaceNav', () => {
  it('shows only Browse to a free user with no org', () => {
    renderAt('/marketplace');
    expect(names()).toEqual(['Browse']);
  });

  it('adds Publishers for a Starter+ user', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'u1', tier: 'pro', effective_tier: 'pro' } });
    renderAt('/marketplace');
    expect(names()).toEqual(['Browse', 'Publishers']);
  });

  it('shows everything to a platform admin', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'u1', tier: 'admin' } });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'o1', name: 'One', role: 'member' }], isLoading: false });
    renderAt('/marketplace');
    expect(names()).toEqual(['Browse', 'Publishers', 'Usage', 'Moderation']);
  });

  it('marks Browse current only on /marketplace and Publishers on nested routes', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'u1', tier: 'admin' } });
    const first = renderAt('/marketplace');
    expect(screen.getByRole('link', { name: 'Browse' })).toHaveAttribute('aria-current', 'page');
    first.unmount();

    renderAt('/marketplace/publishers/pub_1/analytics');
    expect(screen.getByRole('link', { name: 'Browse' })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('link', { name: 'Publishers' })).toHaveAttribute('aria-current', 'page');
  });
});
