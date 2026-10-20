/**
 * The tier to show a signed-in user.
 *
 * `/me` returns both the raw `users.tier` column (`tier`) and the tier the
 * server actually enforces (`effective_tier`, resolved through the user's
 * org entitlement first). Org members — including an org's creator, whose
 * subscription moves to the org — carry a raw tier of 'free' while their
 * org grants Team or Enterprise, so user-facing labels must use the
 * effective tier. The raw tier is only a fallback for older servers that
 * don't send `effective_tier`.
 *
 * Platform-admin checks are a separate concern and keep reading the raw
 * `tier === 'admin'`.
 */
export function displayTier(
  me: { effective_tier?: string | null; tier?: string | null } | null | undefined,
): string {
  return me?.effective_tier || me?.tier || 'free';
}
