/**
 * Safe localStorage wrapper.
 *
 * Guards against environments where `window.localStorage` is missing or
 * doesn't implement the full Storage interface. Covers: SSR, Safari private
 * mode, sandboxed iframes with cookies blocked, and test environments where
 * jsdom exposes `localStorage` as a plain object without methods.
 *
 * All methods are best-effort and never throw — callers get `null` on read
 * failure and a boolean on write failure if they care.
 */

function getStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    const ls = window.localStorage;
    if (!ls) return null;
    // Full Storage interface check — jsdom+vitest4 gives a plain object
    if (
      typeof ls.getItem !== 'function' ||
      typeof ls.setItem !== 'function' ||
      typeof ls.removeItem !== 'function'
    ) {
      return null;
    }
    return ls;
  } catch {
    // Accessing localStorage itself can throw (SecurityError) in some contexts
    return null;
  }
}

export function getItem(key: string): string | null {
  const ls = getStorage();
  if (!ls) return null;
  try {
    return ls.getItem(key);
  } catch {
    return null;
  }
}

export function setItem(key: string, value: string): boolean {
  const ls = getStorage();
  if (!ls) return false;
  try {
    ls.setItem(key, value);
    return true;
  } catch {
    // QuotaExceededError, private mode quota, etc.
    return false;
  }
}

export function removeItem(key: string): boolean {
  const ls = getStorage();
  if (!ls) return false;
  try {
    ls.removeItem(key);
    return true;
  } catch {
    return false;
  }
}
