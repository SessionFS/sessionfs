import { describe, expect, it } from 'vitest';

import { hasMinimumOrgRole, isOrgAdminRole } from './roles';

describe('org role helpers', () => {
  it('owners and admins have admin standing; members and unknowns do not', () => {
    expect(isOrgAdminRole('owner')).toBe(true);
    expect(isOrgAdminRole('admin')).toBe(true);
    expect(isOrgAdminRole('member')).toBe(false);
    expect(isOrgAdminRole('platform_admin')).toBe(false);
    expect(isOrgAdminRole(null)).toBe(false);
    expect(isOrgAdminRole(undefined)).toBe(false);
  });

  it('ranks member < admin < owner', () => {
    expect(hasMinimumOrgRole('admin', 'owner')).toBe(false);
    expect(hasMinimumOrgRole('owner', 'owner')).toBe(true);
    expect(hasMinimumOrgRole('member', 'member')).toBe(true);
  });
});
