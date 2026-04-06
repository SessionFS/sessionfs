import { useState, useMemo, useRef, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSessions } from '../hooks/useSessions';
import { useFolders, useAddBookmark, useFolderSessions } from '../hooks/useBookmarks';
import type { SessionSummary, HandoffListResponse } from '../api/client';
import { abbreviateModel, fullToolName } from '../utils/models';
import { formatTokens } from '../utils/tokens';
import RelativeDate from '../components/RelativeDate';
import BookmarkSidebar, { MobileNavChips } from '../components/BookmarkSidebar';
import type { NavFilter } from '../components/BookmarkSidebar';
import { useToast } from '../hooks/useToast';
import { useAuth } from '../auth/AuthContext';
import { TOOL_COLORS } from '../utils/tools';

const CAPTURE_ONLY_TOOLS = new Set(['cursor', 'cline', 'roo-code', 'amp']);

interface SessionSummaryWithAudit extends SessionSummary {
  audit_trust_score?: number | null;
}

const TOOLS = ['all', 'claude-code', 'codex', 'gemini', 'copilot', 'cursor', 'amp', 'cline', 'roo-code'] as const;
const DATE_RANGES = [
  { label: 'All time', value: '' },
  { label: 'Last 24h', value: '24h' },
  { label: 'Last 7d', value: '7d' },
  { label: 'Last 30d', value: '30d' },
] as const;

type SortKey = 'date' | 'messages' | 'tokens' | 'title';

interface LineageGroup {
  root: SessionSummary;
  children: SessionSummary[];
}

function groupByLineage(sessions: SessionSummary[]): LineageGroup[] {
  const byId = new Map(sessions.map(s => [s.id, s]));

  // Find the root of each session's chain (walk up parent links)
  function findRoot(id: string, visited = new Set<string>()): string {
    const s = byId.get(id);
    if (!s || !s.parent_session_id || !byId.has(s.parent_session_id) || visited.has(id)) return id;
    visited.add(id);
    return findRoot(s.parent_session_id, visited);
  }

  // Group all sessions by their root
  const rootToChildren = new Map<string, SessionSummary[]>();
  for (const s of sessions) {
    const rootId = findRoot(s.id);
    if (rootId === s.id) continue; // this IS the root
    if (!rootToChildren.has(rootId)) rootToChildren.set(rootId, []);
    rootToChildren.get(rootId)!.push(s);
  }

  // Build groups: roots with all descendants, ordered by updated_at
  const childIds = new Set<string>();
  for (const kids of rootToChildren.values()) {
    for (const k of kids) childIds.add(k.id);
  }

  const groups: LineageGroup[] = [];
  for (const s of sessions) {
    if (!childIds.has(s.id)) {
      const kids = rootToChildren.get(s.id) || [];
      kids.sort((a, b) => new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime());
      groups.push({ root: s, children: kids });
    }
  }
  return groups;
}

function findDuplicateGroups(sessions: SessionSummary[]): Map<string, SessionSummary[]> {
  const groups = new Map<string, SessionSummary[]>();
  for (const s of sessions) {
    // Strong key: title + message_count + source_tool + etag (content hash)
    // etag is the SHA-256 of the session archive — identical content = identical etag
    const key = `${s.title || ''}:${s.message_count}:${s.source_tool}:${(s as any).etag || ''}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(s);
  }
  const dupes = new Map<string, SessionSummary[]>();
  for (const [key, group] of groups) {
    if (group.length >= 2) dupes.set(key, group);
  }
  return dupes;
}

function isToday(dateStr: string): boolean {
  const d = new Date(dateStr);
  const now = new Date();
  return d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
}

function isThisWeek(dateStr: string): boolean {
  const d = new Date(dateStr);
  const now = new Date();
  const weekAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7);
  return d >= weekAgo && !isToday(dateStr);
}

export default function SessionList() {
  const navigate = useNavigate();
  const { auth } = useAuth();
  const { addToast } = useToast();
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [toolFilter, setToolFilter] = useState('all');
  const [dateRange, setDateRange] = useState('');
  const [sortBy, setSortBy] = useState<SortKey>('date');
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null);
  const [navFilter, setNavFilter] = useState<NavFilter>('all');
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [selectMode, setSelectMode] = useState(false);

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

  function clearSelection() {
    setSelectedIds(new Set());
    setSelectMode(false);
  }

  async function handleBulkDelete() {
    if (!confirm(`Delete ${selectedIds.size} session(s)? This cannot be undone.`)) return;

    const results = await Promise.all(
      Array.from(selectedIds).map(id =>
        auth!.client.deleteSession(id).then(() => true).catch(() => false)
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

  // Handoffs inbox query
  const { data: handoffsData } = useQuery<HandoffListResponse>({
    queryKey: ['handoffs', 'inbox'],
    queryFn: () => auth!.client.listInbox(),
    enabled: !!auth,
    staleTime: 60_000,
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
      let list: SessionSummary[] = folderSessionsData.sessions;
      if (dateRange) {
        const now = Date.now();
        const ms = dateRange === '24h' ? 86400000 : dateRange === '7d' ? 604800000 : 2592000000;
        list = list.filter((s) => now - new Date(s.updated_at).getTime() < ms);
      }
      const sorted = [...list];
      if (sortBy === 'messages') sorted.sort((a, b) => b.message_count - a.message_count);
      else if (sortBy === 'tokens')
        sorted.sort(
          (a, b) =>
            b.total_input_tokens + b.total_output_tokens -
            (a.total_input_tokens + a.total_output_tokens),
        );
      else if (sortBy === 'title')
        sorted.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
      return sorted;
    }

    if (!data?.sessions) return [];
    let list = data.sessions;

    // Date range filter (client-side on current page)
    if (dateRange) {
      const now = Date.now();
      const ms = dateRange === '24h' ? 86400000 : dateRange === '7d' ? 604800000 : 2592000000;
      list = list.filter((s) => now - new Date(s.updated_at).getTime() < ms);
    }

    // Sort (client-side on current page -- server sorts by date by default)
    const sorted = [...list];
    if (sortBy === 'messages') sorted.sort((a, b) => b.message_count - a.message_count);
    else if (sortBy === 'tokens')
      sorted.sort(
        (a, b) =>
          b.total_input_tokens + b.total_output_tokens -
          (a.total_input_tokens + a.total_output_tokens),
      );
    else if (sortBy === 'title')
      sorted.sort((a, b) => (a.title || '').localeCompare(b.title || ''));

    return sorted;
  }, [data, dateRange, sortBy, selectedFolderId, folderSessionsData]);

  // Clear selection when filters change
  useEffect(() => {
    setSelectedIds(new Set());
  }, [toolFilter, dateRange, sortBy, selectedFolderId, navFilter, page]);

  // Most recent session (for hero + repo detection)
  const mostRecent = useMemo(() => {
    if (!data?.sessions || data.sessions.length === 0) return null;
    return [...data.sessions].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    )[0];
  }, [data]);

  // Clear folder selection when switching to "all"
  useEffect(() => {
    if (navFilter === 'all') setSelectedFolderId(null);
  }, [navFilter]);

  // Auto-expand group if the most recent session is a child
  useEffect(() => {
    if (!data?.sessions || data.sessions.length === 0) return;
    const sorted = [...data.sessions].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    );
    const mostRecentId = sorted[0]?.id;
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

  // Date grouping
  const grouped = useMemo(() => {
    const today: SessionSummary[] = [];
    const thisWeek: SessionSummary[] = [];
    const earlier: SessionSummary[] = [];

    for (const s of filteredSessions) {
      if (isToday(s.updated_at)) today.push(s);
      else if (isThisWeek(s.updated_at)) thisWeek.push(s);
      else earlier.push(s);
    }

    return { today, thisWeek, earlier };
  }, [filteredSessions]);

  const hasMore = selectedFolderId ? false : (data?.has_more ?? false);
  // totalSessions for analytics = current view count
  const totalSessions = selectedFolderId ? (folderSessionsData?.total ?? 0) : (data?.total ?? 0);
  // allSessionsCount for sidebar "All Sessions" label = always the real total from API
  const allSessionsCount = data?.total ?? 0;

  // Insights strip data
  const insights = useMemo(() => {
    if (filteredSessions.length === 0) return null;
    const toolCounts: Record<string, number> = {};
    let totalTokensToday = 0;
    const hourCounts = new Array(24).fill(0);

    for (const s of filteredSessions) {
      toolCounts[s.source_tool] = (toolCounts[s.source_tool] || 0) + 1;
      if (isToday(s.created_at)) {
        totalTokensToday += s.total_input_tokens + s.total_output_tokens;
      }
      const h = new Date(s.created_at).getHours();
      hourCounts[h]++;
    }

    const total = filteredSessions.length;
    const sortedTools = Object.entries(toolCounts).sort((a, b) => b[1] - a[1]).slice(0, 3);
    const toolMix = sortedTools.map(([tool, count]) =>
      `${fullToolName(tool)} ${Math.round((count / total) * 100)}%`
    ).join(' / ');

    let peakHour = 0;
    let peakCount = 0;
    for (let h = 0; h < 24; h++) {
      const windowCount = hourCounts[h] + hourCounts[(h + 1) % 24];
      if (windowCount > peakCount) { peakCount = windowCount; peakHour = h; }
    }
    const fmtHour = (h: number) => {
      const ampm = h >= 12 ? 'PM' : 'AM';
      return `${h % 12 || 12} ${ampm}`;
    };
    const peak = peakCount > 0 ? `${fmtHour(peakHour)}-${fmtHour((peakHour + 2) % 24)}` : null;

    return { toolMix, tokensToday: totalTokensToday, peak };
  }, [filteredSessions]);

  function handleRowClick(id: string) {
    navigate(`/sessions/${id}`);
  }

  function handleRowKeyDown(e: React.KeyboardEvent, id: string) {
    if (e.key === 'Enter') navigate(`/sessions/${id}`);
  }

  function handleResume(s: SessionSummary) {
    const tool = s.source_tool || 'claude-code';
    const resumeTool = CAPTURE_ONLY_TOOLS.has(tool) ? 'claude-code' : tool;
    navigator.clipboard.writeText(`sfs resume ${s.id} --in ${resumeTool}`);
    addToast('success', `Resume command copied${CAPTURE_ONLY_TOOLS.has(tool) ? ` (${tool} is capture-only, using claude-code)` : ''}`);
  }

  const latestHandoff = pendingHandoffs.length > 0 ? pendingHandoffs[0] : null;

  return (
    <div className="flex flex-1 min-h-0">
      <BookmarkSidebar
        selectedFilter={navFilter}
        onSelectFilter={(f) => { setNavFilter(f); setPage(1); }}
        totalCount={allSessionsCount}
        bookmarkedCount={totalBookmarked}
        inRepoCount={0}
        inRepoLabel={null}
        handoffCount={pendingHandoffs.length}
        selectedFolderId={selectedFolderId}
        onSelectFolder={(id) => { setSelectedFolderId(id); setPage(1); }}
      />
      <div className="flex-1 max-w-7xl mx-auto px-4 py-4 overflow-y-auto">

        {/* Mobile nav chips */}
        <MobileNavChips
          selectedFilter={navFilter}
          onSelectFilter={(f) => { setNavFilter(f); setPage(1); }}
          totalCount={allSessionsCount}
          bookmarkedCount={totalBookmarked}
          inRepoCount={0}
          inRepoLabel={null}
          handoffCount={pendingHandoffs.length}
          selectedFolderId={selectedFolderId}
          onSelectFolder={(id) => { setSelectedFolderId(id); setPage(1); }}
        />

        {/* ── Hero: Best next action ── */}
        {!isLoading && mostRecent && (
          <div className="bg-[var(--surface)] border border-[var(--border)] rounded-xl px-5 py-4 mb-5 shadow-[var(--shadow-sm)]">
            <div className="flex flex-col sm:flex-row sm:items-center gap-4">
              {/* Left: session to resume */}
              <div className="flex-1 min-w-0">
                <h2
                  className="text-xl font-semibold text-[var(--text-primary)] truncate cursor-pointer hover:text-[var(--brand)] transition-colors"
                  onClick={() => navigate(`/sessions/${mostRecent.id}`)}
                >
                  {mostRecent.title || 'Untitled session'}
                </h2>
                <div className="flex items-center gap-2 mt-1 text-sm text-[var(--text-tertiary)]">
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ backgroundColor: TOOL_COLORS[mostRecent.source_tool] || '#6B7280' }}
                  />
                  <span>{fullToolName(mostRecent.source_tool)}</span>
                  {mostRecent.model_id && mostRecent.model_id !== '<synthetic>' && (
                    <>
                      <span className="opacity-40">&middot;</span>
                      <span>{abbreviateModel(mostRecent.model_id)}</span>
                    </>
                  )}
                  <span className="opacity-40">&middot;</span>
                  <RelativeDate iso={mostRecent.updated_at} />
                </div>
                <div className="mt-3 flex items-center gap-3">
                  <button
                    onClick={() => handleResume(mostRecent)}
                    className="px-4 py-2 bg-[var(--brand)] text-white text-sm font-semibold rounded-lg hover:opacity-90 transition-opacity"
                  >
                    {CAPTURE_ONLY_TOOLS.has(mostRecent.source_tool) ? 'Resume in Claude Code' : 'Resume'}
                  </button>
                  <button
                    onClick={() => navigate(`/sessions/${mostRecent.id}`)}
                    className="px-4 py-2 border border-[var(--border)] text-sm font-medium text-[var(--text-secondary)] rounded-lg hover:bg-[var(--surface-hover)] transition-colors"
                  >
                    View
                  </button>
                </div>
              </div>

              {/* Right: compact status chips */}
              <div className="flex sm:flex-col items-start gap-2 shrink-0">
                {pendingHandoffs.length > 0 ? (
                  <Link
                    to={latestHandoff ? `/handoffs/${latestHandoff.id}` : '#'}
                    className="inline-flex items-center gap-2 px-3 py-1.5 text-sm font-medium text-[var(--brand)] bg-[var(--brand)]/10 rounded-lg hover:bg-[var(--brand)]/20 transition-colors"
                  >
                    <span className="w-2 h-2 rounded-full bg-[var(--brand)] animate-pulse" />
                    {pendingHandoffs.length} pending handoff{pendingHandoffs.length !== 1 ? 's' : ''}
                  </Link>
                ) : (
                  <span className="inline-flex items-center gap-2 px-3 py-1.5 text-sm text-[var(--text-tertiary)]">
                    No pending handoffs
                  </span>
                )}
                <Link
                  to="/projects"
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-[var(--text-tertiary)] hover:text-[var(--text-primary)] transition-colors"
                >
                  Projects <span aria-hidden="true">&rarr;</span>
                </Link>
              </div>
            </div>
          </div>
        )}

        {/* ── Filters row ── */}
        <div className="flex flex-wrap items-center gap-3 mb-4">
          <div className="relative">
            <select
              value={toolFilter}
              onChange={(e) => { setToolFilter(e.target.value); setPage(1); }}
              className="appearance-none bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 pr-8 text-[14px] font-medium text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)] cursor-pointer transition-colors"
            >
              {TOOLS.map((t) => (
                <option key={t} value={t}>{t === 'all' ? 'All Tools' : fullToolName(t)}</option>
              ))}
            </select>
            <svg className="absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--text-tertiary)] pointer-events-none" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </div>
          <div className="relative">
            <select
              value={dateRange}
              onChange={(e) => setDateRange(e.target.value)}
              className="appearance-none bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 pr-8 text-[14px] font-medium text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)] cursor-pointer transition-colors"
            >
              {DATE_RANGES.map((d) => (
                <option key={d.value} value={d.value}>{d.label}</option>
              ))}
            </select>
            <svg className="absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--text-tertiary)] pointer-events-none" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </div>
          <div className="relative">
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value as SortKey)}
              className="appearance-none bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 pr-8 text-[14px] font-medium text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)] cursor-pointer transition-colors"
            >
              <option value="date">Sort: Date</option>
              <option value="messages">Sort: Messages</option>
              <option value="tokens">Sort: Tokens</option>
              <option value="title">Sort: Title</option>
            </select>
            <svg className="absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--text-tertiary)] pointer-events-none" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </div>
          <button
            onClick={() => { if (selectMode) clearSelection(); else setSelectMode(true); }}
            className="text-[13px] text-[var(--text-tertiary)] hover:text-[var(--text-primary)] px-3 py-2 border border-[var(--border)] rounded-lg"
          >
            {selectMode ? 'Cancel' : 'Select'}
          </button>
          <button
            onClick={() => {
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
            }}
            className="text-[13px] text-[var(--text-tertiary)] hover:text-[var(--text-primary)] px-3 py-2 border border-[var(--border)] rounded-lg"
          >
            Find Duplicates
          </button>
        </div>

        {/* Error */}
        {error && (
          <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
            Failed to load sessions: {String(error)}
          </div>
        )}

        {/* Loading */}
        {isLoading && (
          <div className="space-y-3">
            {Array.from({length: 6}).map((_, i) => (
              <div key={i} className="bg-[var(--bg-elevated,var(--surface))] border border-[var(--border)] rounded-xl p-4 animate-pulse">
                <div className="flex items-center gap-3">
                  <div className="w-3 h-3 rounded-full bg-[var(--bg-tertiary)]" />
                  <div className="h-4 w-24 rounded bg-[var(--bg-tertiary)]" />
                  <div className="h-3 w-32 rounded bg-[var(--bg-tertiary)]" />
                  <div className="flex-1" />
                  <div className="h-3 w-16 rounded bg-[var(--bg-tertiary)]" />
                </div>
                <div className="mt-2 flex items-center gap-3 pl-6">
                  <div className="h-3 w-16 rounded bg-[var(--bg-tertiary)]" />
                  <div className="h-3 w-64 rounded bg-[var(--bg-tertiary)]" />
                </div>
              </div>
            ))}
          </div>
        )}

        {/* ── Session list with date grouping ── */}
        {!isLoading && filteredSessions.length > 0 && (
          <>
            <DateGroup label="Today" sessions={grouped.today} onRowClick={handleRowClick} onRowKeyDown={handleRowKeyDown} onResume={handleResume} expandedGroups={expandedGroups} onToggleGroup={toggleGroup} isFirst selectMode={selectMode} selectedIds={selectedIds} onToggleSelect={toggleSelect} isInFolder={!!selectedFolderId} />
            <DateGroup label="This Week" sessions={grouped.thisWeek} onRowClick={handleRowClick} onRowKeyDown={handleRowKeyDown} onResume={handleResume} expandedGroups={expandedGroups} onToggleGroup={toggleGroup} selectMode={selectMode} selectedIds={selectedIds} onToggleSelect={toggleSelect} isInFolder={!!selectedFolderId} />
            <DateGroup label="Earlier" sessions={grouped.earlier} onRowClick={handleRowClick} onRowKeyDown={handleRowKeyDown} onResume={handleResume} expandedGroups={expandedGroups} onToggleGroup={toggleGroup} selectMode={selectMode} selectedIds={selectedIds} onToggleSelect={toggleSelect} isInFolder={!!selectedFolderId} />

            {/* Pagination */}
            <div className="flex items-center justify-between mt-4 text-sm text-[var(--text-tertiary)]">
              <span>
                Page {page} -- {filteredSessions.length} sessions
                {totalSessions > 0 && ` (${totalSessions} total)`}
              </span>
              <div className="flex gap-2">
                {page > 1 && (
                  <button
                    onClick={() => setPage((p) => p - 1)}
                    className="px-3 py-1.5 bg-[var(--surface)] border border-[var(--border)] rounded-lg hover:border-[var(--border-strong)] transition-colors text-[var(--text-secondary)]"
                  >
                    Previous
                  </button>
                )}
                {hasMore && (
                  <button
                    onClick={() => setPage((p) => p + 1)}
                    className="px-3 py-1.5 bg-[var(--surface)] border border-[var(--border)] rounded-lg hover:border-[var(--border-strong)] transition-colors text-[var(--text-secondary)]"
                  >
                    Next
                  </button>
                )}
              </div>
            </div>

            {/* ── Insights strip ── */}
            {insights && (
              <div className="mt-4 py-6 border-t border-[var(--border)] text-[12px] text-[var(--text-tertiary)] flex flex-wrap items-center gap-1">
                {insights.toolMix && <><span>Tool Mix: {insights.toolMix}</span><span className="opacity-40">&middot;</span></>}
                <span>{formatTokens(insights.tokensToday)} tokens today</span>
                {insights.peak && <><span className="opacity-40">&middot;</span><span>Peak: {insights.peak}</span></>}
              </div>
            )}
          </>
        )}

        {/* Empty state */}
        {!isLoading && filteredSessions.length === 0 && !error && (
          <div className="text-center py-16">
            <p className="text-[var(--text-secondary)] mb-2">No sessions found</p>
            <p className="text-[var(--text-tertiary)] text-sm">
              {selectedFolderId
                ? 'No sessions bookmarked in this folder yet.'
                : <>Push sessions from the CLI: <code className="bg-[var(--bg-secondary)] px-1.5 py-0.5 rounded">sfs push &lt;id&gt;</code></>
              }
            </p>
          </div>
        )}
      </div>

      {/* ── Bulk selection toolbar ── */}
      {selectedIds.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-lg)] px-5 py-3 flex items-center gap-4">
          <span className="text-[14px] font-medium text-[var(--text-primary)]">
            {selectedIds.size} selected
          </span>
          <button onClick={selectAll} className="text-[13px] text-[var(--brand)] hover:underline">
            Select all
          </button>
          <button
            onClick={handleBulkDelete}
            className="px-4 py-2 bg-red-500 text-white rounded-lg text-[13px] font-semibold hover:bg-red-600 transition-colors"
          >
            Delete selected
          </button>
          <button onClick={clearSelection} className="text-[13px] text-[var(--text-tertiary)] hover:text-[var(--text-primary)]">
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

/* ── Date-grouped session section with lineage grouping ── */

function DateGroup({
  label,
  sessions,
  onRowClick,
  onRowKeyDown,
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
  onRowKeyDown: (e: React.KeyboardEvent, id: string) => void;
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
    <div className={`mb-5 ${isFirst ? 'mt-0' : 'mt-6'}`}>
      <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--text-tertiary)] pb-2 mb-3 border-b border-[var(--border)]">
        {label}
      </h3>
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
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    if (selectMode) {
                      onToggleSelect(group.root.id);
                    } else if (hasChildren) {
                      onToggleGroup(group.root.id);
                    } else {
                      onRowClick(group.root.id);
                    }
                  }
                }}
                onResume={() => onResume(group.root)}
                onNavigate={hasChildren ? () => onRowClick(group.root.id) : undefined}
                hasChildren={hasChildren}
                isBookmarked={!!isInFolder}
                selectMode={selectMode}
                selected={selectedIds.has(group.root.id)}
                onToggleSelect={() => onToggleSelect(group.root.id)}
                lineageBadge={hasChildren ? (
                  <button
                    onClick={(e) => { e.stopPropagation(); onToggleGroup(group.root.id); }}
                    className="flex items-center gap-1.5 px-2 py-1 rounded-md text-[13px] font-medium text-[var(--brand)] hover:bg-[var(--brand)]/10 transition-colors"
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
                  </button>
                ) : undefined}
              />
              {hasChildren && isExpanded && (
                <div className="flex flex-col gap-1 mt-1 ml-6 border-l-2 border-[var(--brand)]/30 pl-3 space-y-1">
                  {group.children.map((child) => (
                    <SessionRow
                      key={child.id}
                      session={child}
                      onClick={() => selectMode ? onToggleSelect(child.id) : onRowClick(child.id)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          if (selectMode) onToggleSelect(child.id);
                          else onRowKeyDown(e, child.id);
                        }
                      }}
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

/* ── Individual session row with hover actions ── */

function SessionRow({
  session: s,
  onClick,
  onKeyDown,
  onResume,
  onNavigate,
  lineageBadge,
  isChild,
  hasChildren,
  isBookmarked,
  selectMode,
  selected,
  onToggleSelect,
}: {
  session: SessionSummary;
  onClick: () => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
  onResume: () => void;
  onNavigate?: () => void;
  lineageBadge?: React.ReactNode;
  isChild?: boolean;
  hasChildren?: boolean;
  isBookmarked?: boolean;
  selectMode?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}) {
  return (
    <div
      role="button"
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === ' ') { e.preventDefault(); onClick(); } else { onKeyDown(e); } }}
      tabIndex={0}
      className={`group bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg cursor-pointer hover:bg-[var(--surface-hover)] hover:shadow-[var(--shadow-sm)] transition-all duration-150 focus:bg-[var(--surface-hover)] outline-none focus:ring-1 focus:ring-[var(--brand)] ${isChild ? 'px-3 py-3' : 'px-4 py-4'} ${hasChildren ? 'border-l-[3px] border-l-[var(--brand)]/40' : ''} ${selected ? 'ring-1 ring-[var(--brand)] bg-[var(--brand)]/5' : ''}`}
    >
      {/* Line 1: Title + hover actions + timestamp */}
      <div className="flex items-center gap-3 mb-1">
        {selectMode && (
          <input
            type="checkbox"
            checked={selected || false}
            onChange={() => onToggleSelect?.()}
            className="w-4 h-4 rounded border-[var(--border)] accent-[var(--brand)] mr-3 shrink-0"
            onClick={(e) => e.stopPropagation()}
          />
        )}
        <span
          className={`font-semibold text-[var(--text-primary)] truncate flex-1 ${isChild ? 'text-[14px]' : 'text-base'}`}
          onClick={onNavigate ? (e) => { e.stopPropagation(); onNavigate(); } : undefined}
          style={onNavigate ? { cursor: 'pointer' } : undefined}
        >
          {isChild && <span className="text-[var(--text-tertiary)] mr-1">&#8627;</span>}
          {s.title || <span className="text-[var(--text-tertiary)] italic">Untitled session</span>}
          {s.parent_session_id && !isChild && (
            <span className="text-[var(--text-tertiary)] text-xs ml-1.5">&crarr; fork</span>
          )}
        </span>
        <div className="ml-auto flex items-center gap-2 shrink-0">
          {/* Row actions */}
          <div className="flex items-center gap-1.5">
            {onNavigate && (
              <button
                onClick={(e) => { e.stopPropagation(); onNavigate(); }}
                className="px-2 py-0.5 text-xs font-medium text-[var(--text-secondary)] bg-[var(--surface)] border border-[var(--border)] rounded hover:bg-[var(--surface-hover)] transition-colors opacity-0 group-hover:opacity-100"
              >
                View
              </button>
            )}
            <button
              onClick={(e) => { e.stopPropagation(); onResume(); }}
              className="px-2.5 py-1 text-xs font-semibold text-white bg-[var(--brand)] rounded-md hover:opacity-90 transition-opacity opacity-0 group-hover:opacity-100 group-focus:opacity-100"
            >
              Resume
            </button>
          </div>
          {lineageBadge}
          <TrustBadge score={(s as SessionSummaryWithAudit).audit_trust_score} />
          <span className={`text-[var(--text-tertiary)] ${isChild ? 'text-[12px]' : 'text-[13px]'}`}>
            <RelativeDate iso={s.updated_at} />
          </span>
          <div onClick={(e) => e.stopPropagation()}>
            <BookmarkDropdown sessionId={s.id} isBookmarked={isBookmarked} />
          </div>
        </div>
      </div>
      {/* Line 2: Tool dot + tool name + metadata */}
      <div className={`flex items-center gap-2 ${isChild ? 'text-[11px]' : 'text-[12px]'} text-[var(--text-tertiary)] opacity-70`}>
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{ backgroundColor: TOOL_COLORS[s.source_tool] || '#6B7280' }}
        />
        <span className="font-medium whitespace-nowrap">{fullToolName(s.source_tool)}</span>
        <span className="opacity-40">&middot;</span>
        <span className="font-mono">{s.id.slice(0, 12)}</span>
        <span className="opacity-40">&middot;</span>
        <span className="tabular-nums">{s.message_count} msgs</span>
        {!isChild && (s.total_input_tokens + s.total_output_tokens) > 0 && (
          <>
            <span className="opacity-40">&middot;</span>
            <span className="tabular-nums">{formatTokens(s.total_input_tokens + s.total_output_tokens)}</span>
          </>
        )}
        {s.model_id && s.model_id !== '<synthetic>' && (
          <>
            <span className="opacity-40">&middot;</span>
            <span>{abbreviateModel(s.model_id)}</span>
          </>
        )}
      </div>
    </div>
  );
}

/* ── Bookmark dropdown (unchanged logic) ── */

function BookmarkDropdown({ sessionId, isBookmarked: initialBookmarked }: { sessionId: string; isBookmarked?: boolean }) {
  const [open, setOpen] = useState(false);
  const [popping, setPopping] = useState(false);
  const [bookmarked, setBookmarked] = useState(initialBookmarked ?? false);
  const ref = useRef<HTMLDivElement>(null);
  const { data: foldersData } = useFolders();
  const addBookmark = useAddBookmark();

  const folders = foldersData?.folders ?? [];

  // Update when prop changes
  useEffect(() => {
    if (initialBookmarked !== undefined) setBookmarked(initialBookmarked);
  }, [initialBookmarked]);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  function handleIconClick() {
    setPopping(true);
    setTimeout(() => setPopping(false), 200);
    setOpen(!open);
  }

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={handleIconClick}
        className={`transition-colors text-sm p-1 rounded hover:bg-[var(--surface-hover)] ${bookmarked ? 'text-[var(--brand)]' : 'text-[var(--text-tertiary)] hover:text-[var(--brand)]'} ${popping ? 'animate-[bookmark-pop_200ms_ease-out]' : ''}`}
        title="Bookmark"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill={bookmarked ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />
        </svg>
      </button>
      {open && (
        <div className="absolute right-0 top-8 z-20 bg-[var(--bg-elevated,var(--surface))] border border-[var(--border)] rounded-lg shadow-[var(--shadow-md)] py-1 min-w-[140px]">
          {folders.length === 0 && (
            <div className="px-3 py-1.5 text-sm text-[var(--text-tertiary)]">No folders yet</div>
          )}
          {folders.map((f) => (
            <button
              key={f.id}
              onClick={() => {
                addBookmark.mutate({ folderId: f.id, sessionId }, {
                  onSuccess: () => { setOpen(false); setBookmarked(true); },
                  onError: () => setOpen(false),
                });
              }}
              className="w-full text-left px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] flex items-center gap-2 transition-colors"
            >
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{ backgroundColor: f.color || '#4f9cf7' }}
              />
              {f.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function TrustBadge({ score }: { score?: number | null }) {
  if (score == null) return null;
  const pct = Math.round(score * 100);
  const color =
    pct >= 90
      ? 'bg-green-500'
      : pct >= 70
        ? 'bg-yellow-500'
        : 'bg-red-500';
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full shrink-0 ${color}`}
      title={`Trust score: ${pct}%`}
    />
  );
}
