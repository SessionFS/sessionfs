import { useEffect, useRef, useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useSearch, type SearchResult } from '../hooks/useSearch';
import { abbreviateTool } from '../utils/models';
import { renderSnippet } from '../utils/highlight';
import RelativeDate from '../components/RelativeDate';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { Select } from '../components/ui';

const TOOLS = [
  { label: 'All Tools', value: '' },
  { label: 'Claude Code', value: 'claude-code' },
  { label: 'Codex', value: 'codex' },
  { label: 'Gemini CLI', value: 'gemini' },
  { label: 'Copilot', value: 'copilot' },
  { label: 'Cursor', value: 'cursor' },
  { label: 'Amp', value: 'amp' },
  { label: 'Cline', value: 'cline' },
  { label: 'Roo Code', value: 'roo-code' },
] as const;

const DATE_RANGES = [
  { label: 'All time', value: 0 },
  { label: 'Last 7 days', value: 7 },
  { label: 'Last 30 days', value: 30 },
  { label: 'Last 90 days', value: 90 },
] as const;

interface MobileFilterSheetProps {
  open: boolean;
  initialTool: string;
  initialDays: number;
  onApply: (tool: string, days: number) => void;
  onClose: () => void;
}

function MobileFilterSheet({
  open,
  initialTool,
  initialDays,
  onApply,
  onClose,
}: MobileFilterSheetProps) {
  const [draftTool, setDraftTool] = useState(initialTool);
  const [draftDays, setDraftDays] = useState(initialDays);
  const dialogRef = useRef<HTMLDivElement>(null);

  useFocusTrap(dialogRef);

  useEffect(() => {
    if (!open) return;
    setDraftTool(initialTool);
    setDraftDays(initialDays);
  }, [open, initialTool, initialDays]);

  useEffect(() => {
    if (!open) return;

    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      document.body.style.overflow = previous;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-labelledby="search-filter-title">
      <div className="absolute inset-0 bg-black/45" onClick={onClose} />
      <div className="absolute inset-x-0 bottom-0 rounded-t-3xl border border-border bg-bg-elevated shadow-[var(--shadow-lg)]">
        <div ref={dialogRef} className="px-4 pb-5 pt-4">
          <div className="mx-auto mb-4 h-1.5 w-12 rounded-full bg-[var(--border)]" />
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <p className="text-2xs font-semibold uppercase tracking-[0.12em] text-text-tertiary">
                Search Filters
              </p>
              <h2 id="search-filter-title" className="mt-1 text-lg font-semibold text-text-primary">
                Refine results
              </h2>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-surface text-text-secondary transition-colors hover:bg-surface-hover"
              aria-label="Close filters"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <line x1="4" y1="4" x2="12" y2="12" />
                <line x1="12" y1="4" x2="4" y2="12" />
              </svg>
            </button>
          </div>

          <div className="space-y-4">
            <Select
              title="Tool"
              value={draftTool}
              onValueChange={setDraftTool}
              options={TOOLS.map((t) => ({ value: t.value, label: t.label }))}
              className="bg-surface rounded-xl px-3 py-2.5"
            />
            <Select
              title="Date range"
              value={String(draftDays)}
              onValueChange={(v) => setDraftDays(Number(v))}
              options={DATE_RANGES.map((d) => ({ value: String(d.value), label: d.label }))}
              className="bg-surface rounded-xl px-3 py-2.5"
            />
          </div>

          <div className="mt-5 flex items-center justify-between gap-3">
            <button
              type="button"
              onClick={() => {
                setDraftTool('');
                setDraftDays(0);
                onApply('', 0);
                onClose();
              }}
              className="rounded-lg px-3 py-2 text-sm font-medium text-text-tertiary transition-colors hover:text-text-secondary"
            >
              Clear all
            </button>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-text-secondary transition-colors hover:bg-surface-hover"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => {
                  onApply(draftTool, draftDays);
                  onClose();
                }}
                className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-[var(--brand-hover)]"
              >
                Apply filters
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function SearchResults() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryParam = searchParams.get('q') || '';

  const [tool, setTool] = useState('');
  const [days, setDays] = useState(0);
  const [page, setPage] = useState(1);
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);

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
  const activeFilterCount = Number(Boolean(tool)) + Number(days > 0);
  const activeDateLabel = DATE_RANGES.find((d) => d.value === days)?.label;
  const activeToolLabel = TOOLS.find((t) => t.value === tool)?.label;

  return (
    <div className="max-w-7xl mx-auto px-4 py-4 flex gap-6">
      {/* Filters sidebar */}
      <aside className="w-48 shrink-0 hidden md:block">
        <h3 className="text-sm uppercase tracking-wider text-text-muted mb-2">Filters</h3>
        <Select
          title="Tool"
          value={tool}
          onValueChange={setTool}
          options={TOOLS.map((t) => ({ value: t.value, label: t.label }))}
          className="!px-2 !py-1.5 bg-bg-secondary rounded"
        />
        <div className="mt-3" />
        <Select
          title="Date range"
          value={String(days)}
          onValueChange={(v) => setDays(Number(v))}
          options={DATE_RANGES.map((d) => ({ value: String(d.value), label: d.label }))}
          className="!px-2 !py-1.5 bg-bg-secondary rounded"
        />
      </aside>

      {/* Results */}
      <div className="flex-1 min-w-0">
        <div className="mb-3 md:hidden">
          <button
            type="button"
            onClick={() => setMobileFiltersOpen(true)}
            className="inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3.5 py-2 text-sm font-medium text-text-secondary shadow-[var(--shadow-sm)] transition-colors hover:bg-surface-hover"
            aria-haspopup="dialog"
            aria-expanded={mobileFiltersOpen}
            aria-label="Open search filters"
          >
            <svg width="15" height="15" viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
              <line x1="2" y1="4" x2="13" y2="4" />
              <line x1="4" y1="8" x2="11" y2="8" />
              <line x1="6" y1="12" x2="9" y2="12" />
            </svg>
            Filters
            {activeFilterCount > 0 && (
              <span className="rounded-full bg-brand px-1.5 py-0.5 text-2xs font-semibold text-white">
                {activeFilterCount}
              </span>
            )}
          </button>
          {activeFilterCount > 0 && (
            <div
              className="mt-2 flex flex-wrap gap-2"
              aria-label="Active search filters"
            >
              {tool && (
                <span className="rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium text-text-secondary">
                  {activeToolLabel}
                </span>
              )}
              {days > 0 && (
                <span className="rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium text-text-secondary">
                  {activeDateLabel}
                </span>
              )}
            </div>
          )}
        </div>

        <div className="mb-4">
          <h2 className="text-sm text-text-secondary">
            {data ? (
              <>
                <span className="text-text-primary font-medium">{data.total}</span>{' '}
                {data.total === 1 ? 'result' : 'results'} for{' '}
                <span className="text-text-primary font-medium">"{data.query}"</span>
              </>
            ) : isLoading ? (
              'Searching…'
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
          <div className="text-center py-12 text-text-muted">Searching…</div>
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
                    {r.title || r.alias || 'Untitled session'}
                  </span>
                  <span className="text-2xs font-medium px-1.5 py-0.5 rounded bg-accent/15 text-accent shrink-0">
                    {abbreviateTool(r.source_tool)}
                  </span>
                  <span className="text-sm text-text-muted shrink-0">
                    {r.message_count} msgs
                  </span>
                  <span className="text-sm text-text-muted shrink-0">
                    <RelativeDate iso={r.updated_at} />
                  </span>
                </div>
                {r.matches.slice(0, 3).map((m, i) => (
                  <p
                    key={i}
                    className="text-sm text-text-secondary mt-1 truncate [&_mark]:bg-accent/20 [&_mark]:text-accent [&_mark]:px-0.5 [&_mark]:rounded"
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
              className="px-3 py-1 text-sm bg-bg-secondary border border-border rounded hover:border-text-muted transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Previous
            </button>
            <span className="text-sm text-text-muted">
              Page {page} of {totalPages}
            </span>
            <button
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1 text-sm bg-bg-secondary border border-border rounded hover:border-text-muted transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        )}
      </div>

      <MobileFilterSheet
        open={mobileFiltersOpen}
        initialTool={tool}
        initialDays={days}
        onApply={(nextTool, nextDays) => {
          setTool(nextTool);
          setDays(nextDays);
        }}
        onClose={() => setMobileFiltersOpen(false)}
      />
    </div>
  );
}
