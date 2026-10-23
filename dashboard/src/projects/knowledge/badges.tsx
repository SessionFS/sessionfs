

/** Tint of a semantic token, so entry-type badges follow the theme. */
const MIX = (token: string, pct: number) =>
  `color-mix(in srgb, var(${token}) ${pct}%, transparent)`;

const ENTRY_TYPE_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  decision: { bg: MIX('--success', 12), text: 'var(--success)', border: MIX('--success', 30) },
  pattern: { bg: MIX('--info', 12), text: 'var(--info)', border: MIX('--info', 30) },
  discovery: { bg: MIX('--role-discovery', 12), text: 'var(--role-discovery)', border: MIX('--role-discovery', 30) },
  convention: { bg: MIX('--tool-cline', 12), text: 'var(--tool-cline)', border: MIX('--tool-cline', 30) },
  bug: { bg: MIX('--danger', 12), text: 'var(--danger)', border: MIX('--danger', 30) },
  dependency: { bg: MIX('--warning', 12), text: 'var(--warning)', border: MIX('--warning', 30) },
};

export function EntryTypeBadge({ type }: { type: string }) {
  const colors = ENTRY_TYPE_COLORS[type] || {
    bg: 'var(--bg-tertiary)',
    text: 'var(--text-secondary)',
    border: 'var(--border)',
  };
  return (
    <span
      className="inline-block rounded-full px-1.5 py-0.5 text-xs font-medium"
      style={{
        backgroundColor: colors.bg,
        color: colors.text,
        border: `1px solid ${colors.border}`,
      }}
    >
      {type}
    </span>
  );
}

const CLAIM_CLASS_STYLES: Record<string, { bg: string; text: string; border: string; outline: boolean }> = {
  claim: { bg: 'var(--brand)', text: 'var(--text-on-brand)', border: 'var(--brand)', outline: false },
  note: { bg: 'transparent', text: 'var(--text-tertiary)', border: 'var(--border)', outline: true },
  evidence: { bg: 'transparent', text: 'var(--info)', border: MIX('--info', 40), outline: true },
};

export function ClaimClassBadge({ claimClass }: { claimClass: string }) {
  const style = CLAIM_CLASS_STYLES[claimClass] || CLAIM_CLASS_STYLES.note;
  return (
    <span
      className="inline-block rounded-full px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-wide"
      style={{
        backgroundColor: style.outline ? 'transparent' : style.bg,
        color: style.text,
        border: `1px solid ${style.border}`,
      }}
    >
      {claimClass}
    </span>
  );
}

const FRESHNESS_DOT_CLASSES: Record<string, string> = {
  current: 'bg-success',
  aging: 'bg-warning',
  stale: 'bg-danger',
  superseded: 'bg-text-tertiary',
};

/**
 * Freshness indicator. Hue alone is not enough, so the dot exposes the class
 * as its accessible name. Pass `decorative` when the class is already spelled
 * out in adjacent text (e.g. the legend).
 */
export function FreshnessDot({
  freshnessClass,
  decorative = false,
}: {
  freshnessClass: string;
  decorative?: boolean;
}) {
  const bg = FRESHNESS_DOT_CLASSES[freshnessClass] || 'bg-text-tertiary';
  const a11y = decorative
    ? { 'aria-hidden': true as const }
    : { role: 'img' as const, 'aria-label': `Freshness: ${freshnessClass}`, title: freshnessClass };
  return <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${bg}`} {...a11y} />;
}
