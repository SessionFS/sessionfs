export const CAPTURE_ONLY_TOOLS = new Set(['cursor', 'cline', 'roo-code', 'amp', 'antigravity']);

export const TOOLS = ['all', 'claude-code', 'codex', 'gemini', 'copilot', 'cursor', 'amp', 'cline', 'roo-code', 'antigravity'] as const;
export const DATE_RANGES = [
  { label: 'All time', value: '' },
  { label: 'Last 24h', value: '24h' },
  { label: 'Last 7d', value: '7d' },
  { label: 'Last 30d', value: '30d' },
] as const;
