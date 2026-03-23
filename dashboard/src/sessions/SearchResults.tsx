import { useState, useEffect } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useSearch, type SearchResult } from '../hooks/useSearch';
import { abbreviateTool } from '../utils/models';
import { renderSnippet } from '../utils/highlight';
import RelativeDate from '../components/RelativeDate';

const TOOLS = [
  { label: 'All Tools', value: '' },
  { label: 'Claude Code', value: 'claude-code' },
  { label: 'Codex', value: 'codex' },
  { label: 'Cursor', value: 'cursor' },
] as const;

const DATE_RANGES = [
  { label: 'All time', value: 0 },
  { label: 'Last 7 days', value: 7 },
  { label: 'Last 30 days', value: 30 },
  { label: 'Last 90 days', value: 90 },
] as const;

export default function SearchResults() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryParam = searchParams.get('q') || '';

  const [tool, setTool] = useState('');
  const [days, setDays] = useState(0);
  const [page, setPage] = useState(1);

  // Reset page when filters change
  useEffect(() => { setPage(1); }, [tool, days, queryParam]);

  const { data, isLoading, error } = useSearch(queryParam, {
    tool: tool || undefined,
    days: days || undefined,
    page,
  });

  const errObj = error as unknown as Record<string, unknown> | null;
  const isPaywalled = errObj && typeof errObj === 'object' && 'status' in errObj && errObj.status === 403;

  function handleResultClick(r: SearchResult) {
    navigate(`/sessions/${r.session_id}`);
  }

  const totalPages = data ? Math.ceil(data.total / data.page_size) : 0;

  return (
    <div className="max-w-7xl mx-auto px-4 py-4 flex gap-6">
      {/* Filters sidebar */}
      <aside className="w-48 shrink-0 hidden md:block">
        <h3 className="text-xs uppercase tracking-wider text-text-muted mb-2">Filters</h3>

        <label className="block text-xs text-text-secondary mb-1 mt-3">Tool</label>
        <select
          value={tool}
          onChange={(e) => setTool(e.target.value)}
          className="w-full px-2 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-secondary focus:outline-none"
        >
          {TOOLS.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>

        <label className="block text-xs text-text-secondary mb-1 mt-3">Date range</label>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="w-full px-2 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-secondary focus:outline-none"
        >
          {DATE_RANGES.map((d) => (
            <option key={d.value} value={d.value}>{d.label}</option>
          ))}
        </select>
      </aside>

      {/* Results */}
      <div className="flex-1 min-w-0">
        <div className="mb-4">
          <h2 className="text-sm text-text-secondary">
            {data ? (
              <>
                <span className="text-text-primary font-medium">{data.total}</span>{' '}
                {data.total === 1 ? 'result' : 'results'} for{' '}
                <span className="text-text-primary font-medium">"{data.query}"</span>
              </>
            ) : isLoading ? (
              'Searching...'
            ) : null}
          </h2>
        </div>

        {/* Error: paywall */}
        {isPaywalled && (
          <div className="p-6 bg-bg-secondary border border-border rounded-lg text-center">
            <p className="text-text-secondary mb-2">Search requires Pro.</p>
            <a
              href="https://sessionfs.dev"
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent hover:underline text-sm"
            >
              Upgrade at sessionfs.dev
            </a>
          </div>
        )}

        {/* Error: other */}
        {error && !isPaywalled && (
          <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
            Search failed: {String(error)}
          </div>
        )}

        {/* Loading */}
        {isLoading && (
          <div className="text-center py-12 text-text-muted">Searching...</div>
        )}

        {/* Empty state */}
        {!isLoading && data && data.results.length === 0 && !error && (
          <div className="text-center py-16">
            <p className="text-text-secondary">No sessions match your search</p>
          </div>
        )}

        {/* Results list */}
        {data && data.results.length > 0 && (
          <div className="space-y-2">
            {data.results.map((r) => (
              <button
                key={r.session_id}
                onClick={() => handleResultClick(r)}
                className="w-full text-left p-3 bg-bg-secondary border border-border rounded-lg hover:border-text-muted transition-colors"
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-text-primary truncate flex-1">
                    {r.title || 'Untitled session'}
                  </span>
                  <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-accent/15 text-accent shrink-0">
                    {abbreviateTool(r.source_tool)}
                  </span>
                  <span className="text-xs text-text-muted shrink-0">
                    {r.message_count} msgs
                  </span>
                  <span className="text-xs text-text-muted shrink-0">
                    <RelativeDate iso={r.updated_at} />
                  </span>
                </div>
                {r.matches.slice(0, 3).map((m, i) => (
                  <p
                    key={i}
                    className="text-xs text-text-secondary mt-1 truncate [&_mark]:bg-accent/20 [&_mark]:text-accent [&_mark]:px-0.5 [&_mark]:rounded"
                    dangerouslySetInnerHTML={{ __html: renderSnippet(m.snippet) }}
                  />
                ))}
              </button>
            ))}
          </div>
        )}

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-center gap-2 mt-4">
            <button
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
              className="px-3 py-1 text-xs bg-bg-secondary border border-border rounded hover:border-text-muted transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Previous
            </button>
            <span className="text-xs text-text-muted">
              Page {page} of {totalPages}
            </span>
            <button
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1 text-xs bg-bg-secondary border border-border rounded hover:border-text-muted transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
