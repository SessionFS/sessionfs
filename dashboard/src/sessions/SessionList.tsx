import { useState, useMemo, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSessions } from '../hooks/useSessions';
import { useFolders, useFolderSessions } from '../hooks/useBookmarks';
import type { SessionSummary, HandoffListResponse } from '../api/client';
import DeleteScopeDialog from './DeleteScopeDialog';
import type { DeleteScope } from './DeleteScopeDialog';
import { fullToolName } from '../utils/models';
import { formatTokens } from '../utils/tokens';
import RelativeDate from '../components/RelativeDate';
import { MobileNavChips } from '../components/BookmarkSidebar';
import type { NavFilter } from '../components/BookmarkSidebar';
import { useToast } from '../hooks/useToast';
import { useAuth } from '../auth/AuthContext';
import { TOOL_COLORS } from '../utils/tools';
import { Alert, Button, IconButton, PageContainer, PageHeader, Select } from '../components/ui';
import { CAPTURE_ONLY_TOOLS, TOOLS, DATE_RANGES } from './list/constants';
import {
  computeInsights, filterByDateRange, findDuplicateGroups, findMostRecent, groupByLineage, groupSessions, sortSessions,
} from './list/sessionSort';
import type { SortKey, SortDir } from './list/sessionSort';
import { GridGroup } from './list/GridView';
import { DateGroup } from './list/DateGroup';
import { TrashView } from './list/TrashView';
import { FolderDropdown } from './list/FolderDropdown';
import { NoSessionsCard, SessionListSkeleton } from './list/EmptyState';
import { errorMessage } from '../utils/errorMessage';

export default function SessionList() {
  const navigate = useNavigate();
  const { auth } = useAuth();
  const { addToast } = useToast();
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [toolFilter, setToolFilter] = useState('all');
  const [dateRange, setDateRange] = useState('');
  const [sortBy, setSortBy] = useState<SortKey>('date');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null);
  const [navFilter, setNavFilter] = useState<NavFilter>('all');
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [selectMode, setSelectMode] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  const [showBulkDeleteDialog, setShowBulkDeleteDialog] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [viewMode, setViewMode] = useState<'list' | 'grid'>(() => {
    const stored = typeof window !== 'undefined' ? window.localStorage.getItem('sfs-sessions-view') : null;
    return stored === 'grid' ? 'grid' : 'list';
  });

  function toggleSelect(id: string) {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function selectAll() {
    setSelectedIds(new Set(sessions.map(s => s.id)));
  }

  // Persist view mode
  function handleViewMode(mode: 'list' | 'grid') {
    setViewMode(mode);
    try { window.localStorage.setItem('sfs-sessions-view', mode); } catch { /* noop */ }
  }

  function clearSelection() {
    setSelectedIds(new Set());
    setSelectMode(false);
  }

  function handleBulkDelete() {
    setShowBulkDeleteDialog(true);
  }

  async function executeBulkDelete(scope: DeleteScope) {
    setBulkDeleting(true);
    const results = await Promise.all(
      Array.from(selectedIds).map(id =>
        auth!.client.deleteSession(id, scope).then(() => true).catch(() => false)
      )
    );

    const succeeded = results.filter(Boolean).length;
    const failed = results.length - succeeded;

    if (failed === 0) {
      addToast('success', `${succeeded} session(s) deleted`);
    } else if (succeeded === 0) {
      addToast('error', `Failed to delete ${failed} session(s)`);
    } else {
      addToast('warning', `${succeeded} deleted, ${failed} failed`);
    }

    setBulkDeleting(false);
    setShowBulkDeleteDialog(false);
    clearSelection();
    queryClient.invalidateQueries({ queryKey: ['sessions'] });
  }

  function toggleGroup(rootId: string) {
    setExpandedGroups(prev => {
      const next = new Set(prev);
      if (next.has(rootId)) next.delete(rootId);
      else next.add(rootId);
      return next;
    });
  }

  const PAGE_SIZE = 20;

  const { data, isLoading, error } = useSessions({
    page,
    page_size: PAGE_SIZE,
    source_tool: toolFilter === 'all' ? undefined : toolFilter,
  });

  const { data: folderSessionsData } = useFolderSessions(selectedFolderId);
  const { data: foldersData } = useFolders();

  // Handoffs inbox query. 5-min stale window — incoming handoffs are
  // async events that don't need sub-minute precision in the badge
  // counter, and SessionList is the hottest screen so cutting
  // remount refetches matters. The badge stays accurate after any
  // claim/accept flow because those paths invalidate the cache.
  const { data: handoffsData } = useQuery<HandoffListResponse>({
    queryKey: ['handoffs', 'inbox'],
    queryFn: () => auth!.client.listInbox(),
    enabled: !!auth,
    staleTime: 300_000,
  });

  const pendingHandoffs = useMemo(() => {
    if (!handoffsData?.handoffs) return [];
    return handoffsData.handoffs.filter((h) => h.status === 'pending');
  }, [handoffsData]);

  const allFolders = foldersData?.folders ?? [];
  const totalBookmarked = allFolders.reduce((sum, f) => sum + f.bookmark_count, 0);

  const sessions = useMemo(() => {
    // If a folder is selected, show folder sessions instead
    if (selectedFolderId && folderSessionsData) {
      return sortSessions(filterByDateRange(folderSessionsData.sessions, dateRange), sortBy, sortDir);
    }

    if (!data?.sessions) return [];

    // Date range filter (client-side on current page), then sort
    // (client-side on current page -- server sorts by date by default)
    return sortSessions(filterByDateRange(data.sessions, dateRange), sortBy);
  }, [data, dateRange, sortBy, sortDir, selectedFolderId, folderSessionsData]);

  // Clear selection when filters change
  useEffect(() => {
    setSelectedIds(new Set());
  }, [toolFilter, dateRange, sortBy, sortDir, selectedFolderId, navFilter, page]);

  // Most recent session (for hero + repo detection)
  const mostRecent = useMemo(() => findMostRecent(data?.sessions ?? []), [data]);

  // Clear folder selection when switching to "all"
  useEffect(() => {
    if (navFilter === 'all') setSelectedFolderId(null);
  }, [navFilter]);

  // Auto-expand group if the most recent session is a child
  useEffect(() => {
    if (!data?.sessions || data.sessions.length === 0) return;
    const mostRecentId = findMostRecent(data.sessions)?.id;
    if (!mostRecentId) return;
    const allGroups = groupByLineage(data.sessions);
    for (const group of allGroups) {
      if (group.children.some(c => c.id === mostRecentId)) {
        setExpandedGroups(prev => new Set([...prev, group.root.id]));
        break;
      }
    }
  }, [data?.sessions]);

  const filteredSessions = sessions;

  // Grouping: default is date buckets, but "Sort: Tool" switches to tool buckets.
  const grouped = useMemo(() => groupSessions(filteredSessions, sortBy), [filteredSessions, sortBy]);

  const hasMore = selectedFolderId ? false : (data?.has_more ?? false);
  // totalSessions for analytics = current view count
  const totalSessions = selectedFolderId ? (folderSessionsData?.total ?? 0) : (data?.total ?? 0);
  // allSessionsCount for sidebar "All Sessions" label = always the real total from API
  const allSessionsCount = data?.total ?? 0;

  // Insights strip data
  const insights = useMemo(() => computeInsights(filteredSessions), [filteredSessions]);

  function handleRowClick(id: string) {
    navigate(`/sessions/${id}`);
  }

  function handleToggleSelect() {
    if (selectMode) clearSelection();
    else { setSelectMode(true); if (viewMode === 'grid') handleViewMode('list'); }
  }

  function handleFindDuplicates() {
    const dupes = findDuplicateGroups(sessions);
    if (dupes.size === 0) {
      addToast('info', 'No duplicates found');
      return;
    }
    setSelectMode(true);
    const toSelect = new Set<string>();
    for (const group of dupes.values()) {
      const sorted = [...group].sort((a, b) =>
        new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
      );
      for (let i = 1; i < sorted.length; i++) {
        toSelect.add(sorted[i].id);
      }
    }
    setSelectedIds(toSelect);
    addToast('warning', `Found ${toSelect.size} duplicate(s) across ${dupes.size} group(s) — oldest copies selected for review`);
  }

  function handleResume(s: SessionSummary) {
    const tool = s.source_tool || 'claude-code';
    const resumeTool = CAPTURE_ONLY_TOOLS.has(tool) ? 'claude-code' : tool;
    navigator.clipboard.writeText(`sfs resume ${s.id} --in ${resumeTool}`);
    addToast('success', `Resume command copied${CAPTURE_ONLY_TOOLS.has(tool) ? ` (${tool} is capture-only, using claude-code)` : ''}`);
  }

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <PageContainer width="full" className="!py-4">
        <PageHeader
          title="Sessions"
          className="mb-4"
          actions={
            <div className="flex flex-wrap items-center gap-3">
              {/* List/Grid view toggle */}
              <div className="flex items-center rounded-lg border border-border bg-surface overflow-hidden">
                <IconButton
                  label="List view"
                  onClick={() => handleViewMode('list')}
                  aria-pressed={viewMode === 'list'}
                  className={`!rounded-none ${viewMode === 'list' ? 'bg-bg-elevated text-text-primary' : ''}`}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
                    <line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
                  </svg>
                </IconButton>
                <IconButton
                  label="Grid view"
                  onClick={() => handleViewMode('grid')}
                  aria-pressed={viewMode === 'grid'}
                  className={`!rounded-none ${viewMode === 'grid' ? 'bg-bg-elevated text-text-primary' : ''}`}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
                    <rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
                  </svg>
                </IconButton>
              </div>
              <Button variant="secondary" className="font-normal text-text-tertiary" onClick={handleToggleSelect}>
                {selectMode ? 'Cancel' : 'Select'}
              </Button>
              <Button variant="secondary" className="font-normal text-text-tertiary" onClick={handleFindDuplicates}>
                Find Duplicates
              </Button>
              <Button
                variant="secondary"
                aria-pressed={showTrash}
                className={`font-normal ${showTrash ? 'text-text-primary border-brand bg-brand/10' : 'text-text-tertiary'}`}
                onClick={() => { setShowTrash(!showTrash); clearSelection(); }}
              >
                {showTrash ? 'Active' : 'Trash'}
              </Button>
            </div>
          }
        />

        {/* Mobile nav chips */}
        <MobileNavChips
          selectedFilter={navFilter}
          onSelectFilter={(f) => { setNavFilter(f); setPage(1); }}
          totalCount={allSessionsCount}
          bookmarkedCount={totalBookmarked}
          inRepoCount={0}
          inRepoLabel={null}
          selectedFolderId={selectedFolderId}
          onSelectFolder={(id) => { setSelectedFolderId(id); setPage(1); }}
        />

        {/* ── Compact resume bar ── */}
        {!isLoading && !showTrash && mostRecent && (
          <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:py-0 sm:h-16 bg-bg-elevated border border-border rounded-lg mb-5">
            {/* Left: eyebrow + title + meta */}
            <div className="flex-1 min-w-0 flex flex-col justify-center gap-0.5">
              <span className="text-micro uppercase font-semibold text-text-tertiary">Continue where you left off</span>
              <div className="flex items-center gap-x-2 gap-y-1 min-w-0 flex-wrap sm:flex-nowrap">
                <span
                  className="text-md font-semibold text-text-primary truncate max-w-full cursor-pointer hover:text-brand transition-colors"
                  onClick={() => navigate(`/sessions/${mostRecent.id}`)}
                >
                  {mostRecent.title || 'Untitled session'}
                </span>
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: TOOL_COLORS[mostRecent.source_tool] || 'var(--text-tertiary)' }}
                />
                <span className="text-mono-chip">{fullToolName(mostRecent.source_tool)}</span>
                <span className="text-caption text-text-tertiary">
                  <RelativeDate iso={mostRecent.updated_at} />
                </span>
              </div>
            </div>

            {/* Right: pending handoff badge + actions */}
            <div className="flex items-center gap-2 flex-wrap sm:shrink-0">
              {pendingHandoffs.length > 0 && (
                <Link
                  to="/handoffs"
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-full bg-brand/10 text-brand hover:bg-brand/20 transition-colors"
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-brand" />
                  {pendingHandoffs.length} pending handoff{pendingHandoffs.length !== 1 ? 's' : ''}
                </Link>
              )}
              <Button
                onClick={() => handleResume(mostRecent)}
              >
                {CAPTURE_ONLY_TOOLS.has(mostRecent.source_tool) ? 'Resume in Claude Code' : 'Resume'}
              </Button>
              <Button
                variant="secondary"
                onClick={() => navigate(`/sessions/${mostRecent.id}`)}
              >
                View
              </Button>
            </div>
          </div>
        )}

        {/* ── Filters row ── */}
        <div className="flex flex-wrap items-center gap-3 mb-4">
          <FolderDropdown
            navFilter={navFilter}
            selectedFolderId={selectedFolderId}
            onSelectFilter={(f) => { setNavFilter(f); setPage(1); }}
            onSelectFolder={(id) => { setSelectedFolderId(id); setPage(1); }}
            totalCount={allSessionsCount}
            bookmarkedCount={totalBookmarked}
          />
          <Select
            aria-label="Filter by tool"
            value={toolFilter}
            onValueChange={(v) => { setToolFilter(v); setPage(1); }}
            options={TOOLS.map((t) => ({ value: t, label: t === 'all' ? 'All Tools' : fullToolName(t) }))}
            className="!w-auto bg-surface text-base font-medium text-text-secondary transition-colors"
          />
          <Select
            aria-label="Filter by date range"
            value={dateRange}
            onValueChange={setDateRange}
            options={DATE_RANGES.map((d) => ({ value: d.value, label: d.label }))}
            className="!w-auto bg-surface text-base font-medium text-text-secondary transition-colors"
          />
          <Select
            aria-label="Sort sessions"
            value={sortBy}
            onValueChange={(v) => setSortBy(v as SortKey)}
            options={[
              { value: 'date', label: 'Sort: Date' },
              { value: 'tool', label: 'Sort: Tool' },
              { value: 'messages', label: 'Sort: Messages' },
              { value: 'tokens', label: 'Sort: Tokens' },
              { value: 'title', label: 'Sort: Title' },
            ]}
            className="!w-auto bg-surface text-base font-medium text-text-secondary transition-colors"
          />
          <IconButton
            label={`Sort direction: ${sortDir === 'desc' ? 'descending' : 'ascending'}`}
            variant="secondary"
            title={sortDir === 'desc' ? 'Showing most first — click for least first' : 'Showing least first — click for most first'}
            onClick={() => setSortDir(d => d === 'desc' ? 'asc' : 'desc')}
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              {sortDir === 'desc' ? (
                <path d="M12 5v14M5 12l7 7 7-7" />
              ) : (
                <path d="M12 19V5M5 12l7-7 7 7" />
              )}
            </svg>
          </IconButton>
        </div>

        {/* Error */}
        {error && (
          <Alert variant="danger" className="mb-4">
            Failed to load sessions: {errorMessage(error)}
          </Alert>
        )}

        {/* Loading */}
        {isLoading && (
          <SessionListSkeleton />
        )}

        {/* ── Session list ── */}
        {!isLoading && !showTrash && filteredSessions.length > 0 && (
          <>
            {viewMode === 'grid' ? (
              /* Grid view */
              grouped.mode === 'tool' ? (
                grouped.toolGroups.map((group, index) => (
                  <GridGroup key={group.label} label={group.label} sessions={group.sessions} isFirst={index === 0}
                    onRowClick={handleRowClick} onResume={handleResume} isInFolder={!!selectedFolderId} />
                ))
              ) : (
                <>
                  <GridGroup label="Today" sessions={grouped.today} isFirst onRowClick={handleRowClick} onResume={handleResume} isInFolder={!!selectedFolderId} />
                  <GridGroup label="This Week" sessions={grouped.thisWeek} onRowClick={handleRowClick} onResume={handleResume} isInFolder={!!selectedFolderId} />
                  <GridGroup label="Earlier" sessions={grouped.earlier} onRowClick={handleRowClick} onResume={handleResume} isInFolder={!!selectedFolderId} />
                </>
              )
            ) : (
              /* List view */
              grouped.mode === 'tool' ? (
                grouped.toolGroups.map((group, index) => (
                  <DateGroup
                    key={group.label}
                    label={group.label}
                    sessions={group.sessions}
                    onRowClick={handleRowClick}
                   
                    onResume={handleResume}
                    expandedGroups={expandedGroups}
                    onToggleGroup={toggleGroup}
                    isFirst={index === 0}
                    selectMode={selectMode}
                    selectedIds={selectedIds}
                    onToggleSelect={toggleSelect}
                    isInFolder={!!selectedFolderId}
                  />
                ))
              ) : (
                <>
                  <DateGroup label="Today" sessions={grouped.today} onRowClick={handleRowClick} onResume={handleResume} expandedGroups={expandedGroups} onToggleGroup={toggleGroup} isFirst selectMode={selectMode} selectedIds={selectedIds} onToggleSelect={toggleSelect} isInFolder={!!selectedFolderId} />
                  <DateGroup label="This Week" sessions={grouped.thisWeek} onRowClick={handleRowClick} onResume={handleResume} expandedGroups={expandedGroups} onToggleGroup={toggleGroup} selectMode={selectMode} selectedIds={selectedIds} onToggleSelect={toggleSelect} isInFolder={!!selectedFolderId} />
                  <DateGroup label="Earlier" sessions={grouped.earlier} onRowClick={handleRowClick} onResume={handleResume} expandedGroups={expandedGroups} onToggleGroup={toggleGroup} selectMode={selectMode} selectedIds={selectedIds} onToggleSelect={toggleSelect} isInFolder={!!selectedFolderId} />
                </>
              )
            )}

            {/* Pagination */}
            <div className="flex items-center justify-between mt-4 text-sm text-text-tertiary">
              <span>
                Page {page} · {filteredSessions.length} sessions
                {totalSessions > 0 && ` (${totalSessions} total)`}
              </span>
              <div className="flex gap-2">
                {page > 1 && (
                  <Button variant="secondary" size="sm" onClick={() => setPage((p) => p - 1)}>
                    Previous
                  </Button>
                )}
                {hasMore && (
                  <Button variant="secondary" size="sm" onClick={() => setPage((p) => p + 1)}>
                    Next
                  </Button>
                )}
              </div>
            </div>

            {/* ── Insights strip ── */}
            {insights && (
              <div className="mt-4 py-6 border-t border-border text-xs text-text-tertiary flex flex-wrap items-center gap-1">
                {insights.toolMix && <><span>Tool Mix: {insights.toolMix}</span><span className="opacity-40">&middot;</span></>}
                <span>{formatTokens(insights.tokensToday)} tokens today</span>
                {insights.peak && <><span className="opacity-40">&middot;</span><span>Peak: {insights.peak}</span></>}
              </div>
            )}
          </>
        )}

        {/* Empty state */}
        {!isLoading && !showTrash && filteredSessions.length === 0 && !error && (
          selectedFolderId ? (
            <div className="text-center py-16">
              <p className="text-text-secondary text-base font-medium mb-2">No sessions yet</p>
              <p className="text-text-tertiary text-sm">
                No sessions bookmarked in this folder yet.
              </p>
            </div>
          ) : (
            <NoSessionsCard />
          )
        )}

        {/* Trash view */}
        {showTrash && <TrashView />}

      </PageContainer>

      {/* ── Bulk selection toolbar ── */}
      {selectedIds.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 bg-bg-elevated border border-border rounded-xl shadow-[var(--shadow-lg)] px-5 py-3 flex items-center gap-4">
          <span className="text-base font-medium text-text-primary">
            {selectedIds.size} selected
          </span>
          <Button variant="ghost" size="sm" onClick={selectAll} className="text-brand hover:text-brand">
            Select all
          </Button>
          <Button variant="danger" onClick={handleBulkDelete}>
            Delete selected
          </Button>
          <Button variant="ghost" size="sm" onClick={clearSelection}>
            Cancel
          </Button>
        </div>
      )}

      {/* Bulk delete scope dialog */}
      {showBulkDeleteDialog && (
        <DeleteScopeDialog
          count={selectedIds.size}
          isPending={bulkDeleting}
          onCancel={() => setShowBulkDeleteDialog(false)}
          onConfirm={executeBulkDelete}
        />
      )}
    </div>
  );
}
