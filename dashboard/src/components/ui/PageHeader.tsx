import type { ReactNode } from 'react';

interface PageHeaderProps {
  /** Rendered as the page's single h1. */
  title: ReactNode;
  description?: ReactNode;
  /** Right-aligned controls (buttons, view toggles). */
  actions?: ReactNode;
  /** Breadcrumb or back link shown above the title. */
  breadcrumb?: ReactNode;
  className?: string;
}

export function PageHeader({ title, description, actions, breadcrumb, className = 'mb-5' }: PageHeaderProps) {
  return (
    <header className={className}>
      {breadcrumb && <div className="mb-2 text-sm text-text-tertiary">{breadcrumb}</div>}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h1 className="text-3xl font-bold tracking-tight text-text-primary">{title}</h1>
        {actions && <div className="flex items-center gap-3">{actions}</div>}
      </div>
      {description && <p className="mt-2 text-sm text-text-tertiary">{description}</p>}
    </header>
  );
}
