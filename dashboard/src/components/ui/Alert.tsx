import type { ReactNode } from 'react';
import { CircleCheck, CircleX, Info, TriangleAlert, type LucideIcon } from 'lucide-react';
import { Icon } from './Icon';

type AlertVariant = 'info' | 'success' | 'warning' | 'danger';

interface AlertProps {
  variant?: AlertVariant;
  title?: ReactNode;
  children?: ReactNode;
  /** Trailing control, e.g. a dismiss or retry button */
  action?: ReactNode;
  className?: string;
  /** Set false inside an existing aria-live region so the message is
   *  announced once (the region announces it; the Alert adds no role). */
  announce?: boolean;
}

const TOKENS: Record<AlertVariant, string> = {
  info: '--info',
  success: '--success',
  warning: '--warning',
  danger: '--danger',
};

const ICONS: Record<AlertVariant, LucideIcon> = {
  info: Info,
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleX,
};

/** Inline banner. Tints derive from the semantic token so they follow the theme. */
export function Alert({ variant = 'info', title, children, action, className = '', announce = true }: AlertProps) {
  const token = TOKENS[variant];
  return (
    <div
      role={announce ? (variant === 'danger' ? 'alert' : 'status') : undefined}
      className={`flex items-start gap-3 rounded-lg border p-3 ${className}`}
      style={{
        backgroundColor: `color-mix(in oklch, var(${token}) 10%, transparent)`,
        borderColor: `color-mix(in oklch, var(${token}) 30%, transparent)`,
      }}
    >
      <Icon icon={ICONS[variant]} className="mt-0.5 shrink-0" style={{ color: `var(${token})` }} />
      <div className="min-w-[9rem] flex-1 text-sm text-text-primary">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={title ? 'mt-0.5 text-text-secondary' : ''}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
