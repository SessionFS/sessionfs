import { useState, useEffect, useCallback, type ReactNode } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import {
  useProject,
  useUpdateProjectContext,
  useDeleteProject,
  useKnowledgeEntries,
  useCompileProject,
} from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import RelativeDate from '../components/RelativeDate';
import RulesTab from './RulesTab';
import PersonasTab from './PersonasTab';
import TicketsTab from './TicketsTab';
import AgentRunsTab from './AgentRunsTab';
import MarketplaceTab from './MarketplaceTab';
import ProjectSettings from './ProjectSettings';
import ProjectHeader from './ProjectHeader';
import ContextDocument from './ContextDocument';
import KnowledgeEntriesTab from './knowledge/EntriesTab';
import PagesTab from './knowledge/PagesTab';
import HistoryTab from './knowledge/HistoryTab';
import { Card, Button, Textarea, Dialog, DialogHeader, DialogFooter, Tabs, TabPanel } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';
import { ApiError } from '../api/client';

// The page has five sections. The active one lives in the URL as
// `?tab=<section>`, and the active sub-tab of the sections that have them as
// `&sub=<subtab>`, so a refresh or a shared link lands on the same view.
// Two params rather than one `tab=knowledge/entries` value: the section is
// what the header tabs read, the sub-tab only matters inside its section, and
// each can fall back to its default independently when a value is unknown.
type Section = 'context' | 'knowledge' | 'work' | 'config' | 'settings';

const SECTIONS: { key: Section; label: string }[] = [
  { key: 'context', label: 'Overview' },
  { key: 'knowledge', label: 'Knowledge' },
  { key: 'work', label: 'Work' },
  { key: 'config', label: 'Rules & Skills' },
  { key: 'settings', label: 'Settings' },
];

const SUB_TABS: Partial<Record<Section, { key: string; label: string }[]>> = {
  knowledge: [
    { key: 'entries', label: 'Entries' },
    { key: 'pages', label: 'Pages' },
    { key: 'history', label: 'History' },
  ],
  work: [
    { key: 'tickets', label: 'Tickets' },
    { key: 'agent-runs', label: 'Agent runs' },
    { key: 'personas', label: 'Personas' },
  ],
  config: [
    { key: 'rules', label: 'Rules' },
    // Not "Marketplace": that is the org-wide catalog in the sidebar; this
    // sub-tab is the project's own skill installs and requests (GitHub #87).
    { key: 'marketplace', label: 'Skills' },
  ],
};

// Before the restructure every tab key was top-level; old links still work.
const LEGACY_TABS: Record<string, [Section, string?]> = {
  entries: ['knowledge', 'entries'],
  pages: ['knowledge', 'pages'],
  history: ['knowledge', 'history'],
  tickets: ['work', 'tickets'],
  'agent-runs': ['work', 'agent-runs'],
  personas: ['work', 'personas'],
  rules: ['config', 'rules'],
  marketplace: ['config', 'marketplace'],
  repos: ['settings'],
  merge: ['settings'],
  transfer: ['settings'],
  context: ['context'],
};

/** Target project id from a 410 project_merged response, if that is the error. */
function mergedIntoId(error: unknown): string | undefined {
  if (!(error instanceof ApiError) || error.status !== 410) return undefined;
  const target = error.details?.merged_into;
  if (typeof target === 'string') return target;
  try {
    const json = JSON.parse(error.raw);
    const legacy = json?.detail?.merged_into;
    return typeof legacy === 'string' ? legacy : undefined;
  } catch {
    return undefined;
  }
}

function resolveView(tabParam: string | null, subParam: string | null): { section: Section; sub: string | undefined } {
  let section: Section = 'context';
  let legacySub: string | undefined;
  if (tabParam && SECTIONS.some((s) => s.key === tabParam)) {
    section = tabParam as Section;
  } else if (tabParam && Object.prototype.hasOwnProperty.call(LEGACY_TABS, tabParam)) {
    [section, legacySub] = LEGACY_TABS[tabParam];
  }
  const subs = SUB_TABS[section];
  if (!subs) return { section, sub: undefined };
  const wanted = legacySub ?? subParam;
  const sub = subs.find((t) => t.key === wanted)?.key ?? subs[0].key;
  return { section, sub };
}

// One wrapper for every section body, so panels look the same whichever
// section or sub-tab is showing. `padded` is for bodies that do not pad
// themselves; the knowledge bodies already do.
function Panel({ children, padded = false, sub }: { children: ReactNode; padded?: boolean; sub: string }) {
  return (
    <TabPanel idPrefix="project-sub" tabKey={sub}>
      <Card level="elevated" className={`mt-4 min-w-0 rounded-xl shadow-[var(--shadow-sm)] ${padded ? 'p-4' : ''}`}>
        {children}
      </Card>
    </TabPanel>
  );
}

// ------------------------------------------------------------------
// Main component
// ------------------------------------------------------------------
export default function ProjectDetail() {
  // The URL segment is a project id, or a legacy percent-encoded git remote
  // from before projects were routed by id (old emails, docs, bookmarks).
  const { id } = useParams<{ id: string }>();
  const idOrRemote = decodeURIComponent(id || '');
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { data: project, isLoading, error } = useProject(idOrRemote);

  // Land every project on its id URL, keeping ?tab=&sub= and the rest.
  const search = searchParams.toString();
  const query = search ? `?${search}` : '';
  useEffect(() => {
    if (project && project.id !== idOrRemote) {
      navigate(`/projects/${project.id}${query}`, { replace: true });
    }
  }, [project, idOrRemote, query, navigate]);

  // A merged project answers 410 with the project it moved into.
  const mergedInto = mergedIntoId(error);
  useEffect(() => {
    if (mergedInto && mergedInto !== idOrRemote) {
      navigate(`/projects/${mergedInto}${query}`, { replace: true });
    }
  }, [mergedInto, idOrRemote, query, navigate]);
  const updateContext = useUpdateProjectContext();
  const deleteProject = useDeleteProject();
  const { addToast } = useToast();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  // Fetch pending entries count for workflow hint
  const { data: pendingData } = useKnowledgeEntries(project?.id, { pending: true, limit: 1 });
  const pendingCount = pendingData?.total ?? 0;
  const compile = useCompileProject(project?.id);

  const { section, sub } = resolveView(searchParams.get('tab'), searchParams.get('sub'));

  // A section change is a navigation (Back returns to the previous section);
  // a sub-tab change replaces the entry so browsing sub-tabs does not bury it.
  // Other params, such as ?edit=1, are kept.
  const switchSection = useCallback((next: string) => {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev);
      params.set('tab', next);
      params.delete('sub');
      return params;
    });
  }, [setSearchParams]);

  const switchSub = useCallback((next: string) => {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev);
      params.set('sub', next);
      return params;
    }, { replace: true });
  }, [setSearchParams]);

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
    if (!project) return;
    updateContext.mutate(
      { remote: project.git_remote_normalized, doc: draft },
      {
        onSuccess: () => {
          addToast('success', 'Context document saved');
          setEditing(false);
        },
        onError: (err) => {
          addToast('error', `Save failed: ${errorMessage(err)}`);
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
        addToast('error', `Delete failed: ${errorMessage(err)}`);
      },
    });
  }

  if (isLoading || mergedInto) {
    return <div className="p-8 text-text-tertiary">Loading project…</div>;
  }

  if (error || !project) {
    return (
      <div className="p-8">
        <Button variant="ghost" onClick={() => navigate('/projects')}>
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="15 18 9 12 15 6" />
          </svg>
          Back to Projects
        </Button>
        <p className="text-danger mt-4">Failed to load project: {errorMessage(error)}</p>
      </div>
    );
  }

  function handleCompileFromContext() {
    compile.mutate(undefined, {
      onSuccess: (result) => {
        const n = result.entries_compiled;
        if (n === 0) {
          addToast('info', 'No pending entries to compile.');
        } else {
          addToast('success', `Project context updated. ${n} entries compiled.`);
        }
      },
      onError: (err) => addToast('error', `Compile failed: ${errorMessage(err)}`),
    });
  }

  const subTabs = SUB_TABS[section];

  return (
    <div className="max-w-6xl w-full mx-auto px-4 py-6">
      {/* Back link */}
      <Button variant="ghost" onClick={() => navigate('/projects')} className="mb-4">
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="15 18 9 12 15 6" />
        </svg>
        Back to Projects
      </Button>

      <ProjectHeader
        project={project}
        activeTab={section}
        badgeTab="knowledge"
        tabs={SECTIONS}
        onTabChange={switchSection}
        pendingCount={pendingCount}
        compilePending={compile.isPending}
        onCompile={handleCompileFromContext}
      />

      <TabPanel idPrefix="project-section" tabKey={section}>
      {subTabs && (
        <div className="mt-4">
          <Tabs
            bare
            idPrefix="project-sub"
            aria-label="Section views"
            activeKey={sub ?? subTabs[0].key}
            onChange={switchSub}
            tabs={subTabs.map((t) => ({
              key: t.key,
              label: (
                <span className="inline-flex items-center gap-1.5">
                  {t.label}
                  {section === 'knowledge' && t.key === 'entries' && pendingCount > 0 && (
                    <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full text-2xs font-semibold bg-brand text-white leading-none">
                      {pendingCount > 99 ? '99+' : pendingCount}
                    </span>
                  )}
                </span>
              ),
            }))}
          />
        </div>
      )}

      {section === 'context' && (
        <div className="mt-4">
          {/* Context hero header card */}
          {!editing && (
            <Card level="elevated" className="mb-3 rounded-xl shadow-[var(--shadow-sm)] px-5 py-4">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-base font-semibold text-text-primary">Compiled Context</h2>
                  <div className="flex items-center gap-3 mt-1 text-xs text-text-tertiary">
                    {project.context_document ? (
                      <>
                        <span>{project.context_document.trim().split(/\s+/).length.toLocaleString()} words</span>
                        <span>&middot;</span>
                        <span>Last updated <RelativeDate iso={project.updated_at} /></span>
                      </>
                    ) : (
                      <span>No context compiled yet</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Button variant="secondary" onClick={handleEdit}>Edit</Button>
                </div>
              </div>
            </Card>
          )}

          {/* Context document body */}
          <Card level="elevated" className="rounded-xl shadow-[var(--shadow-sm)]">
            <div className="px-4 py-5 sm:px-8 sm:py-7">
              {editing ? (
                <div>
                  <Textarea
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    className="min-h-[400px] font-mono text-base"
                    placeholder={"Write your project context document here…\n\nThis will be shared with all sessions in this project."}
                    autoFocus
                  />
                  <div className="flex justify-end gap-3 mt-3">
                    <Button variant="ghost" onClick={handleCancel}>Cancel</Button>
                    <Button
                      onClick={handleSave}
                      disabled={updateContext.isPending}
                      loading={updateContext.isPending}
                    >
                      Save
                    </Button>
                  </div>
                </div>
              ) : project.context_document ? (
                <ContextDocument markdown={project.context_document} />
              ) : (
                <div className="text-center py-12">
                  <p className="text-text-tertiary text-sm mb-1">
                    No context document yet.
                  </p>
                  <p className="text-sm text-text-tertiary mb-4">
                    Add knowledge entries, then compile to generate a context document.
                  </p>
                  <Button variant="ghost" onClick={handleEdit}>Write one manually</Button>
                </div>
              )}
            </div>
          </Card>
        </div>
      )}

      {section === 'knowledge' && sub === 'entries' && (
        <Panel sub={sub}><KnowledgeEntriesTab projectId={project.id} /></Panel>
      )}
      {section === 'knowledge' && sub === 'pages' && (
        <Panel sub={sub}><PagesTab projectId={project.id} /></Panel>
      )}
      {section === 'knowledge' && sub === 'history' && (
        <Panel sub={sub}><HistoryTab projectId={project.id} /></Panel>
      )}

      {section === 'work' && sub === 'tickets' && (
        <Panel padded sub={sub}><TicketsTab projectId={project.id} /></Panel>
      )}
      {section === 'work' && sub === 'agent-runs' && (
        <Panel padded sub={sub}><AgentRunsTab projectId={project.id} /></Panel>
      )}
      {section === 'work' && sub === 'personas' && (
        <Panel padded sub={sub}><PersonasTab projectId={project.id} /></Panel>
      )}

      {section === 'config' && sub === 'rules' && (
        <Panel sub={sub}><RulesTab projectId={project.id} /></Panel>
      )}
      {section === 'config' && sub === 'marketplace' && (
        <Panel padded sub={sub}><MarketplaceTab projectId={project.id} /></Panel>
      )}

      {section === 'settings' && (
        <div className="mt-4">
          <ProjectSettings project={project} onRequestDelete={() => setShowDeleteConfirm(true)} />
        </div>
      )}

      </TabPanel>

      {/* Delete confirmation modal */}
      <Dialog open={showDeleteConfirm} onClose={() => setShowDeleteConfirm(false)} titleId="delete-project-title">
        <DialogHeader titleId="delete-project-title">Delete project?</DialogHeader>
        <p className="text-sm text-text-tertiary mb-4">
          This will permanently delete the project context for{' '}
          <span className="font-medium text-text-secondary">{project.git_remote_normalized}</span>.
          This action cannot be undone.
        </p>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setShowDeleteConfirm(false)}>Cancel</Button>
          <Button
            variant="danger"
            onClick={handleDelete}
            disabled={deleteProject.isPending}
            loading={deleteProject.isPending}
          >
            Delete
          </Button>
        </DialogFooter>
      </Dialog>
    </div>
  );
}
