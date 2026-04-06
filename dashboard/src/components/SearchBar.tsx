import { useState, useRef, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useSearch } from '../hooks/useSearch';
import { abbreviateTool } from '../utils/models';
import { renderSnippet } from '../utils/highlight';
import RelativeDate from './RelativeDate';

export default function SearchBar() {
  const navigate = useNavigate();
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
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

  // Open dropdown when we have results
  useEffect(() => {
    if (data && data.results.length > 0 && query.length >= 2) {
      setOpen(true);
    } else if (query.length < 2) {
      setOpen(false);
    }
  }, [data, query]);

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

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      setOpen(false);
      inputRef.current?.blur();
    } else if (e.key === 'Enter' && input.trim().length >= 2) {
      setOpen(false);
      navigate(`/search?q=${encodeURIComponent(input.trim())}`);
    }
  }

  function handleResultClick(sessionId: string) {
    setOpen(false);
    setInput('');
    setQuery('');
    navigate(`/sessions/${sessionId}`);
  }

  const results = data?.results.slice(0, 5) ?? [];

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
          value={input}
          onChange={(e) => handleChange(e.target.value)}
          onFocus={() => { if (results.length > 0) setOpen(true); }}
          onKeyDown={handleKeyDown}
          aria-label="Search sessions"
          placeholder="Search sessions..."
          className="w-full pl-8 pr-3 py-1.5 bg-bg-tertiary border border-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors"
        />
      </div>

      {open && results.length > 0 && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-bg-secondary border border-border rounded-lg shadow-lg overflow-hidden z-50">
          {results.map((r) => (
            <button
              key={r.session_id}
              onClick={() => handleResultClick(r.session_id)}
              className="w-full px-3 py-2 text-left hover:bg-bg-tertiary transition-colors border-b border-border last:border-b-0"
            >
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
                  className="text-sm text-text-secondary truncate [&_mark]:bg-accent/20 [&_mark]:text-accent [&_mark]:px-0.5 [&_mark]:rounded"
                  dangerouslySetInnerHTML={{ __html: renderSnippet(r.matches[0].snippet) }}
                />
              )}
            </button>
          ))}
          <button
            onClick={() => {
              setOpen(false);
              navigate(`/search?q=${encodeURIComponent(input.trim())}`);
            }}
            className="w-full px-3 py-2 text-sm text-accent hover:bg-bg-tertiary transition-colors text-center"
          >
            View all results
          </button>
        </div>
      )}

      {open && query.length >= 2 && data && data.results.length === 0 && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-bg-secondary border border-border rounded-lg shadow-lg z-50 px-3 py-3 text-sm text-text-muted text-center">
          No results found
        </div>
      )}
    </div>
  );
}
