import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { FieldWrapper } from './Input';
import { baseField, describedBy, errorField } from './fieldStyles';

/* ── Combobox ──
 *
 * A Select with a search box: the trigger looks and behaves like ui/Select,
 * and opening it shows a text input (role="combobox") pinned above a
 * scrollable listbox. The active option is exposed with
 * aria-activedescendant while focus stays in the search box.
 *
 * Filtering is client-side by label and value unless the caller passes
 * `onSearchChange`, in which case the caller owns filtering (e.g. a debounced
 * server search) and `options` are shown as given.
 *
 * The popup stays in the DOM next to its trigger (no portal) so a Dialog's
 * focus trap keeps working; its height is clamped to the space the viewport
 * leaves, and it opens upward when there is more room above. */

export interface ComboboxOption {
  value: string;
  label: string;
}

interface ComboboxProps {
  value: string;
  onValueChange: (value: string, option: ComboboxOption) => void;
  options: ComboboxOption[];
  title?: string;
  /** Accessible name when there is no visible title label. */
  'aria-label'?: string;
  placeholder?: string;
  id?: string;
  disabled?: boolean;
  error?: string;
  hint?: ReactNode;
  className?: string;
  /** Trigger text when `value` is not among `options` (e.g. a server page
   *  that does not include the current selection). */
  selectedLabel?: string;
  searchPlaceholder?: string;
  /** Take over filtering: called with the raw search text on every change. */
  onSearchChange?: (query: string) => void;
  /** Results are being fetched. */
  loading?: boolean;
  /** The shown options do not belong to the current search text yet (a
   *  debounced or in-flight server search). Nothing can be picked until they
   *  do: no active option, Enter and clicks do nothing, the list is dimmed. */
  pending?: boolean;
  /** Shown when there are no options at all and nothing has been typed. */
  emptyMessage?: ReactNode;
  /** Shown when the search text matches nothing. */
  noMatchesMessage?: (query: string) => ReactNode;
  /** A status line under the list, e.g. "Showing first 50 — type to search". */
  footer?: ReactNode;
}

const MAX_POPUP_HEIGHT = 360;
const POPUP_GAP = 4;
const VIEWPORT_MARGIN = 12;

export function Combobox({
  value,
  onValueChange,
  options,
  title,
  'aria-label': ariaLabel,
  placeholder,
  id,
  disabled,
  error,
  hint,
  className = '',
  selectedLabel: selectedLabelProp,
  searchPlaceholder = 'Type to search…',
  onSearchChange,
  loading = false,
  pending = false,
  emptyMessage = 'No options',
  noMatchesMessage = (q) => `No matches for “${q}”`,
  footer,
}: ComboboxProps) {
  const generatedId = useId();
  const controlId = id || generatedId;
  const listId = `${controlId}-listbox`;
  const statusId = `${controlId}-status`;
  const optionId = (i: number) => `${controlId}-opt-${i}`;
  const name = title || ariaLabel || 'Select option';

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(-1);
  const [placement, setPlacement] = useState<{ above: boolean; maxHeight: number }>({
    above: false,
    maxHeight: MAX_POPUP_HEIGHT,
  });
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const serverFiltered = onSearchChange != null;
  const trimmed = query.trim();
  const visible = useMemo(() => {
    if (serverFiltered || trimmed === '') return options;
    const needle = trimmed.toLowerCase();
    return options.filter(
      (o) => o.label.toLowerCase().includes(needle) || o.value.toLowerCase().includes(needle),
    );
  }, [options, serverFiltered, trimmed]);

  const selectedLabel =
    options.find((o) => o.value === value)?.label ?? (value ? (selectedLabelProp ?? value) : undefined);

  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    setActiveIndex(-1);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  function updateQuery(next: string) {
    setQuery(next);
    setActiveIndex(next.trim() === '' ? -1 : 0);
    onSearchChange?.(next);
  }

  function openPopup() {
    if (disabled) return;
    if (query !== '') updateQuery('');
    const selectedIndex = options.findIndex((o) => o.value === value);
    setActiveIndex(selectedIndex);
    setOpen(true);
  }

  // Clamp the popup to the viewport and flip it upward when the space below
  // the trigger is short (dialogs on phones, fields near the bottom).
  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return;
    function place() {
      const rect = triggerRef.current!.getBoundingClientRect();
      const viewport = window.innerHeight || document.documentElement.clientHeight;
      const below = viewport - rect.bottom - POPUP_GAP - VIEWPORT_MARGIN;
      const above = rect.top - POPUP_GAP - VIEWPORT_MARGIN;
      const flip = below < 240 && above > below;
      const room = flip ? above : below;
      setPlacement({
        above: flip,
        // Never taller than the room the viewport leaves, even when that is
        // very little (a phone in landscape): the list inside scrolls.
        maxHeight: Math.max(0, Math.min(MAX_POPUP_HEIGHT, Math.floor(room))),
      });
    }
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [open]);

  // Focus the search box once the popup mounts.
  useEffect(() => {
    if (open) searchRef.current?.focus();
  }, [open]);

  // Outside click closes without stealing focus back.
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) close();
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open, close]);

  // Escape closes only the popup. A Dialog listens for Escape in the
  // document capture phase, so claim the key earlier, on window.
  useEffect(() => {
    if (!open) return;
    function handleEscape(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      if (!wrapperRef.current?.contains(e.target as Node)) return;
      e.preventDefault();
      e.stopPropagation();
      close(true);
    }
    window.addEventListener('keydown', handleEscape, true);
    return () => window.removeEventListener('keydown', handleEscape, true);
  }, [open, close]);

  // When a server search swaps the results in, the old active index points at
  // an unrelated row: start again from the first match (or nothing, when the
  // search box is empty). Keyed on the option values, not array identity.
  const visibleKey = visible.map((o) => o.value).join('\u0000');
  const [seenVisibleKey, setSeenVisibleKey] = useState(visibleKey);
  if (seenVisibleKey !== visibleKey) {
    setSeenVisibleKey(visibleKey);
    if (open && serverFiltered) setActiveIndex(trimmed && visible.length ? 0 : -1);
  }

  // Keep the active option in view while arrowing through a long list.
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    const item = listRef.current.querySelector(`[data-option-index="${activeIndex}"]`);
    item?.scrollIntoView?.({ block: 'nearest' });
  }, [activeIndex]);

  function choose(option: ComboboxOption) {
    onValueChange(option.value, option);
    close(true);
  }

  function handleTriggerKeyDown(e: ReactKeyboardEvent) {
    if (disabled || open) return;
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowUp':
      case 'Enter':
      case ' ':
        e.preventDefault();
        openPopup();
        break;
    }
  }

  function handleSearchKeyDown(e: ReactKeyboardEvent<HTMLInputElement>) {
    // Stale results are not navigable: they belong to an earlier search.
    const count = pending ? 0 : visible.length;
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        if (count) setActiveIndex((prev) => (prev + 1 >= count ? 0 : prev + 1));
        break;
      case 'ArrowUp':
        e.preventDefault();
        if (count) setActiveIndex((prev) => (prev - 1 < 0 ? count - 1 : prev - 1));
        break;
      case 'Home':
        if (count) {
          e.preventDefault();
          setActiveIndex(0);
        }
        break;
      case 'End':
        if (count) {
          e.preventDefault();
          setActiveIndex(count - 1);
        }
        break;
      case 'Enter':
        // Never submit the surrounding form from the search box.
        e.preventDefault();
        if (!pending && activeIndex >= 0 && visible[activeIndex]) choose(visible[activeIndex]);
        break;
      case 'Tab':
        close();
        break;
    }
  }

  let status: ReactNode = null;
  if (loading || pending) status = 'Searching…';
  else if (visible.length === 0) status = trimmed ? noMatchesMessage(trimmed) : emptyMessage;
  else if (footer) status = footer;

  const activeId =
    !pending && activeIndex >= 0 && activeIndex < visible.length ? optionId(activeIndex) : undefined;

  return (
    <FieldWrapper label={title} htmlFor={controlId} error={error} hint={hint}>
      <div ref={wrapperRef} className="relative">
        <button
          id={controlId}
          type="button"
          ref={triggerRef}
          disabled={disabled}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          aria-label={ariaLabel ?? (selectedLabel || placeholder ? undefined : name)}
          aria-describedby={describedBy(controlId, undefined, hint, error)}
          className={`${baseField} flex items-center justify-between gap-2 text-left ${!selectedLabel ? 'text-text-tertiary' : ''} ${error ? errorField : ''} ${className}`}
          onClick={() => (open ? close() : openPopup())}
          onKeyDown={handleTriggerKeyDown}
        >
          <span className="truncate">{selectedLabel || placeholder}</span>
          <svg
            className={`w-3.5 h-3.5 text-text-tertiary shrink-0 transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>

        {open && (
          <div
            data-testid="combobox-popup"
            className={`absolute left-0 right-0 z-40 flex flex-col rounded-lg border border-border bg-overlay overflow-hidden ${
              placement.above ? 'bottom-full mb-1' : 'top-full mt-1'
            }`}
            style={{ boxShadow: 'var(--shadow-overlay)', maxHeight: placement.maxHeight }}
          >
            <div className="p-2 border-b border-border shrink-0">
              <input
                ref={searchRef}
                type="text"
                role="combobox"
                aria-label={`Search ${name.toLowerCase()}`}
                // With no options the listbox is not rendered, so the
                // combobox reports itself collapsed and the status line
                // (aria-describedby) says why.
                aria-expanded={visible.length > 0}
                aria-controls={visible.length ? listId : undefined}
                aria-autocomplete="list"
                aria-activedescendant={activeId}
                aria-describedby={status ? statusId : undefined}
                autoComplete="off"
                spellCheck={false}
                value={query}
                placeholder={searchPlaceholder}
                onChange={(e) => updateQuery(e.target.value)}
                onKeyDown={handleSearchKeyDown}
                className={`${baseField} py-1.5`}
              />
            </div>
            {visible.length > 0 && (
              <ul
                ref={listRef}
                id={listId}
                role="listbox"
                aria-label={name}
                aria-busy={loading || pending || undefined}
                data-testid="combobox-list"
                className={`flex-1 min-h-0 overflow-y-auto overscroll-contain py-1 transition-opacity duration-150 ${pending ? 'opacity-60' : ''}`}
                style={{ maxHeight: MAX_POPUP_HEIGHT }}
              >
                {visible.map((opt, i) => {
                  const isSelected = opt.value === value;
                  const isActive = !pending && i === activeIndex;
                  return (
                    <li
                      key={opt.value}
                      id={optionId(i)}
                      role="option"
                      aria-selected={isSelected}
                      data-option-index={i}
                      onMouseEnter={() => setActiveIndex(i)}
                      onMouseDown={(e) => e.preventDefault() /* keep focus in the search box */}
                      aria-disabled={pending || undefined}
                      onClick={() => {
                        if (!pending) choose(opt);
                      }}
                      className={`${pending ? 'cursor-default' : 'cursor-pointer'} mx-1 px-2 py-1.5 text-sm flex items-center gap-2 transition-colors duration-150 rounded-sm ${
                        isActive ? 'bg-surface-active text-text-primary' : 'text-text-secondary'
                      }`}
                    >
                      <span className="w-4 shrink-0" aria-hidden="true">
                        {isSelected && (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <polyline points="20 6 9 17 4 12" />
                          </svg>
                        )}
                      </span>
                      <span className="flex-1 truncate" title={opt.label}>
                        {opt.label}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {/* Always mounted so screen readers hear loading / no-match /
                truncation changes as they happen. */}
            <div
              id={statusId}
              role="status"
              className={
                status
                  ? `shrink-0 px-3 py-2 text-xs text-text-tertiary ${visible.length ? 'border-t border-border' : ''}`
                  : 'sr-only'
              }
            >
              {status}
            </div>
          </div>
        )}
      </div>
    </FieldWrapper>
  );
}
