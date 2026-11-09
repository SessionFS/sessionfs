import { useId, type ButtonHTMLAttributes, type MouseEvent, type ReactNode, type Ref } from 'react';
import { Tooltip } from './Tooltip';

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
type ButtonSize = 'sm' | 'md';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  /** Why the button can't be used right now. When the button is disabled
   *  (not merely loading) it stays focusable via aria-disabled, ignores
   *  clicks, and shows this in a tooltip and as its description. */
  disabledReason?: string;
  children: ReactNode;
  /** React 19 passes ref as a prop; it lands on the <button> via ...rest. */
  ref?: Ref<HTMLButtonElement>;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    'bg-brand text-white hover:opacity-90 shadow-[0_4px_16px_var(--brand-glow)]',
  secondary:
    'bg-surface text-text-secondary border border-border hover:bg-bg-elevated hover:border-border-strong',
  ghost:
    'bg-transparent text-text-secondary hover:bg-surface-hover hover:text-text-primary',
  danger:
    'bg-[var(--danger-solid)] text-white hover:opacity-90',
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-xs',
  md: 'px-4 py-2 text-sm',
};

function Spinner() {
  return (
    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled,
  disabledReason,
  children,
  className = '',
  onClick,
  'aria-describedby': describedBy,
  ...rest
}: ButtonProps) {
  const reasonId = useId();
  const explained = Boolean(disabled && !loading && disabledReason);
  const isDisabled = explained ? false : disabled || loading;

  const button = (
    <button
      disabled={isDisabled}
      aria-disabled={explained ? true : undefined}
      aria-describedby={explained ? reasonId : describedBy}
      onClick={(e: MouseEvent<HTMLButtonElement>) => {
        if (explained) {
          e.preventDefault();
          return;
        }
        onClick?.(e);
      }}
      className={`inline-flex items-center justify-center gap-2 font-semibold rounded-lg transition-[background-color,border-color,opacity,transform] duration-150 ease-out outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] active:scale-[0.97] disabled:opacity-50 disabled:cursor-not-allowed disabled:scale-100 aria-disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:scale-100 ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...rest}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );

  // Always the same tree, so a button that becomes enabled keeps its DOM
  // node (and focus); the tooltip only shows while there is a reason.
  return (
    <>
      <Tooltip content={explained ? disabledReason : null} multiline describe={false} toggleOnClick>
        {button}
      </Tooltip>
      {explained && (
        <span id={reasonId} hidden>
          {disabledReason}
        </span>
      )}
    </>
  );
}
