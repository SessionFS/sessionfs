import type { ReactNode } from 'react';

/** A centred, designed state for the drawer body (no plan, no key, errors). */
export default function HelperNotice({
  icon,
  tone = 'brand',
  title,
  children,
  actions,
}: {
  icon: ReactNode;
  tone?: 'brand' | 'warning' | 'danger';
  title: string;
  children: ReactNode;
  actions?: ReactNode;
}) {
  const tint =
    tone === 'warning'
      ? 'bg-[color-mix(in_oklch,var(--warning)_16%,transparent)] text-warning'
      : tone === 'danger'
        ? 'bg-[color-mix(in_oklch,var(--danger)_16%,transparent)] text-danger'
        : 'bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand';
  return (
    <div className="flex flex-1 flex-col justify-center px-6 py-10 sm:px-8">
      <div className="mx-auto w-full max-w-sm text-center">
        <span className={`mx-auto grid h-11 w-11 place-items-center rounded-xl ${tint}`}>{icon}</span>
        <h3 className="mt-4 !text-lg !font-semibold text-text-primary">{title}</h3>
        <div className="mt-2 space-y-2 text-sm text-text-secondary">{children}</div>
        {actions && <div className="mt-5 flex flex-wrap items-center justify-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}
