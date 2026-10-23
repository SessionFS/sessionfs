import { useId, type ButtonHTMLAttributes, type ReactNode } from 'react';

interface SwitchProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange' | 'role' | 'aria-checked'> {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: ReactNode;
}

/** role=switch toggle; Space/Enter activate via native button behaviour. */
export function Switch({ checked, onChange, label, id, disabled, className = '', ...rest }: SwitchProps) {
  const autoId = useId();
  const switchId = id ?? autoId;
  return (
    <div className={`inline-flex items-center gap-2 ${className}`}>
      <button
        {...rest}
        id={switchId}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-border transition-colors duration-150 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:cursor-not-allowed disabled:opacity-50 ${checked ? 'bg-brand' : 'bg-bg-tertiary'}`}
      >
        <span
          aria-hidden="true"
          className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform duration-150 ${checked ? 'translate-x-[18px]' : 'translate-x-0.5'}`}
        />
      </button>
      {label && (
        <label htmlFor={switchId} className={`text-sm text-text-primary ${disabled ? 'opacity-50' : 'cursor-pointer'}`}>
          {label}
        </label>
      )}
    </div>
  );
}
