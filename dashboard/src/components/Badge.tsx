const VARIANT_STYLES: Record<string, { bg: string; text: string; border: string }> = {
  default: {
    bg: 'var(--bg-tertiary)',
    text: 'var(--text-secondary)',
    border: 'var(--border)',
  },
  success: {
    bg: 'rgba(61,220,132,0.12)',
    text: 'var(--accent)',
    border: 'rgba(61,220,132,0.3)',
  },
  warning: {
    bg: 'rgba(240,192,64,0.12)',
    text: 'var(--warning)',
    border: 'rgba(240,192,64,0.3)',
  },
  danger: {
    bg: 'rgba(240,64,96,0.12)',
    text: 'var(--danger)',
    border: 'rgba(240,64,96,0.3)',
  },
  info: {
    bg: 'rgba(79,156,247,0.12)',
    text: 'var(--info)',
    border: 'rgba(79,156,247,0.3)',
  },
  tool: {
    bg: 'var(--bg-tertiary)',
    text: 'var(--text-secondary)',
    border: 'var(--border)',
  },
};

// Tint variant: soft color-mix background, no border, semantic text color
const TINT_SEMANTIC_COLORS: Record<string, string> = {
  default: 'var(--text-secondary)',
  success: 'var(--accent)',
  warning: 'var(--warning)',
  danger: 'var(--danger)',
  info: 'var(--info)',
};

const TOOL_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  'claude-code': {
    bg: 'rgba(217,119,6,0.12)',
    text: 'var(--tool-claude)',
    border: 'rgba(217,119,6,0.3)',
  },
  codex: {
    bg: 'rgba(16,185,129,0.12)',
    text: 'var(--tool-codex)',
    border: 'rgba(16,185,129,0.3)',
  },
  cursor: {
    bg: 'rgba(139,92,246,0.12)',
    text: 'var(--tool-cursor)',
    border: 'rgba(139,92,246,0.3)',
  },
  gemini: {
    bg: 'rgba(99,102,241,0.12)',
    text: 'var(--tool-gemini)',
    border: 'rgba(99,102,241,0.3)',
  },
  copilot: {
    bg: 'rgba(59,130,246,0.12)',
    text: 'var(--tool-copilot)',
    border: 'rgba(59,130,246,0.3)',
  },
  amp: {
    bg: 'rgba(236,72,153,0.12)',
    text: 'var(--tool-amp)',
    border: 'rgba(236,72,153,0.3)',
  },
  cline: {
    bg: 'rgba(20,184,166,0.12)',
    text: 'var(--tool-cline)',
    border: 'rgba(20,184,166,0.3)',
  },
  roo: {
    bg: 'rgba(249,115,22,0.12)',
    text: 'var(--tool-roo)',
    border: 'rgba(249,115,22,0.3)',
  },
};

interface BadgeProps {
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' | 'tool';
  label: string;
  size?: 'sm' | 'md';
  /** Soft color-mix background, no border, semantic text — for status pills */
  tint?: boolean;
}

export function Badge({ variant = 'default', label, size = 'sm', tint = false }: BadgeProps) {
  const styles =
    variant === 'tool'
      ? TOOL_COLORS[label.toLowerCase()] || VARIANT_STYLES.tool
      : VARIANT_STYLES[variant] || VARIANT_STYLES.default;

  const sizeClass = size === 'sm' ? 'px-1.5 py-0.5 text-xs' : 'px-2 py-0.5 text-sm';

  if (tint) {
    const semanticColor = TINT_SEMANTIC_COLORS[variant] || TINT_SEMANTIC_COLORS.default;
    return (
      <span
        className={`inline-block rounded-full font-medium ${sizeClass}`}
        style={{
          backgroundColor: `color-mix(in srgb, ${semanticColor} 15%, transparent)`,
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
