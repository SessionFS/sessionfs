import { useState, useRef, useEffect } from 'react';
import { DEFAULT_FOLDER_COLOR } from '../../utils/folderColors';
import { useFolders, useAddBookmark } from '../../hooks/useBookmarks';
import { IconButton } from '../../components/ui';

export function BookmarkDropdown({ sessionId, isBookmarked: initialBookmarked }: { sessionId: string; isBookmarked?: boolean }) {
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
      <IconButton
        label="Bookmark"
        variant="ghost"
        size="sm"
        aria-expanded={open}
        onClick={handleIconClick}
        className={`rounded text-sm duration-150 ${bookmarked ? 'text-brand hover:text-brand' : 'hover:text-brand'} ${popping ? 'scale-125' : ''}`}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill={bookmarked ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />
        </svg>
      </IconButton>
      {open && (
        <div className="absolute right-0 top-8 z-20 bg-bg-elevated border border-border rounded-lg shadow-[var(--shadow-md)] py-1 min-w-[140px]">
          {folders.length === 0 && (
            <div className="px-3 py-1.5 text-sm text-text-tertiary">No folders yet</div>
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
              className="w-full text-left px-3 py-1.5 text-sm text-text-secondary hover:bg-surface-hover flex items-center gap-2 transition-colors"
            >
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{ backgroundColor: f.color || DEFAULT_FOLDER_COLOR }}
              />
              {f.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
