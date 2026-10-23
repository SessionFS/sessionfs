import type { ButtonHTMLAttributes, ReactNode } from 'react';

type IconButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
type IconButtonSize = 'sm' | 'md';

interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> {
  /** Accessible name; also shown as the native tooltip */
  label: string;
  variant?: IconButtonVariant;
  size?: IconButtonSize;
  children: ReactNode;
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
  ...rest
}: IconButtonProps) {
  return (
    <button
      type={type}
      aria-label={label}
      title={title ?? label}
      className={`inline-flex items-center justify-center rounded-lg transition-[background-color,border-color,color,opacity,transform] duration-150 ease-out outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 disabled:cursor-not-allowed ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}
