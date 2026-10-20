import { describe, expect, it } from 'vitest';
import { displayTier } from './tier';

describe('displayTier', () => {
  it('prefers the effective tier over the raw users.tier', () => {
    // An org creator: raw tier is free, the org grants team.
    expect(displayTier({ tier: 'free', effective_tier: 'team' })).toBe('team');
  });

  it('falls back to the raw tier when the server sends no effective tier', () => {
    expect(displayTier({ tier: 'pro' })).toBe('pro');
    expect(displayTier({ tier: 'pro', effective_tier: null })).toBe('pro');
  });

  it('defaults to free when nothing is known', () => {
    expect(displayTier(undefined)).toBe('free');
    expect(displayTier(null)).toBe('free');
    expect(displayTier({})).toBe('free');
  });
});
