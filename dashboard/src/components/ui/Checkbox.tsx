import { useId, type InputHTMLAttributes, type ReactNode } from 'react';

interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label?: ReactNode;
  description?: ReactNode;
}

export function Checkbox({ label, description, id, className = '', disabled, ...rest }: CheckboxProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  return (
    <div className={`flex items-start gap-2 ${className}`}>
      <input
        id={inputId}
        type="checkbox"
        disabled={disabled}
        className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-border accent-[var(--brand)] outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:cursor-not-allowed disabled:opacity-50"
        {...rest}
      />
      {(label || description) && (
        <label htmlFor={inputId} className={`text-sm ${disabled ? 'opacity-50' : 'cursor-pointer'}`}>
          {label && <span className="text-text-primary">{label}</span>}
          {description && <span className="block text-xs text-text-tertiary">{description}</span>}
        </label>
      )}
    </div>
  );
}
