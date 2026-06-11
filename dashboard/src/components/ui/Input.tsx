import type { InputHTMLAttributes, TextareaHTMLAttributes, SelectHTMLAttributes, ReactNode } from 'react';

/* ── shared field styling ── */

const baseField =
  'w-full bg-[var(--bg-sunken)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-tertiary)] transition-[background-color,border-color,box-shadow] duration-150 ease-out outline-none focus:border-[var(--brand)] focus:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 disabled:cursor-not-allowed';

const errorField =
  'border-[var(--danger)] focus:border-[var(--danger)] focus:shadow-[0_0_0_3px_rgba(240,64,96,0.25)]';

/* ── field wrapper ── */

interface FieldWrapperProps {
  label?: string;
  error?: string;
  children: ReactNode;
}

function fieldId(label: string): string {
  return `field-${label.toLowerCase().replace(/\s+/g, '-')}`;
}

function FieldWrapper({ label, error, children }: FieldWrapperProps) {
  return (
    <div className="flex flex-col gap-1">
      {label && (
        <label htmlFor={fieldId(label)} className="text-[13px] font-medium text-[var(--text-secondary)]">
          {label}
        </label>
      )}
      {children}
      {error && (
        <p className="text-[12px] text-[var(--danger)] mt-0.5" role="alert">
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

export function Input({ error, className = '', id, ...rest }: InputProps) {
  return (
    <FieldWrapper label={rest.title} error={error}>
      <input
        id={id}
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

export function Textarea({ error, className = '', id, ...rest }: TextareaProps) {
  return (
    <FieldWrapper label={rest.title} error={error}>
      <textarea
        id={id}
        className={`${baseField} resize-y min-h-[80px] ${error ? errorField : ''} ${className}`}
        {...rest}
      />
    </FieldWrapper>
  );
}

/* ── Select ── */

interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  error?: string;
  options: Array<{ value: string; label: string }>;
}

export function Select({ error, options, className = '', id, ...rest }: SelectProps) {
  return (
    <FieldWrapper label={rest.title} error={error}>
      <div className="relative">
        <select
          id={id}
          className={`${baseField} appearance-none pr-8 cursor-pointer ${error ? errorField : ''} ${className}`}
          {...rest}
        >
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <svg
          className="absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--text-tertiary)] pointer-events-none"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </div>
    </FieldWrapper>
  );
}
