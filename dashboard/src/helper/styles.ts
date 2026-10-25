/** Class strings for router links styled as buttons inside the helper drawer. */

const FOCUS = 'outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]';

export const linkButtonPrimary = `inline-flex items-center justify-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-text-on-brand transition-opacity hover:opacity-90 ${FOCUS}`;

export const linkButtonSecondary = `inline-flex items-center justify-center gap-2 rounded-lg border border-border bg-surface px-4 py-2 text-sm font-semibold text-text-secondary transition-colors hover:border-border-strong hover:bg-bg-elevated hover:text-text-primary ${FOCUS}`;

export const linkButtonSmall = `inline-flex items-center justify-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-1.5 text-xs font-semibold text-text-secondary transition-colors hover:border-border-strong hover:bg-bg-elevated hover:text-text-primary ${FOCUS}`;
