import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useProjects } from '../hooks/useProjects';
import type { ProjectContext } from '../api/client';
import RelativeDate from '../components/RelativeDate';
import CreateProjectModal from './CreateProjectModal';
import { Card, Button } from '../components/ui';

// v0.10.0 added `projects.org_id` (migration 035). The generated
// ProjectContext type predates that; responses carry an extra org_id.
type ProjectContextWithOrg = ProjectContext & { org_id?: string | null };

/** Strip leading markdown heading markers and return the first non-empty line. */
function firstContentLine(md: string | undefined | null): string | null {
  if (!md) return null;
  for (const raw of md.split('\n')) {
    const line = raw.replace(/^#+\s*/, '').trim();
    if (line.length > 0) return line.length > 120 ? line.slice(0, 117) + '…' : line;
  }
  return null;
}

function ProjectCard({ project, onClick }: { project: ProjectContext; onClick: () => void }) {
  const sessionCount = project.session_count ?? 0;
  const preview = firstContentLine(project.context_document);
  const displayName = project.name || project.git_remote_normalized;
  const p = project as ProjectContextWithOrg;

  return (
    <Card
      level="elevated"
      topEdge={p.org_id ? 'var(--brand)' : undefined}
      role="button"
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); }
      }}
      tabIndex={0}
      className="group p-5 cursor-pointer hover:shadow-[var(--shadow-md)] hover:border-[var(--brand)]/40 transition-[box-shadow,border-color] duration-150 focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] outline-none rounded-xl"
    >
      {/* Title row */}
      <div className="flex items-start justify-between gap-3 mb-2">
        <h2 className="text-lg font-bold text-text-primary truncate leading-snug group-hover:text-brand transition-colors">
          {displayName}
        </h2>
      </div>

      {/* Subtitle — repo path when name differs */}
      {project.name && project.name !== project.git_remote_normalized && (
        <p className="text-xs text-text-tertiary font-mono truncate -mt-1 mb-2">
          {project.git_remote_normalized}
        </p>
      )}

      {/* Context preview */}
      {preview && (
        <p className="text-sm text-text-secondary leading-relaxed truncate mb-3">
          {preview}
        </p>
      )}

      {/* Badges row */}
      <div className="flex items-center gap-2 flex-wrap">
        {/* Session count badge */}
        <span className="inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full bg-bg-tertiary text-text-secondary tabular-nums">
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
        <span className="ml-auto text-xs text-text-tertiary tabular-nums shrink-0">
          <RelativeDate iso={project.updated_at} />
        </span>
      </div>
    </Card>
  );
}

export default function ProjectsPage() {
  const navigate = useNavigate();
  const { data: projects, isLoading, error } = useProjects();
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div className="max-w-4xl mx-auto px-4 py-6">
      <div className="flex items-center justify-between mb-5">
        <h1 className="text-3xl font-bold tracking-tight text-text-primary">Projects</h1>
        <Button onClick={() => setShowCreate(true)}>+ New Project</Button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm">
          Failed to load projects: {String(error)}
        </div>
      )}

      {isLoading && (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="bg-bg-elevated border border-border rounded-xl p-5 animate-pulse">
              <div className="h-5 bg-bg-tertiary rounded w-1/3 mb-3" />
              <div className="h-4 bg-bg-tertiary rounded w-2/3 mb-3" />
              <div className="flex gap-2">
                <div className="h-5 bg-bg-tertiary rounded-full w-20" />
                <div className="h-5 bg-bg-tertiary rounded-full w-16" />
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
        <div className="flex items-center justify-center py-10">
          <Card
            level="elevated"
            className="max-w-[560px] w-full overflow-hidden"
          >
            {/* Dot-grid banner */}
            <div
              className="relative flex items-center justify-center py-10"
              style={{
                backgroundImage: 'var(--dot-grid)',
                backgroundSize: '16px 16px',
              }}
            >
              {/* Project folder illustration — folder with plus document */}
              <svg
                width="88"
                height="72"
                viewBox="0 0 88 72"
                fill="none"
                xmlns="http://www.w3.org/2000/svg"
                role="img"
                aria-label="Project folder illustration"
              >
                {/* Folder shape */}
                <path
                  d="M8 12a2 2 0 0 1 2-2h22l4 6h36a2 2 0 0 1 2 2v36a2 2 0 0 1-2 2H10a2 2 0 0 1-2-2V12z"
                  stroke="var(--text-secondary)" strokeWidth="1.5"
                />
                {/* Plus document */}
                <rect
                  x="28" y="24" width="32" height="26" rx="4"
                  stroke="var(--brand)" strokeWidth="1.5"
                />
                <line
                  x1="44" y1="32" x2="44" y2="42"
                  stroke="var(--brand)" strokeWidth="1.5" strokeLinecap="round"
                />
                <line
                  x1="39" y1="37" x2="49" y2="37"
                  stroke="var(--brand)" strokeWidth="1.5" strokeLinecap="round"
                />
              </svg>
            </div>
            {/* Content area */}
            <div className="text-center px-6 pb-7 pt-5">
              <h2 className="text-title text-text-primary mb-1.5">
                No projects yet
              </h2>
              <p className="text-caption max-w-sm mx-auto mb-5 leading-relaxed">
                Project contexts let you share instructions, conventions, and knowledge
                across all sessions in a repository.
              </p>
              <Button onClick={() => setShowCreate(true)}>Create your first project</Button>
              <p className="text-text-tertiary text-xs mt-4">
                Or from the terminal: <code className="font-mono bg-bg-tertiary px-1.5 py-0.5 rounded text-text-secondary">sfs project set &lt;git-remote&gt;</code>
              </p>
              <p className="text-text-tertiary text-xs mt-3">
                New to SessionFS?{' '}
                <Link to="/getting-started" className="text-[var(--accent)] hover:underline">
                  Start here
                </Link>
              </p>
            </div>
          </Card>
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
