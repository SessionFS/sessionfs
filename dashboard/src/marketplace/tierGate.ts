import { ApiError } from '../api/client';

/** A 403 tier gate (`upgrade_required` / `feature_not_available` / tier-scoped 403) — not "no data". */
export function isTierGate(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  const code = (error.code || '').toLowerCase();
  return code.includes('upgrade') || code.includes('tier') || code.includes('feature') || (error.status === 403 && /tier|upgrade|team/i.test(error.message));
}
