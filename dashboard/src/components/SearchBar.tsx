import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useSearch } from '../hooks/useSearch';
import type { SearchResult } from '../hooks/useSearch';
import { abbreviateTool } from '../utils/models';
import { renderSnippet } from '../utils/highlight';
import RelativeDate from './RelativeDate';

interface GroupedResults {
  sessions: SearchResult[];
  projects: SearchResult[];
  handoffs: SearchResult[];
}

function classifyResult(r: SearchResult): keyof GroupedResults {
  if (r.title?.toLowerCase().includes('handoff') || r.source_tool === 'handoff') {
    return 'handoffs';
  }
  if (r.source_tool === 'project') {
    return 'projects';
  }
  return 'sessions';
}

function actionLabel(group: keyof GroupedResults): string {
  switch (group) {
    case 'handoffs':
      return 'Claim';
    case 'projects':
      return 'View';
    case 'sessions':
      return 'Open';
  }
}

const GROUP_LABELS: Record<keyof GroupedResults, string> = {
  sessions: 'Sessions',
  projects: 'Projects',
  handoffs: 'Handoffs',
};

const GROUP_ORDER: (keyof GroupedResults)[] = ['sessions', 'projects', 'handoffs'];

export default function SearchBar() {
  const navigate = useNavigate();
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const { data } = useSearch(query);

  // Debounce input -> query
  const handleChange = useCallback((value: string) => {
    setInput(value);
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      setQuery(value.trim());
    }, 300);
  }, []);

  const results = useMemo(() => data?.results.slice(0, 8) ?? [], [data]);

  // Build grouped structure and a flat index for keyboard nav
  const { groups, flatItems } = useMemo(() => {
    const g: GroupedResults = { sessions: [], projects: [], handoffs: [] };
    for (const r of results) {
      g[classifyResult(r)].push(r);
    }
    const flat: { result: SearchResult; group: keyof GroupedResults }[] = [];
    for (const key of GROUP_ORDER) {
      for (const r of g[key]) {
        flat.push({ result: r, group: key });
      }
    }
    return { groups: g, flatItems: flat };
  }, [results]);

  // Open dropdown when we have results
  useEffect(() => {
    if (data && data.results.length > 0 && query.length >= 2) {
      setOpen(true);
    } else if (query.length < 2) {
      setOpen(false);
    }
  }, [data, query]);

  // Reset active index when results change
  useEffect(() => {
    setActiveIndex(flatItems.length > 0 ? 0 : -1);
  }, [flatItems]);

  // Close on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  // Global Cmd/Ctrl+K shortcut
  useEffect(() => {
    function handleGlobalKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    }
    document.addEventListener('keydown', handleGlobalKey);
    return () => document.removeEventListener('keydown', handleGlobalKey);
  }, []);

  // Scroll active item into view
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    const item = listRef.current.querySelector(`[data-index="${activeIndex}"]`);
    if (item) {
      item.scrollIntoView({ block: 'nearest' });
    }
  }, [activeIndex]);

  function selectResult(sessionId: string) {
    setOpen(false);
    setInput('');
    setQuery('');
    navigate(`/sessions/${sessionId}`);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      setOpen(false);
      inputRef.current?.blur();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!open && flatItems.length > 0) {
        setOpen(true);
      }
      setActiveIndex((prev) => (prev < flatItems.length - 1 ? prev + 1 : 0));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((prev) => (prev > 0 ? prev - 1 : flatItems.length - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (open && activeIndex >= 0 && flatItems[activeIndex]) {
        selectResult(flatItems[activeIndex].result.session_id);
      } else if (input.trim().length >= 2) {
        setOpen(false);
        navigate(`/search?q=${encodeURIComponent(input.trim())}`);
      }
    }
  }

  const listboxId = 'search-palette-listbox';
  const activeDescendant =
    activeIndex >= 0 && flatItems[activeIndex]
      ? `search-result-${flatItems[activeIndex].result.session_id}`
      : undefined;

  // Render a group section
  function renderGroup(key: keyof GroupedResults) {
    const items = groups[key];
    if (items.length === 0) return null;

    // Find the flat-index offset for this group's first item
    const groupStartIndex = flatItems.findIndex((f) => f.group === key);

    return (
      <li key={key} role="presentation">
        <div className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-text-muted select-none">
          {GROUP_LABELS[key]}
        </div>
        <ul role="group" aria-label={GROUP_LABELS[key]}>
          {items.map((r, i) => {
            const flatIdx = groupStartIndex + i;
            const isActive = flatIdx === activeIndex;
            const action = actionLabel(key);
            return (
              <li
                key={r.session_id}
                id={`search-result-${r.session_id}`}
                role="option"
                aria-selected={isActive}
                data-index={flatIdx}
                onMouseEnter={() => setActiveIndex(flatIdx)}
                onClick={() => selectResult(r.session_id)}
                className={`flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors border-b border-border/50 last:border-b-0 ${
                  isActive ? 'bg-accent/10' : 'bg-transparent'
                }`}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-sm text-text-primary truncate flex-1">
                      {r.title || r.alias || 'Untitled session'}
                    </span>
                    <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-accent/15 text-accent shrink-0">
                      {abbreviateTool(r.source_tool)}
                    </span>
                    <span className="text-[10px] text-text-muted shrink-0">
                      <RelativeDate iso={r.updated_at} />
                    </span>
                  </div>
                  {r.matches[0] && (
                    <p
                      className="text-xs text-text-secondary truncate [&_mark]:bg-accent/20 [&_mark]:text-accent [&_mark]:px-0.5 [&_mark]:rounded"
                      dangerouslySetInnerHTML={{
                        __html: renderSnippet(r.matches[0].snippet),
                      }}
                    />
                  )}
                </div>
                <span
                  className={`text-[11px] font-medium px-2 py-0.5 rounded shrink-0 transition-opacity ${
                    isActive
                      ? 'opacity-100 text-accent bg-accent/10'
                      : 'opacity-0'
                  }`}
                >
                  {action}
                </span>
              </li>
            );
          })}
        </ul>
      </li>
    );
  }

  return (
    <div ref={wrapperRef} className="relative flex-1 max-w-md mx-4">
      <div className="relative">
        <svg
          className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-text-muted"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
          />
        </svg>
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          aria-label="Search sessions"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-activedescendant={open ? activeDescendant : undefined}
          aria-autocomplete="list"
          aria-haspopup="listbox"
          value={input}
          onChange={(e) => handleChange(e.target.value)}
          onFocus={() => {
            if (results.length > 0) setOpen(true);
          }}
          onKeyDown={handleKeyDown}
          placeholder="Search sessions... (\u2318K)"
          className="w-full pl-8 pr-12 py-1.5 bg-bg-tertiary border border-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/30 transition-colors"
        />
        <kbd className="absolute right-2.5 top-1/2 -translate-y-1/2 hidden sm:inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium text-text-muted bg-bg-primary border border-border rounded">
          <span className="text-[11px]">{'\u2318'}</span>K
        </kbd>
      </div>

      {open && flatItems.length > 0 && (
        <ul
          ref={listRef}
          id={listboxId}
          role="listbox"
          aria-label="Search results"
          className="absolute top-full left-0 right-0 mt-1 bg-bg-secondary border border-border rounded-lg shadow-lg overflow-auto max-h-[360px] z-50"
        >
          {GROUP_ORDER.map((key) => renderGroup(key))}
          <li role="presentation">
            <button
              onClick={() => {
                setOpen(false);
                navigate(`/search?q=${encodeURIComponent(input.trim())}`);
              }}
              className="w-full px-3 py-2 text-xs text-accent hover:bg-bg-tertiary transition-colors text-center border-t border-border"
            >
              View all results
            </button>
          </li>
        </ul>
      )}

      {open && query.length >= 2 && data && data.results.length === 0 && (
        <div
          role="listbox"
          id={listboxId}
          className="absolute top-full left-0 right-0 mt-1 bg-bg-secondary border border-border rounded-lg shadow-lg z-50 px-3 py-3 text-sm text-text-muted text-center"
        >
          No results found
        </div>
      )}
    </div>
  );
}
