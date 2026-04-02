import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useProjects } from '../hooks/useProjects';
import RelativeDate from '../components/RelativeDate';
import CreateProjectModal from './CreateProjectModal';

export default function ProjectsPage() {
  const navigate = useNavigate();
  const { data: projects, isLoading, error } = useProjects();
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div className="max-w-4xl mx-auto px-4 py-6">
      <div className="flex items-center justify-between mb-5">
        <h1 className="text-lg font-semibold text-[var(--text-primary)]">Projects</h1>
        <button
          onClick={() => setShowCreate(true)}
          className="px-3 py-1.5 text-sm bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors font-medium"
        >
          + New Project
        </button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm">
          Failed to load projects: {String(error)}
        </div>
      )}

      {isLoading && (
        <div className="text-center py-12 text-[var(--text-tertiary)]">Loading projects...</div>
      )}

      {!isLoading && projects && projects.length > 0 && (
        <div className="space-y-3">
          {projects.map((p) => (
            <div
              key={p.id}
              onClick={() => navigate(`/projects/${encodeURIComponent(p.git_remote_normalized)}`)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') navigate(`/projects/${encodeURIComponent(p.git_remote_normalized)}`);
              }}
              tabIndex={0}
              className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 cursor-pointer hover:shadow-[var(--shadow-md)] transition-all focus:border-[var(--brand)] outline-none"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-semibold text-[var(--text-primary)] truncate">
                    {p.git_remote_normalized}
                  </div>
                  {p.context_document && (
                    <div className="mt-1 text-sm text-[var(--text-tertiary)] truncate">
                      {p.context_document.slice(0, 80)}
                      {p.context_document.length > 80 ? '...' : ''}
                    </div>
                  )}
                </div>
                <span className="text-xs text-[var(--text-tertiary)] shrink-0">
                  <RelativeDate iso={p.updated_at} />
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {!isLoading && projects && projects.length === 0 && !error && (
        <div className="text-center py-16">
          <svg
            width="48"
            height="48"
            viewBox="0 0 24 24"
            fill="none"
            stroke="var(--text-tertiary)"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="mx-auto mb-4 opacity-40"
          >
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
          </svg>
          <p className="text-[var(--text-primary)] font-medium mb-1">No project contexts yet</p>
          <p className="text-[var(--text-tertiary)] text-sm mb-4">
            Create a project context to share instructions across sessions.
          </p>
          <button
            onClick={() => setShowCreate(true)}
            className="text-sm text-[var(--brand)] hover:underline"
          >
            Create your first project
          </button>
          <p className="text-[var(--text-tertiary)] text-xs mt-3">
            Or use the CLI: <code className="font-mono bg-[var(--bg-tertiary)] px-1.5 py-0.5 rounded">sfs project set &lt;git-remote&gt;</code>
          </p>
        </div>
      )}

      {showCreate && (
        <CreateProjectModal
          onClose={() => setShowCreate(false)}
          onCreated={(remote) => {
            setShowCreate(false);
            navigate(`/projects/${encodeURIComponent(remote)}?edit=1`);
          }}
        />
      )}
    </div>
  );
}
