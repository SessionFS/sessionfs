import { useState } from 'react';
import RelativeDate from '../components/RelativeDate';
import type { ProjectContext } from '../api/client';
import { Alert, Tabs, Card, Button, IconButton, MonoText } from '../components/ui';

export interface ProjectHeaderTab {
  key: string;
  label: string;
}

interface ProjectHeaderProps {
  project: ProjectContext;
  activeTab: string;
  /** Top-level tab that carries the pending-entries count badge. */
  badgeTab?: string;
  tabs: ProjectHeaderTab[];
  onTabChange: (tab: string) => void;
  pendingCount: number;
  compilePending: boolean;
  onCompile: () => void;
}

export default function ProjectHeader({
  project,
  activeTab,
  badgeTab,
  tabs,
  onTabChange,
  pendingCount,
  compilePending,
  onCompile,
}: ProjectHeaderProps) {
  const [workflowHintDismissed, setWorkflowHintDismissed] = useState(
    () => sessionStorage.getItem('sfs-workflow-hint-dismissed') === '1',
  );
  const repoCount = project?.repos?.length;

  function dismissWorkflowHint() {
    setWorkflowHintDismissed(true);
    sessionStorage.setItem('sfs-workflow-hint-dismissed', '1');
  }

  return (
    <Card level="elevated" className="rounded-xl shadow-[var(--shadow-sm)]">
      <div className="px-5 pt-4">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold text-text-primary break-all mb-1">
            {project.name || project.git_remote_normalized}
          </h1>
          {/* Repo path (when name differs) */}
          {project.name && project.name !== project.git_remote_normalized && (
            <p className="text-xs text-text-tertiary -mt-0.5 mb-1 min-w-0">
              <MonoText value={project.git_remote_normalized} />
            </p>
          )}
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-text-tertiary">
            <span>
              Created <RelativeDate iso={project.created_at} />
            </span>
            <span>&middot;</span>
            <span>
              Updated <RelativeDate iso={project.updated_at} />
            </span>
            {repoCount !== undefined && repoCount > 0 && (
              <>
                <span>&middot;</span>
                <span>{repoCount} {repoCount === 1 ? 'repo' : 'repos'}</span>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Pending entries don't compile themselves; the user (or an agent
          acting on their behalf) has to trigger a compile pass. This is the
          page's one compile action, so it works from any section. */}
      {!workflowHintDismissed && pendingCount > 0 && project.can_edit !== false && (
        <Alert
          variant="info"
          className="mx-5 mt-3 items-center py-2 flex-wrap"
          action={
            <div className="flex items-center gap-1">
              <Button
                size="sm"
                onClick={onCompile}
                disabled={compilePending}
                title="Run a compile pass — promotes pending claims into the project context document"
              >
                {compilePending ? 'Compiling…' : 'Compile now'}
              </Button>
              <IconButton label="Dismiss" variant="ghost" size="sm" onClick={dismissWorkflowHint}>
                &times;
              </IconButton>
            </div>
          }
        >
          <span className="text-xs font-medium">
            {pendingCount} pending {pendingCount === 1 ? 'entry' : 'entries'} — not yet in context
          </span>
        </Alert>
      )}

      {/* Repo reclaimed notice */}
      {project.repo_reclaimed_at && (
        // A standing state of the project, not the result of an action, so
        // warning (polite) rather than danger (announced as an alert).
        <Alert variant="warning" className="mx-5 mt-3 items-center py-2 flex-wrap">
          <span className="text-xs font-medium">
            All repos were reclaimed. Link a new repo to revive this project.
          </span>
        </Alert>
      )}

      {/* Tabs */}
      <div className="px-5 mt-2 border-t border-border pt-2">
        <Tabs
          bare
          idPrefix="project-section"
          aria-label="Project sections"
          activeKey={activeTab}
          onChange={onTabChange}
          tabs={tabs.map((tab) => ({
            key: tab.key,
            label: (
              <span className="inline-flex items-center gap-1.5">
                {tab.label}
                {tab.key === badgeTab && pendingCount > 0 && (
                  <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full text-2xs font-semibold bg-brand text-white leading-none">
                    {pendingCount > 99 ? '99+' : pendingCount}
                  </span>
                )}
              </span>
            ),
          }))}
        />
      </div>
    </Card>
  );
}
