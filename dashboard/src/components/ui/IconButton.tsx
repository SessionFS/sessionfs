import { useId, type ButtonHTMLAttributes, type MouseEvent, type ReactNode, type Ref } from 'react';
import { Tooltip } from './Tooltip';

type IconButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
type IconButtonSize = 'sm' | 'md';

interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> {
  /** Accessible name; also shown as a tooltip on hover and keyboard focus. */
  label: string;
  /** Why the button can't be used right now. With `disabled`, the button
   *  stays focusable (aria-disabled) and the tooltip says this instead. */
  disabledReason?: string;
  variant?: IconButtonVariant;
  size?: IconButtonSize;
  children: ReactNode;
  /** Forwarded to the <button> (React 19 passes ref as a prop). */
  ref?: Ref<HTMLButtonElement>;
}

const VARIANT_CLASSES: Record<IconButtonVariant, string> = {
  primary: 'bg-brand text-white hover:opacity-90',
  secondary:
    'bg-surface text-text-secondary border border-border hover:bg-bg-elevated hover:border-border-strong',
  ghost: 'bg-transparent text-text-tertiary hover:bg-surface-hover hover:text-text-primary',
  danger: 'bg-[var(--danger)] text-white hover:opacity-90',
};

const SIZE_CLASSES: Record<IconButtonSize, string> = {
  sm: 'p-1',
  md: 'p-2',
};

export function IconButton({
  label,
  variant = 'ghost',
  size = 'md',
  type = 'button',
  title,
  className = '',
  children,
  disabled,
  disabledReason,
  onClick,
  'aria-describedby': describedBy,
  ...rest
}: IconButtonProps) {
  const reasonId = useId();
  const explained = Boolean(disabled && disabledReason);
  const button = (
    <button
      type={type}
      aria-label={label}
      title={title}
      disabled={explained ? undefined : disabled}
      aria-disabled={explained ? true : undefined}
      aria-describedby={explained ? reasonId : describedBy}
      onClick={(e: MouseEvent<HTMLButtonElement>) => {
        if (explained) {
          e.preventDefault();
          return;
        }
        onClick?.(e);
      }}
      className={`inline-flex items-center justify-center rounded-lg transition-[background-color,border-color,color,opacity,transform] duration-150 ease-out outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 disabled:cursor-not-allowed aria-disabled:opacity-50 aria-disabled:cursor-not-allowed ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
  return (
    <>
      <Tooltip content={explained ? `${label}: ${disabledReason}` : label} describe={false}>
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
