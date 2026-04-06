import { useState, useEffect } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import {
  useProject,
  useUpdateProjectContext,
  useDeleteProject,
  useKnowledgeEntries,
  useDismissEntry,
  useCompileProject,
  useCompilations,
  useWikiPages,
  useWikiPage,
  useUpdateWikiPage,
  useDeleteWikiPage,
  useRegenerateWikiPage,
  useUpdateProjectSettings,
} from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import RelativeDate from '../components/RelativeDate';
import type { KnowledgeEntry, WikiPage } from '../api/client';

type ProjectTab = 'context' | 'pages' | 'entries' | 'history';

const ENTRY_TYPE_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  decision: { bg: 'rgba(34,197,94,0.12)', text: '#22c55e', border: 'rgba(34,197,94,0.3)' },
  pattern: { bg: 'rgba(59,130,246,0.12)', text: '#3b82f6', border: 'rgba(59,130,246,0.3)' },
  discovery: { bg: 'rgba(168,85,247,0.12)', text: '#a855f7', border: 'rgba(168,85,247,0.3)' },
  convention: { bg: 'rgba(20,184,166,0.12)', text: '#14b8a6', border: 'rgba(20,184,166,0.3)' },
  bug: { bg: 'rgba(239,68,68,0.12)', text: '#ef4444', border: 'rgba(239,68,68,0.3)' },
  dependency: { bg: 'rgba(249,115,22,0.12)', text: '#f97316', border: 'rgba(249,115,22,0.3)' },
};

function EntryTypeBadge({ type }: { type: string }) {
  const colors = ENTRY_TYPE_COLORS[type] || {
    bg: 'var(--bg-tertiary)',
    text: 'var(--text-secondary)',
    border: 'var(--border)',
  };
  return (
    <span
      className="inline-block rounded-full px-1.5 py-0.5 text-xs font-medium"
      style={{
        backgroundColor: colors.bg,
        color: colors.text,
        border: `1px solid ${colors.border}`,
      }}
    >
      {type}
    </span>
  );
}

// ------------------------------------------------------------------
// Knowledge Entries Tab
// ------------------------------------------------------------------
function KnowledgeEntriesTab({ projectId }: { projectId: string }) {
  const [pendingOnly, setPendingOnly] = useState(false);
  const [typeFilter, setTypeFilter] = useState<string>('');
  const { data, isLoading } = useKnowledgeEntries(projectId, {
    pending: pendingOnly || undefined,
    type: typeFilter || undefined,
    limit: 50,
  });
  const dismissEntry = useDismissEntry(projectId);
  const compile = useCompileProject(projectId);
  const { addToast } = useToast();

  function handleCompile() {
    compile.mutate(undefined, {
      onSuccess: (result) => {
        const n = result.entries_compiled;
        if (n === 0) {
          addToast('info', 'No pending entries to compile.');
        } else {
          addToast('success', `Project context updated. ${n} entries compiled.`);
        }
      },
      onError: (err) => addToast('error', `Compile failed: ${String(err)}`),
    });
  }

  function handleDismiss(entry: KnowledgeEntry) {
    dismissEntry.mutate(entry.id, {
      onSuccess: () => addToast('success', `Entry ${entry.id} dismissed.`),
      onError: (err) => addToast('error', `Dismiss failed: ${String(err)}`),
    });
  }

  const entryTypes = ['decision', 'pattern', 'discovery', 'convention', 'bug', 'dependency'];

  return (
    <div className="p-5">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-sm text-[var(--text-secondary)]">
            <input
              type="checkbox"
              checked={pendingOnly}
              onChange={(e) => setPendingOnly(e.target.checked)}
              className="accent-[var(--brand)]"
            />
            Pending only
          </label>
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="text-sm bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg px-2 py-1 text-[var(--text-secondary)]"
          >
            <option value="">All types</option>
            {entryTypes.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
        <button
          onClick={handleCompile}
          disabled={compile.isPending}
          className="px-4 py-2 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {compile.isPending ? 'Compiling...' : 'Compile Now'}
        </button>
      </div>

      {/* Entries list */}
      {isLoading ? (
        <p className="text-[var(--text-tertiary)] text-sm py-4">Loading entries...</p>
      ) : !data?.entries?.length ? (
        <p className="text-[var(--text-tertiary)] text-sm py-8 text-center">
          No knowledge entries found. Entries are auto-extracted from sessions.
        </p>
      ) : (
        <div className="space-y-2">
          {data.entries.map((entry) => (
            <div
              key={entry.id}
              className={`flex items-start gap-3 px-4 py-3 rounded-lg border transition-colors ${
                entry.dismissed
                  ? 'border-[var(--border)] bg-[var(--bg-primary)] opacity-60'
                  : 'border-[var(--border)] bg-[var(--bg-elevated)]'
              }`}
            >
              <div className="shrink-0 pt-0.5">
                <EntryTypeBadge type={entry.entry_type} />
              </div>
              <div className="flex-1 min-w-0">
                <p
                  className={`text-sm text-[var(--text-primary)] ${
                    entry.dismissed ? 'line-through text-[var(--text-tertiary)]' : ''
                  }`}
                >
                  {entry.content}
                </p>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--text-tertiary)]">
                  <span>
                    <RelativeDate iso={entry.created_at} />
                  </span>
                  {entry.session_id && (
                    <>
                      <span>&middot;</span>
                      <span className="font-mono">
                        {entry.session_id.length > 14
                          ? entry.session_id.slice(0, 14) + '..'
                          : entry.session_id}
                      </span>
                    </>
                  )}
                  {entry.compiled_at && (
                    <>
                      <span>&middot;</span>
                      <span className="text-green-500">compiled</span>
                    </>
                  )}
                  {entry.confidence !== undefined && entry.confidence < 1 && (
                    <>
                      <span>&middot;</span>
                      <span>{Math.round(entry.confidence * 100)}% confidence</span>
                    </>
                  )}
                </div>
              </div>
              {!entry.dismissed && !entry.compiled_at && (
                <button
                  onClick={() => handleDismiss(entry)}
                  disabled={dismissEntry.isPending}
                  className="shrink-0 text-xs text-[var(--text-tertiary)] hover:text-red-400 transition-colors px-2 py-1"
                  title="Dismiss this entry"
                >
                  Dismiss
                </button>
              )}
            </div>
          ))}
          <p className="text-xs text-[var(--text-tertiary)] pt-2">
            {data.total > data.entries.length
              ? `Showing ${data.entries.length} of ${data.total} entries`
              : `${data.entries.length} ${data.entries.length === 1 ? 'entry' : 'entries'}`}
          </p>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------
// Pages Tab
// ------------------------------------------------------------------
function PagesTab({ projectId }: { projectId: string }) {
  const { data, isLoading } = useWikiPages(projectId);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState('');
  const [showNewPage, setShowNewPage] = useState(false);
  const [newSlug, setNewSlug] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newContent, setNewContent] = useState('');
  const { addToast } = useToast();

  const { data: pageDetail } = useWikiPage(projectId, selectedSlug);
  const updatePage = useUpdateWikiPage(projectId);
  const deletePage = useDeleteWikiPage(projectId);
  const regeneratePage = useRegenerateWikiPage(projectId);

  function handleSaveEdit(slug: string) {
    updatePage.mutate(
      { slug, content: editDraft },
      {
        onSuccess: () => {
          addToast('success', 'Page saved.');
          setEditingSlug(null);
          setEditDraft('');
        },
        onError: (err) => addToast('error', `Save failed: ${String(err)}`),
      },
    );
  }

  function handleDelete(slug: string) {
    deletePage.mutate(slug, {
      onSuccess: () => {
        addToast('success', 'Page deleted.');
        if (selectedSlug === slug) setSelectedSlug(null);
      },
      onError: (err) => addToast('error', `Delete failed: ${String(err)}`),
    });
  }

  function handleRegenerate(slug: string) {
    regeneratePage.mutate(slug, {
      onSuccess: (result) => {
        addToast('success', `Article regenerated (${result.word_count} words from ${result.entries_used} entries).`);
      },
      onError: (err) => addToast('error', `Regenerate failed: ${String(err)}`),
    });
  }

  function handleCreatePage() {
    if (!newSlug.trim()) {
      addToast('error', 'Slug is required.');
      return;
    }
    updatePage.mutate(
      { slug: newSlug.trim(), content: newContent, title: newTitle || undefined },
      {
        onSuccess: () => {
          addToast('success', `Page "${newSlug.trim()}" created.`);
          setShowNewPage(false);
          setNewSlug('');
          setNewTitle('');
          setNewContent('');
        },
        onError: (err) => addToast('error', `Create failed: ${String(err)}`),
      },
    );
  }

  if (isLoading) {
    return <p className="p-5 text-[var(--text-tertiary)] text-sm">Loading pages...</p>;
  }

  const pages = data?.pages || [];

  return (
    <div className="p-5">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-4">
        <span className="text-sm text-[var(--text-tertiary)]">
          {pages.length} {pages.length === 1 ? 'page' : 'pages'}
        </span>
        <button
          onClick={() => setShowNewPage(true)}
          className="px-4 py-2 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors"
        >
          + New Page
        </button>
      </div>

      {/* New page form */}
      {showNewPage && (
        <div className="mb-4 px-4 py-3 rounded-lg border border-[var(--brand)] bg-[var(--bg-primary)]">
          <div className="flex gap-3 mb-2">
            <input
              value={newSlug}
              onChange={(e) => setNewSlug(e.target.value)}
              placeholder="page-slug"
              className="flex-1 px-3 py-1.5 text-sm bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)] placeholder:text-[var(--text-tertiary)]"
              autoFocus
            />
            <input
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="Page Title (optional)"
              className="flex-1 px-3 py-1.5 text-sm bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)] placeholder:text-[var(--text-tertiary)]"
            />
          </div>
          <textarea
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder="Page content (markdown)..."
            className="w-full min-h-[120px] px-3 py-2 text-sm font-mono bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)] resize-y placeholder:text-[var(--text-tertiary)]"
          />
          <div className="flex justify-end gap-3 mt-2">
            <button
              onClick={() => { setShowNewPage(false); setNewSlug(''); setNewTitle(''); setNewContent(''); }}
              className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleCreatePage}
              disabled={updatePage.isPending}
              className="px-4 py-1.5 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
            >
              {updatePage.isPending ? 'Creating...' : 'Create'}
            </button>
          </div>
        </div>
      )}

      {/* Page list */}
      {!pages.length ? (
        <p className="text-[var(--text-tertiary)] text-sm py-8 text-center">
          No wiki pages yet. Create one to start documenting project knowledge.
        </p>
      ) : (
        <div className="space-y-2">
          {pages.map((page: WikiPage) => {
            const isSelected = selectedSlug === page.slug;
            const isEditing = editingSlug === page.slug;

            return (
              <div
                key={page.slug}
                className="rounded-lg border border-[var(--border)] bg-[var(--bg-elevated)] overflow-hidden"
              >
                {/* Card header */}
                <button
                  onClick={() => setSelectedSlug(isSelected ? null : page.slug)}
                  className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-[var(--surface-hover)] transition-colors"
                >
                  <span className="shrink-0 text-base" title={page.auto_generated ? 'Auto-generated' : 'User-written'}>
                    {page.auto_generated ? '\uD83D\uDCA1' : '\uD83D\uDCC4'}
                  </span>
                  <div className="flex-1 min-w-0">
                    <span className="text-sm font-medium text-[var(--text-primary)]">
                      {page.title || page.slug}
                    </span>
                    {page.title && (
                      <span className="ml-2 text-xs text-[var(--text-tertiary)] font-mono">
                        {page.slug}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 shrink-0 text-xs text-[var(--text-tertiary)]">
                    <span>{page.word_count} words</span>
                    <span>{page.entry_count} entries</span>
                    <span className="text-[10px]">{isSelected ? '\u25B2' : '\u25BC'}</span>
                  </div>
                </button>

                {/* Expanded detail */}
                {isSelected && (
                  <div className="border-t border-[var(--border)] px-4 py-3">
                    {page.auto_generated && !isEditing && (
                      <div className="mb-3 px-3 py-2 rounded-lg bg-[rgba(249,115,22,0.08)] border border-[rgba(249,115,22,0.2)] text-xs text-[#f97316]">
                        Auto-maintained. Manual edits may be overwritten during compilation.
                      </div>
                    )}

                    {isEditing ? (
                      <div>
                        <textarea
                          value={editDraft}
                          onChange={(e) => setEditDraft(e.target.value)}
                          className="w-full min-h-[300px] px-3 py-3 text-[14px] font-mono bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)] resize-y"
                          autoFocus
                        />
                        <div className="flex justify-end gap-3 mt-3">
                          <button
                            onClick={() => { setEditingSlug(null); setEditDraft(''); }}
                            className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => handleSaveEdit(page.slug)}
                            disabled={updatePage.isPending}
                            className="px-5 py-2.5 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
                          >
                            {updatePage.isPending ? 'Saving...' : 'Save'}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <div className="prose prose-sm dark:prose-invert max-w-none text-[var(--text-secondary)]">
                          <ReactMarkdown>{pageDetail?.content ?? page.content ?? ''}</ReactMarkdown>
                        </div>

                        {/* Backlinks */}
                        {pageDetail?.backlinks && pageDetail.backlinks.length > 0 && (
                          <div className="mt-4 pt-3 border-t border-[var(--border)]">
                            <span className="text-xs font-medium text-[var(--text-tertiary)]">Backlinks:</span>
                            <div className="flex flex-wrap gap-2 mt-1">
                              {pageDetail.backlinks.map((bl, idx) => (
                                <span
                                  key={`${bl.source_type}-${bl.source_id}-${idx}`}
                                  className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border border-[var(--border)] bg-[var(--bg-primary)] text-[var(--text-secondary)]"
                                >
                                  <span className="text-[var(--text-tertiary)]">[{bl.source_type}]</span>
                                  <span className="font-mono">{bl.source_id.length > 20 ? bl.source_id.slice(0, 20) + '..' : bl.source_id}</span>
                                  <span className="text-[var(--text-tertiary)]">({bl.link_type})</span>
                                </span>
                              ))}
                            </div>
                          </div>
                        )}

                        {/* Actions */}
                        <div className="flex items-center gap-3 mt-4 pt-3 border-t border-[var(--border)]">
                          <button
                            onClick={() => { setEditingSlug(page.slug); setEditDraft(pageDetail?.content ?? page.content ?? ''); }}
                            className="text-xs text-[var(--brand)] hover:underline"
                          >
                            Edit
                          </button>
                          {page.auto_generated && (
                            <button
                              onClick={() => handleRegenerate(page.slug)}
                              disabled={regeneratePage.isPending}
                              className="text-xs text-[var(--brand)] hover:underline disabled:opacity-50"
                            >
                              {regeneratePage.isPending ? 'Regenerating...' : 'Regenerate'}
                            </button>
                          )}
                          <button
                            onClick={() => handleDelete(page.slug)}
                            disabled={deletePage.isPending}
                            className="text-xs text-red-400 hover:underline disabled:opacity-50"
                          >
                            Delete
                          </button>
                          <span className="text-xs text-[var(--text-tertiary)] ml-auto">
                            Updated <RelativeDate iso={page.updated_at} />
                          </span>
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------
// History Tab
// ------------------------------------------------------------------
function HistoryTab({ projectId }: { projectId: string }) {
  const { data, isLoading } = useCompilations(projectId);

  if (isLoading) {
    return <p className="p-5 text-[var(--text-tertiary)] text-sm">Loading history...</p>;
  }

  const compilations = data?.compilations || [];

  if (!compilations.length) {
    return (
      <p className="p-5 text-[var(--text-tertiary)] text-sm text-center py-8">
        No compilations yet. Compile pending entries to create the first compilation.
      </p>
    );
  }

  return (
    <div className="p-5 space-y-3">
      {compilations.map((c) => (
        <div
          key={c.id}
          className="px-4 py-3 rounded-lg border border-[var(--border)] bg-[var(--bg-elevated)]"
        >
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-[var(--text-primary)]">
              {c.entries_compiled} {c.entries_compiled === 1 ? 'entry' : 'entries'} compiled
            </span>
            <span className="text-xs text-[var(--text-tertiary)]">
              <RelativeDate iso={c.compiled_at} />
            </span>
          </div>
          {c.context_after && c.context_before && (
            <p className="text-xs text-[var(--text-tertiary)] mt-1">
              Context: {c.context_before.length} chars &rarr; {c.context_after.length} chars
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------
// Main component
// ------------------------------------------------------------------
export default function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const remote = decodeURIComponent(id || '');
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { data: project, isLoading, error } = useProject(remote);
  const updateContext = useUpdateProjectContext();
  const deleteProject = useDeleteProject();
  const { addToast } = useToast();

  const [activeTab, setActiveTab] = useState<ProjectTab>('context');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showMoreMenu, setShowMoreMenu] = useState(false);
  const [autoNarrative, setAutoNarrative] = useState(project?.auto_narrative ?? false);
  const updateSettings = useUpdateProjectSettings(project?.id);

  // Sync auto_narrative state when project data loads
  useEffect(() => {
    if (project) {
      setAutoNarrative(project.auto_narrative ?? false);
    }
  }, [project]);

  // Enter edit mode if ?edit=1
  useEffect(() => {
    if (searchParams.get('edit') === '1' && project) {
      setDraft(project.context_document || '');
      setEditing(true);
    }
  }, [searchParams, project]);

  function handleEdit() {
    setDraft(project?.context_document || '');
    setEditing(true);
  }

  function handleCancel() {
    setDraft('');
    setEditing(false);
  }

  function handleSave() {
    updateContext.mutate(
      { remote, doc: draft },
      {
        onSuccess: () => {
          addToast('success', 'Context document saved');
          setEditing(false);
        },
        onError: (err) => {
          addToast('error', `Failed to save: ${String(err)}`);
        },
      },
    );
  }

  function handleDelete() {
    if (!project) return;
    deleteProject.mutate(project.id, {
      onSuccess: () => {
        addToast('success', 'Project deleted');
        navigate('/projects');
      },
      onError: (err) => {
        addToast('error', `Failed to delete: ${String(err)}`);
      },
    });
  }

  if (isLoading) {
    return <div className="p-8 text-[var(--text-tertiary)]">Loading project...</div>;
  }

  if (error || !project) {
    return (
      <div className="p-8">
        <button
          onClick={() => navigate('/projects')}
          className="text-[var(--brand)] text-sm hover:underline inline-flex items-center gap-1"
        >
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="15 18 9 12 15 6" />
          </svg>
          Back to Projects
        </button>
        <p className="text-red-400 mt-4">Failed to load project: {String(error)}</p>
      </div>
    );
  }

  const tabs: { key: ProjectTab; label: string }[] = [
    { key: 'context', label: 'Context' },
    { key: 'pages', label: 'Pages' },
    { key: 'entries', label: 'Knowledge Entries' },
    { key: 'history', label: 'History' },
  ];

  return (
    <div className="max-w-4xl mx-auto px-4 py-6">
      {/* Back link */}
      <button
        onClick={() => navigate('/projects')}
        className="text-[var(--brand)] text-sm hover:underline inline-flex items-center gap-1 mb-4"
      >
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="15 18 9 12 15 6" />
        </svg>
        Back to Projects
      </button>

      {/* Header card */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-sm)]">
        <div className="px-5 pt-4 flex items-start justify-between">
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl font-semibold text-[var(--text-primary)] break-all">
              {project.git_remote_normalized}
            </h1>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[13px] text-[var(--text-tertiary)]">
              <span>
                Created <RelativeDate iso={project.created_at} />
              </span>
              <span>&middot;</span>
              <span>
                Updated <RelativeDate iso={project.updated_at} />
              </span>
            </div>
            <label className="flex items-center gap-2 text-[13px] text-[var(--text-tertiary)] mt-1">
              <input
                type="checkbox"
                checked={autoNarrative}
                onChange={(e) => {
                  const val = e.target.checked;
                  setAutoNarrative(val);
                  updateSettings.mutate(
                    { auto_narrative: val },
                    {
                      onSuccess: () => addToast('success', `Auto-narrative ${val ? 'enabled' : 'disabled'}.`),
                      onError: (err) => { setAutoNarrative(!val); addToast('error', `Failed: ${String(err)}`); },
                    },
                  );
                }}
                className="accent-[var(--brand)]"
              />
              Auto-narrative on sync
            </label>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {!editing && activeTab === 'context' && (
              <button
                onClick={handleEdit}
                className="px-5 py-2.5 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors"
              >
                Edit
              </button>
            )}
            <div className="relative">
              <button
                onClick={() => setShowMoreMenu(!showMoreMenu)}
                className="p-1.5 text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] rounded-lg transition-colors"
              >
                <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
                  <circle cx="12" cy="5" r="2" />
                  <circle cx="12" cy="12" r="2" />
                  <circle cx="12" cy="19" r="2" />
                </svg>
              </button>
              {showMoreMenu && (
                <>
                  <div className="fixed inset-0 z-30" onClick={() => setShowMoreMenu(false)} />
                  <div className="absolute right-0 top-10 z-40 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-lg shadow-[var(--shadow-md)] py-1 min-w-[160px]">
                    <button
                      onClick={() => {
                        setShowMoreMenu(false);
                        setShowDeleteConfirm(true);
                      }}
                      className="w-full text-left px-3 py-2 text-sm text-red-400 hover:bg-[var(--surface-hover)] transition-colors"
                    >
                      Delete Project
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex px-5 mt-2 border-t border-[var(--border)]">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`px-4 py-3 text-[14px] font-medium transition-colors relative ${
                activeTab === tab.key
                  ? 'text-[var(--brand)]'
                  : 'text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]'
              }`}
            >
              {tab.label}
              {activeTab === tab.key && (
                <span className="absolute bottom-0 left-4 right-4 h-0.5 bg-[var(--brand)] rounded-full" />
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      {activeTab === 'context' && (
        <div className="mt-4 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-sm)]">
          <div className="px-5 py-4">
            {editing ? (
              <div>
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  className="w-full min-h-[400px] px-3 py-3 text-[14px] font-mono bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)] resize-y placeholder:text-[var(--text-tertiary)]"
                  placeholder="Write your project context document here...&#10;&#10;This will be shared with all sessions in this project."
                  autoFocus
                />
                <div className="flex justify-end gap-3 mt-3">
                  <button
                    onClick={handleCancel}
                    className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleSave}
                    disabled={updateContext.isPending}
                    className="px-5 py-2.5 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {updateContext.isPending ? 'Saving...' : 'Save'}
                  </button>
                </div>
              </div>
            ) : project.context_document ? (
              <div className="prose prose-sm dark:prose-invert max-w-none text-[var(--text-secondary)]">
                <ReactMarkdown>{project.context_document}</ReactMarkdown>
              </div>
            ) : (
              <div className="text-center py-8">
                <p className="text-[var(--text-tertiary)] text-sm mb-3">
                  No context document yet.
                </p>
                <button
                  onClick={handleEdit}
                  className="text-sm text-[var(--brand)] hover:underline"
                >
                  Add context document
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {activeTab === 'pages' && (
        <div className="mt-4 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-sm)]">
          <PagesTab projectId={project.id} />
        </div>
      )}

      {activeTab === 'entries' && (
        <div className="mt-4 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-sm)]">
          <KnowledgeEntriesTab projectId={project.id} />
        </div>
      )}

      {activeTab === 'history' && (
        <div className="mt-4 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-sm)]">
          <HistoryTab projectId={project.id} />
        </div>
      )}

      {/* Delete confirmation modal */}
      {showDeleteConfirm && (
        <>
          <div className="fixed inset-0 z-50 bg-black/50" onClick={() => setShowDeleteConfirm(false)} onKeyDown={(e) => { if (e.key === 'Escape') setShowDeleteConfirm(false); }} />
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pointer-events-none">
            <div
              role="dialog"
              aria-modal="true"
              aria-labelledby="delete-project-title"
              onKeyDown={(e) => { if (e.key === 'Escape') setShowDeleteConfirm(false); }}
              className="pointer-events-auto w-full max-w-sm bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-lg)] p-6"
            >
              <h3 id="delete-project-title" className="text-base font-semibold text-[var(--text-primary)] mb-2">
                Delete project?
              </h3>
              <p className="text-sm text-[var(--text-tertiary)] mb-5">
                This will permanently delete the project context for{' '}
                <span className="font-medium text-[var(--text-secondary)]">{project.git_remote_normalized}</span>.
                This action cannot be undone.
              </p>
              <div className="flex justify-end gap-3">
                <button
                  onClick={() => setShowDeleteConfirm(false)}
                  className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleDelete}
                  disabled={deleteProject.isPending}
                  className="px-4 py-1.5 text-sm bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors font-medium disabled:opacity-50"
                >
                  {deleteProject.isPending ? 'Deleting...' : 'Delete'}
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
