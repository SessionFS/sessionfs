/**
 * Pure form logic for the assistant (session helper) settings forms: the
 * draft shape, client-side bounds, the merge-patch builder (only changed
 * fields; an explicit null resets/clears) and 422 → per-field errors.
 *
 * Bounds and defaults mirror `server/helper/settings.py`.
 */

import { ApiError, type HelperModelSettings, type HelperSettingsPatch, type OrgHelperModelSettings } from '../api/client';

export const LIMITS = {
  outputTokens: { min: 256, max: 32_000 },
  temperature: { min: 0, max: 2 },
  topP: { min: 0, max: 1 },
  toolSteps: { min: 1, max: 20 },
  orgInstructions: 4000,
  personalInstructions: 2000,
} as const;

export const DEFAULTS: HelperModelSettings = {
  max_output_tokens: 4096,
  temperature: 0,
  top_p: null,
  max_tool_steps: 8,
  reasoning_effort: null,
  answer_style: 'concise',
};

/** Settings keys whose explicit null returns them to the default. */
export type ResettableKey = 'max_output_tokens' | 'max_tool_steps' | 'answer_style';

export type FieldKey =
  | keyof OrgHelperModelSettings
  | 'monthly_token_budget'
  | 'instructions';

export type FieldErrors = Partial<Record<FieldKey, string>>;

/** Form state: numbers stay strings so a half-typed value isn't coerced. */
export interface Draft {
  max_output_tokens: string;
  /** True = "Provider default" (sent as null). */
  temperatureProviderDefault: boolean;
  temperature: string;
  top_p: string;
  max_tool_steps: string;
  /** '' = not set (sent as null). */
  reasoning_effort: '' | 'off' | 'low' | 'medium' | 'high';
  answer_style: 'concise' | 'detailed';
  ceiling_max_output_tokens: string;
  ceiling_max_tool_steps: string;
  monthly_token_budget: string;
  instructions: string;
}

export interface Original {
  settings: HelperModelSettings & Partial<Pick<OrgHelperModelSettings, 'ceiling_max_output_tokens' | 'ceiling_max_tool_steps'>>;
  instructions: string | null;
  monthly_token_budget?: number | null;
}

const numStr = (n: number | null | undefined) => (n == null ? '' : String(n));

export function draftFrom(o: Original): Draft {
  const s = o.settings;
  return {
    max_output_tokens: numStr(s.max_output_tokens),
    temperatureProviderDefault: s.temperature === null,
    temperature: numStr(s.temperature ?? DEFAULTS.temperature),
    top_p: numStr(s.top_p),
    max_tool_steps: numStr(s.max_tool_steps),
    reasoning_effort: s.reasoning_effort ?? '',
    answer_style: s.answer_style,
    ceiling_max_output_tokens: numStr(s.ceiling_max_output_tokens),
    ceiling_max_tool_steps: numStr(s.ceiling_max_tool_steps),
    monthly_token_budget: numStr(o.monthly_token_budget),
    instructions: o.instructions ?? '',
  };
}

type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

/** Characters as the server counts them (code points, Python's len). */
export function charCount(text: string): number {
  return [...text].length;
}

/** Strips digit-group separators people type: "6,000,000", "6_000_000", "6 000 000". */
function ungroup(raw: string): string {
  // No lookbehind: Safari before 16.4 can't parse it.
  return raw.trim().replace(/(\d)[,_\s](?=\d)/g, '$1');
}

function parseInt_(raw: string, min: number, max: number, optional: boolean): Parsed<number | null> {
  const t = ungroup(raw);
  if (t === '') return optional ? { ok: true, value: null } : { ok: false, error: 'Enter a value.' };
  if (!/^\d+$/.test(t)) return { ok: false, error: 'Enter a whole number.' };
  const n = Number(t);
  if (n < min || n > max) return { ok: false, error: `Enter a number from ${min.toLocaleString()} to ${max.toLocaleString()}.` };
  return { ok: true, value: n };
}

function parseFloat_(raw: string, min: number, max: number, optional: boolean): Parsed<number | null> {
  // Accept a single decimal comma ("0,5"), as typed on many locales' keypads.
  const t = raw.trim().replace(/^(\d*),(\d+)$/, '$1.$2');
  if (t === '') return optional ? { ok: true, value: null } : { ok: false, error: 'Enter a value.' };
  if (!/^(\d+(\.\d*)?|\.\d+)$/.test(t)) return { ok: false, error: 'Enter a number.' };
  const n = Number(t);
  if (n < min || n > max) return { ok: false, error: `Enter a number from ${min} to ${max}.` };
  return { ok: true, value: n };
}

function parseBudget(raw: string): Parsed<number | null> {
  const t = ungroup(raw);
  if (t === '') return { ok: true, value: null };
  if (!/^\d+$/.test(t)) return { ok: false, error: 'Enter a whole number of tokens, or leave empty for no budget.' };
  const n = Number(t);
  if (!Number.isSafeInteger(n)) return { ok: false, error: 'That number is too large.' };
  return { ok: true, value: n };
}

export interface PatchBody {
  settings?: HelperSettingsPatch;
  instructions?: string | null;
  monthly_token_budget?: number | null;
}

/**
 * Build the merge-PUT body: only fields that differ from what the server
 * returned, plus an explicit null for each field the user reset. Returns the
 * client-side validation errors instead when any field is invalid.
 */
export function buildPatch(
  original: Original,
  draft: Draft,
  resets: ReadonlySet<ResettableKey>,
  opts: { org: boolean; instructionsLimit: number },
): { body: PatchBody; errors: FieldErrors } {
  const errors: FieldErrors = {};
  const settings: HelperSettingsPatch = {};
  const s = original.settings;

  const take = <K extends keyof HelperSettingsPatch>(key: K, parsed: Parsed<HelperSettingsPatch[K]>, before: unknown) => {
    if (!parsed.ok) {
      errors[key] = parsed.error;
      return;
    }
    if (parsed.value !== before) settings[key] = parsed.value;
  };

  if (resets.has('max_output_tokens')) settings.max_output_tokens = null;
  else take('max_output_tokens', parseInt_(draft.max_output_tokens, LIMITS.outputTokens.min, LIMITS.outputTokens.max, false), s.max_output_tokens);

  if (draft.temperatureProviderDefault) {
    if (s.temperature !== null) settings.temperature = null;
  } else {
    take('temperature', parseFloat_(draft.temperature, LIMITS.temperature.min, LIMITS.temperature.max, false), s.temperature);
  }

  take('top_p', parseFloat_(draft.top_p, LIMITS.topP.min, LIMITS.topP.max, true), s.top_p);

  if (resets.has('max_tool_steps')) settings.max_tool_steps = null;
  else take('max_tool_steps', parseInt_(draft.max_tool_steps, LIMITS.toolSteps.min, LIMITS.toolSteps.max, false), s.max_tool_steps);

  const effort = draft.reasoning_effort === '' ? null : draft.reasoning_effort;
  if (effort !== s.reasoning_effort) settings.reasoning_effort = effort;

  if (resets.has('answer_style')) settings.answer_style = null;
  else if (draft.answer_style !== s.answer_style) settings.answer_style = draft.answer_style;

  const body: PatchBody = {};

  if (opts.org) {
    take(
      'ceiling_max_output_tokens',
      parseInt_(draft.ceiling_max_output_tokens, LIMITS.outputTokens.min, LIMITS.outputTokens.max, true),
      s.ceiling_max_output_tokens ?? null,
    );
    take(
      'ceiling_max_tool_steps',
      parseInt_(draft.ceiling_max_tool_steps, LIMITS.toolSteps.min, LIMITS.toolSteps.max, true),
      s.ceiling_max_tool_steps ?? null,
    );
    const budget = parseBudget(draft.monthly_token_budget);
    if (!budget.ok) errors.monthly_token_budget = budget.error;
    else if (budget.value !== (original.monthly_token_budget ?? null)) body.monthly_token_budget = budget.value;
  }

  if (charCount(draft.instructions) > opts.instructionsLimit) {
    errors.instructions = `Use at most ${opts.instructionsLimit.toLocaleString()} characters.`;
  } else {
    const next = draft.instructions.trim() === '' ? null : draft.instructions;
    if (next !== (original.instructions || null)) body.instructions = next;
  }

  if (Object.keys(settings).length > 0) body.settings = settings;
  return { body, errors };
}

export function isEmptyPatch(body: PatchBody): boolean {
  return body.settings === undefined && body.instructions === undefined && body.monthly_token_budget === undefined;
}

const FIELD_KEYS: readonly FieldKey[] = [
  'max_output_tokens',
  'temperature',
  'top_p',
  'max_tool_steps',
  'reasoning_effort',
  'answer_style',
  'ceiling_max_output_tokens',
  'ceiling_max_tool_steps',
  'monthly_token_budget',
  'instructions',
];

function envelope(err: ApiError): { code?: string; message?: string; errors?: unknown } {
  try {
    const json = JSON.parse(err.raw) as {
      error?: { code?: unknown; message?: unknown; details?: { errors?: unknown } };
      detail?: unknown;
    };
    const e = json.error;
    if (e && typeof e === 'object') {
      return {
        code: typeof e.code === 'string' ? e.code : undefined,
        message: typeof e.message === 'string' ? e.message : undefined,
        errors: e.details?.errors,
      };
    }
    // FastAPI's default 422 shape (no envelope handler).
    if (Array.isArray(json.detail)) return { errors: json.detail };
    if (json.detail && typeof json.detail === 'object') {
      const d = json.detail as { code?: unknown; message?: unknown };
      return {
        code: typeof d.code === 'string' ? d.code : undefined,
        message: typeof d.message === 'string' ? d.message : undefined,
      };
    }
  } catch {
    /* not JSON */
  }
  return {};
}

function friendlyPydantic(msg: string): string {
  const m = /^Input should be (.+)$/.exec(msg);
  return m ? `Must be ${m[1]}.` : msg;
}

/**
 * Per-field messages from a 422: pydantic validation errors (`loc` names the
 * field) and the instruction errors (`instructions_invalid`,
 * `instructions_too_long`, `instructions_redaction_failed`). Null when the
 * error isn't a field error.
 */
export function fieldErrorsFrom(err: unknown): FieldErrors | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null;
  const env = envelope(err);
  if (env.code?.startsWith('instructions_')) {
    return { instructions: env.message ?? 'These instructions can’t be saved.' };
  }
  if (!Array.isArray(env.errors)) return null;
  const out: FieldErrors = {};
  for (const item of env.errors as Array<{ loc?: unknown; msg?: unknown }>) {
    const loc = Array.isArray(item?.loc) ? item.loc.map(String) : [];
    const key = [...loc].reverse().find((p): p is FieldKey => (FIELD_KEYS as readonly string[]).includes(p));
    if (!key || out[key]) continue;
    out[key] = typeof item.msg === 'string' ? friendlyPydantic(item.msg) : 'Invalid value.';
  }
  return Object.keys(out).length > 0 ? out : null;
}

/** True when the API refused because the plan lacks the helper. */
export function isUpgradeRequired(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 403) return false;
  const code = err.code ?? envelope(err).code;
  return code === undefined || code === 'upgrade_required';
}

// ── Display ──

export const REASONING_LABELS: Record<string, string> = {
  '': 'Not set (model default)',
  off: 'Off',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
};

export const STYLE_LABELS: Record<string, string> = {
  concise: 'Concise',
  detailed: 'Detailed',
};

/** Read-only rows for the effective settings. */
export function describeSettings(s: HelperModelSettings): Array<{ label: string; value: string }> {
  return [
    { label: 'Max output tokens', value: s.max_output_tokens.toLocaleString() },
    { label: 'Temperature', value: s.temperature === null ? 'Provider default' : String(s.temperature) },
    { label: 'Top P', value: s.top_p === null ? 'Not sent' : String(s.top_p) },
    { label: 'Max tool steps', value: String(s.max_tool_steps) },
    { label: 'Reasoning effort', value: REASONING_LABELS[s.reasoning_effort ?? ''] },
    { label: 'Answer style', value: STYLE_LABELS[s.answer_style] ?? s.answer_style },
  ];
}
