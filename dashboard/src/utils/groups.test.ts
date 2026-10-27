import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import {
  groupErrorMessage,
  groupPath,
  isUpgradeRequired,
  orgHasProjectGroups,
  slugError,
  slugifyGroupName,
} from './groups';

const envelope = (status: number, code: string, message = 'x', details?: Record<string, unknown>) =>
  new ApiError(status, JSON.stringify({ error: { code, message, details } }));

describe('slugifyGroupName', () => {
  it('mirrors the server slugify', () => {
    expect(slugifyGroupName('Payments Team')).toBe('payments-team');
    expect(slugifyGroupName('  Checkout & Payments — EU!  ')).toBe('checkout-payments-eu');
    expect(slugifyGroupName('---')).toBe('');
  });

  it('caps at 100 characters without a trailing hyphen', () => {
    const slug = slugifyGroupName(`${'a'.repeat(99)} b`);
    expect(slug.length).toBeLessThanOrEqual(100);
    expect(slug.endsWith('-')).toBe(false);
  });
});

describe('slugError', () => {
  it('accepts valid slugs and rejects invalid ones', () => {
    expect(slugError('payments-2')).toBeUndefined();
    expect(slugError('')).toMatch(/enter a slug/i);
    expect(slugError('-lead')).toMatch(/lowercase/i);
    expect(slugError('Has Space')).toMatch(/lowercase/i);
    expect(slugError('a'.repeat(101))).toMatch(/100/);
  });
});

describe('plan helpers', () => {
  it('gates on team and enterprise, and trusts the server when the tier is unknown', () => {
    expect(orgHasProjectGroups('team')).toBe(true);
    expect(orgHasProjectGroups('enterprise')).toBe(true);
    expect(orgHasProjectGroups('pro')).toBe(false);
    expect(orgHasProjectGroups('free')).toBe(false);
    expect(orgHasProjectGroups('starter')).toBe(false);
    expect(orgHasProjectGroups(undefined)).toBe(true);
    // Unknown tiers are left to the server.
    expect(orgHasProjectGroups('business')).toBe(true);
    expect(orgHasProjectGroups('admin')).toBe(true);
  });

  it('detects upgrade_required in both envelope shapes', () => {
    expect(isUpgradeRequired(envelope(403, '403', 'm', { error: 'upgrade_required' }))).toBe(true);
    expect(isUpgradeRequired(envelope(403, 'upgrade_required'))).toBe(true);
    expect(isUpgradeRequired(envelope(403, '403'))).toBe(false);
    expect(isUpgradeRequired(new Error('upgrade_required'))).toBe(false);
  });
});

describe('groupErrorMessage', () => {
  it('maps the documented codes', () => {
    expect(groupErrorMessage(envelope(422, 'not_org_member'))).toMatch(/not a member of this organization/);
    expect(groupErrorMessage(envelope(422, 'project_not_in_org'))).toMatch(/belong to this organization/);
    expect(groupErrorMessage(envelope(403, 'project_edit_forbidden'))).toMatch(/only add projects you can edit/);
    expect(groupErrorMessage(envelope(409, 'grant_conflict'))).toMatch(/same moment/);
    expect(groupErrorMessage(envelope(409, 'group_project_conflict'))).toMatch(/same moment/);
    expect(groupErrorMessage(envelope(403, '403', 'm', { error: 'upgrade_required' }))).toMatch(/Team or Enterprise/);
  });

  it('maps statuses without a semantic code', () => {
    expect(groupErrorMessage(envelope(409, '409', "slug 'x' exists"))).toMatch(/slug already exists/);
    expect(groupErrorMessage(envelope(404, '404'))).toMatch(/no longer exists/);
    expect(groupErrorMessage(envelope(403, '403', 'Group admin role required'))).toMatch(/permission/);
    expect(groupErrorMessage(new Error('boom'))).toBe('boom');
  });
});

it('groupPath links by id with the org', () => {
  expect(groupPath('org_1', 'pg_1')).toBe('/settings/organization/groups/pg_1?org=org_1');
});
