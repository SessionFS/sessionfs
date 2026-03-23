import { useState, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useSessions } from '../hooks/useSessions';
import { abbreviateModel, abbreviateTool } from '../utils/models';
import { formatTokens } from '../utils/tokens';
import RelativeDate from '../components/RelativeDate';

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

  const { data, isLoading, error } = useSessions({
    page,
    page_size: 100,
    source_tool: toolFilter === 'all' ? undefined : toolFilter,
  });

  const sessions = useMemo(() => {
    if (!data?.sessions) return [];
    let list = data.sessions;

    // Date range filter
    if (dateRange) {
      const now = Date.now();
      const ms = dateRange === '24h' ? 86400000 : dateRange === '7d' ? 604800000 : 2592000000;
      list = list.filter((s) => now - new Date(s.updated_at).getTime() < ms);
    }

    // Sort
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
    // 'date' is default from server (already sorted)

    return sorted;
  }, [data, dateRange, sortBy]);

  const PAGE_SIZE = 20;
  const paged = sessions.slice(0, PAGE_SIZE * page);
  const hasMore = sessions.length > paged.length;

  function handleRowClick(id: string) {
    navigate(`/sessions/${id}`);
  }

  function handleRowKeyDown(e: React.KeyboardEvent, id: string) {
    if (e.key === 'Enter') navigate(`/sessions/${id}`);
  }

  return (
    <div className="max-w-7xl mx-auto px-4 py-4">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <select
          value={toolFilter}
          onChange={(e) => { setToolFilter(e.target.value); setPage(1); }}
          className="px-2 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-secondary focus:outline-none"
        >
          {TOOLS.map((t) => (
            <option key={t} value={t}>{t === 'all' ? 'All Tools' : t}</option>
          ))}
        </select>
        <select
          value={dateRange}
          onChange={(e) => setDateRange(e.target.value)}
          className="px-2 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-secondary focus:outline-none"
        >
          {DATE_RANGES.map((d) => (
            <option key={d.value} value={d.value}>{d.label}</option>
          ))}
        </select>
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as SortKey)}
          className="px-2 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-secondary focus:outline-none"
        >
          <option value="date">Sort: Date</option>
          <option value="messages">Sort: Messages</option>
          <option value="tokens">Sort: Tokens</option>
          <option value="title">Sort: Title</option>
        </select>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
          Failed to load sessions: {String(error)}
        </div>
      )}

      {/* Loading */}
      {isLoading && (
        <div className="text-center py-12 text-text-muted">Loading sessions...</div>
      )}

      {/* Table */}
      {!isLoading && sessions.length > 0 && (
        <>
          <div className="border border-border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-bg-secondary text-text-secondary text-xs uppercase tracking-wider">
                  <th className="px-3 py-2 text-left w-20">ID</th>
                  <th className="px-3 py-2 text-left w-10">Src</th>
                  <th className="px-3 py-2 text-left w-24">Model</th>
                  <th className="px-3 py-2 text-right w-14">Msgs</th>
                  <th className="px-3 py-2 text-right w-16">Tokens</th>
                  <th className="px-3 py-2 text-left w-20">Updated</th>
                  <th className="px-3 py-2 text-left">Title</th>
                </tr>
              </thead>
              <tbody>
                {paged.map((s) => (
                  <tr
                    key={s.id}
                    onClick={() => handleRowClick(s.id)}
                    onKeyDown={(e) => handleRowKeyDown(e, s.id)}
                    tabIndex={0}
                    className="border-t border-border hover:bg-bg-tertiary cursor-pointer transition-colors focus:bg-bg-tertiary outline-none"
                  >
                    <td className="px-3 py-2 font-mono text-accent text-xs">{s.id.slice(4, 12)}</td>
                    <td className="px-3 py-2 text-text-muted text-xs">{abbreviateTool(s.source_tool)}</td>
                    <td className="px-3 py-2 text-text-secondary text-xs">{abbreviateModel(s.model_id)}</td>
                    <td className="px-3 py-2 text-right text-text-secondary tabular-nums">{s.message_count}</td>
                    <td className="px-3 py-2 text-right text-text-secondary tabular-nums">
                      {formatTokens(s.total_input_tokens + s.total_output_tokens)}
                    </td>
                    <td className="px-3 py-2 text-text-muted text-xs">
                      <RelativeDate iso={s.updated_at} />
                    </td>
                    <td className="px-3 py-2 text-text-primary truncate max-w-xs">
                      {s.title || <span className="text-text-muted italic">Untitled</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          <div className="flex items-center justify-between mt-3 text-xs text-text-muted">
            <span>
              Showing {paged.length} of {sessions.length} sessions
              {data && data.total > sessions.length && ` (${data.total} total on server)`}
            </span>
            {hasMore && (
              <button
                onClick={() => setPage((p) => p + 1)}
                className="px-3 py-1 bg-bg-secondary border border-border rounded hover:border-text-muted transition-colors"
              >
                Load more
              </button>
            )}
          </div>
        </>
      )}

      {/* Empty state */}
      {!isLoading && sessions.length === 0 && !error && (
        <div className="text-center py-16">
          <p className="text-text-secondary mb-2">No sessions found</p>
          <p className="text-text-muted text-sm">
            Push sessions from the CLI: <code className="bg-bg-secondary px-1.5 py-0.5 rounded">sfs push &lt;id&gt;</code>
          </p>
        </div>
      )}
    </div>
  );
}
