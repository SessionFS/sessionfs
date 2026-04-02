import { useState, useRef, useEffect } from 'react';
import type { FolderResponse } from '../api/client';
import { useFolders, useCreateFolder, useUpdateFolder, useDeleteFolder } from '../hooks/useBookmarks';

const PRESET_COLORS = [
  '#4f9cf7', '#3ddc84', '#bc8cff', '#f0a040',
  '#f04060', '#40c4f0', '#f0c040', '#f06090',
];

interface BookmarkSidebarProps {
  selectedFolderId: string | null;
  onSelectFolder: (folderId: string | null) => void;
}

export default function BookmarkSidebar({ selectedFolderId, onSelectFolder }: BookmarkSidebarProps) {
  const { data } = useFolders();
  const createFolder = useCreateFolder();
  const updateFolder = useUpdateFolder();
  const deleteFolder = useDeleteFolder();

  const [collapsed, setCollapsed] = useState(() => {
    try {
      const stored = localStorage.getItem('sfs-sidebar-collapsed');
      return stored !== null ? stored === 'true' : true; // default collapsed
    } catch {
      return true;
    }
  });
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newColor, setNewColor] = useState(PRESET_COLORS[0]);
  const [menuFolder, setMenuFolder] = useState<string | null>(null);
  const [editingFolder, setEditingFolder] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editColor, setEditColor] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);

  const folders = data?.folders ?? [];

  function toggleCollapsed() {
    setCollapsed((prev) => {
      const next = !prev;
      try { localStorage.setItem('sfs-sidebar-collapsed', String(next)); } catch { /* noop */ }
      return next;
    });
  }

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
        if (selectedFolderId === folderId) onSelectFolder(null);
      },
    });
  }

  if (collapsed) {
    return (
      <div className="hidden sm:flex w-12 shrink-0 border-r border-border bg-bg-secondary flex-col items-center pt-3 gap-2">
        <button
          onClick={toggleCollapsed}
          className="text-text-muted hover:text-text-secondary p-1 rounded hover:bg-bg-tertiary transition-colors"
          title="Expand folders"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>
        <button
          onClick={() => onSelectFolder(null)}
          className={`p-1.5 rounded transition-colors ${selectedFolderId === null ? 'bg-bg-tertiary' : 'hover:bg-bg-tertiary'}`}
          title="All Sessions"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="text-text-muted">
            <path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
          </svg>
        </button>
        {folders.map((f) => (
          <button
            key={f.id}
            onClick={() => onSelectFolder(f.id)}
            className={`p-1.5 rounded transition-colors ${selectedFolderId === f.id ? 'bg-bg-tertiary' : 'hover:bg-bg-tertiary'}`}
            title={f.name}
          >
            <span
              className="block w-3 h-3 rounded-full"
              style={{ backgroundColor: f.color || '#4f9cf7' }}
            />
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="hidden sm:block w-52 shrink-0 border-r border-border bg-bg-secondary overflow-y-auto">
      <div className="p-3">
        {/* Header */}
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-1.5">
            <button
              onClick={toggleCollapsed}
              className="text-text-muted hover:text-text-secondary p-0.5 rounded hover:bg-bg-tertiary transition-colors"
              title="Collapse sidebar"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="15 18 9 12 15 6" />
              </svg>
            </button>
            <span className="text-[10px] uppercase tracking-wider text-text-muted">Folders</span>
          </div>
          <button
            onClick={() => setShowCreate(true)}
            className="text-text-muted hover:text-accent text-lg leading-none"
            title="Create folder"
          >
            +
          </button>
        </div>

        {/* All Sessions */}
        <button
          onClick={() => onSelectFolder(null)}
          className={`w-full text-left px-2 py-1.5 text-sm rounded transition-colors ${
            selectedFolderId === null
              ? 'bg-bg-tertiary text-text-primary'
              : 'text-text-secondary hover:bg-bg-tertiary'
          }`}
        >
          All Sessions
        </button>

        {/* Folder list */}
        {folders.map((f) => (
          <div key={f.id} className="relative">
            {editingFolder === f.id ? (
              <div className="px-2 py-1.5 space-y-1.5">
                <input
                  type="text"
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleSaveEdit(f.id);
                    if (e.key === 'Escape') setEditingFolder(null);
                  }}
                  autoFocus
                  className="w-full px-1.5 py-0.5 text-sm bg-bg-primary border border-border rounded text-text-primary focus:outline-none focus:border-accent"
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
                    className="px-2 py-0.5 text-xs bg-accent text-white rounded hover:bg-accent/90"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => setEditingFolder(null)}
                    className="px-2 py-0.5 text-xs text-text-muted hover:text-text-secondary"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex items-center group">
                <button
                  onClick={() => onSelectFolder(f.id)}
                  className={`flex-1 flex items-center gap-2 px-2 py-1.5 text-sm rounded transition-colors text-left ${
                    selectedFolderId === f.id
                      ? 'bg-bg-tertiary text-text-primary'
                      : 'text-text-secondary hover:bg-bg-tertiary'
                  }`}
                >
                  <span
                    className="w-2.5 h-2.5 rounded-full shrink-0"
                    style={{ backgroundColor: f.color || '#4f9cf7' }}
                  />
                  <span className="truncate flex-1">{f.name}</span>
                  <span className="text-text-muted text-xs tabular-nums">{f.bookmark_count}</span>
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuFolder(menuFolder === f.id ? null : f.id);
                  }}
                  className="px-1 text-text-muted hover:text-text-secondary opacity-0 group-hover:opacity-100 transition-opacity text-xs"
                >
                  ...
                </button>
              </div>
            )}

            {/* Context menu */}
            {menuFolder === f.id && (
              <div
                ref={menuRef}
                className="absolute right-0 top-8 z-10 bg-bg-primary border border-border rounded shadow-lg py-1 min-w-[100px]"
              >
                <button
                  onClick={() => handleStartEdit(f)}
                  className="w-full text-left px-3 py-1 text-sm text-text-secondary hover:bg-bg-tertiary"
                >
                  Rename
                </button>
                <button
                  onClick={() => handleDelete(f.id)}
                  className="w-full text-left px-3 py-1 text-sm text-red-400 hover:bg-bg-tertiary"
                >
                  Delete
                </button>
              </div>
            )}
          </div>
        ))}

        {/* Create folder modal inline */}
        {showCreate && (
          <div className="mt-2 p-2 bg-bg-primary border border-border rounded space-y-2">
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
              className="w-full px-2 py-1 text-sm bg-bg-secondary border border-border rounded text-text-primary focus:outline-none focus:border-accent"
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
                className="px-2 py-0.5 text-xs bg-accent text-white rounded hover:bg-accent/90 disabled:opacity-50"
              >
                Create
              </button>
              <button
                onClick={() => setShowCreate(false)}
                className="px-2 py-0.5 text-xs text-text-muted hover:text-text-secondary"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
