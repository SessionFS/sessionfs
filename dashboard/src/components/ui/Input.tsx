import { useState, useRef, useEffect, useCallback, useId, type InputHTMLAttributes, type TextareaHTMLAttributes, type ReactNode } from 'react';

/* ── shared field styling ── */

const baseField =
  'w-full bg-bg-sunken border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary transition-[background-color,border-color,box-shadow] duration-150 ease-out outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 disabled:cursor-not-allowed';

const errorField =
  'border-[var(--danger)] focus-visible:border-[var(--danger)] focus-visible:shadow-[0_0_0_3px_color-mix(in_srgb,var(--danger)_25%,transparent)]';

/* ── field wrapper ── */

interface FieldWrapperProps {
  label?: string;
  htmlFor?: string;
  error?: string;
  /** Helper text under the control; tied to it with aria-describedby. */
  hint?: ReactNode;
  children: ReactNode;
}

function FieldWrapper({ label, htmlFor, error, hint, children }: FieldWrapperProps) {
  return (
    <div className="flex flex-col gap-1">
      {label && (
        <label htmlFor={htmlFor} className="text-sm font-medium text-text-secondary">
          {label}
        </label>
      )}
      {children}
      {hint && (
        <p id={htmlFor ? `${htmlFor}-hint` : undefined} className="text-xs text-text-tertiary">
          {hint}
        </p>
      )}
      {error && (
        <p id={htmlFor ? `${htmlFor}-error` : undefined} className="text-xs text-danger mt-0.5" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

/** Join the caller's aria-describedby with the hint/error ids the wrapper renders. */
function describedBy(controlId: string, own: string | undefined, hint: unknown, error: unknown): string | undefined {
  const ids = [own, hint ? `${controlId}-hint` : null, error ? `${controlId}-error` : null].filter(Boolean);
  return ids.length ? ids.join(' ') : undefined;
}

/* ── Input ── */

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  error?: string;
  hint?: ReactNode;
}

export function Input({ error, hint, className = '', id, title, 'aria-describedby': ariaDescribedBy, ...rest }: InputProps) {
  const generatedId = useId();
  const controlId = id || generatedId;

  return (
    <FieldWrapper label={title} htmlFor={controlId} error={error} hint={hint}>
      <input
        id={controlId}
        title={title}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(controlId, ariaDescribedBy, hint, error)}
        className={`${baseField} ${error ? errorField : ''} ${className}`}
        {...rest}
      />
    </FieldWrapper>
  );
}

/* ── Textarea ── */

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  error?: string;
  hint?: ReactNode;
}

export function Textarea({ error, hint, className = '', id, title, 'aria-describedby': ariaDescribedBy, ...rest }: TextareaProps) {
  const generatedId = useId();
  const controlId = id || generatedId;

  return (
    <FieldWrapper label={title} htmlFor={controlId} error={error} hint={hint}>
      <textarea
        id={controlId}
        title={title}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(controlId, ariaDescribedBy, hint, error)}
        className={`${baseField} resize-y min-h-[80px] ${error ? errorField : ''} ${className}`}
        {...rest}
      />
    </FieldWrapper>
  );
}

/* ── Select ──
 *
 * Custom anchored dropdown built on the same positioning + keyboard
 * machinery as ui/Dropdown. Replaces the native <select> so popup
 * placement is deterministic (native popups can't be reliably
 * positioned via CSS — CEO hit this when the invite Role dropdown
 * rendered its options detached far to the right). */

interface SelectOption {
  value: string;
  label: string;
}

interface SelectProps {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  title?: string;
  error?: string;
  placeholder?: string;
  id?: string;
  disabled?: boolean;
  className?: string;
  /** Accessible name when there is no visible title label. */
  'aria-label'?: string;
  /** Helper text under the control; tied to it with aria-describedby. */
  hint?: ReactNode;
}

export function Select({
  value,
  onValueChange,
  options,
  title,
  error,
  placeholder,
  id,
  disabled,
  className = '',
  'aria-label': ariaLabel,
  hint,
}: SelectProps) {
  const generatedId = useId();
  const controlId = id || generatedId;
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    setActiveIndex(-1);
    if (restoreFocus) triggerRef.current?.focus();
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

  // One keyboard handler shared by the trigger button (closed: open on
  // Arrow/Enter/Space) and the listbox (open: navigation). Focus moves to the
  // listbox while open and the active option is exposed with
  // aria-activedescendant, matching the native <select> keyboard contract.
  const selectedIndex = options.findIndex((o) => o.value === value);
  function openAt(index: number) {
    setActiveIndex(index < 0 ? 0 : index);
    setOpen(true);
  }
  function handleTriggerKeyDown(e: React.KeyboardEvent) {
    if (disabled) return;
    if (!open) {
      switch (e.key) {
        case 'ArrowDown':
        case 'ArrowUp':
        case 'Enter':
        case ' ':
          e.preventDefault();
          openAt(selectedIndex);
          break;
        case 'Home':
          e.preventDefault();
          openAt(0);
          break;
        case 'End':
          e.preventDefault();
          openAt(options.length - 1);
          break;
      }
      return;
    }
    switch (e.key) {
      case 'Escape':
        e.preventDefault();
        close(true);
        break;
      case 'Tab':
        close();
        break;
      case 'ArrowDown':
        e.preventDefault();
        setActiveIndex((prev) => (prev + 1 >= options.length ? 0 : prev + 1));
        break;
      case 'ArrowUp':
        e.preventDefault();
        setActiveIndex((prev) => (prev - 1 < 0 ? options.length - 1 : prev - 1));
        break;
      case 'Home':
        e.preventDefault();
        setActiveIndex(0);
        break;
      case 'End':
        e.preventDefault();
        setActiveIndex(options.length - 1);
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        if (activeIndex >= 0 && options[activeIndex]) {
          onValueChange(options[activeIndex].value);
          close(true);
        }
        break;
    }
  }

  // Hand focus to the listbox once it mounts so it owns aria-activedescendant.
  useEffect(() => {
    if (open) listRef.current?.focus();
  }, [open]);

  // Scroll active into view
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    const item = listRef.current.querySelector(`[data-option-index="${activeIndex}"]`);
    item?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  const selectedLabel = options.find((o) => o.value === value)?.label;
  const listId = `${controlId}-listbox`;
  const optionId = (i: number) => `${controlId}-opt-${i}`;

  return (
    <FieldWrapper label={title} htmlFor={controlId} error={error} hint={hint}>
      <div ref={wrapperRef} className="relative">
        <button
          id={controlId}
          type="button"
          disabled={disabled}
          ref={triggerRef}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          aria-label={ariaLabel ?? (selectedLabel || placeholder ? undefined : title || 'Select option')}
          aria-describedby={describedBy(controlId, undefined, hint, error)}
          className={`${baseField} flex items-center justify-between gap-2 text-left ${!selectedLabel ? 'text-text-tertiary' : ''} ${error ? errorField : ''} ${className}`}
          onClick={() => !disabled && setOpen((v) => !v)}
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
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>

        {open && (
          <div
            className="absolute top-full left-0 mt-1 z-40 min-w-full rounded-lg py-1 border border-border bg-overlay"
            style={{ boxShadow: 'var(--shadow-overlay)' }}
          >
            {/* Listbox popup with aria-activedescendant — options are NOT
                separately focusable; the listbox itself takes focus. */}
            <ul
              ref={listRef}
              id={listId}
              role="listbox"
              tabIndex={-1}
              aria-label={title || ariaLabel || 'Select option'}
              aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
              onKeyDown={handleTriggerKeyDown}
              className="outline-none"
            >
              {options.map((opt, i) => {
                const isSelected = opt.value === value;
                const isActive = i === activeIndex;
                return (
                  <li
                    key={opt.value}
                    id={optionId(i)}
                    role="option"
                    aria-selected={isSelected}
                    data-option-index={i}
                    onMouseEnter={() => setActiveIndex(i)}
                    onMouseDown={(e) => e.preventDefault() /* keep focus on the listbox */}
                    onClick={() => {
                      onValueChange(opt.value);
                      close(true);
                    }}
                    className={`cursor-pointer mx-1 px-2 py-1.5 text-sm flex items-center gap-2 transition-colors duration-150 rounded-sm ${
                      isActive
                        ? 'bg-surface-active text-text-primary'
                        : 'text-text-secondary'
                    }`}
                  >
                    <span className="w-4 shrink-0">
                      {isSelected && (
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="20 6 9 17 4 12" />
                        </svg>
                      )}
                    </span>
                    <span className="flex-1 truncate">{opt.label}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>
    </FieldWrapper>
  );
}
