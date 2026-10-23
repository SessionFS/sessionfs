/**
 * Settings → Account shows the tier the server enforces (`effective_tier`
 * from /me), not the raw users.tier column. An org's creator has a raw tier
 * of 'free' once their subscription moves to the org, while the org grants
 * Team — the page must say Team.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import AccountSection from './AccountSection';

function profile(extra: Record<string, unknown> = {}) {
  return {
    email: 'owner@example.com',
    email_verified: true,
    tier: 'free',
    effective_tier: 'team',
    capture_health: null,
    last_client_version: null,
    ...extra,
  };
}

describe('AccountSection tier', () => {
  it('shows the effective tier for an org member whose raw tier is free', () => {
    render(<AccountSection profile={profile()} logout={vi.fn()} />);
    expect(screen.getByTestId('account-tier')).toHaveTextContent('team');
  });

  it('shows enterprise when the org grants enterprise', () => {
    render(<AccountSection profile={profile({ effective_tier: 'enterprise' })} logout={vi.fn()} />);
    expect(screen.getByTestId('account-tier')).toHaveTextContent('enterprise');
  });

  it('falls back to the raw tier when the server omits effective_tier', () => {
    render(
      <AccountSection profile={profile({ tier: 'pro', effective_tier: undefined })} logout={vi.fn()} />,
    );
    expect(screen.getByTestId('account-tier')).toHaveTextContent('pro');
  });
});
