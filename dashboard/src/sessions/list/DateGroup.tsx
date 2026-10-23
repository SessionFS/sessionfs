import type { SessionSummary } from '../../api/client';
import { groupByLineage } from './sessionSort';
import { Button } from '../../components/ui';
import { SessionRow } from './SessionRow';

/* ── Date-grouped session section with lineage grouping ── */

export function DateGroup({
  label,
  sessions,
  onRowClick,
  onResume,
  expandedGroups,
  onToggleGroup,
  isFirst,
  isInFolder,
  selectMode,
  selectedIds,
  onToggleSelect,
}: {
  label: string;
  sessions: SessionSummary[];
  onRowClick: (id: string) => void;
  onResume: (s: SessionSummary) => void;
  expandedGroups: Set<string>;
  onToggleGroup: (rootId: string) => void;
  isFirst?: boolean;
  isInFolder?: boolean;
  selectMode: boolean;
  selectedIds: Set<string>;
  onToggleSelect: (id: string) => void;
}) {
  if (sessions.length === 0) return null;

  const lineageGroups = groupByLineage(sessions);

  return (
    <div className={`mb-5 ${isFirst ? 'mt-0' : 'mt-8'}`}>
      <h2 className="text-micro font-semibold uppercase text-text-tertiary pt-3 pb-2 mb-3 border-b border-border">
        {label}
      </h2>
      <div className="flex flex-col gap-1.5">
        {lineageGroups.map((group) => {
          const isExpanded = expandedGroups.has(group.root.id);
          const hasChildren = group.children.length > 0;

          return (
            <div key={group.root.id}>
              <SessionRow
                session={group.root}
                onClick={() => {
                  if (selectMode) {
                    onToggleSelect(group.root.id);
                  } else if (hasChildren) {
                    onToggleGroup(group.root.id);
                  } else {
                    onRowClick(group.root.id);
                  }
                }}
                onResume={() => onResume(group.root)}
                onNavigate={hasChildren ? () => onRowClick(group.root.id) : undefined}
                isBookmarked={!!isInFolder}
                selectMode={selectMode}
                selected={selectedIds.has(group.root.id)}
                onToggleSelect={() => onToggleSelect(group.root.id)}
                lineageBadge={hasChildren ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onToggleGroup(group.root.id)}
                    className="gap-1.5 !px-2 !py-1 text-sm font-medium text-brand hover:bg-brand/10 hover:text-brand"
                    title={isExpanded ? 'Collapse follow-ups' : 'Expand follow-ups'}
                  >
                    <span>{group.children.length === 1 ? '1 follow-up' : `${group.children.length} follow-ups`}</span>
                    <svg
                      className={`w-3.5 h-3.5 transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`}
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <polyline points="6 9 12 15 18 9" />
                    </svg>
                  </Button>
                ) : undefined}
              />
              {hasChildren && isExpanded && (
                <div className="flex flex-col gap-1 mt-1 ml-6 border-l border-border pl-3">
                  {group.children.map((child) => (
                    <SessionRow
                      key={child.id}
                      session={child}
                      onClick={() => selectMode ? onToggleSelect(child.id) : onRowClick(child.id)}
                      onResume={() => onResume(child)}
                      isChild
                      isBookmarked={!!isInFolder}
                      selectMode={selectMode}
                      selected={selectedIds.has(child.id)}
                      onToggleSelect={() => onToggleSelect(child.id)}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
