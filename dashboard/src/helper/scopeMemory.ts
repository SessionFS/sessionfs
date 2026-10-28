/**
 * The scope the user last chose in the helper drawer, kept per browser tab.
 *
 * A new conversation opened without an entry scope (the header button, the
 * help page) starts from this instead of falling back to "All my sessions",
 * so the choice survives navigating away and back (GitHub #113 A2). Storage
 * can be unavailable (private mode, blocked site data); every access is
 * wrapped and the drawer works without it.
 */

import type { HelperScope } from './HelperProvider';

const KEY = 'sfs.helper.lastScope';

// In-memory copy for when storage is unavailable: the module outlives route
// changes, so the choice still survives navigation within the page load.
let memory: string | null = null;

/**
 * Who a remembered scope belongs to: a short FNV-1a hash of the API base URL
 * and key, so a different sign-in in the same tab never sees (or is sent to)
 * the previous user's project or group. The key itself is never stored here.
 */
export function scopeOwner(baseUrl: string, apiKey: string): string {
  let h = 0x811c9dc5;
  const text = `${baseUrl}\n${apiKey}`;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

function readRaw(): string | null {
  let raw: string | null = null;
  try {
    raw = window.sessionStorage.getItem(KEY);
  } catch {
    raw = null;
  }
  return raw ?? memory;
}

/** `undefined` = nothing remembered for this owner; `{}` = the user chose
 *  "All my sessions". */
export function readRememberedScope(owner: string): HelperScope | undefined {
  if (!owner) return undefined;
  try {
    const raw = readRaw();
    if (!raw) return undefined;
    const v = JSON.parse(raw) as unknown;
    if (!v || typeof v !== 'object') return undefined;
    const stored = v as Record<string, unknown>;
    if (stored.owner !== owner) return undefined;
    const o = (stored.scope && typeof stored.scope === 'object' ? stored.scope : {}) as Record<string, unknown>;
    const str = (x: unknown) => (typeof x === 'string' && x.length > 0 && x.length <= 128 ? x : undefined);
    const ids = Array.isArray(o.session_ids)
      ? o.session_ids.filter((x): x is string => typeof x === 'string' && x.length <= 128).slice(0, 50)
      : undefined;
    const out: HelperScope = {};
    if (str(o.group_id)) out.group_id = str(o.group_id);
    else if (str(o.project_id)) out.project_id = str(o.project_id);
    else if (ids && ids.length > 0) out.session_ids = ids;
    if (str(o.label)) out.label = str(o.label);
    return out;
  } catch {
    return undefined;
  }
}

export function rememberScope(owner: string, scope: HelperScope | undefined): void {
  if (!owner) return;
  const value: HelperScope = scope
    ? {
        ...(scope.group_id ? { group_id: scope.group_id } : {}),
        ...(!scope.group_id && scope.project_id ? { project_id: scope.project_id } : {}),
        ...(!scope.group_id && !scope.project_id && scope.session_ids?.length ? { session_ids: scope.session_ids } : {}),
        ...(scope.label ? { label: scope.label } : {}),
      }
    : {};
  memory = JSON.stringify({ owner, scope: value });
  try {
    window.sessionStorage.setItem(KEY, memory);
  } catch {
    // Storage unavailable: the in-memory copy lasts for this page load.
  }
}

/** Forget the remembered scope (sign-in, sign-out, or a scope that is gone). */
export function clearRememberedScope(): void {
  memory = null;
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}
