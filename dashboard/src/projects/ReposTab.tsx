import { useState } from 'react';
import {
  useProjectRepos,
  useLinkRepo,
  useUnlinkRepo,
} from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import type { ProjectRepoResponse } from '../api/client';
import { ApiError } from '../api/client';
import { Button, Input, Dialog, DialogHeader, DialogFooter } from '../components/ui';

function VerifiedBadge({ repo }: { repo: ProjectRepoResponse }) {
  if (repo.verified && repo.verification_method === 'github_app') {
    return (
      <span
        className="inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
        title="Verified via GitHub App"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
        verified
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400"
      title={repo.verification_method === 'owner_attested' ? 'Owner-attested — install the GitHub App for verified linking' : 'Legacy — verified status unknown'}
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      {repo.verification_method === 'owner_attested' ? 'unverified' : 'legacy'}
    </span>
  );
}

function PrimaryMarker() {
  return (
    <span
      className="inline-flex items-center gap-0.5 text-xs font-medium px-2 py-0.5 rounded-full bg-brand/10 text-brand"
      title="Primary repo"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none">
        <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
      </svg>
      primary
    </span>
  );
}

interface AddRepoModalProps {
  projectId: string;
  onClose: () => void;
  onLinked: () => void;
}

function AddRepoModal({ projectId, onClose, onLinked }: AddRepoModalProps) {
  const [gitRemote, setGitRemote] = useState('');
  const [error, setError] = useState('');
  const linkRepo = useLinkRepo(projectId);
  const { addToast } = useToast();

  function handleSubmit() {
    const trimmed = gitRemote.trim();
    if (!trimmed) {
      setError('Enter a git remote URL.');
      return;
    }
    setError('');
    linkRepo.mutate(
      { git_remote: trimmed },
      {
        onSuccess: (result) => {
          addToast('success', `Repo linked: ${result.git_remote_normalized}`);
          onLinked();
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            if (err.code === 'repo_already_linked') {
              setError(err.message);
            } else if (err.code === 'cross_org_denied') {
              setError(err.message);
            } else {
              setError(err.message);
            }
          } else {
            setError(String(err));
          }
        },
      },
    );
  }

  return (
    <Dialog open onClose={onClose} titleId="add-repo-title">
      <DialogHeader titleId="add-repo-title">Add repo to project</DialogHeader>
      <p className="text-sm text-text-tertiary mb-4">
        Paste a git remote URL (e.g. <code className="font-mono bg-bg-tertiary px-1 py-0.5 rounded text-text-secondary">github.com/acme/backend</code>).
        The server will verify ownership if the SessionFS GitHub App is installed.
      </p>
      <Input
        value={gitRemote}
        onChange={(e) => { setGitRemote(e.target.value); setError(''); }}
        placeholder="github.com/org/repo"
        error={error || undefined}
        autoFocus
        onKeyDown={(e) => { if (e.key === 'Enter') handleSubmit(); }}
      />
      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button
          onClick={handleSubmit}
          disabled={linkRepo.isPending || !gitRemote.trim()}
          loading={linkRepo.isPending}
        >
          Link repo
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

interface UnlinkConfirmProps {
  repo: ProjectRepoResponse;
  repoCount: number;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
  error?: string;
}

function UnlinkConfirm({ repo, repoCount, onConfirm, onCancel, isPending, error }: UnlinkConfirmProps) {
  const isLast = repoCount <= 1;
  return (
    <div className="mt-2 p-3 rounded-lg border border-border bg-bg-primary">
      <p className="text-sm text-text-secondary mb-2">
        Unlink <span className="font-medium text-text-primary">{repo.git_remote_normalized}</span>?
      </p>
      {isLast ? (
        <p className="text-xs text-[var(--danger)] mb-2" role="alert">
          Cannot unlink the last repo. A project must have at least one linked repo.
        </p>
      ) : (
        <p className="text-xs text-text-tertiary mb-2">
          Knowledge entries and tickets from this repo stay in the project.
          New sessions will flow to any remaining linked repo.
        </p>
      )}
      {error && (
        <p className="text-xs text-[var(--danger)] mb-2" role="alert">{error}</p>
      )}
      <div className="flex gap-2 justify-end">
        <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
        {!isLast && (
          <Button
            variant="danger"
            size="sm"
            onClick={onConfirm}
            disabled={isPending}
            loading={isPending}
          >
            Unlink
          </Button>
        )}
      </div>
    </div>
  );
}

export default function ReposTab({ projectId }: { projectId: string }) {
  const { data: repos, isLoading, error } = useProjectRepos(projectId);
  const unlinkRepo = useUnlinkRepo(projectId);
  const { addToast } = useToast();
  const [showAdd, setShowAdd] = useState(false);
  const [unlinkTarget, setUnlinkTarget] = useState<string | null>(null);
  const [unlinkError, setUnlinkError] = useState('');

  function handleUnlink(repo: ProjectRepoResponse) {
    setUnlinkError('');
    unlinkRepo.mutate(repo.id, {
      onSuccess: () => {
        addToast('success', `Unlinked ${repo.git_remote_normalized}`);
        setUnlinkTarget(null);
      },
      onError: (err) => {
        if (err instanceof ApiError) {
          setUnlinkError(err.message);
        } else {
          setUnlinkError(String(err));
        }
      },
    });
  }

  if (isLoading) {
    return <p className="p-5 text-text-tertiary text-sm">Loading repos…</p>;
  }

  if (error) {
    return <p className="p-5 text-red-400 text-sm">Failed to load repos: {String(error)}</p>;
  }

  const repoList = repos || [];

  return (
    <div className="p-5">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-4">
        <span className="text-sm text-text-tertiary">
          {repoList.length} {repoList.length === 1 ? 'repo' : 'repos'}
        </span>
        <Button onClick={() => setShowAdd(true)}>+ Add repo</Button>
      </div>

      {/* Empty state */}
      {!repoList.length ? (
        <p className="text-text-tertiary text-sm py-8 text-center">
          No repos linked. Link your first repo to get started.
        </p>
      ) : (
        <div className="space-y-2">
          {repoList.map((repo) => (
            <div
              key={repo.id}
              className="px-4 py-3 rounded-lg border border-border bg-bg-elevated"
            >
              <div className="flex items-center gap-3">
                {/* Git icon */}
                <svg
                  className="shrink-0 w-4 h-4 text-text-tertiary"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <circle cx="12" cy="18" r="3" />
                  <circle cx="6" cy="6" r="3" />
                  <circle cx="18" cy="6" r="3" />
                  <path d="M18 9v1a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V9" />
                  <path d="M12 3v4" />
                </svg>
                <div className="flex-1 min-w-0">
                  <span className="text-sm font-medium text-text-primary font-mono truncate block">
                    {repo.git_remote_normalized}
                  </span>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {repo.is_primary && <PrimaryMarker />}
                  <VerifiedBadge repo={repo} />
                  {/* Unlink button — separate from expand/collapse (a11y: sibling, not nested) */}
                  <button
                    onClick={() => setUnlinkTarget(unlinkTarget === repo.id ? null : repo.id)}
                    className="text-xs text-text-tertiary hover:text-red-400 transition-colors px-2 py-1"
                    title={`Unlink ${repo.git_remote_normalized}`}
                    aria-label={`Unlink ${repo.git_remote_normalized}`}
                  >
                    Unlink
                  </button>
                </div>
              </div>

              {/* Inline unlink confirmation */}
              {unlinkTarget === repo.id && (
                <UnlinkConfirm
                  repo={repo}
                  repoCount={repoList.length}
                  onConfirm={() => handleUnlink(repo)}
                  onCancel={() => setUnlinkTarget(null)}
                  isPending={unlinkRepo.isPending}
                  error={unlinkError}
                />
              )}
            </div>
          ))}
        </div>
      )}

      {/* Add repo modal */}
      {showAdd && (
        <AddRepoModal
          projectId={projectId}
          onClose={() => setShowAdd(false)}
          onLinked={() => setShowAdd(false)}
        />
      )}
    </div>
  );
}
