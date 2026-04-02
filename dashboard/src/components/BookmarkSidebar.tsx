import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import type { FolderResponse } from '../api/client';
import { useFolders, useCreateFolder, useUpdateFolder, useDeleteFolder } from '../hooks/useBookmarks';

const PRESET_COLORS = [
  '#4f9cf7', '#3ddc84', '#bc8cff', '#f0a040',
  '#f04060', '#40c4f0', '#f0c040', '#f06090',
];

export type NavFilter = 'all' | 'bookmarked' | 'in-repo' | string; // string = folder ID

interface BookmarkSidebarProps {
  selectedFilter: NavFilter;
  onSelectFilter: (filter: NavFilter) => void;
  totalCount: number;
  bookmarkedCount: number;
  inRepoCount: number;
  inRepoLabel: string | null;
  handoffCount: number;
  selectedFolderId: string | null;
  onSelectFolder: (folderId: string | null) => void;
}

export default function BookmarkSidebar({
  selectedFilter,
  onSelectFilter,
  totalCount,
  bookmarkedCount,
  inRepoCount,
  inRepoLabel,
  handoffCount,
  selectedFolderId,
  onSelectFolder,
}: BookmarkSidebarProps) {
  const navigate = useNavigate();
  const { data } = useFolders();
  const createFolder = useCreateFolder();
  const updateFolder = useUpdateFolder();
  const deleteFolder = useDeleteFolder();

  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newColor, setNewColor] = useState(PRESET_COLORS[0]);
  const [menuFolder, setMenuFolder] = useState<string | null>(null);
  const [editingFolder, setEditingFolder] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editColor, setEditColor] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);

  const folders = data?.folders ?? [];

  // Close menu on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuFolder(null);
      }
    }
    if (menuFolder) document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [menuFolder]);

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

  function handleStartEdit(folder: FolderResponse) {
    setEditingFolder(folder.id);
    setEditName(folder.name);
    setEditColor(folder.color || PRESET_COLORS[0]);
    setMenuFolder(null);
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
        setMenuFolder(null);
        if (selectedFolderId === folderId) {
          onSelectFolder(null);
          onSelectFilter('all');
        }
      },
    });
  }

  function isActive(filter: NavFilter) {
    return selectedFilter === filter && !selectedFolderId;
  }

  function isFolderActive(folderId: string) {
    return selectedFolderId === folderId;
  }

  const activeClass = 'bg-[var(--surface-active,var(--bg-tertiary))] text-[var(--text-primary)] border-l-[3px] border-l-[var(--brand)]';
  const inactiveClass = 'text-[var(--text-secondary)] hover:bg-[var(--surface-hover,var(--bg-tertiary))] border-l-[3px] border-l-transparent';

  // Mobile chips are rendered separately via MobileNavChips

  // Desktop: vertical rail
  const desktopRail = (
    <div className="hidden sm:flex w-[220px] shrink-0 border-r border-[var(--border)] bg-[var(--bg-secondary)] flex-col overflow-y-auto">
      <div className="p-3 flex flex-col gap-0.5">
        {/* Nav items */}
        <button
          onClick={() => { onSelectFilter('all'); onSelectFolder(null); }}
          className={`w-full text-left px-3 py-2 text-sm rounded-r-md transition-colors flex items-center justify-between ${
            isActive('all') ? activeClass : inactiveClass
          }`}
        >
          <span>All Sessions</span>
          <span className="text-xs text-[var(--text-tertiary)] tabular-nums">{totalCount}</span>
        </button>

        {inRepoLabel && (
          <button
            onClick={() => { onSelectFilter('in-repo'); onSelectFolder(null); }}
            className={`w-full text-left px-3 py-2 text-sm rounded-r-md transition-colors flex items-center justify-between ${
              isActive('in-repo') ? activeClass : inactiveClass
            }`}
          >
            <span className="truncate">In This Repo</span>
            <span className="text-xs text-[var(--text-tertiary)] tabular-nums">{inRepoCount}</span>
          </button>
        )}

        <button
          onClick={() => { onSelectFilter('bookmarked'); onSelectFolder(null); }}
          className={`w-full text-left px-3 py-2 text-sm rounded-r-md transition-colors flex items-center justify-between ${
            isActive('bookmarked') ? activeClass : inactiveClass
          }`}
        >
          <span>Bookmarked</span>
          <span className="text-xs text-[var(--text-tertiary)] tabular-nums">{bookmarkedCount}</span>
        </button>

        <button
          onClick={() => navigate('/handoffs')}
          className={`w-full text-left px-3 py-2 text-sm rounded-r-md transition-colors flex items-center justify-between ${inactiveClass}`}
        >
          <span>Handoffs</span>
          <span className="text-xs text-[var(--text-tertiary)] tabular-nums">{handoffCount}</span>
        </button>

        {/* Divider */}
        <div className="border-t border-[var(--border)] my-2" />

        {/* Folders header */}
        <div className="flex items-center justify-between px-3 mb-1">
          <span className="text-[10px] uppercase tracking-wider text-[var(--text-tertiary)]">Folders</span>
          <button
            onClick={() => setShowCreate(true)}
            className="text-[var(--text-tertiary)] hover:text-[var(--brand)] text-lg leading-none"
            title="Create folder"
          >
            +
          </button>
        </div>

        {/* Folder list */}
        {folders.map((f) => (
          <div key={f.id} className="relative">
            {editingFolder === f.id ? (
              <div className="px-3 py-1.5 space-y-1.5">
                <input
                  type="text"
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleSaveEdit(f.id);
                    if (e.key === 'Escape') setEditingFolder(null);
                  }}
                  autoFocus
                  className="w-full px-1.5 py-0.5 text-sm bg-[var(--bg-primary)] border border-[var(--border)] rounded text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)]"
                />
                <div className="flex gap-1">
                  {PRESET_COLORS.map((c) => (
                    <button
                      key={c}
                      onClick={() => setEditColor(c)}
                      className={`w-4 h-4 rounded-full border-2 ${editColor === c ? 'border-white' : 'border-transparent'}`}
                      style={{ backgroundColor: c }}
                    />
                  ))}
                </div>
                <div className="flex gap-1">
                  <button
                    onClick={() => handleSaveEdit(f.id)}
                    className="px-2 py-0.5 text-xs bg-[var(--brand)] text-white rounded hover:opacity-90"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => setEditingFolder(null)}
                    className="px-2 py-0.5 text-xs text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex items-center group">
                <button
                  onClick={() => { onSelectFolder(f.id); onSelectFilter(f.id); }}
                  className={`flex-1 flex items-center gap-2 px-3 py-2 text-sm rounded-r-md transition-colors text-left ${
                    isFolderActive(f.id) ? activeClass : inactiveClass
                  }`}
                >
                  <span
                    className="w-2.5 h-2.5 rounded-full shrink-0"
                    style={{ backgroundColor: f.color || '#4f9cf7' }}
                  />
                  <span className="truncate flex-1">{f.name}</span>
                  <span className="text-[var(--text-tertiary)] text-xs tabular-nums">{f.bookmark_count}</span>
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuFolder(menuFolder === f.id ? null : f.id);
                  }}
                  className="px-1 text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] opacity-0 group-hover:opacity-100 transition-opacity text-xs"
                >
                  ...
                </button>
              </div>
            )}

            {/* Context menu */}
            {menuFolder === f.id && (
              <div
                ref={menuRef}
                className="absolute right-0 top-8 z-10 bg-[var(--bg-primary)] border border-[var(--border)] rounded shadow-lg py-1 min-w-[100px]"
              >
                <button
                  onClick={() => handleStartEdit(f)}
                  className="w-full text-left px-3 py-1 text-sm text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
                >
                  Rename
                </button>
                <button
                  onClick={() => handleDelete(f.id)}
                  className="w-full text-left px-3 py-1 text-sm text-red-400 hover:bg-[var(--bg-tertiary)]"
                >
                  Delete
                </button>
              </div>
            )}
          </div>
        ))}

        {/* Create folder inline */}
        {showCreate && (
          <div className="mt-2 p-2 bg-[var(--bg-primary)] border border-[var(--border)] rounded space-y-2">
            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleCreate();
                if (e.key === 'Escape') setShowCreate(false);
              }}
              placeholder="Folder name"
              autoFocus
              className="w-full px-2 py-1 text-sm bg-[var(--bg-secondary)] border border-[var(--border)] rounded text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)]"
            />
            <div className="flex gap-1">
              {PRESET_COLORS.map((c) => (
                <button
                  key={c}
                  onClick={() => setNewColor(c)}
                  className={`w-4 h-4 rounded-full border-2 ${newColor === c ? 'border-white' : 'border-transparent'}`}
                  style={{ backgroundColor: c }}
                />
              ))}
            </div>
            <div className="flex gap-1">
              <button
                onClick={handleCreate}
                disabled={!newName.trim() || createFolder.isPending}
                className="px-2 py-0.5 text-xs bg-[var(--brand)] text-white rounded hover:opacity-90 disabled:opacity-50"
              >
                Create
              </button>
              <button
                onClick={() => setShowCreate(false)}
                className="px-2 py-0.5 text-xs text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {folders.length === 0 && !showCreate && (
          <div className="px-3 py-2 text-xs text-[var(--text-tertiary)]">
            No folders yet
          </div>
        )}
      </div>
    </div>
  );

  return desktopRail;
}

/* Mobile horizontal chip row — rendered inside content area by SessionList */
export function MobileNavChips({
  selectedFilter,
  onSelectFilter,
  totalCount,
  bookmarkedCount,
  inRepoCount,
  inRepoLabel,
  handoffCount,
  selectedFolderId,
  onSelectFolder,
}: Omit<BookmarkSidebarProps, never>) {
  const navigate = useNavigate();
  const { data } = useFolders();
  const folders = data?.folders ?? [];

  function isActive(filter: NavFilter) {
    return selectedFilter === filter && !selectedFolderId;
  }

  function isFolderActive(folderId: string) {
    return selectedFolderId === folderId;
  }

  return (
    <div className="sm:hidden flex gap-2 overflow-x-auto pb-2 mb-3 -mx-1 px-1">
      <button
        onClick={() => { onSelectFilter('all'); onSelectFolder(null); }}
        className={`shrink-0 px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
          isActive('all') ? 'bg-[var(--brand)] text-white' : 'bg-[var(--surface)] text-[var(--text-secondary)] border border-[var(--border)]'
        }`}
      >
        All ({totalCount})
      </button>
      {inRepoLabel && (
        <button
          onClick={() => { onSelectFilter('in-repo'); onSelectFolder(null); }}
          className={`shrink-0 px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
            isActive('in-repo') ? 'bg-[var(--brand)] text-white' : 'bg-[var(--surface)] text-[var(--text-secondary)] border border-[var(--border)]'
          }`}
        >
          This Repo ({inRepoCount})
        </button>
      )}
      <button
        onClick={() => { onSelectFilter('bookmarked'); onSelectFolder(null); }}
        className={`shrink-0 px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
          isActive('bookmarked') ? 'bg-[var(--brand)] text-white' : 'bg-[var(--surface)] text-[var(--text-secondary)] border border-[var(--border)]'
        }`}
      >
        Bookmarked ({bookmarkedCount})
      </button>
      <button
        onClick={() => navigate('/handoffs')}
        className="shrink-0 px-3 py-1.5 rounded-full text-xs font-medium bg-[var(--surface)] text-[var(--text-secondary)] border border-[var(--border)] transition-colors"
      >
        Handoffs ({handoffCount})
      </button>
      {folders.map((f) => (
        <button
          key={f.id}
          onClick={() => { onSelectFolder(f.id); onSelectFilter(f.id); }}
          className={`shrink-0 px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
            isFolderActive(f.id) ? 'bg-[var(--brand)] text-white' : 'bg-[var(--surface)] text-[var(--text-secondary)] border border-[var(--border)]'
          }`}
        >
          {f.name} ({f.bookmark_count})
        </button>
      ))}
    </div>
  );
}
