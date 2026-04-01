import { useState, useMemo, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useSessions } from '../hooks/useSessions';
import { useFolders, useAddBookmark, useFolderSessions } from '../hooks/useBookmarks';
import type { SessionSummary } from '../api/client';
import { abbreviateModel, fullToolName } from '../utils/models';
import { formatTokens } from '../utils/tokens';
import RelativeDate from '../components/RelativeDate';
import BookmarkSidebar from '../components/BookmarkSidebar';

const TOOL_COLORS: Record<string, string> = {
  'claude-code': 'var(--tool-claude)',
  'cursor': 'var(--tool-cursor)',
  'codex': 'var(--tool-codex)',
  'gemini': 'var(--tool-gemini)',
  'gemini-cli': 'var(--tool-gemini)',
  'copilot': 'var(--tool-copilot)',
  'copilot-cli': 'var(--tool-copilot)',
  'amp': 'var(--tool-amp)',
  'cline': 'var(--tool-cline)',
  'roo-code': 'var(--tool-roo)',
};

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

export default function SessionList() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [toolFilter, setToolFilter] = useState('all');
  const [dateRange, setDateRange] = useState('');
  const [sortBy, setSortBy] = useState<SortKey>('date');
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      const mod = navigator.platform.includes('Mac') ? e.metaKey : e.ctrlKey;
      if (mod && e.key === 'k') {
        e.preventDefault();
        searchRef.current?.focus();
      }
    }
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  const PAGE_SIZE = 20;

  const { data, isLoading, error } = useSessions({
    page,
    page_size: PAGE_SIZE,
    source_tool: toolFilter === 'all' ? undefined : toolFilter,
  });

  const { data: folderSessionsData } = useFolderSessions(selectedFolderId);

  const sessions = useMemo(() => {
    // If a folder is selected, show folder sessions instead
    if (selectedFolderId && folderSessionsData) {
      let list: SessionSummary[] = folderSessionsData.sessions;
      if (dateRange) {
        const now = Date.now();
        const ms = dateRange === '24h' ? 86400000 : dateRange === '7d' ? 604800000 : 2592000000;
        list = list.filter((s) => now - new Date(s.updated_at).getTime() < ms);
      }
      if (searchQuery) {
        const q = searchQuery.toLowerCase();
        list = list.filter(
          (s) =>
            (s.title || '').toLowerCase().includes(q) ||
            s.id.toLowerCase().includes(q) ||
            (s.alias || '').toLowerCase().includes(q),
        );
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

    // Search filter (client-side)
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      list = list.filter(
        (s) =>
          (s.title || '').toLowerCase().includes(q) ||
          s.id.toLowerCase().includes(q) ||
          (s.alias || '').toLowerCase().includes(q),
      );
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
  }, [data, dateRange, sortBy, selectedFolderId, folderSessionsData, searchQuery]);

  const hasMore = selectedFolderId ? false : (data?.has_more ?? false);
  const totalSessions = selectedFolderId ? (folderSessionsData?.total ?? 0) : (data?.total ?? 0);

  function handleRowClick(id: string) {
    navigate(`/sessions/${id}`);
  }

  function handleRowKeyDown(e: React.KeyboardEvent, id: string) {
    if (e.key === 'Enter') navigate(`/sessions/${id}`);
  }

  return (
    <div className="flex flex-1 min-h-0">
      <BookmarkSidebar selectedFolderId={selectedFolderId} onSelectFolder={(id) => { setSelectedFolderId(id); setPage(1); }} />
      <div className="flex-1 max-w-7xl mx-auto px-4 py-4">
      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <input
            ref={searchRef}
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search sessions..."
            className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 pr-16 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-tertiary)] focus:outline-none focus:border-[var(--brand)] transition-colors"
          />
          <span className="absolute right-8 top-1/2 -translate-y-1/2 flex items-center gap-0.5 pointer-events-none">
            <kbd className="font-mono text-[11px] px-1.5 py-0.5 rounded border border-[var(--border)] bg-[var(--bg-tertiary)] text-[var(--text-tertiary)]">
              {typeof navigator !== 'undefined' && navigator.platform.includes('Mac') ? '\u2318' : 'Ctrl'}
            </kbd>
            <kbd className="font-mono text-[11px] px-1.5 py-0.5 rounded border border-[var(--border)] bg-[var(--bg-tertiary)] text-[var(--text-tertiary)]">K</kbd>
          </span>
          <svg className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-tertiary)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
        </div>
        <div className="relative">
          <select
            value={toolFilter}
            onChange={(e) => { setToolFilter(e.target.value); setPage(1); }}
            className="appearance-none bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 pr-8 text-sm text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)] cursor-pointer transition-colors"
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
            className="appearance-none bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 pr-8 text-sm text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)] cursor-pointer transition-colors"
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
            className="appearance-none bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 pr-8 text-sm text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)] cursor-pointer transition-colors"
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
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
          Failed to load sessions: {String(error)}
        </div>
      )}

      {/* Analytics Cards */}
      {!isLoading && sessions.length > 0 && <AnalyticsCards sessions={sessions} />}

      {/* Loading */}
      {isLoading && (
        <div className="space-y-3">
          {Array.from({length: 6}).map((_, i) => (
            <div key={i} className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-4 animate-pulse">
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

      {/* Session rows */}
      {!isLoading && sessions.length > 0 && (
        <>
          <div className="flex flex-col gap-1.5">
            {sessions.map((s) => (
              <div
                key={s.id}
                onClick={() => handleRowClick(s.id)}
                onKeyDown={(e) => handleRowKeyDown(e, s.id)}
                tabIndex={0}
                className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-lg px-4 py-3 cursor-pointer hover:bg-[var(--surface-hover)] hover:shadow-[var(--shadow-sm)] transition-all duration-150 focus:bg-[var(--surface-hover)] outline-none focus:ring-1 focus:ring-[var(--brand)]"
              >
                {/* Line 1: Tool, ID, message count, tokens, date */}
                <div className="flex items-center gap-3 mb-1">
                  <div className="flex items-center gap-2 shrink-0">
                    <span
                      className="w-2.5 h-2.5 rounded-full shrink-0"
                      style={{ backgroundColor: TOOL_COLORS[s.source_tool] || '#6B7280' }}
                    />
                    <span className="text-sm font-medium text-[var(--text-primary)] whitespace-nowrap">
                      {fullToolName(s.source_tool)}
                    </span>
                  </div>
                  <span className="text-xs font-mono text-[var(--text-tertiary)]">
                    {s.id.slice(0, 12)}
                  </span>
                  <div className="flex items-center gap-1 text-xs text-[var(--text-tertiary)]">
                    <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                    </svg>
                    <span className="tabular-nums">{s.message_count}</span>
                  </div>
                  <div className="flex items-center gap-1 text-xs text-[var(--text-tertiary)]">
                    <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <circle cx="12" cy="12" r="10" />
                      <path d="M12 6v6l4 2" />
                    </svg>
                    <span className="tabular-nums">{formatTokens(s.total_input_tokens + s.total_output_tokens)}</span>
                  </div>
                  <div className="ml-auto flex items-center gap-2">
                    <TrustBadge score={(s as SessionSummaryWithAudit).audit_trust_score} />
                    <span className="text-xs text-[var(--text-tertiary)]">
                      <RelativeDate iso={s.updated_at} />
                    </span>
                  </div>
                </div>
                {/* Line 2: Model, title, bookmark */}
                <div className="flex items-center gap-3">
                  <span className="text-xs text-[var(--text-tertiary)] shrink-0">
                    {abbreviateModel(s.model_id)}
                  </span>
                  <span className="text-sm text-[var(--text-primary)] truncate flex-1">
                    {s.title || <span className="text-[var(--text-tertiary)] italic">Untitled</span>}
                    {s.parent_session_id && (
                      <span className="text-[var(--text-tertiary)] text-xs ml-1.5">&crarr; fork</span>
                    )}
                  </span>
                  <div onClick={(e) => e.stopPropagation()} className="shrink-0">
                    <BookmarkDropdown sessionId={s.id} />
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Pagination */}
          <div className="flex items-center justify-between mt-4 text-sm text-[var(--text-tertiary)]">
            <span>
              Page {page} -- {sessions.length} sessions
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
        </>
      )}

      {/* Empty state */}
      {!isLoading && sessions.length === 0 && !error && (
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
    </div>
  );
}

function BookmarkDropdown({ sessionId }: { sessionId: string }) {
  const [open, setOpen] = useState(false);
  const [popping, setPopping] = useState(false);
  const [bookmarked, setBookmarked] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { data: foldersData } = useFolders();
  const addBookmark = useAddBookmark();

  const folders = foldersData?.folders ?? [];

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
        <div className="absolute right-0 top-8 z-20 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-lg shadow-[var(--shadow-md)] py-1 min-w-[140px]">
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

function AnalyticsCards({ sessions }: { sessions: SessionSummary[] }) {
  const stats = useMemo(() => {
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const yesterdayStart = todayStart - 86400000;

    // Sessions today
    const sessionsToday = sessions.filter(
      (s) => new Date(s.created_at).getTime() >= todayStart,
    ).length;
    const sessionsYesterday = sessions.filter((s) => {
      const t = new Date(s.created_at).getTime();
      return t >= yesterdayStart && t < todayStart;
    }).length;

    // Tool breakdown
    const toolCounts: Record<string, number> = {};
    for (const s of sessions) {
      toolCounts[s.source_tool] = (toolCounts[s.source_tool] || 0) + 1;
    }
    const sortedTools = Object.entries(toolCounts).sort((a, b) => b[1] - a[1]);
    const topTools = sortedTools.slice(0, 3);
    const otherCount = sortedTools.slice(3).reduce((sum, [, c]) => sum + c, 0);
    if (otherCount > 0) topTools.push(['Other', otherCount]);
    const totalForBar = sessions.length;

    // Total tokens
    const totalTokens = sessions.reduce(
      (sum, s) => sum + s.total_input_tokens + s.total_output_tokens,
      0,
    );

    // Active hours -- find the peak hour
    const hourCounts = new Array(24).fill(0);
    for (const s of sessions) {
      const h = new Date(s.created_at).getHours();
      hourCounts[h]++;
    }
    let peakHour = 0;
    let peakCount = 0;
    for (let h = 0; h < 24; h++) {
      const windowCount = hourCounts[h] + hourCounts[(h + 1) % 24];
      if (windowCount > peakCount) {
        peakCount = windowCount;
        peakHour = h;
      }
    }
    const fmtHour = (h: number) => {
      const ampm = h >= 12 ? 'PM' : 'AM';
      const h12 = h % 12 || 12;
      return `${h12} ${ampm}`;
    };
    const peakLabel = peakCount > 0
      ? `${fmtHour(peakHour)}-${fmtHour((peakHour + 2) % 24)}`
      : 'N/A';

    return {
      sessionsToday,
      sessionsYesterday,
      topTools,
      totalForBar,
      totalTokens,
      peakLabel,
    };
  }, [sessions]);

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
      {/* Sessions Today */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-4 shadow-[var(--shadow-sm)] hover:shadow-[var(--shadow-md)] transition-shadow duration-150">
        <div className="text-2xl font-bold text-[var(--text-primary)] tabular-nums">{stats.sessionsToday}</div>
        <div className="text-xs text-[var(--text-tertiary)] uppercase tracking-wide mt-1">Sessions Today</div>
        {stats.sessionsYesterday > 0 && (
          <div className="text-xs text-[var(--text-tertiary)] mt-1">
            +{stats.sessionsYesterday} yesterday
          </div>
        )}
      </div>

      {/* Tool Breakdown */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-4 shadow-[var(--shadow-sm)] hover:shadow-[var(--shadow-md)] transition-shadow duration-150">
        <div className="text-xs text-[var(--text-tertiary)] uppercase tracking-wide mb-2">Tool Breakdown</div>
        {stats.totalForBar > 0 && (
          <>
            <div className="flex h-2 rounded-full overflow-hidden mb-2">
              {stats.topTools.map(([tool, count]) => (
                <div
                  key={tool}
                  style={{
                    width: `${(count / stats.totalForBar) * 100}%`,
                    backgroundColor: TOOL_COLORS[tool] || '#6B7280',
                  }}
                  title={`${fullToolName(tool)}: ${count}`}
                />
              ))}
            </div>
            <div className="space-y-0.5">
              {stats.topTools.map(([tool, count]) => (
                <div key={tool} className="flex items-center gap-1.5 text-xs">
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ backgroundColor: TOOL_COLORS[tool] || '#6B7280' }}
                  />
                  <span className="text-[var(--text-secondary)] truncate">{fullToolName(tool)}</span>
                  <span className="text-[var(--text-tertiary)] ml-auto tabular-nums">{count}</span>
                </div>
              ))}
            </div>
          </>
        )}
        {stats.totalForBar === 0 && (
          <div className="text-sm text-[var(--text-tertiary)]">No data</div>
        )}
      </div>

      {/* Total Tokens */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-4 shadow-[var(--shadow-sm)] hover:shadow-[var(--shadow-md)] transition-shadow duration-150">
        <div className="text-2xl font-bold text-[var(--text-primary)] tabular-nums">
          {formatTokens(stats.totalTokens)}
        </div>
        <div className="text-xs text-[var(--text-tertiary)] uppercase tracking-wide mt-1">Total Tokens</div>
      </div>

      {/* Active Hours */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-4 shadow-[var(--shadow-sm)] hover:shadow-[var(--shadow-md)] transition-shadow duration-150">
        <div className="text-2xl font-bold text-[var(--text-primary)]">{stats.peakLabel}</div>
        <div className="text-xs text-[var(--text-tertiary)] uppercase tracking-wide mt-1">Peak Hours</div>
      </div>
    </div>
  );
}
