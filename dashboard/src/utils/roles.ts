/**
 * Org role helpers. Mirrors the server's roles.has_minimum_role: roles are
 * ranked (member < admin < owner), so an owner always has admin standing.
 * Never gate admin UI on `role === 'admin'` — that hides it from owners.
 */

const ROLE_LEVEL: Record<string, number> = { member: 10, admin: 50, owner: 100 };

export function hasMinimumOrgRole(
  role: string | null | undefined,
  required: 'member' | 'admin' | 'owner',
): boolean {
  if (!role) return false;
  const level = ROLE_LEVEL[role];
  return level !== undefined && level >= ROLE_LEVEL[required];
}

/** True for admins and owners. */
export function isOrgAdminRole(role: string | null | undefined): boolean {
  return hasMinimumOrgRole(role, 'admin');
}
