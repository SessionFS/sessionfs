import type { FlooredCount } from '../api/client';

/** README link and image rules (marketplace v2 §9.2). */
const SAFE_LINK = /^(https?:|mailto:)/i;

/** An http(s) / mailto / in-page link, or null (rendered as plain text). */
export function safeLinkHref(href: string | undefined | null): string | null {
  if (!href) return null;
  const trimmed = href.trim();
  if (trimmed.startsWith('#')) return trimmed;
  return SAFE_LINK.test(trimmed) ? trimmed : null;
}

/** A package-relative image path, or null for anything remote / absolute
 *  (remote images are never loaded: no tracking pixels). */
export function packageImagePath(src: string | undefined | null): string | null {
  if (!src) return null;
  const value = src.trim().replace(/^\.\//, '');
  if (!value || /^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith('/') || value.startsWith('\\')) return null;
  const parts = value.split('/');
  if (parts.some((part) => part === '' || part === '.' || part === '..')) return null;
  return value;
}

/** Display text of a k-floored count ("<10" under the floor). */
export function installsLabel(count: FlooredCount | undefined): string {
  return count ? count.display : '—';
}
