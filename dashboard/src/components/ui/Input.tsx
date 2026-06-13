import { useState, useRef, useEffect, useCallback, useId, type InputHTMLAttributes, type TextareaHTMLAttributes, type ReactNode } from 'react';

/* ── shared field styling ── */

const baseField =
  'w-full bg-bg-sunken border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary transition-[background-color,border-color,box-shadow] duration-150 ease-out outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 disabled:cursor-not-allowed';

const errorField =
  'border-[var(--danger)] focus-visible:border-[var(--danger)] focus-visible:shadow-[0_0_0_3px_rgba(240,64,96,0.25)]';

/* ── field wrapper ── */

interface FieldWrapperProps {
  label?: string;
  htmlFor?: string;
  error?: string;
  children: ReactNode;
}

function FieldWrapper({ label, htmlFor, error, children }: FieldWrapperProps) {
  return (
    <div className="flex flex-col gap-1">
      {label && (
        <label htmlFor={htmlFor} className="text-sm font-medium text-text-secondary">
          {label}
        </label>
      )}
      {children}
      {error && (
        <p className="text-xs text-danger mt-0.5" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

/* ── Input ── */

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  error?: string;
}

export function Input({ error, className = '', id, title, ...rest }: InputProps) {
  const generatedId = useId();
  const controlId = id || generatedId;

  return (
    <FieldWrapper label={title} htmlFor={controlId} error={error}>
      <input
        id={controlId}
        title={title}
        className={`${baseField} ${error ? errorField : ''} ${className}`}
        {...rest}
      />
    </FieldWrapper>
  );
}

/* ── Textarea ── */

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  error?: string;
}

export function Textarea({ error, className = '', id, title, ...rest }: TextareaProps) {
  const generatedId = useId();
  const controlId = id || generatedId;

  return (
    <FieldWrapper label={title} htmlFor={controlId} error={error}>
      <textarea
        id={controlId}
        title={title}
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
}: SelectProps) {
  const generatedId = useId();
  const controlId = id || generatedId;
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
      switch (e.key) {
        case 'Escape':
          e.preventDefault();
          close();
          break;
        case 'ArrowDown':
          e.preventDefault();
          setActiveIndex((prev) => {
            const next = prev + 1;
            return next >= options.length ? 0 : next;
          });
          break;
        case 'ArrowUp':
          e.preventDefault();
          setActiveIndex((prev) => {
            const next = prev - 1;
            return next < 0 ? options.length - 1 : next;
          });
          break;
        case 'Enter':
          e.preventDefault();
          if (activeIndex >= 0 && options[activeIndex]) {
            onValueChange(options[activeIndex].value);
            close();
          }
          break;
      }
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [open, activeIndex, options, onValueChange, close]);

  // Scroll active into view
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    const item = listRef.current.querySelector(`[data-option-index="${activeIndex}"]`);
    item?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  const selectedLabel = options.find((o) => o.value === value)?.label;

  return (
    <FieldWrapper label={title} htmlFor={controlId} error={error}>
      <div ref={wrapperRef} className="relative">
        <button
          id={controlId}
          type="button"
          disabled={disabled}
          role="combobox"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label={ariaLabel}
          className={`${baseField} flex items-center justify-between gap-2 text-left ${!selectedLabel ? 'text-text-tertiary' : ''} ${error ? errorField : ''} ${className}`}
          onClick={() => !disabled && setOpen((v) => !v)}
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
            <ul ref={listRef} role="listbox" aria-label={title || 'Select option'}>
              {options.map((opt, i) => {
                const isSelected = opt.value === value;
                const isActive = i === activeIndex;
                return (
                  <li key={opt.value} role="option" aria-selected={isSelected}>
                    <button
                      type="button"
                      data-option-index={i}
                      className={`w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 transition-colors duration-150 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-sm ${
                        isActive
                          ? 'bg-surface-active text-text-primary'
                          : 'text-text-secondary hover:bg-surface-hover'
                      }`}
                      onClick={() => {
                        onValueChange(opt.value);
                        close();
                      }}
                    >
                      <span className="w-4 shrink-0">
                        {isSelected && (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <polyline points="20 6 9 17 4 12" />
                          </svg>
                        )}
                      </span>
                      <span className="flex-1 truncate">{opt.label}</span>
                    </button>
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
