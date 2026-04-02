import { useState, useEffect } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import { useProject, useUpdateProjectContext, useDeleteProject } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import RelativeDate from '../components/RelativeDate';

export default function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const remote = decodeURIComponent(id || '');
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { data: project, isLoading, error } = useProject(remote);
  const updateContext = useUpdateProjectContext();
  const deleteProject = useDeleteProject();
  const { addToast } = useToast();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showMoreMenu, setShowMoreMenu] = useState(false);

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
            <h1 className="text-lg font-semibold text-[var(--text-primary)] break-all">
              {project.git_remote_normalized}
            </h1>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-[var(--text-tertiary)]">
              <span>
                Created <RelativeDate iso={project.created_at} />
              </span>
              <span>&middot;</span>
              <span>
                Updated <RelativeDate iso={project.updated_at} />
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {!editing && (
              <button
                onClick={handleEdit}
                className="px-3 py-1.5 text-sm bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors font-medium"
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

        {/* Content area */}
        <div className="px-5 py-4">
          {editing ? (
            <div>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                className="w-full min-h-[400px] px-3 py-3 text-sm font-mono bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)] resize-y placeholder:text-[var(--text-tertiary)]"
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
                  className="px-4 py-1.5 text-sm bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {updateContext.isPending ? 'Saving...' : 'Save'}
                </button>
              </div>
            </div>
          ) : project.context_document ? (
            <pre
              className="text-sm text-[var(--text-secondary)] leading-relaxed"
              style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
            >
              {project.context_document}
            </pre>
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

      {/* Delete confirmation modal */}
      {showDeleteConfirm && (
        <>
          <div className="fixed inset-0 z-50 bg-black/50" onClick={() => setShowDeleteConfirm(false)} />
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pointer-events-none">
            <div className="pointer-events-auto w-full max-w-sm bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-lg)] p-6">
              <h3 className="text-base font-semibold text-[var(--text-primary)] mb-2">
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
