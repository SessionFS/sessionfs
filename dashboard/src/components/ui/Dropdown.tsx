import { useState, useRef, useEffect, useCallback, type ReactNode } from 'react';

interface DropdownItem {
  key: string;
  label: string;
  /** Optional left icon or element. */
  icon?: ReactNode;
  danger?: boolean;
  disabled?: boolean;
  /** Render a hairline separator instead of a menu item. */
  separator?: boolean;
  /** Render as a non-interactive header (not focusable, excluded from keyboard nav). */
  header?: boolean;
  /** Optional right-aligned badge text (e.g. pending count). */
  badge?: string;
}

interface DropdownProps {
  /** Trigger element or render function receiving open state (for aria-expanded). */
  trigger: ReactNode | ((open: boolean) => ReactNode);
  items: DropdownItem[];
  onSelect: (key: string) => void;
  /** Accessible label for the menu. */
  menuLabel: string;
  /** Optional min-width override class (default min-w-[160px]). */
  minWidthClass?: string;
}

export function Dropdown({ trigger, items, onSelect, menuLabel, minWidthClass }: DropdownProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    setActiveIndex(-1);
  }, []);

  // Outside click
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        close();
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open, close]);

  // Keyboard
  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      const enabled = items.filter((i) => !i.disabled && !i.separator && !i.header);
      switch (e.key) {
        case 'Escape':
          e.preventDefault();
          close();
          break;
        case 'ArrowDown':
          e.preventDefault();
          setActiveIndex((prev) => {
            const next = prev + 1;
            return next >= enabled.length ? 0 : next;
          });
          break;
        case 'ArrowUp':
          e.preventDefault();
          setActiveIndex((prev) => {
            const next = prev - 1;
            return next < 0 ? enabled.length - 1 : next;
          });
          break;
        case 'Enter':
          e.preventDefault();
          if (activeIndex >= 0 && enabled[activeIndex]) {
            onSelect(enabled[activeIndex].key);
            close();
          }
          break;
      }
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [open, activeIndex, items, onSelect, close]);

  // Scroll active into view
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    const item = listRef.current.querySelector(`[data-dropdown-index="${activeIndex}"]`);
    item?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  const triggerNode = typeof trigger === 'function' ? trigger(open) : trigger;

  return (
    <div ref={wrapperRef} className="relative inline-block">
      <div onClick={() => setOpen((v) => !v)} role="presentation">{triggerNode}</div>

      {open && (
        <div
          className={`absolute top-full right-0 mt-1 z-40 ${minWidthClass || 'min-w-[160px]'} rounded-lg py-1 shadow-[var(--shadow-lg)] dropdown-enter`}
          style={{
            backgroundColor: 'var(--overlay)',
            border: '1px solid var(--border)',
          }}
        >
          <ul ref={listRef} role="menu" aria-label={menuLabel}>
            {items.map((item, i) => {
              if (item.separator) {
                return (
                  <li key={item.key} role="none">
                    <div className="border-t border-[var(--border)] my-1" />
                  </li>
                );
              }
              if (item.header) {
                return (
                  <li key={item.key} role="none">
                    <div className="flex items-center gap-2 px-3 py-2 text-[13px] text-[var(--text-primary)]">
                      {item.icon}
                      <span className="flex-1 truncate">{item.label}</span>
                      {item.badge && (
                        <span className="shrink-0">{item.badge}</span>
                      )}
                    </div>
                  </li>
                );
              }
              const enabledIdx = items.filter((it, idx) => idx < i && !it.disabled && !it.separator && !it.header).length;
              return (
                <li key={item.key} role="none">
                  <button
                    role="menuitem"
                    disabled={item.disabled}
                    data-dropdown-index={enabledIdx}
                    onClick={() => {
                      if (!item.disabled) {
                        onSelect(item.key);
                        close();
                      }
                    }}
                    className={`w-full text-left px-3 py-1.5 text-[13px] flex items-center gap-2 transition-colors duration-150 ${
                      item.disabled
                        ? 'text-[var(--text-tertiary)] cursor-not-allowed opacity-50'
                        : enabledIdx === activeIndex
                          ? 'bg-[var(--surface-active)] text-[var(--text-primary)]'
                          : item.danger
                            ? 'text-[var(--danger)] hover:bg-[var(--surface-hover)]'
                            : 'text-[var(--text-secondary)] hover:bg-[var(--surface-hover)]'
                    }`}
                  >
                    {item.icon}
                    <span className="flex-1">{item.label}</span>
                    {item.badge && (
                      <span
                        className="ml-auto px-1.5 py-0.5 text-[11px] rounded-full font-medium shrink-0"
                        style={{
                          backgroundColor: 'rgba(240,192,64,0.15)',
                          color: 'var(--warning)',
                        }}
                      >
                        {item.badge}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
