const MODEL_SHORT: Record<string, string> = {
  'claude-opus-4-6': 'opus-4.6',
  'claude-sonnet-4-6': 'sonnet-4.6',
  'claude-haiku-4-5': 'haiku-4.5',
  'claude-3-5-sonnet-20241022': 'sonnet-3.5',
  'claude-3-5-haiku-20241022': 'haiku-3.5',
  'gpt-4o': 'gpt-4o',
  'gpt-4o-mini': '4o-mini',
  'o3-mini': 'o3-mini',
};

export function abbreviateModel(id: string | null | undefined): string {
  if (!id) return '';
  if (id in MODEL_SHORT) return MODEL_SHORT[id];
  for (const [full, short] of Object.entries(MODEL_SHORT)) {
    if (id.startsWith(full)) return short;
  }
  return id.length > 12 ? id.slice(0, 11) + '\u2026' : id;
}

const TOOL_SHORT: Record<string, string> = {
  'claude-code': 'CC',
  codex: 'CDX',
  cursor: 'CUR',
  aider: 'AID',
};

export function abbreviateTool(name: string | null | undefined): string {
  if (!name) return '';
  const lower = name.toLowerCase();
  for (const [full, short] of Object.entries(TOOL_SHORT)) {
    if (lower.startsWith(full)) return short;
  }
  return name.length > 6 ? name.slice(0, 5) + '\u2026' : name;
}
