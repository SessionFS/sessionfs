/**
 * v0.10.0 Phase 6 — Org general settings panel.
 *
 * Edits the three KB creation defaults that new org-scoped projects
 * inherit at project-create time (see routes/projects.py):
 *   - kb_retention_days
 *   - kb_max_context_words
 *   - kb_section_page_limit
 *
 * Round 3 (KB entry 298) narrowed the surface to these three fields
 * after Codex flagged that `retention_days` and `compile_model` had
 * no runtime consumers. Those fields can be re-added in a future
 * phase once the daemon retention / compile-route model-selection
 * paths are wired to read them.
 *
 * DLP policy lives in its own panel (existing `/api/v1/dlp/policy`
 * surface). This component intentionally scopes to non-DLP settings
 * to keep the form straightforward.
 *
 * Backend invariants enforced server-side (Phase 6 KB entry 230 #7):
 *   - Admin role required for PUT.
 *   - Range validation: 1-730 for kb_retention_days, 100-50000 for
 *     kb_max_context_words, 1-200 for kb_section_page_limit.
 *   - PUT body replaces the "general" block entirely; the route does
 *     a structural merge so the DLP block survives.
 */

import { useEffect, useState } from 'react';

import { useToast } from '../hooks/useToast';
import {
  type OrgGeneralSettings,
  useOrgSettings,
  useUpdateOrgSettings,
} from './useOrgSettings';

interface OrgSettingsTabProps {
  orgId: string;
  /** Set to false when the viewer is a plain member; disables edits. */
  canEdit: boolean;
}

type FormState = {
  kb_retention_days: string;
  kb_max_context_words: string;
  kb_section_page_limit: string;
};

function settingsToForm(s: OrgGeneralSettings | undefined): FormState {
  return {
    kb_retention_days: s?.kb_retention_days?.toString() ?? '',
    kb_max_context_words: s?.kb_max_context_words?.toString() ?? '',
    kb_section_page_limit: s?.kb_section_page_limit?.toString() ?? '',
  };
}

function parseIntOrNull(v: string): number | null {
  const trimmed = v.trim();
  if (trimmed === '') return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && Number.isInteger(n) ? n : NaN;
}

export default function OrgSettingsTab({ orgId, canEdit }: OrgSettingsTabProps) {
  const { data, isLoading, error } = useOrgSettings(orgId);
  const update = useUpdateOrgSettings(orgId);
  const { addToast } = useToast();

  const [form, setForm] = useState<FormState>(settingsToForm(undefined));

  // Re-seed the form from server state once it lands.
  useEffect(() => {
    setForm(settingsToForm(data));
  }, [data]);

  if (isLoading) return <p>Loading settings…</p>;
  if (error) return <p role="alert">Failed to load settings: {String(error)}</p>;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const payload: OrgGeneralSettings = {
      kb_retention_days: parseIntOrNull(form.kb_retention_days),
      kb_max_context_words: parseIntOrNull(form.kb_max_context_words),
      kb_section_page_limit: parseIntOrNull(form.kb_section_page_limit),
    };
    // Reject any NaN (bad numeric input) before sending.
    for (const [k, v] of Object.entries(payload)) {
      if (Number.isNaN(v as number)) {
        addToast('error', `${k}: must be a whole number or blank`);
        return;
      }
    }
    update.mutate(payload, {
      onSuccess: () => addToast('success', 'Settings saved'),
      onError: (err) => addToast('error', `Save failed: ${err.message}`),
    });
  };

  const field = (key: keyof FormState, label: string, hint: string) => (
    <label className="block">
      <span className="block text-[13px] text-[var(--text-tertiary)] mb-1">{label}</span>
      <input
        type="number"
        value={form[key]}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        disabled={!canEdit || update.isPending}
        aria-label={label}
        className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm"
      />
      <span className="block text-[12px] text-[var(--text-tertiary)] mt-1">{hint}</span>
    </label>
  );

  return (
    <section aria-labelledby="org-settings-heading">
      <h3 id="org-settings-heading">Org defaults</h3>
      <p className="text-sm text-[var(--text-secondary)]">
        Creation defaults for new projects added to this org. Existing
        projects keep their current values; change a project's own KB
        knobs from the project settings to override. Leave a field
        blank to fall back to the server's built-in default.
      </p>
      <form onSubmit={handleSubmit} aria-label="Org settings">
        {field(
          'kb_retention_days',
          'KB retention (days)',
          'Knowledge entry retention for new org projects (1-730).',
        )}
        {field(
          'kb_max_context_words',
          'KB compile word budget',
          'Maximum words injected into a compiled context document (100-50000).',
        )}
        {field(
          'kb_section_page_limit',
          'KB section page limit',
          'Maximum pages a single KB section can generate (1-200).',
        )}
        <button
          type="submit"
          disabled={!canEdit || update.isPending}
          title={canEdit ? undefined : 'Only org admins can change settings'}
          aria-label="Save org settings"
        >
          {update.isPending ? 'Saving…' : 'Save settings'}
        </button>
      </form>
    </section>
  );
}
