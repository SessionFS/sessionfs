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

/** Display-only model name: strips a trailing `-YYYYMMDD`/`@YYYYMMDD` date
 * stamp so ids like `gpt-5-20250127` read as `gpt-5`. The raw id stays
 * intact for filtering and the tooltip; the "unknown" bucket (null/empty
 * model id) has no raw id so callers label it "Unknown model" themselves. */
export function friendlyModelName(id: string | null | undefined): string {
  if (!id) return '';
  return id.replace(/[-@]\d{8}$/, '');
}

/** Model ids that carry no real model: the server folds these (and NULL)
 * into its "unknown" bucket — `?model=unknown` matches them and
 * `/sessions/models` reports them as `model_id: null`. */
const UNKNOWN_MODEL_IDS = new Set(['', 'unknown', '<synthetic>']);

export function isUnknownModelId(id: string | null | undefined): boolean {
  return id == null || UNKNOWN_MODEL_IDS.has(id);
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

const TOOL_FULL_NAMES: Record<string, string> = {
  'claude-code': 'Claude Code',
  codex: 'Codex',
  cursor: 'Cursor',
  gemini: 'Gemini CLI',
  'gemini-cli': 'Gemini CLI',
  copilot: 'Copilot',
  'copilot-cli': 'Copilot',
  amp: 'Amp',
  cline: 'Cline',
  'roo-code': 'Roo Code',
  antigravity: 'Antigravity',
  aider: 'Aider',
};

export function fullToolName(name: string | null | undefined): string {
  if (!name) return '';
  const lower = name.toLowerCase();
  for (const [key, display] of Object.entries(TOOL_FULL_NAMES)) {
    if (lower === key || lower.startsWith(key)) return display;
  }
  return name;
}
