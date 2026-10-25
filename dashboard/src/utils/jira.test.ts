import { describe, expect, it } from 'vitest';
import { canUnlinkTicket, hasJiraEnterpriseTier, jiraErrorCode } from './jira';

/**
 * Pure helpers behind the Jira ticket surface. `hasJiraEnterpriseTier` gates
 * the Promote button; `canUnlinkTicket` mirrors the server's project-admin
 * rule using the full org membership (not the primary-org `me.org_id`).
 */

describe('hasJiraEnterpriseTier', () => {
  it('is true for an enterprise effective tier', () => {
    expect(hasJiraEnterpriseTier({ tier: 'enterprise', effective_tier: 'enterprise' })).toBe(true);
  });

  it('is true for legacy admin accounts', () => {
    expect(hasJiraEnterpriseTier({ tier: 'admin', effective_tier: 'enterprise' })).toBe(true);
  });

  it('is false for lower tiers', () => {
    expect(hasJiraEnterpriseTier({ tier: 'team', effective_tier: 'team' })).toBe(false);
    expect(hasJiraEnterpriseTier({ tier: 'pro', effective_tier: 'pro' })).toBe(false);
  });

  it('is false when me is missing', () => {
    expect(hasJiraEnterpriseTier(null)).toBe(false);
    expect(hasJiraEnterpriseTier(undefined)).toBe(false);
  });
});

describe('canUnlinkTicket', () => {
  const project = { owner_id: 'owner_1', org_id: 'org_x' };

  it('is true for the project owner', () => {
    expect(canUnlinkTicket(project, { user_id: 'owner_1' }, [])).toBe(true);
  });

  it('is true for an admin of the project org, resolved from full membership', () => {
    const me = { user_id: 'member_1' };
    const myOrgs = [
      { org_id: 'org_primary', role: 'member' },
      { org_id: 'org_x', role: 'admin' },
    ];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(true);
  });

  it('is true for an owner of the project org', () => {
    const me = { user_id: 'member_1' };
    const myOrgs = [{ org_id: 'org_x', role: 'owner' }];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(true);
  });

  it('does not treat primary-org role as the project org role', () => {
    // me is an admin of their *primary* org, but only a member of the
    // project's org — they must not be able to unlink.
    const me = { user_id: 'member_1' };
    const myOrgs = [
      { org_id: 'org_primary', role: 'admin' },
      { org_id: 'org_x', role: 'member' },
    ];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(false);
  });

  it('is false for a member of the project org', () => {
    const me = { user_id: 'member_1' };
    const myOrgs = [{ org_id: 'org_x', role: 'member' }];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(false);
  });

  it('is false when the user has no membership in the project org', () => {
    const me = { user_id: 'member_1' };
    const myOrgs = [{ org_id: 'org_y', role: 'admin' }];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(false);
  });

  it('is false when there is no org scope and the user is not the owner', () => {
    expect(
      canUnlinkTicket({ owner_id: 'owner_1', org_id: null }, { user_id: 'member_1' }, []),
    ).toBe(false);
  });

  it('is false when me, project, or membership is missing', () => {
    expect(canUnlinkTicket(null, { user_id: 'owner_1' }, [])).toBe(false);
    expect(canUnlinkTicket(project, null, [])).toBe(false);
    expect(canUnlinkTicket(project, { user_id: 'member_1' }, undefined)).toBe(false);
  });
});

describe('jiraErrorCode', () => {
  it('returns undefined for non-ApiError values', () => {
    expect(jiraErrorCode(new Error('boom'))).toBeUndefined();
    expect(jiraErrorCode('nope')).toBeUndefined();
  });
});
