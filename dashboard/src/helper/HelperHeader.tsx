import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';

import { IconButton } from '../components/ui';
import {
  CheckIcon,
  ChevronDownIcon,
  CloseIcon,
  FolderIcon,
  FoldersIcon,
  HistoryIcon,
  LayersIcon,
  NewChatIcon,
  SessionIcon,
  SparkIcon,
} from './icons';
import type { ScopeKind, ScopeOption } from './scope';
import { TITLE_ID } from './format';

export function ScopeIcon({ kind, size = 14 }: { kind: ScopeKind; size?: number }) {
  if (kind === 'project') return <FolderIcon size={size} />;
  if (kind === 'group') return <FoldersIcon size={size} />;
  if (kind === 'session') return <SessionIcon size={size} />;
  return <LayersIcon size={size} />;
}

/** Consecutive options sharing a `section` form one labelled block; the flat
 *  index is kept for roving focus across blocks. */
function sectionsOf(options: ScopeOption[]) {
  const blocks: Array<{ section?: string; items: Array<{ option: ScopeOption; index: number }> }> = [];
  options.forEach((option, index) => {
    const last = blocks[blocks.length - 1];
    if (last && last.section === option.section) last.items.push({ option, index });
    else blocks.push({ section: option.section, items: [{ option, index }] });
  });
  return blocks;
}

/**
 * The scope chip: shows what the helper will search and, when more than one
 * scope is available, opens a small radio menu to switch. Switching while a
 * thread is open starts a new conversation (the server pins a thread's scope).
 */
function ScopeMenu({
  options,
  active,
  onChange,
  hasThread,
  disabled,
}: {
  options: ScopeOption[];
  active: ScopeOption;
  onChange: (o: ScopeOption) => void;
  hasThread: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [focusIdx, setFocusIdx] = useState(0);
  const wrapRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const menuId = useId();
  const switchable = options.length > 1;

  useEffect(() => {
    if (!open) return;
    itemRefs.current[focusIdx]?.focus();
  }, [open, focusIdx]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const close = (refocus = true) => {
    setOpen(false);
    if (refocus) buttonRef.current?.focus();
  };

  const onMenuKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      // Keep the drawer open: only the menu closes.
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setFocusIdx((i) => (i + 1) % options.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setFocusIdx((i) => (i - 1 + options.length) % options.length);
    } else if (e.key === 'Home') {
      e.preventDefault();
      setFocusIdx(0);
    } else if (e.key === 'End') {
      e.preventDefault();
      setFocusIdx(options.length - 1);
    } else if (e.key === 'Tab') {
      close(false);
    }
  };

  const chip = (
    <>
      <span className="text-text-tertiary">
        <ScopeIcon kind={active.kind} size={13} />
      </span>
      <span className="truncate">{active.label}</span>
    </>
  );

  if (!switchable) {
    return (
      <span
        className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md border border-border bg-bg-primary px-2 py-1 text-xs font-medium text-text-secondary"
        title="The helper searches everything you can read"
      >
        <span className="sr-only">Scope: </span>
        {chip}
      </span>
    );
  }

  return (
    <div ref={wrapRef} className="relative min-w-0 max-w-full">
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`Scope: ${active.label}. Change scope`}
        onClick={() => {
          setFocusIdx(Math.max(0, options.findIndex((o) => o.key === active.key)));
          setOpen((v) => !v);
        }}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault();
            setFocusIdx(Math.max(0, options.findIndex((o) => o.key === active.key)));
            setOpen(true);
          }
        }}
        className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md border border-border bg-bg-primary px-2 py-1 text-xs font-medium text-text-secondary transition-colors hover:border-border-strong hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:cursor-not-allowed disabled:opacity-60"
      >
        {chip}
        <ChevronDownIcon size={12} className="text-text-tertiary" />
      </button>

      {open && (
        <div
          onKeyDown={onMenuKey}
          className="absolute left-0 top-full z-20 mt-1.5 w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-overlay p-1"
          style={{ boxShadow: 'var(--shadow-overlay)' }}
        >
          <p aria-hidden="true" className="px-2.5 pb-1 pt-1.5 text-2xs font-semibold uppercase tracking-wide text-text-tertiary">
            Ask about
          </p>
          <div
            id={menuId}
            role="menu"
            className="max-h-[min(60vh,22rem)] overflow-y-auto overscroll-contain"
            aria-label="Ask about"
            aria-describedby={hasThread ? `${menuId}-note` : undefined}
          >
          {sectionsOf(options).map((block) => {
            const items = block.items.map(({ option: o, index: i }) => {
              const checked = o.key === active.key;
              return (
                <button
                  key={o.key}
                  ref={(el) => {
                    itemRefs.current[i] = el;
                  }}
                  type="button"
                  role="menuitemradio"
                  aria-checked={checked}
                  tabIndex={i === focusIdx ? 0 : -1}
                  onClick={() => {
                    close();
                    if (!checked) onChange(o);
                  }}
                  className={`flex w-full items-start gap-2.5 rounded-md px-2.5 py-2 text-left outline-none transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover focus-visible:shadow-[0_0_0_2px_var(--brand-glow)] ${
                    checked ? 'text-text-primary' : 'text-text-secondary'
                  }`}
                >
                  <span className="mt-0.5 text-text-tertiary">
                    <ScopeIcon kind={o.kind} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{o.label}</span>
                    <span className="block text-xs text-text-tertiary">{o.description}</span>
                  </span>
                  <span className={`mt-0.5 text-brand ${checked ? '' : 'invisible'}`}>
                    <CheckIcon size={14} />
                  </span>
                </button>
              );
            });
            if (!block.section) return items;
            return (
              <div key={`section:${block.section}`} role="group" aria-label={block.section}>
                <p
                  aria-hidden="true"
                  className="mt-1 border-t border-border px-2.5 pb-1 pt-2 text-2xs font-semibold uppercase tracking-wide text-text-tertiary"
                >
                  {block.section}
                </p>
                {items}
              </div>
            );
          })}
          </div>
          {hasThread && (
            <p id={`${menuId}-note`} className="mx-2.5 mb-1 mt-1 border-t border-border pt-2 text-2xs text-text-tertiary">
              Switching scope starts a new conversation.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export default function HelperHeader({
  options,
  active,
  onScopeChange,
  threadTitle,
  hasThread,
  view,
  onToggleHistory,
  onNew,
  onClose,
  showControls,
}: {
  options: ScopeOption[];
  active: ScopeOption;
  onScopeChange: (o: ScopeOption) => void;
  threadTitle: string | null;
  hasThread: boolean;
  view: 'chat' | 'history';
  onToggleHistory: () => void;
  onNew: () => void;
  onClose: () => void;
  /** False only before the first status arrives; gate/failed scopes keep controls so the user can switch back. */
  showControls: boolean;
}) {
  return (
    <div className="shrink-0 border-b border-border px-4 pb-3 pt-3 sm:px-5">
      <div className="flex items-center gap-2.5">
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand">
          <SparkIcon size={15} />
        </span>
        <h2 id={TITLE_ID} className="min-w-0 flex-1 truncate !text-md !font-semibold !tracking-normal text-text-primary">
          Session helper
        </h2>
        <div className="flex shrink-0 items-center gap-0.5">
          {showControls && (
            <>
              <IconButton
                label={view === 'history' ? 'Back to conversation' : 'Conversation history'}
                aria-pressed={view === 'history'}
                onClick={onToggleHistory}
                className={view === 'history' ? '!bg-surface-active !text-text-primary' : ''}
              >
                <HistoryIcon />
              </IconButton>
              <IconButton label="New conversation" onClick={onNew}>
                <NewChatIcon />
              </IconButton>
              <span aria-hidden="true" className="mx-1 h-4 w-px bg-border" />
            </>
          )}
          <IconButton label="Close helper" onClick={onClose}>
            <CloseIcon />
          </IconButton>
        </div>
      </div>
      {showControls && (
        <div className="mt-2.5 flex min-w-0 items-center gap-2">
          <ScopeMenu options={options} active={active} onChange={onScopeChange} hasThread={hasThread} />
          {threadTitle && view === 'chat' && (
            <span className="min-w-0 truncate text-xs text-text-tertiary" title={threadTitle}>
              {threadTitle}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
