/**
 * Render a raw URL as a clickable link only when it parses to an http(s)
 * scheme. Anything else (`javascript:`, `data:`, malformed) falls back to
 * null so callers render it as plain text instead of an executable href.
 */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    if (u.protocol === 'http:' || u.protocol === 'https:') {
      return u.toString();
    }
  } catch {
    // Falls through to null — malformed URLs are not clickable.
  }
  return null;
}
