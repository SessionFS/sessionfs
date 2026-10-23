import type { ReactNode } from 'react';

type AlertVariant = 'info' | 'success' | 'warning' | 'danger';

interface AlertProps {
  variant?: AlertVariant;
  title?: ReactNode;
  children?: ReactNode;
  /** Trailing control, e.g. a dismiss or retry button */
  action?: ReactNode;
  className?: string;
}

const TOKENS: Record<AlertVariant, string> = {
  info: '--info',
  success: '--success',
  warning: '--warning',
  danger: '--danger',
};

const ICONS: Record<AlertVariant, ReactNode> = {
  info: (
    <>
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </>
  ),
  success: (
    <>
      <circle cx="12" cy="12" r="10" />
      <polyline points="8 12 11 15 16 9" />
    </>
  ),
  warning: (
    <>
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </>
  ),
  danger: (
    <>
      <circle cx="12" cy="12" r="10" />
      <line x1="15" y1="9" x2="9" y2="15" />
      <line x1="9" y1="9" x2="15" y2="15" />
    </>
  ),
};

/** Inline banner. Tints derive from the semantic token so they follow the theme. */
export function Alert({ variant = 'info', title, children, action, className = '' }: AlertProps) {
  const token = TOKENS[variant];
  return (
    <div
      role={variant === 'danger' ? 'alert' : 'status'}
      className={`flex items-start gap-3 rounded-lg border p-3 ${className}`}
      style={{
        backgroundColor: `color-mix(in oklch, var(${token}) 10%, transparent)`,
        borderColor: `color-mix(in oklch, var(${token}) 30%, transparent)`,
      }}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        className="mt-0.5 shrink-0"
        style={{ color: `var(${token})` }}
      >
        {ICONS[variant]}
      </svg>
      <div className="min-w-[9rem] flex-1 text-sm text-text-primary">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={title ? 'mt-0.5 text-text-secondary' : ''}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
