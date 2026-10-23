import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { useWikiPages, useWikiPage, useUpdateWikiPage, useDeleteWikiPage, useRegenerateWikiPage } from '../../hooks/useProjects';
import { useToast } from '../../hooks/useToast';
import RelativeDate from '../../components/RelativeDate';
import type { WikiPage } from '../../api/client';
import { Button, Input, Textarea } from '../../components/ui';
import { errorMessage } from '../../utils/errorMessage';

export default function PagesTab({ projectId }: { projectId: string }) {
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
        onError: (err) => addToast('error', `Save failed: ${errorMessage(err)}`),
      },
    );
  }

  function handleDelete(slug: string) {
    deletePage.mutate(slug, {
      onSuccess: () => {
        addToast('success', 'Page deleted.');
        if (selectedSlug === slug) setSelectedSlug(null);
      },
      onError: (err) => addToast('error', `Delete failed: ${errorMessage(err)}`),
    });
  }

  function handleRegenerate(slug: string) {
    regeneratePage.mutate(slug, {
      onSuccess: (result) => {
        addToast('success', `Article regenerated (${result.word_count} words from ${result.entries_used} entries).`);
      },
      onError: (err) => addToast('error', `Regenerate failed: ${errorMessage(err)}`),
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
        onError: (err) => addToast('error', `Create failed: ${errorMessage(err)}`),
      },
    );
  }

  if (isLoading) {
    return <p className="p-5 text-text-tertiary text-sm">Loading pages…</p>;
  }

  const pages = data?.pages || [];

  return (
    <div className="p-5">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-4">
        <span className="text-sm text-text-tertiary">
          {pages.length} {pages.length === 1 ? 'page' : 'pages'}
        </span>
        <Button onClick={() => setShowNewPage(true)}>+ New Page</Button>
      </div>

      {/* New page form */}
      {showNewPage && (
        <div className="mb-4 px-4 py-3 rounded-lg border border-[var(--brand)] bg-bg-primary">
          <div className="flex gap-3 mb-2">
            <Input
              value={newSlug}
              onChange={(e) => setNewSlug(e.target.value)}
              placeholder="page-slug"
              className="flex-1"
              autoFocus
            />
            <Input
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="Page Title (optional)"
              className="flex-1"
            />
          </div>
          <Textarea
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder="Page content (markdown)…"
            className="min-h-[120px] font-mono"
          />
          <div className="flex justify-end gap-3 mt-2">
            <Button variant="ghost" onClick={() => { setShowNewPage(false); setNewSlug(''); setNewTitle(''); setNewContent(''); }}>
              Cancel
            </Button>
            <Button onClick={handleCreatePage} disabled={updatePage.isPending} loading={updatePage.isPending}>
              Create
            </Button>
          </div>
        </div>
      )}

      {/* Page list */}
      {!pages.length ? (
        <p className="text-text-tertiary text-sm py-8 text-center">
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
                className="rounded-lg border border-border bg-bg-elevated overflow-hidden"
              >
                {/* Card header */}
                <button
                  onClick={() => setSelectedSlug(isSelected ? null : page.slug)}
                  className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-surface-hover transition-colors"
                >
                  <span className="shrink-0 text-base" title={page.auto_generated ? 'Auto-generated' : 'User-written'}>
                    {page.auto_generated ? '\uD83D\uDCA1' : '\uD83D\uDCC4'}
                  </span>
                  <div className="flex-1 min-w-0">
                    <span className="text-sm font-medium text-text-primary">
                      {page.title || page.slug}
                    </span>
                    {page.title && (
                      <span className="ml-2 text-xs text-text-tertiary font-mono">
                        {page.slug}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 shrink-0 text-xs text-text-tertiary">
                    <span>{page.word_count} words</span>
                    <span>{page.entry_count} entries</span>
                    <span className="text-2xs">{isSelected ? '\u25B2' : '\u25BC'}</span>
                  </div>
                </button>

                {/* Expanded detail */}
                {isSelected && (
                  <div className="border-t border-border px-4 py-3">
                    {page.auto_generated && !isEditing && (
                      <div className="mb-3 px-3 py-2 rounded-lg bg-[rgba(249,115,22,0.08)] border border-[rgba(249,115,22,0.2)] text-xs text-[#f97316]">
                        Auto-maintained. Manual edits may be overwritten during compilation.
                      </div>
                    )}

                    {isEditing ? (
                      <div>
                        <Textarea
                          value={editDraft}
                          onChange={(e) => setEditDraft(e.target.value)}
                          className="min-h-[300px] font-mono text-base"
                          autoFocus
                        />
                        <div className="flex justify-end gap-3 mt-3">
                          <Button variant="ghost" onClick={() => { setEditingSlug(null); setEditDraft(''); }}>
                            Cancel
                          </Button>
                          <Button onClick={() => handleSaveEdit(page.slug)} disabled={updatePage.isPending} loading={updatePage.isPending}>
                            Save
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <div className="prose prose-sm dark:prose-invert max-w-[65ch] text-text-secondary">
                          <ReactMarkdown>{pageDetail?.content ?? page.content ?? ''}</ReactMarkdown>
                        </div>

                        {/* Backlinks */}
                        {pageDetail?.backlinks && pageDetail.backlinks.length > 0 && (
                          <div className="mt-4 pt-3 border-t border-border">
                            <span className="text-xs font-medium text-text-tertiary">Backlinks:</span>
                            <div className="flex flex-wrap gap-2 mt-1">
                              {pageDetail.backlinks.map((bl, idx) => (
                                <span
                                  key={`${bl.source_type}-${bl.source_id}-${idx}`}
                                  className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border border-border bg-bg-primary text-text-secondary"
                                >
                                  <span className="text-text-tertiary">[{bl.source_type}]</span>
                                  <span className="font-mono">{bl.source_id.length > 20 ? bl.source_id.slice(0, 20) + '..' : bl.source_id}</span>
                                  <span className="text-text-tertiary">({bl.link_type})</span>
                                </span>
                              ))}
                            </div>
                          </div>
                        )}

                        {/* Actions */}
                        <div className="flex items-center gap-3 mt-4 pt-3 border-t border-border">
                          <button
                            onClick={() => { setEditingSlug(page.slug); setEditDraft(pageDetail?.content ?? page.content ?? ''); }}
                            className="text-xs text-brand hover:underline"
                          >
                            Edit
                          </button>
                          {page.auto_generated && (
                            <button
                              onClick={() => handleRegenerate(page.slug)}
                              disabled={regeneratePage.isPending}
                              className="text-xs text-brand hover:underline disabled:opacity-50"
                            >
                              {regeneratePage.isPending ? 'Regenerating…' : 'Regenerate'}
                            </button>
                          )}
                          <button
                            onClick={() => handleDelete(page.slug)}
                            disabled={deletePage.isPending}
                            className="text-xs text-danger hover:underline disabled:opacity-50"
                          >
                            Delete
                          </button>
                          <span className="text-xs text-text-tertiary ml-auto">
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
