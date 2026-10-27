import { useId, type ReactNode } from 'react';
import { Button } from '../../components/ui';

/* Shared building blocks for the org integration pages (Jira, Confluence).
 * Calm by default: hairline-divided sections instead of nested cards, one
 * heading level per section, and problems that only appear when non-zero. */

/** A titled section inside an integration's detail tab. */
export function IntegrationSection({
  title,
  description,
  actions,
  children,
  label,
  className = '',
}: {
  title: string;
  description?: ReactNode;
  /** Right-aligned controls next to the title (e.g. "Add mapping"). */
  actions?: ReactNode;
  children: ReactNode;
  /** Accessible name for the region when it differs from the visible title. */
  label?: string;
  className?: string;
}) {
  const headingId = useId();
  return (
    <section
      className={`py-5 first:pt-0 ${className}`}
      {...(label ? { 'aria-label': label } : { 'aria-labelledby': headingId })}
    >
      <div className="mb-3 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 flex-[1_1_16rem]">
          <h3 id={headingId} className="text-sm font-semibold text-text-primary">
            {title}
          </h3>
          {description && <p className="mt-0.5 text-sm text-text-tertiary">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

/** Sections separated by hairlines, the calm replacement for stacked cards. */
export function SectionStack({ children }: { children: ReactNode }) {
  return <div className="divide-y divide-border">{children}</div>;
}

/** Label/value pairs, e.g. the read-only connection summary. */
export function SummaryList({ items }: { items: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="max-w-2xl divide-y divide-border border-y border-border text-sm">
      {items.map((item) => (
        <div key={item.label} className="grid grid-cols-1 gap-x-4 gap-y-0.5 py-2 sm:grid-cols-[11rem_1fr]">
          <dt className="text-text-tertiary">{item.label}</dt>
          <dd className="min-w-0 break-words text-text-primary">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Connection state dot + label, used on the overview rows and detail headers. */
export function ConnectionState({ connected, label }: { connected: boolean; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-text-secondary">
      <span
        aria-hidden="true"
        className={`inline-block h-2 w-2 rounded-full ${connected ? 'bg-success' : 'bg-text-tertiary'}`}
      />
      {label ?? (connected ? 'Connected' : 'Not connected')}
    </span>
  );
}

export type ProblemTone = 'danger' | 'warning' | 'info';

/** One non-zero health finding, with an optional fix action. */
export interface IntegrationProblem {
  key: string;
  tone: ProblemTone;
  title: string;
  /** Short phrase for the one-line overview summary; omit for info rows. */
  summary?: string;
  body?: ReactNode;
  /** Ticket ids, page keys, etc. Rendered under a disclosure when long. */
  details?: ReactNode[];
  action?: { label: string; onClick: () => void };
}

const TONE_DOT: Record<ProblemTone, string> = {
  danger: 'bg-danger',
  warning: 'bg-warning',
  info: 'bg-info',
};

const TONE_TITLE: Record<ProblemTone, string> = {
  danger: 'text-danger',
  warning: 'text-text-primary',
  info: 'text-text-primary',
};

/** Up to this many detail lines show inline; more fold under a disclosure. */
const INLINE_DETAILS = 3;

/**
 * Health findings as a divided list. Only non-zero problems are passed in;
 * with none, a single quiet "All good" line is shown instead.
 */
export function ProblemList({
  problems,
  allGood,
  label,
}: {
  problems: IntegrationProblem[];
  allGood: string;
  label: string;
}) {
  if (problems.length === 0) {
    return (
      <p className="flex items-center gap-2 text-sm text-text-secondary" role="status">
        <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full bg-success" />
        {allGood}
      </p>
    );
  }
  return (
    <ul aria-label={label} className="divide-y divide-border rounded-lg border border-border bg-surface">
      {problems.map((p) => {
        const details = p.details ?? [];
        const inline = details.length <= INLINE_DETAILS;
        return (
          <li key={p.key} className="flex flex-wrap items-start gap-x-3 gap-y-2 px-4 py-3">
            <span aria-hidden="true" className={`mt-1.5 inline-block h-2 w-2 shrink-0 rounded-full ${TONE_DOT[p.tone]}`} />
            <div className="min-w-0 flex-[1_1_14rem]">
              <p className={`text-sm font-medium ${TONE_TITLE[p.tone]}`}>{p.title}</p>
              {p.body && <div className="mt-0.5 text-xs leading-5 text-text-tertiary">{p.body}</div>}
              {details.length > 0 &&
                (inline ? (
                  <ul className="mt-1 space-y-0.5 text-xs text-text-secondary">
                    {details.map((d, i) => (
                      <li key={i} className="break-all">{d}</li>
                    ))}
                  </ul>
                ) : (
                  <details className="mt-1 text-xs text-text-secondary">
                    <summary className="cursor-pointer text-text-tertiary hover:text-text-primary">
                      Show {details.length}
                    </summary>
                    <ul className="mt-1 space-y-0.5">
                      {details.map((d, i) => (
                        <li key={i} className="break-all">{d}</li>
                      ))}
                    </ul>
                  </details>
                ))}
            </div>
            {p.action && (
              <Button size="sm" variant="secondary" className="ml-5 sm:ml-0" onClick={p.action.onClick}>
                {p.action.label}
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** "1 unmapped project · 1 push failure" from the actionable problems. */
export function problemSummary(problems: { summary?: string }[]): string {
  return problems
    .map((p) => p.summary)
    .filter((s): s is string => !!s)
    .join(' · ');
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The host part of a site URL (full URL on hover). */
export function SiteHost({ url }: { url: string | undefined | null }) {
  if (!url) return null;
  let host = url;
  try {
    host = new URL(url).host;
  } catch {
    /* keep the raw value */
  }
  return (
    <span className="font-mono text-xs text-text-secondary" title={url}>
      {host}
    </span>
  );
}
