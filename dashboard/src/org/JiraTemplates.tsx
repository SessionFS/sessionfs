import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Combobox,
  Dialog,
  DialogFooter,
  DialogHeader,
  IconButton,
  Input,
  MonoText,
  Select,
  Table,
  Textarea,
  Tooltip,
} from '../components/ui';
import { useProjects } from '../hooks/useProjects';
import { useTickets } from '../hooks/useTickets';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import {
  JIRA_SOURCE_FIELDS,
  JIRA_TEMPLATES,
  jiraErrorCode,
  jiraSourceFieldsForMode,
} from '../utils/jira';
import {
  useCreateJiraTemplate,
  useDeleteJiraTemplate,
  useJiraMappings,
  useJiraTemplates,
  useJiraTemplateVersions,
  usePreviewJiraTemplate,
  useUnarchiveJiraTemplate,
  useUpdateJiraTemplate,
} from './useJira';
import { IntegrationSection } from './integrations/parts';
import type {
  JiraTemplate,
  JiraTemplateMode,
  JiraTemplatePreview,
  JiraTemplateSection,
  JiraTemplateSectionRequest,
} from '../api/client';

/* ── Org-custom Jira story templates (Enterprise, owner/admin) ──
 *
 * Built-ins are listed read-only. Custom templates are an ordered list of
 * sections; each section is verbatim (copied from the ticket, never the model)
 * or rewrite (may be reshaped by the model, cited). The editor only offers the
 * source fields the server can render for a section's mode, and previews the
 * unsaved draft deterministically against a real ticket from a mapped project. */

const TEMPLATE_MAX_SECTIONS = 12;
const TEMPLATE_MAX_HEADING = 80;
const TEMPLATE_MAX_GUIDANCE = 500;
const TEMPLATE_MAX_NAME = 120;
const TEMPLATE_MAX_SLUG = 50;
const TEMPLATE_MAX_DESCRIPTION = 1000;

const TEMPLATE_MODE_OPTIONS: { value: JiraTemplateMode; label: string }[] = [
  { value: 'rewrite', label: 'Rewrite' },
  { value: 'verbatim', label: 'Verbatim' },
];

/* Guidance shown in the editor's info tooltips. Mirrors the server rules in
 * integrations/trackers/templates.py and promote.py. */
const MODE_HELP =
  'Rewrite: the section starts as the ticked fields, in the order you ticked them. When a model is ' +
  'available for Jira drafts it may reshape that into prose, but every paragraph it writes must cite ' +
  'the ticket field it came from, or the paragraph is dropped. Guidance steers this section only.\n\n' +
  'Verbatim: copied from the ticket exactly and never written by the model; only acceptance criteria, ' +
  'completion notes and linked sessions can be verbatim.';

const SOURCE_FIELDS_HELP =
  'Each ticked field adds its content to this section, in the order you ticked them; a field that is ' +
  'empty on a ticket adds nothing. For rewrite sections the model is shown the ticket with secrets ' +
  '(and your DLP categories) redacted first. Verbatim sections are filled from the ticket directly.';

const FIELD_HELP: Record<string, string> = {
  title: 'The ticket title, as one paragraph.',
  description: 'The full ticket description (Markdown).',
  acceptance_criteria: 'Each acceptance criterion, as a list, word for word and in order.',
  priority: 'A “Priority: …” line (low, medium, high or critical).',
  kind: 'A “Kind: …” line: task or issue.',
  persona: 'An “Assigned to: …” line with the assigned persona; empty when unassigned.',
  completion_notes: 'The notes written when the ticket was completed; empty until then.',
  linked_sessions:
    'Title and id of each related session in the ticket’s project. Deleted sessions and sessions from other projects are left out.',
};

function InfoIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="11" x2="12" y2="16" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  );
}

/** A small "i" button with an explanation in a tooltip (hover, focus or tap).
 *  The text is also the button's description for screen readers. */
function InfoTip({ label, text, align = 'start' }: { label: string; text: string; align?: 'start' | 'center' | 'end' }) {
  const id = useId();
  return (
    <Tooltip content={text} multiline side="bottom" align={align} toggleOnClick>
      <button
        type="button"
        aria-label={`About ${label}`}
        aria-describedby={id}
        className="inline-grid h-5 w-5 place-items-center rounded-full text-text-tertiary transition-colors hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        <InfoIcon />
      </button>
      <span id={id} className="sr-only">
        {text}
      </span>
    </Tooltip>
  );
}

interface SectionDraft {
  /** Stable client-side identity: React key + focus target across reorders. */
  id: string;
  key: string;
  heading: string;
  mode: JiraTemplateMode;
  sourceFields: string[];
  guidance: string;
}

let sectionSeq = 0;
function nextSectionId(): string {
  sectionSeq += 1;
  return `section-${sectionSeq}`;
}

function emptySection(): SectionDraft {
  return {
    id: nextSectionId(),
    key: '',
    heading: '',
    mode: 'rewrite',
    sourceFields: ['title'],
    guidance: '',
  };
}

/** The starting section nobody has typed into yet. */
function isUntouchedSection(s: SectionDraft): boolean {
  const blank = emptySection();
  return (
    s.key.trim() === '' &&
    s.heading.trim() === '' &&
    s.guidance.trim() === '' &&
    s.mode === blank.mode &&
    s.sourceFields.join(',') === blank.sourceFields.join(',')
  );
}

function sourceFieldLabel(value: string): string {
  return JIRA_SOURCE_FIELDS.find((f) => f.value === value)?.label ?? value;
}

function allowedFields(mode: JiraTemplateMode): string[] {
  return jiraSourceFieldsForMode(mode).map((f) => f.value);
}

function toSectionDrafts(sections: JiraTemplateSection[]): SectionDraft[] {
  return sections.map((s) => ({
    id: nextSectionId(),
    key: s.key,
    heading: s.heading,
    mode: s.mode,
    sourceFields: [...s.source_fields],
    guidance: s.guidance ?? '',
  }));
}

function toSectionRequests(sections: SectionDraft[]): JiraTemplateSectionRequest[] {
  return sections.map((s) => ({
    key: s.key.trim(),
    heading: s.heading.trim(),
    mode: s.mode,
    source_fields: s.sourceFields,
    guidance: s.mode === 'rewrite' && s.guidance.trim() ? s.guidance.trim() : null,
  }));
}

function ArrowUpIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="18 15 12 9 6 15" />
    </svg>
  );
}

function ArrowDownIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

type TemplateDialogState =
  | { mode: 'new' }
  | { mode: 'edit'; template: JiraTemplate }
  | { mode: 'versions'; template: JiraTemplate }
  | { mode: 'archive'; template: JiraTemplate };

export function TemplatesSection({ orgId }: { orgId: string }) {
  const { data: templates, isLoading, isError, error } = useJiraTemplates(orgId);
  const unarchive = useUnarchiveJiraTemplate(orgId);
  const [dialog, setDialog] = useState<TemplateDialogState | null>(null);
  const { addToast } = useToast();

  // Built-in slugs plus every custom slug (archived ones stay reserved) — a
  // new template can't reuse either. The server enforces both; this is an
  // early hint.
  const existingSlugs = useMemo(() => {
    const slugs = new Set(JIRA_TEMPLATES.map((t) => t.value));
    for (const t of templates ?? []) {
      if (!t.builtin) slugs.add(t.slug);
    }
    return slugs;
  }, [templates]);

  const close = () => setDialog(null);

  async function handleUnarchive(t: JiraTemplate) {
    try {
      await unarchive.mutateAsync(t.slug);
      addToast('success', `${t.name} unarchived`);
    } catch (err: unknown) {
      addToast('error', errorMessage(err, 'Could not unarchive the template'));
    }
  }

  const all = templates ?? [];
  const custom = all.filter((t) => !t.builtin);
  const builtins = all.filter((t) => t.builtin);

  return (
    <IntegrationSection
      title="Story templates"
      label="Jira story templates"
      description="How a promoted ticket is laid out in Jira. Custom templates are versioned; built-ins are read-only."
      actions={
        <Button size="sm" variant="secondary" onClick={() => setDialog({ mode: 'new' })}>
          New template
        </Button>
      }
    >
      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : isError ? (
        <Alert variant="danger">{errorMessage(error, 'Could not load Jira templates')}</Alert>
      ) : (
        <div className="space-y-4">
          {custom.length > 0 ? (
            <Table<JiraTemplate>
              label="Custom Jira story templates table"
              columns={[
                {
                  key: 'name',
                  header: 'Name',
                  render: (t) => (
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span>{t.name}</span>
                      <span className="text-xs text-text-tertiary">v{t.version}</span>
                      {t.archived_at && <Badge variant="warning" label="Archived" tint />}
                    </span>
                  ),
                },
                { key: 'slug', header: 'Slug', render: (t) => <MonoText value={t.slug} /> },
                { key: 'sections', header: 'Sections', render: (t) => `${t.sections.length}` },
                {
                  key: 'actions',
                  header: <span className="sr-only">Actions</span>,
                  align: 'right',
                  render: (t) => (
                    // Wraps rather than scrolling an action out of view on
                    // tablet widths.
                    <div className="flex flex-wrap justify-end gap-1">
                      {!t.archived_at && (
                        <Button size="sm" variant="ghost" onClick={() => setDialog({ mode: 'edit', template: t })} aria-label={`Edit ${t.name}`}>
                          Edit
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" onClick={() => setDialog({ mode: 'versions', template: t })} aria-label={`Versions of ${t.name}`}>
                        Versions
                      </Button>
                      {t.archived_at ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={unarchive.isPending}
                          onClick={() => handleUnarchive(t)}
                          aria-label={`Unarchive ${t.name}`}
                        >
                          Unarchive
                        </Button>
                      ) : (
                        <Button size="sm" variant="ghost" onClick={() => setDialog({ mode: 'archive', template: t })} aria-label={`Archive ${t.name}`}>
                          Archive
                        </Button>
                      )}
                    </div>
                  ),
                },
              ]}
              data={custom}
              rowKey={(t) => t.slug}
            />
          ) : (
            <p className="text-sm text-text-tertiary">
              No custom templates yet. Create one when the built-ins don't match how your team writes stories.
            </p>
          )}
          {custom.some((t) => t.archived_at) && (
            <p className="text-xs text-text-tertiary">
              Archived templates keep their slug reserved and keep working for mappings and published
              tickets already using them; they just can't be newly selected.
            </p>
          )}

          {builtins.length > 0 && <BuiltinTemplates templates={builtins} />}
        </div>
      )}

      {dialog?.mode === 'new' && (
        <TemplateEditorDialog
          orgId={orgId}
          existingSlugs={existingSlugs}
          onClose={close}
          onSaved={() => {
            close();
            addToast('success', 'Template created');
          }}
        />
      )}
      {dialog?.mode === 'edit' && (
        <TemplateEditorDialog
          orgId={orgId}
          existingSlugs={existingSlugs}
          initial={dialog.template}
          onClose={close}
          onSaved={() => {
            close();
            addToast('success', 'Template updated');
          }}
        />
      )}
      {dialog?.mode === 'versions' && (
        <TemplateVersionsDialog orgId={orgId} template={dialog.template} onClose={close} />
      )}
      {dialog?.mode === 'archive' && (
        <ConfirmArchiveTemplateDialog
          orgId={orgId}
          template={dialog.template}
          onClose={close}
          onArchived={() => {
            close();
            addToast('success', 'Template archived');
          }}
        />
      )}
    </IntegrationSection>
  );
}

/** Built-ins fold to one line; they are read-only and rarely looked at. */
function BuiltinTemplates({ templates }: { templates: JiraTemplate[] }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  return (
    <div className="rounded-lg border border-border">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left text-sm text-text-secondary hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-lg"
      >
        <span>
          {templates.length} built-in template{templates.length === 1 ? '' : 's'}
          <span className="ml-2 text-xs text-text-tertiary">Read-only</span>
        </span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {open && (
        <ul id={listId} className="divide-y divide-border border-t border-border">
          {templates.map((t) => (
            <li key={`builtin:${t.slug}`} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-4 py-2 text-sm">
              <span className="text-text-primary">{t.name}</span>
              <span className="font-mono text-xs text-text-tertiary">{t.slug}</span>
              <span className="ml-auto text-xs text-text-tertiary">
                {t.sections.length} section{t.sections.length === 1 ? '' : 's'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function saveErrorMessage(err: unknown): string {
  switch (jiraErrorCode(err)) {
    case 'template_version_conflict':
      return 'Someone else saved this template while you were editing. Close the editor and reopen it to load their version, then re-apply your changes.';
    case 'template_slug_taken':
      return 'That slug is already in use in this organization (archived templates keep their slug).';
    case 'template_limit':
      return errorMessage(err, 'This organization has reached its template limit; archive one first.');
    default:
      return errorMessage(err, 'Could not save the template');
  }
}

interface TemplateEditorDialogProps {
  orgId: string;
  existingSlugs: Set<string>;
  initial?: JiraTemplate;
  onClose: () => void;
  onSaved: () => void;
}

export function TemplateEditorDialog({
  orgId,
  existingSlugs,
  initial,
  onClose,
  onSaved,
}: TemplateEditorDialogProps) {
  const create = useCreateJiraTemplate(orgId);
  const update = useUpdateJiraTemplate(orgId);
  const { data: allTemplates } = useJiraTemplates(orgId);
  const isEdit = initial != null;
  const pending = isEdit ? update.isPending : create.isPending;

  const [slug, setSlug] = useState(initial?.slug ?? '');
  const [name, setName] = useState(initial?.name ?? '');
  const [description, setDescription] = useState(initial?.description ?? '');
  const [sections, setSections] = useState<SectionDraft[]>(() =>
    initial ? toSectionDrafts(initial.sections) : [emptySection()],
  );
  const [error, setError] = useState<string | null>(null);
  const [copiedFrom, setCopiedFrom] = useState<string | null>(null);

  // "Copy from…": built-ins, then the org's active custom templates (not the
  // one being edited). Picking one replaces the sections.
  const copyChoices = useMemo(() => {
    const list = (allTemplates ?? []).filter(
      (t) => t.sections.length > 0 && !t.archived_at && !(isEdit && !t.builtin && t.slug === initial?.slug),
    );
    return [...list.filter((t) => t.builtin), ...list.filter((t) => !t.builtin)];
  }, [allTemplates, isEdit, initial?.slug]);
  const copyOptions = useMemo(
    () =>
      copyChoices.map((t) => ({
        value: `${t.builtin ? 'builtin' : 'custom'}:${t.slug}`,
        label: `${t.builtin ? 'Built-in · ' : ''}${t.name} (${t.sections.length} section${t.sections.length === 1 ? '' : 's'})`,
      })),
    [copyChoices],
  );

  // Copying replaces the sections; ask first unless nothing was written yet
  // (only the untouched starting section).
  const [pendingCopy, setPendingCopy] = useState<JiraTemplate | null>(null);
  // The confirm opens from the select's popup, which is gone by the time it
  // closes, so focus is handed back to the "Copy from…" trigger explicitly.
  const copyTriggerRef = useRef<HTMLDivElement>(null);
  const refocusCopyRef = useRef(false);
  function closeCopyConfirm() {
    refocusCopyRef.current = true;
    setPendingCopy(null);
  }
  useEffect(() => {
    if (pendingCopy || !refocusCopyRef.current) return;
    refocusCopyRef.current = false;
    copyTriggerRef.current?.querySelector<HTMLButtonElement>('button[aria-haspopup="listbox"]')?.focus();
  }, [pendingCopy]);
  const sectionsEdited = !(sections.length === 1 && isUntouchedSection(sections[0]));

  function applyCopy(t: JiraTemplate) {
    setSections(toSectionDrafts(t.sections));
    setCopiedFrom(t.name);
    setError(null);
  }

  function copySectionsFrom(value: string) {
    const t = copyChoices.find((x) => `${x.builtin ? 'builtin' : 'custom'}:${x.slug}` === value);
    if (!t) return;
    if (sectionsEdited) setPendingCopy(t);
    else applyCopy(t);
  }

  // Focus follows a moved section: after a reorder, focus the same arrow on
  // the section's new position (or the other arrow if it hit an edge).
  const sectionRefs = useRef(new Map<string, HTMLDivElement>());
  const pendingFocusRef = useRef<{ id: string; dir: 'up' | 'down' } | null>(null);
  useEffect(() => {
    const pendingFocus = pendingFocusRef.current;
    pendingFocusRef.current = null;
    if (!pendingFocus) return;
    const container = sectionRefs.current.get(pendingFocus.id);
    if (container) {
      const preferred = container.querySelector<HTMLButtonElement>(`[data-move="${pendingFocus.dir}"]`);
      const other = container.querySelector<HTMLButtonElement>(
        `[data-move="${pendingFocus.dir === 'up' ? 'down' : 'up'}"]`,
      );
      const target = preferred && !preferred.disabled ? preferred : other;
      target?.focus();
    }
  }, [sections]);

  const validationError = useMemo(() => {
    if (slug.trim() === '') return 'Template slug is required.';
    if (!/^[a-z0-9][a-z0-9-]*$/.test(slug.trim()))
      return 'Slug must be lowercase letters, digits, and dashes, and start with a letter or digit.';
    if (existingSlugs.has(slug.trim()) && slug.trim() !== initial?.slug)
      return 'That slug is already in use.';
    if (name.trim() === '') return 'Template name is required.';
    if (sections.length === 0) return 'Add at least one section.';
    for (let i = 0; i < sections.length; i++) {
      const s = sections[i];
      if (!s.key.trim()) return `Section ${i + 1} is missing a key.`;
      if (!/^[a-z][a-z0-9_]*$/.test(s.key.trim()))
        return `Section ${i + 1} key must be lower_snake_case.`;
      if (!s.heading.trim()) return `Section ${i + 1} is missing a heading.`;
      if (s.sourceFields.length === 0) return `Section ${i + 1} needs at least one source field.`;
      const allowed = allowedFields(s.mode);
      if (s.sourceFields.some((f) => !allowed.includes(f)))
        return `Section ${i + 1} uses a source field its mode can't render.`;
    }
    const keys = sections.map((s) => s.key.trim());
    if (new Set(keys).size !== keys.length) return 'Section keys must be unique.';
    return null;
  }, [slug, name, sections, existingSlugs, initial?.slug]);

  const ready = validationError === null;

  function patchSection(id: string, patch: Partial<SectionDraft>) {
    setSections((prev) => prev.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  }

  function setSectionMode(id: string, mode: JiraTemplateMode) {
    setSections((prev) =>
      prev.map((s) => {
        if (s.id !== id) return s;
        // Drop fields the new mode can't render; a verbatim section with
        // nothing left starts from acceptance criteria.
        const allowed = allowedFields(mode);
        let sourceFields = s.sourceFields.filter((f) => allowed.includes(f));
        if (sourceFields.length === 0) sourceFields = [allowed[0]];
        return { ...s, mode, sourceFields };
      }),
    );
  }

  function moveSection(id: string, dir: 'up' | 'down') {
    const at = sections.findIndex((s) => s.id === id);
    const to = dir === 'up' ? at - 1 : at + 1;
    // Only a real move schedules a focus hand-off; otherwise a stale request
    // would steal focus on the next unrelated edit.
    if (at < 0 || to < 0 || to >= sections.length) return;
    setSections((prev) => {
      const i = prev.findIndex((s) => s.id === id);
      const j = dir === 'up' ? i - 1 : i + 1;
      if (i < 0 || j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
    pendingFocusRef.current = { id, dir };
  }

  function removeSection(id: string) {
    setSections((prev) => (prev.length <= 1 ? prev : prev.filter((s) => s.id !== id)));
  }

  function addSection() {
    setSections((prev) => (prev.length >= TEMPLATE_MAX_SECTIONS ? prev : [...prev, emptySection()]));
  }

  function toggleSourceField(id: string, field: string) {
    setSections((prev) =>
      prev.map((s) => {
        if (s.id !== id) return s;
        const sourceFields = s.sourceFields.includes(field)
          ? s.sourceFields.filter((f) => f !== field)
          : [...s.sourceFields, field];
        return { ...s, sourceFields };
      }),
    );
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!ready) {
      setError(validationError);
      return;
    }
    setError(null);
    const body = {
      slug: slug.trim(),
      name: name.trim(),
      description: description.trim() || null,
      sections: toSectionRequests(sections),
    };
    try {
      if (isEdit) {
        await update.mutateAsync({
          slug: initial!.slug,
          body: { ...body, expected_version: initial!.version ?? undefined },
        });
      } else {
        await create.mutateAsync(body);
      }
      onSaved();
    } catch (err: unknown) {
      setError(saveErrorMessage(err));
    }
  }

  return (
    <>
    <Dialog
      open
      onClose={onClose}
      titleId="jira-template-editor-title"
      // Wide screens get a side-by-side preview; the dialog never outgrows
      // the viewport and scrolls inside instead.
      className="max-w-2xl lg:max-w-6xl max-h-[calc(100dvh-2rem)] flex flex-col !p-4 sm:!p-6"
    >
      <DialogHeader titleId="jira-template-editor-title">
        {isEdit ? 'Edit template' : 'New template'}
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col" autoComplete="off">
        <div
          data-testid="template-editor-body"
          className="-mx-1 min-h-0 flex-1 overflow-y-auto overscroll-contain px-1 lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)] lg:grid-rows-[minmax(0,1fr)] lg:gap-6 lg:overflow-hidden"
        >
        <div className="space-y-4 lg:min-h-0 lg:overflow-y-auto lg:overscroll-contain lg:pr-2 lg:-mr-1 pb-1">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <Input
            title="Slug"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="design-lite"
            maxLength={TEMPLATE_MAX_SLUG}
            disabled={isEdit}
            hint={isEdit ? 'Slug is immutable after creation.' : 'Lowercase letters, digits, and dashes.'}
          />
          <Input
            title="Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Design Lite"
            maxLength={TEMPLATE_MAX_NAME}
          />
        </div>

        <Textarea
          title="Description (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          maxLength={TEMPLATE_MAX_DESCRIPTION}
          placeholder="When to use this template…"
          hint={`${description.length}/${TEMPLATE_MAX_DESCRIPTION}`}
        />

        <div className="space-y-3">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <span className="text-sm font-semibold text-text-primary">Sections</span>
            <div className="flex flex-wrap items-center gap-2">
              {copyOptions.length > 0 && (
                <div ref={copyTriggerRef} className="w-56 max-w-full">
                  <Select
                    aria-label="Copy sections from a template"
                    value=""
                    onValueChange={copySectionsFrom}
                    options={copyOptions}
                    placeholder="Copy from…"
                  />
                </div>
              )}
              <Button
                type="button"
                size="sm"
                variant="secondary"
                onClick={addSection}
                disabled={sections.length >= TEMPLATE_MAX_SECTIONS}
              >
                Add section
              </Button>
            </div>
          </div>
          {copiedFrom && (
            <p role="status" className="text-xs text-text-tertiary">
              Sections copied from {copiedFrom}. Edit them freely; the original isn’t changed.
            </p>
          )}

          {sections.map((s, i) => {
            const label = s.heading.trim() || `section ${i + 1}`;
            return (
              <div
                key={s.id}
                ref={(el) => {
                  if (el) sectionRefs.current.set(s.id, el);
                  else sectionRefs.current.delete(s.id);
                }}
                role="group"
                aria-label={`Section ${i + 1}`}
                className="rounded-lg border border-border p-3 space-y-3"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-text-tertiary">Section {i + 1}</span>
                  <div className="flex items-center gap-1">
                    <IconButton
                      label={`Move ${label} up`}
                      size="sm"
                      data-move="up"
                      onClick={() => moveSection(s.id, 'up')}
                      disabled={i === 0}
                    >
                      <ArrowUpIcon />
                    </IconButton>
                    <IconButton
                      label={`Move ${label} down`}
                      size="sm"
                      data-move="down"
                      onClick={() => moveSection(s.id, 'down')}
                      disabled={i === sections.length - 1}
                    >
                      <ArrowDownIcon />
                    </IconButton>
                    <IconButton
                      label={`Remove ${label}`}
                      size="sm"
                      variant="danger"
                      onClick={() => removeSection(s.id)}
                      disabled={sections.length <= 1}
                    >
                      <CloseIcon />
                    </IconButton>
                  </div>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <Input
                    title="Key"
                    value={s.key}
                    onChange={(e) => patchSection(s.id, { key: e.target.value })}
                    placeholder="summary"
                    maxLength={64}
                  />
                  <Input
                    title="Heading"
                    value={s.heading}
                    onChange={(e) => patchSection(s.id, { heading: e.target.value })}
                    placeholder="Summary"
                    maxLength={TEMPLATE_MAX_HEADING}
                  />
                </div>

                <div className="space-y-1">
                <div className="flex items-center gap-1">
                  <span className="text-sm font-medium text-text-secondary" aria-hidden="true">Mode</span>
                  <InfoTip label="Mode" text={MODE_HELP} />
                </div>
                <Select
                  aria-label="Mode"
                  value={s.mode}
                  onValueChange={(v) => setSectionMode(s.id, v as JiraTemplateMode)}
                  options={TEMPLATE_MODE_OPTIONS}
                  hint={
                    s.mode === 'verbatim'
                      ? 'Verbatim copies acceptance criteria, completion notes or linked sessions exactly; the model never rewrites it.'
                      : 'Rewrite may be reshaped by the model into prose; every paragraph must cite the ticket.'
                  }
                />
                </div>

                <fieldset className="space-y-1">
                  <legend className="flex items-center gap-1 text-sm font-medium text-text-secondary">
                    Source fields
                    <InfoTip label="source fields" text={SOURCE_FIELDS_HELP} />
                  </legend>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5">
                    {jiraSourceFieldsForMode(s.mode).map((f) => (
                      <div key={f.value} className="flex items-center gap-1">
                        <Checkbox
                          label={f.label}
                          checked={s.sourceFields.includes(f.value)}
                          onChange={() => toggleSourceField(s.id, f.value)}
                        />
                        {FIELD_HELP[f.value] && (
                          // Centred on the icon: fits beside either column, and on phones.
                          <InfoTip label={f.label} text={FIELD_HELP[f.value]} align="center" />
                        )}
                      </div>
                    ))}
                  </div>
                </fieldset>

                {s.mode === 'rewrite' && (
                  <Textarea
                    title="Guidance (optional)"
                    value={s.guidance}
                    onChange={(e) => patchSection(s.id, { guidance: e.target.value })}
                    maxLength={TEMPLATE_MAX_GUIDANCE}
                    placeholder="e.g. Lead with the user problem, then the solution."
                    hint={`${s.guidance.length}/${TEMPLATE_MAX_GUIDANCE} — a hint to the model for this section only; it can't add content the ticket doesn't cite.`}
                  />
                )}
              </div>
            );
          })}
        </div>

        </div>

        <div className="mt-4 lg:mt-0 lg:min-h-0 lg:overflow-y-auto lg:overscroll-contain">
          <TemplateDraftPreview
            orgId={orgId}
            slug={slug.trim() || 'draft'}
            name={name.trim()}
            sections={sections}
            ready={ready}
          />
        </div>
        </div>

        <div className="shrink-0 space-y-2 pt-3">
          {error && <Alert variant="danger">{error}</Alert>}
          {!ready && <p className="text-xs text-text-tertiary">{validationError}</p>}
        </div>

        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={pending || !ready}>
            {pending ? 'Saving…' : isEdit ? 'Save changes' : 'Create template'}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
    {pendingCopy && (
      // A sibling of the editor (not nested in it) so each keeps its own
      // focus trap; Escape closes only this one.
      <Dialog open onClose={closeCopyConfirm} titleId="jira-template-copy-title">
        <DialogHeader titleId="jira-template-copy-title">Replace your sections?</DialogHeader>
        <p className="text-sm text-text-secondary">
          Copying from {pendingCopy.name} replaces the {sections.length} section
          {sections.length === 1 ? '' : 's'} in this editor with its {pendingCopy.sections.length}. Your current
          sections aren’t saved anywhere else.
        </p>
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={closeCopyConfirm}>
            Keep my sections
          </Button>
          <Button
            type="button"
            onClick={() => {
              applyCopy(pendingCopy);
              closeCopyConfirm();
            }}
          >
            Replace sections
          </Button>
        </DialogFooter>
      </Dialog>
    )}
    </>
  );
}

/** Deterministic preview of the editor's draft against a real ticket from a
 *  mapped project (no model call, nothing saved). */
function TemplateDraftPreview({
  orgId,
  slug,
  name,
  sections,
  ready,
}: {
  orgId: string;
  slug: string;
  name: string;
  sections: SectionDraft[];
  ready: boolean;
}) {
  const { data: mappings } = useJiraMappings(orgId);
  const { data: projects } = useProjects();
  const [projectId, setProjectId] = useState('');
  const [ticketId, setTicketId] = useState('');
  const { data: tickets, isLoading: ticketsLoading } = useTickets(projectId || undefined);
  const preview = usePreviewJiraTemplate(orgId);
  const [result, setResult] = useState<JiraTemplatePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  // Same searchable picker as the mapping dialog (GitHub #103): a long list of
  // mapped projects is searched by name or id, sorted by name.
  const projectOptions = useMemo(() => {
    const names = new Map((projects ?? []).map((p) => [p.id, p.name] as const));
    return (mappings ?? [])
      .map((m) => ({
        value: m.project_id,
        label: names.get(m.project_id) ?? m.project_id,
        description: m.jira_project_key ? `Jira ${m.jira_project_key}` : undefined,
        searchText: `${names.get(m.project_id) ?? ''} ${m.project_id} ${m.jira_project_key ?? ''}`,
      }))
      .sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true, sensitivity: 'base' }));
  }, [mappings, projects]);

  const ticketOptions = useMemo(
    () => (tickets ?? []).map((t) => ({ value: t.id, label: `${t.title} (${t.id})` })),
    [tickets],
  );

  async function runPreview() {
    setPreviewError(null);
    try {
      const data = await preview.mutateAsync({
        slug,
        body: {
          project_id: projectId,
          ticket_id: ticketId,
          name: name || undefined,
          sections: toSectionRequests(sections),
        },
      });
      setResult(data);
    } catch (err: unknown) {
      setResult(null);
      setPreviewError(errorMessage(err, 'Could not preview the template'));
    }
  }

  return (
    <div className="rounded-lg border border-border p-3 space-y-3 bg-surface" role="region" aria-label="Template preview">
      <div>
        <span className="text-xs font-semibold text-text-tertiary uppercase tracking-[0.04em]">Preview</span>
        <p className="mt-0.5 text-xs text-text-tertiary">
          Renders the unsaved draft against a real ticket, without the model and without saving.
        </p>
      </div>
      {projectOptions.length === 0 ? (
        <p className="text-sm text-text-tertiary">
          Map a project to Jira to preview this template against one of its tickets.
        </p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] lg:grid-cols-1 gap-3 items-end">
          <Combobox
            title="Mapped project"
            value={projectId}
            onValueChange={(v) => {
              setProjectId(v);
              setTicketId('');
              setResult(null);
            }}
            options={projectOptions}
            placeholder="Pick a project"
            searchPlaceholder="Search by name or ID"
            noMatchesMessage={(q) => `No mapped projects match “${q}”`}
          />
          <Combobox
            title="Ticket"
            value={ticketId}
            onValueChange={(v) => setTicketId(v)}
            options={ticketOptions}
            placeholder={projectId ? 'Select a ticket' : 'Pick a project first'}
            searchPlaceholder="Search by title or ID"
            disabled={!projectId}
            loading={ticketsLoading}
            emptyMessage={ticketsLoading ? 'Loading tickets…' : 'No tickets in this project'}
            noMatchesMessage={(q) => `No tickets match “${q}”`}
          />
          <Button
            type="button"
            variant="secondary"
            onClick={runPreview}
            disabled={!ready || !projectId || !ticketId || preview.isPending}
          >
            {preview.isPending ? 'Rendering…' : 'Preview'}
          </Button>
        </div>
      )}

      {previewError && <Alert variant="danger">{previewError}</Alert>}

      {result ? (
        <div className="space-y-2" aria-live="polite">
          <p className="text-sm font-semibold text-text-primary">{result.summary}</p>
          {result.sections.map((section) => (
            <div key={section.key} className="text-sm">
              <div className="flex items-baseline gap-2">
                <span className="font-semibold text-text-primary">{section.heading || section.key}</span>
                <Badge
                  variant={section.mode === 'verbatim' ? 'default' : 'info'}
                  label={section.mode === 'verbatim' ? 'verbatim' : 'rewrite'}
                />
              </div>
              {section.paragraphs.length === 0 ? (
                <p className="text-xs text-text-tertiary">(empty for this ticket)</p>
              ) : (
                section.paragraphs.map((p, i) => (
                  <p key={i} className="whitespace-pre-wrap text-text-secondary">
                    {p.text}
                  </p>
                ))
              )}
            </div>
          ))}
        </div>
      ) : (
        <ol className="space-y-2" aria-label="Template structure">
          {sections.map((s) => (
            <li key={s.id} className="text-sm">
              <div className="font-semibold text-text-primary">{s.heading.trim() || '(untitled section)'}</div>
              <div className="text-xs text-text-tertiary">
                {s.mode === 'rewrite' ? 'Rewrite' : 'Verbatim'} ·{' '}
                {s.sourceFields.length ? s.sourceFields.map(sourceFieldLabel).join(', ') : 'no source fields'}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function TemplateVersionsDialog({
  orgId,
  template,
  onClose,
}: {
  orgId: string;
  template: JiraTemplate;
  onClose: () => void;
}) {
  const { data: versions, isLoading, isError, error } = useJiraTemplateVersions(orgId, template.slug);

  return (
    <Dialog open onClose={onClose} titleId="jira-template-versions-title">
      <DialogHeader titleId="jira-template-versions-title">Version history — {template.name}</DialogHeader>
      <p className="text-sm text-text-secondary mb-3">
        Every save bumps the version and snapshots the sections. Tickets already linked keep the
        version they were published with.
      </p>

      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : isError ? (
        <Alert variant="danger">{errorMessage(error, 'Could not load version history')}</Alert>
      ) : (
        <ul className="space-y-1 max-h-80 overflow-y-auto">
          {(versions ?? []).map((v) => (
            <li
              key={v.version}
              className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm"
            >
              <span className="flex items-center gap-2">
                <span className="font-semibold text-text-primary">v{v.version}</span>
                {v.version === template.version && <Badge variant="info" label="current" />}
              </span>
              <span className="text-xs text-text-tertiary">
                {v.sections.length} section{v.sections.length === 1 ? '' : 's'} ·{' '}
                {v.created_at ? new Date(v.created_at).toLocaleString() : '—'}
              </span>
            </li>
          ))}
        </ul>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

function ConfirmArchiveTemplateDialog({
  orgId,
  template,
  onClose,
  onArchived,
}: {
  orgId: string;
  template: JiraTemplate;
  onClose: () => void;
  onArchived: () => void;
}) {
  const remove = useDeleteJiraTemplate(orgId);
  const [error, setError] = useState<string | null>(null);

  async function handleArchive() {
    setError(null);
    try {
      await remove.mutateAsync(template.slug);
      onArchived();
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not archive the template'));
    }
  }

  return (
    <Dialog open onClose={onClose} titleId="jira-template-archive-title">
      <DialogHeader titleId="jira-template-archive-title">Archive template?</DialogHeader>
      <p className="text-sm text-text-secondary">
        Archive <span className="font-mono">{template.name}</span>? It stops appearing as a new
        choice in template pickers. Mappings, the connection default and tickets already using it
        keep working, and you can unarchive it later. Its slug stays reserved.
      </p>
      {error && (
        <Alert variant="danger" className="mt-3">
          {error}
        </Alert>
      )}
      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button variant="danger" disabled={remove.isPending} onClick={handleArchive}>
          {remove.isPending ? 'Archiving…' : 'Archive'}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
