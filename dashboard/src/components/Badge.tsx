const TOOL_COLORS: Record<string, string> = {
  'claude-code': 'border-orange-500/40 text-orange-300',
  codex: 'border-green-500/40 text-green-300',
  cursor: 'border-purple-500/40 text-purple-300',
};

export function ToolBadge({ tool }: { tool: string }) {
  const cls = TOOL_COLORS[tool] || 'border-border text-text-secondary';
  return (
    <span className={`inline-block px-1.5 py-0.5 text-xs border rounded ${cls}`}>
      {tool}
    </span>
  );
}

export function ModelBadge({ model }: { model: string | null | undefined }) {
  if (!model) return null;
  return (
    <span className="inline-block px-1.5 py-0.5 text-xs border border-border rounded text-text-secondary">
      {model}
    </span>
  );
}
