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
      {/* Title and description stay together; on narrow screens the actions
          wrap below both instead of splitting them. */}
      <div className={`flex flex-wrap justify-between ${description ? 'items-start gap-x-6 gap-y-3' : 'items-center gap-3'}`}>
        {/* Without a description the title only takes its own width, so
            actions stay beside it whenever they fit (the original layout). */}
        <div className={description ? 'min-w-0 flex-[1_1_18rem]' : 'min-w-0'}>
          <h1 className="text-3xl font-bold tracking-tight text-text-primary">{title}</h1>
          {description && <p className="mt-2 text-sm text-text-tertiary">{description}</p>}
        </div>
        {actions && <div className={`flex items-center gap-3 ${description ? 'flex-wrap' : ''}`}>{actions}</div>}
      </div>
    </header>
  );
}
