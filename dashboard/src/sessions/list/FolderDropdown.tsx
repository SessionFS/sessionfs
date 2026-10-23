import { useState, useRef, useEffect } from 'react';
import { FOLDER_COLORS as PRESET_COLORS, DEFAULT_FOLDER_COLOR } from '../../utils/folderColors';
import { useFolders, useCreateFolder, useUpdateFolder, useDeleteFolder } from '../../hooks/useBookmarks';
import { Button, IconButton, Input } from '../../components/ui';
import type { NavFilter } from '../../components/BookmarkSidebar';

export function FolderDropdown({
  navFilter,
  selectedFolderId,
  onSelectFilter,
  onSelectFolder,
  totalCount,
  bookmarkedCount,
}: {
  navFilter: NavFilter;
  selectedFolderId: string | null;
  onSelectFilter: (f: NavFilter) => void;
  onSelectFolder: (id: string | null) => void;
  totalCount: number;
  bookmarkedCount: number;
}) {
  const { data } = useFolders();
  const createFolder = useCreateFolder();
  const updateFolder = useUpdateFolder();
  const deleteFolder = useDeleteFolder();

  const [open, setOpen] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newColor, setNewColor] = useState(PRESET_COLORS[0]);
  const [editingFolder, setEditingFolder] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editColor, setEditColor] = useState('');
  const wrapperRef = useRef<HTMLDivElement>(null);

  const folders = data?.folders ?? [];

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
        setShowCreate(false);
        setEditingFolder(null);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  function handleCreate() {
    if (!newName.trim()) return;
    createFolder.mutate({ name: newName.trim(), color: newColor }, {
      onSuccess: () => {
        setNewName('');
        setNewColor(PRESET_COLORS[0]);
        setShowCreate(false);
      },
    });
  }

  function handleStartEdit(folderId: string, name: string, color: string) {
    setEditingFolder(folderId);
    setEditName(name);
    setEditColor(color || PRESET_COLORS[0]);
  }

  function handleSaveEdit(folderId: string) {
    if (!editName.trim()) return;
    updateFolder.mutate({
      folderId,
      updates: { name: editName.trim(), color: editColor },
    }, {
      onSuccess: () => setEditingFolder(null),
    });
  }

  function handleDelete(folderId: string) {
    deleteFolder.mutate(folderId, {
      onSuccess: () => {
        if (selectedFolderId === folderId) {
          onSelectFolder(null);
          onSelectFilter('all');
        }
      },
    });
  }

  // Resolve current label
  let currentLabel = 'All Sessions';
  if (navFilter === 'bookmarked') {
    currentLabel = 'Bookmarked';
  } else if (selectedFolderId) {
    const f = folders.find((f) => f.id === selectedFolderId);
    if (f) currentLabel = f.name;
  }

  return (
    <div ref={wrapperRef} className="relative">
      <Button
        variant="secondary"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="!px-3 !py-2 text-base font-medium"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-text-tertiary">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
        </svg>
        <span className="truncate max-w-[140px]">{currentLabel}</span>
        <svg className="shrink-0 w-3.5 h-3.5 text-text-tertiary" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </Button>

      {open && (
        <div
          className="absolute top-full left-0 mt-1 z-40 min-w-[220px] rounded-lg py-1 bg-overlay border border-border shadow-[var(--shadow-overlay)]"
        >
          {/* All Sessions */}
          <button
            onClick={() => { onSelectFilter('all'); onSelectFolder(null); setOpen(false); }}
            className={`w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 transition-colors duration-150 ${
              navFilter === 'all' && !selectedFolderId
                ? 'bg-surface-active text-text-primary font-semibold'
                : 'text-text-secondary hover:bg-surface-hover'
            }`}
          >
            <span className="flex-1">All Sessions</span>
            <span className="text-xs font-medium text-text-tertiary tabular-nums">{totalCount}</span>
          </button>

          {/* Bookmarked */}
          <button
            onClick={() => { onSelectFilter('bookmarked'); onSelectFolder(null); setOpen(false); }}
            className={`w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 transition-colors duration-150 ${
              navFilter === 'bookmarked' && !selectedFolderId
                ? 'bg-surface-active text-text-primary font-semibold'
                : 'text-text-secondary hover:bg-surface-hover'
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
              <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />
            </svg>
            <span className="flex-1">Bookmarked</span>
            <span className="text-xs font-medium text-text-tertiary tabular-nums">{bookmarkedCount}</span>
          </button>

          {/* Divider */}
          <div className="border-t border-border my-1" />

          {/* Folders */}
          {folders.map((f) => (
            <div key={f.id}>
              {editingFolder === f.id ? (
                <div className="px-3 py-1.5 space-y-1.5">
                  <Input
                    type="text"
                    aria-label="Folder name"
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') handleSaveEdit(f.id);
                      if (e.key === 'Escape') setEditingFolder(null);
                    }}
                    autoFocus
                    className="!px-1.5 !py-0.5"
                  />
                  <div className="flex gap-1">
                    {PRESET_COLORS.map((c) => (
                      <button
                        key={c}
                        onClick={() => setEditColor(c)}
                        aria-label={`Color ${c}`}
                        aria-pressed={editColor === c}
                        className={`w-4 h-4 rounded-full border-2 ${editColor === c ? 'border-text-primary' : 'border-transparent'}`}
                        style={{ backgroundColor: c }}
                      />
                    ))}
                  </div>
                  <div className="flex gap-1">
                    <Button size="sm" onClick={() => handleSaveEdit(f.id)} className="!px-2 !py-0.5 shadow-none">
                      Save
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setEditingFolder(null)} className="!px-2 !py-0.5">
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="flex items-center group">
                  <button
                    onClick={() => { onSelectFolder(f.id); onSelectFilter(f.id); setOpen(false); }}
                    className={`flex-1 flex items-center gap-2 px-3 py-1.5 text-sm transition-colors duration-150 text-left ${
                      selectedFolderId === f.id
                        ? 'bg-surface-active text-text-primary font-semibold'
                        : 'text-text-secondary hover:bg-surface-hover'
                    }`}
                  >
                    <span
                      className="w-2.5 h-2.5 rounded-full shrink-0"
                      style={{ backgroundColor: f.color || DEFAULT_FOLDER_COLOR }}
                    />
                    <span className="truncate flex-1">{f.name}</span>
                    <span className="text-text-tertiary text-xs font-medium tabular-nums">{f.bookmark_count}</span>
                  </button>
                  <IconButton
                    label="Rename folder"
                    size="sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleStartEdit(f.id, f.name, f.color || '');
                    }}
                    className="mr-1 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                    </svg>
                  </IconButton>
                  <IconButton
                    label="Delete folder"
                    size="sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDelete(f.id);
                    }}
                    className="hover:text-danger opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="3 6 5 6 21 6" />
                      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                    </svg>
                  </IconButton>
                </div>
              )}
            </div>
          ))}

          {folders.length === 0 && !showCreate && (
            <div className="px-3 py-1.5 text-xs text-text-tertiary">No folders yet</div>
          )}

          {/* Create folder inline */}
          {showCreate && (
            <div className="mx-2 mt-1 p-2 bg-bg-primary border border-border rounded space-y-2">
              <Input
                type="text"
                aria-label="Folder name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleCreate();
                  if (e.key === 'Escape') setShowCreate(false);
                }}
                placeholder="Folder name"
                autoFocus
                className="!px-2 !py-1"
              />
              <div className="flex gap-1">
                {PRESET_COLORS.map((c) => (
                  <button
                    key={c}
                    onClick={() => setNewColor(c)}
                    aria-label={`Color ${c}`}
                    aria-pressed={newColor === c}
                    className={`w-4 h-4 rounded-full border-2 ${newColor === c ? 'border-text-primary' : 'border-transparent'}`}
                    style={{ backgroundColor: c }}
                  />
                ))}
              </div>
              <div className="flex gap-1">
                <Button
                  size="sm"
                  onClick={handleCreate}
                  disabled={!newName.trim() || createFolder.isPending}
                  className="!px-2 !py-0.5 shadow-none"
                >
                  Create
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setShowCreate(false)} className="!px-2 !py-0.5">
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {/* Footer actions */}
          <div className="border-t border-border mt-1 pt-1">
            {!showCreate && (
              <Button
                variant="ghost"
                onClick={() => setShowCreate(true)}
                className="w-full justify-start !rounded-none !px-3 !py-1.5 font-normal"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                New Folder
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
