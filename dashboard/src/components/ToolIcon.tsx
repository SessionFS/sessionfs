const TOOL_MAP: Record<string, { color: string; label: string }> = {
  'claude-code': { color: 'var(--tool-claude)', label: 'Claude Code' },
  claude: { color: 'var(--tool-claude)', label: 'Claude' },
  codex: { color: 'var(--tool-codex)', label: 'Codex' },
  gemini: { color: 'var(--tool-gemini)', label: 'Gemini CLI' },
  'gemini-cli': { color: 'var(--tool-gemini)', label: 'Gemini CLI' },
  copilot: { color: 'var(--tool-copilot)', label: 'Copilot' },
  'copilot-cli': { color: 'var(--tool-copilot)', label: 'Copilot' },
  cursor: { color: 'var(--tool-cursor)', label: 'Cursor' },
  amp: { color: 'var(--tool-amp)', label: 'Amp' },
  cline: { color: 'var(--tool-cline)', label: 'Cline' },
  roo: { color: 'var(--tool-roo)', label: 'Roo Code' },
  'roo-code': { color: 'var(--tool-roo)', label: 'Roo Code' },
};

const SIZES = {
  sm: { circle: 12, font: 7 },
  md: { circle: 20, font: 11 },
};

interface ToolIconProps {
  tool: string;
  size?: 'sm' | 'md';
  showLabel?: boolean;
}

export default function ToolIcon({ tool, size = 'sm', showLabel = true }: ToolIconProps) {
  const key = tool.toLowerCase();
  const entry = TOOL_MAP[key] || { color: 'var(--text-tertiary)', label: tool };
  const s = SIZES[size];

  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        style={{
          width: s.circle,
          height: s.circle,
          borderRadius: '50%',
          backgroundColor: entry.color,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: s.font,
          fontWeight: 600,
          color: 'var(--text-inverse)',
          lineHeight: 1,
          flexShrink: 0,
        }}
      >
        {entry.label.charAt(0).toUpperCase()}
      </span>
      {showLabel && (
        <span className="text-[var(--text-secondary)]" style={{ fontSize: size === 'sm' ? 13 : 14 }}>
          {entry.label}
        </span>
      )}
    </span>
  );
}
