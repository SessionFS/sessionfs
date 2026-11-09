import { Link } from 'react-router-dom';
import type { ProjectContext } from '../api/client';
import { Alert, Button, Dropdown, Tabs } from '../components/ui';

export interface ProjectHeaderTab {
  key: string;
  label: string;
}

interface ProjectHeaderProps {
  project: ProjectContext;
  /** The five page sections, switched from the menu after the project name. */
  sections: ProjectHeaderTab[];
  activeSection: string;
  onSectionChange: (section: string) => void;
  /** Sub-tabs of the active section, if it has any. */
  subTabs?: ProjectHeaderTab[];
  activeSub?: string;
  onSubChange: (sub: string) => void;
  /** Section and sub-tab that carry the pending-entries count. */
  badgeSection?: string;
  badgeSub?: string;
  pendingCount: number;
  compilePending: boolean;
  onCompile: () => void;
  /** Absent when the helper is not available to this user. */
  onAskHelper?: () => void;
}

function CountPill({ count }: { count: number }) {
  return (
    <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full text-2xs font-semibold bg-brand text-white leading-none">
      {count > 99 ? '99+' : count}
    </span>
  );
}

function Chevron() {
  return (
    <svg aria-hidden="true" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-text-tertiary">
      <polyline points="2,3.5 5,6.5 8,3.5" />
    </svg>
  );
}

function Check() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6">
      <polyline points="2.5,6.5 5,9 9.5,3.5" />
    </svg>
  );
}

const Slash = ({ className = '' }: { className?: string }) => (
  <span aria-hidden="true" className={`text-text-tertiary select-none ${className}`}>/</span>
);

/**
 * The project bar: one line on top of every project section.
 *
 *   Projects / <project name> / <section ▾>   <sub-tabs>   <compile> <helper>
 *
 * The section is a menu rather than a second row of tabs; the sub-tabs of
 * the active section sit on the same line. Project details (repo path,
 * created/updated, repo count) live in Settings → About. On phones the
 * sub-tabs drop to their own row under the bar.
 */
export default function ProjectHeader({
  project,
  sections,
  activeSection,
  onSectionChange,
  subTabs,
  activeSub,
  onSubChange,
  badgeSection,
  badgeSub,
  pendingCount,
  compilePending,
  onCompile,
  onAskHelper,
}: ProjectHeaderProps) {
  const name = project.name || project.git_remote_normalized;
  const sectionLabel = sections.find((s) => s.key === activeSection)?.label ?? sections[0]?.label ?? '';
  const showCompile = pendingCount > 0 && project.can_edit !== false;
  const entryWord = pendingCount === 1 ? 'entry' : 'entries';

  return (
    <div>
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border pb-2 sm:flex-nowrap">
        <div className="flex min-w-0 flex-1 items-center gap-1.5 sm:flex-none sm:max-w-[45%]">
          <Link
            to="/projects"
            className="hidden shrink-0 rounded-sm text-sm text-text-secondary outline-none hover:text-text-primary focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] sm:inline"
          >
            Projects
          </Link>
          <Slash className="hidden sm:inline" />
          <h1 className="min-w-0 truncate !text-sm !font-semibold !leading-5 !tracking-normal ![font-family:inherit] text-text-primary" title={name}>
            {name}
          </h1>
          <Slash />
          <Dropdown
            align="left"
            menuLabel="Project sections"
            minWidthClass="min-w-[180px]"
            onSelect={onSectionChange}
            items={sections.map((s) => ({
              key: s.key,
              label: s.label,
              badge: s.key === badgeSection && pendingCount > 0 ? String(pendingCount > 99 ? '99+' : pendingCount) : undefined,
              icon: (
                <span aria-hidden="true" className="inline-flex w-3 justify-center text-brand">
                  {s.key === activeSection && <Check />}
                </span>
              ),
            }))}
            trigger={(open) => (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-haspopup="menu"
                aria-expanded={open}
                aria-label={`Section: ${sectionLabel}`}
                className="!gap-1 !px-1.5 !py-1 !text-sm !font-normal !text-text-primary shrink-0"
              >
                {sectionLabel}
                <Chevron />
              </Button>
            )}
          />
        </div>

        {subTabs && subTabs.length > 0 ? (
          <div className="order-last w-full min-w-0 sm:order-none sm:w-auto sm:flex-1">
            <Tabs
              bare
              variant="subtle"
              idPrefix="project-sub"
              aria-label={`${sectionLabel} views`}
              activeKey={activeSub ?? subTabs[0].key}
              onChange={onSubChange}
              tabs={subTabs.map((t) => ({
                key: t.key,
                label: (
                  <span className="inline-flex items-center gap-1.5">
                    {t.label}
                    {t.key === badgeSub && activeSection === badgeSection && pendingCount > 0 && (
                      <CountPill count={pendingCount} />
                    )}
                  </span>
                ),
              }))}
            />
          </div>
        ) : (
          <div className="hidden sm:block sm:flex-1" />
        )}

        <div className="flex shrink-0 items-center gap-2">
          {showCompile && (
            <Button
              variant="secondary"
              size="sm"
              onClick={onCompile}
              disabled={compilePending}
              aria-label={compilePending ? undefined : `Compile ${pendingCount} pending ${entryWord} into the project context`}
              title={`${pendingCount} pending ${entryWord}, not yet in the project context`}
            >
              {compilePending ? 'Compiling…' : (
                <>
                  Compile <span className="tabular-nums text-text-tertiary">{pendingCount > 99 ? '99+' : pendingCount}</span>
                </>
              )}
            </Button>
          )}
          {onAskHelper && (
            <Button variant="secondary" size="sm" onClick={onAskHelper} aria-label="Ask the helper">
              <span className="sm:hidden">Helper</span>
              <span className="hidden sm:inline">Ask the helper</span>
            </Button>
          )}
        </div>
      </header>

      {project.repo_reclaimed_at && (
        // A standing state of the project, not the result of an action, so
        // warning (polite) rather than danger (announced as an alert).
        <Alert variant="warning" className="mt-3 items-center py-2 flex-wrap">
          <span className="text-xs font-medium">
            All repos were reclaimed. Link a new repo to revive this project.
          </span>
        </Alert>
      )}
    </div>
  );
}
