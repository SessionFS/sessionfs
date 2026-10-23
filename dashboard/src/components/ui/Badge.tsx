// Tints are derived from the semantic text token with color-mix so they follow
// the active theme (darker text on light surfaces, lighter on dark ones).
const tint = (token: string, pct: number) =>
  `color-mix(in oklch, var(${token}) ${pct}%, transparent)`;

interface VariantStyle {
  bg: string;
  text: string;
  border: string;
}

const accented = (token: string): VariantStyle => ({
  bg: tint(token, 12),
  text: `var(${token})`,
  border: tint(token, 30),
});

const NEUTRAL: VariantStyle = {
  bg: 'var(--bg-tertiary)',
  text: 'var(--text-secondary)',
  border: 'var(--border)',
};

const VARIANT_STYLES: Record<string, VariantStyle> = {
  default: NEUTRAL,
  success: accented('--success'),
  warning: accented('--warning'),
  danger: accented('--danger'),
  info: accented('--info'),
  tool: NEUTRAL,
};

// Tint variant: soft color-mix background, no border, semantic text color
const TINT_SEMANTIC_COLORS: Record<string, string> = {
  default: 'var(--text-secondary)',
  success: 'var(--success)',
  warning: 'var(--warning)',
  danger: 'var(--danger)',
  info: 'var(--info)',
};

const TOOL_COLORS: Record<string, VariantStyle> = {
  'claude-code': accented('--tool-claude'),
  codex: accented('--tool-codex'),
  cursor: accented('--tool-cursor'),
  gemini: accented('--tool-gemini'),
  copilot: accented('--tool-copilot'),
  amp: accented('--tool-amp'),
  cline: accented('--tool-cline'),
  roo: accented('--tool-roo'),
};

interface BadgeProps {
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' | 'tool';
  label: string;
  size?: 'sm' | 'md';
  /** Soft color-mix background, no border, semantic text — for status pills */
  tint?: boolean;
}

export function Badge({ variant = 'default', label, size = 'sm', tint: tinted = false }: BadgeProps) {
  const styles =
    variant === 'tool'
      ? TOOL_COLORS[label.toLowerCase()] || VARIANT_STYLES.tool
      : VARIANT_STYLES[variant] || VARIANT_STYLES.default;

  const sizeClass = size === 'sm' ? 'px-1.5 py-0.5 text-xs' : 'px-2 py-0.5 text-sm';

  if (tinted) {
    const semanticColor = TINT_SEMANTIC_COLORS[variant] || TINT_SEMANTIC_COLORS.default;
    return (
      <span
        className={`inline-block rounded-full font-medium ${sizeClass}`}
        style={{
          backgroundColor: `color-mix(in oklch, ${semanticColor} 15%, transparent)`,
          color: semanticColor,
        }}
      >
        {label}
      </span>
    );
  }

  return (
    <span
      className={`inline-block rounded-full font-medium ${sizeClass}`}
      style={{
        backgroundColor: styles.bg,
        color: styles.text,
        border: `1px solid ${styles.border}`,
      }}
    >
      {label}
    </span>
  );
}

/* Legacy exports — keep existing call sites working */

export function ToolBadge({ tool }: { tool: string }) {
  return <Badge variant="tool" label={tool} size="sm" />;
}

export function ModelBadge({ model }: { model: string | null | undefined }) {
  if (!model) return null;
  return <Badge variant="default" label={model} size="sm" />;
}
