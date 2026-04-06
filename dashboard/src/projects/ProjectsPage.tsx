import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useProjects } from '../hooks/useProjects';
import type { ProjectContext } from '../api/client';
import RelativeDate from '../components/RelativeDate';
import CreateProjectModal from './CreateProjectModal';

/** Strip leading markdown heading markers and return the first non-empty line. */
function firstContentLine(md: string | undefined | null): string | null {
  if (!md) return null;
  for (const raw of md.split('\n')) {
    const line = raw.replace(/^#+\s*/, '').trim();
    if (line.length > 0) return line.length > 120 ? line.slice(0, 117) + '...' : line;
  }
  return null;
}

function ProjectCard({ project, onClick }: { project: ProjectContext; onClick: () => void }) {
  const sessionCount = project.session_count ?? 0;
  const preview = firstContentLine(project.context_document);
  const displayName = project.name || project.git_remote_normalized;

  return (
    <div
      role="button"
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); }
      }}
      tabIndex={0}
      className="group bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 cursor-pointer hover:shadow-[var(--shadow-md)] hover:border-[var(--brand)]/40 transition-all focus:border-[var(--brand)] outline-none"
    >
      {/* Title row */}
      <div className="flex items-start justify-between gap-3 mb-2">
        <h2 className="text-[17px] font-bold text-[var(--text-primary)] truncate leading-snug group-hover:text-[var(--brand)] transition-colors">
          {displayName}
        </h2>
      </div>

      {/* Subtitle — repo path when name differs */}
      {project.name && project.name !== project.git_remote_normalized && (
        <p className="text-xs text-[var(--text-tertiary)] font-mono truncate -mt-1 mb-2">
          {project.git_remote_normalized}
        </p>
      )}

      {/* Context preview */}
      {preview && (
        <p className="text-sm text-[var(--text-secondary)] leading-relaxed truncate mb-3">
          {preview}
        </p>
      )}

      {/* Badges row */}
      <div className="flex items-center gap-2 flex-wrap">
        {/* Session count badge */}
        <span className="inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full bg-[var(--bg-tertiary)] text-[var(--text-secondary)] tabular-nums">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="opacity-60">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
          {sessionCount} {sessionCount === 1 ? 'session' : 'sessions'}
        </span>

        {/* Auto-narrative badge */}
        {project.auto_narrative && (
          <span className="inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 inline-block" />
            Auto-narrative
          </span>
        )}

        {/* Last updated */}
        <span className="ml-auto text-xs text-[var(--text-tertiary)] tabular-nums shrink-0">
          <RelativeDate iso={project.updated_at} />
        </span>
      </div>
    </div>
  );
}

export default function ProjectsPage() {
  const navigate = useNavigate();
  const { data: projects, isLoading, error } = useProjects();
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div className="max-w-4xl mx-auto px-4 py-6">
      <div className="flex items-center justify-between mb-5">
        <h1 className="text-3xl font-bold tracking-tight text-[var(--text-primary)]">Projects</h1>
        <button
          onClick={() => setShowCreate(true)}
          className="px-5 py-2.5 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors"
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
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 animate-pulse">
              <div className="h-5 bg-[var(--bg-tertiary)] rounded w-1/3 mb-3" />
              <div className="h-4 bg-[var(--bg-tertiary)] rounded w-2/3 mb-3" />
              <div className="flex gap-2">
                <div className="h-5 bg-[var(--bg-tertiary)] rounded-full w-20" />
                <div className="h-5 bg-[var(--bg-tertiary)] rounded-full w-16" />
              </div>
            </div>
          ))}
        </div>
      )}

      {!isLoading && projects && projects.length > 0 && (
        <div className="space-y-3">
          {projects.map((p) => (
            <ProjectCard
              key={p.id}
              project={p}
              onClick={() => navigate(`/projects/${encodeURIComponent(p.git_remote_normalized)}`)}
            />
          ))}
        </div>
      )}

      {!isLoading && projects && projects.length === 0 && !error && (
        <div className="text-center py-20">
          {/* Folder + sparkle icon */}
          <div className="relative inline-block mb-5">
            <svg
              width="56"
              height="56"
              viewBox="0 0 24 24"
              fill="none"
              stroke="var(--text-tertiary)"
              strokeWidth="1.2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="opacity-30"
            >
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
            </svg>
          </div>
          <h2 className="text-xl font-bold text-[var(--text-primary)] mb-2">
            Welcome to Project Context
          </h2>
          <p className="text-[var(--text-secondary)] text-sm max-w-md mx-auto mb-6 leading-relaxed">
            Project contexts let you share instructions, conventions, and knowledge
            across all sessions in a repository. Your AI tools will automatically
            pick up this context.
          </p>
          <button
            onClick={() => setShowCreate(true)}
            className="px-5 py-2.5 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors"
          >
            Create your first project
          </button>
          <p className="text-[var(--text-tertiary)] text-xs mt-4">
            Or from the terminal: <code className="font-mono bg-[var(--bg-tertiary)] px-1.5 py-0.5 rounded text-[var(--text-secondary)]">sfs project set &lt;git-remote&gt;</code>
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
