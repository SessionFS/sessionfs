/**
 * Draft state for an assistant settings form (org or personal). Tracks the
 * server copy the draft started from, per-field "reset to default" intents and
 * per-field errors, and derives the merge-PUT body (only changed fields).
 *
 * The draft re-syncs from fresh server data only while it has no unsaved
 * edits, so a background refetch never wipes what the user is typing.
 */

import { useState } from 'react';

import {
  DEFAULTS,
  buildPatch,
  draftFrom,
  isEmptyPatch,
  type Draft,
  type FieldErrors,
  type FieldKey,
  type Original,
  type PatchBody,
  type ResettableKey,
} from './assistantSettings';

const FIELD_OF: Partial<Record<keyof Draft, FieldKey>> = {
  temperatureProviderDefault: 'temperature',
};

export interface AssistantForm {
  draft: Draft | null;
  resets: ReadonlySet<ResettableKey>;
  errors: FieldErrors;
  dirty: boolean;
  change: (patch: Partial<Draft>) => void;
  resetField: (key: ResettableKey) => void;
  discard: () => void;
  /** The PUT body, or null after showing client-side errors. */
  validate: () => PatchBody | null;
  setErrors: (errors: FieldErrors) => void;
  /** Adopt a server response (after a save) as the new baseline. */
  load: (next: Original) => void;
}

export function useAssistantForm(
  original: Original | null,
  opts: { org: boolean; instructionsLimit: number },
): AssistantForm {
  const [base, setBase] = useState<Original | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [resets, setResets] = useState<Set<ResettableKey>>(() => new Set());
  const [errors, setErrors] = useState<FieldErrors>({});

  const computed = base && draft ? buildPatch(base, draft, resets, opts) : null;
  const dirty = !!computed && (!isEmptyPatch(computed.body) || Object.keys(computed.errors).length > 0);

  const adopt = (next: Original) => {
    setBase(next);
    setDraft(draftFrom(next));
    setResets(new Set());
    setErrors({});
  };

  // Compare by content: the caller rebuilds `original` every render.
  const originalKey = original ? JSON.stringify(original) : null;
  const baseKey = base ? JSON.stringify(base) : null;
  if (original && originalKey !== baseKey && (!base || !dirty)) adopt(original);

  const clearError = (key: FieldKey) =>
    setErrors((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });

  return {
    draft,
    resets,
    errors,
    dirty,
    change: (patch) => {
      setDraft((d) => (d ? { ...d, ...patch } : d));
      for (const k of Object.keys(patch) as Array<keyof Draft>) {
        const field = FIELD_OF[k] ?? (k as FieldKey);
        clearError(field);
        if (field === 'max_output_tokens' || field === 'max_tool_steps' || field === 'answer_style') {
          setResets((prev) => {
            if (!prev.has(field)) return prev;
            const next = new Set(prev);
            next.delete(field);
            return next;
          });
        }
      }
    },
    resetField: (key) => {
      setDraft((d) => (d ? { ...d, [key]: String(DEFAULTS[key]) } : d));
      setResets((prev) => new Set(prev).add(key));
      clearError(key);
    },
    discard: () => {
      if (base) adopt(base);
    },
    validate: () => {
      if (!computed) return null;
      if (Object.keys(computed.errors).length > 0) {
        setErrors(computed.errors);
        return null;
      }
      return computed.body;
    },
    setErrors,
    load: adopt,
  };
}
