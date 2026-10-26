/**
 * Clickable references inside helper answers. `linkifyRefs` (format.ts) turns
 * ids into `#sfs-ref-<kind>-<id>` links; `renderRefLink` maps those to a
 * ticket-preview button or an in-app link. The provider sits inside the
 * drawer so the preview opens over the conversation instead of leaving it.
 */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { useProject } from '../hooks/useProjects';
import { parseRefHref, repoLinksFor, sourceHref, type RefKind, type RepoLinks } from './format';
import TicketPreview from './TicketPreview';

interface HelperRefs {
  /** GitHub repo for PR / commit links, when the scope's project has one. */
  repo: RepoLinks | null;
  /** Ticket whose preview is open, if any. */
  previewId: string | null;
  openTicket: (ticketId: string, trigger: HTMLElement) => void;
  onNavigate: () => void;
}

const RefsContext = createContext<HelperRefs | null>(null);

export function useHelperRefs(): { repo: RepoLinks | null } {
  return { repo: useContext(RefsContext)?.repo ?? null };
}

export function HelperRefsProvider({
  projectId,
  onNavigate,
  children,
}: {
  projectId?: string;
  onNavigate: () => void;
  children: ReactNode;
}) {
  // Cached from the project page in the usual case, so this costs nothing.
  const project = useProject(projectId ?? '');
  const repos = projectId && project.data?.id === projectId ? project.data.repos : undefined;
  const repo = useMemo(() => repoLinksFor(repos), [repos]);

  const [preview, setPreview] = useState<{ id: string; trigger: HTMLElement } | null>(null);
  const openTicket = useCallback((id: string, trigger: HTMLElement) => {
    setPreview((cur) => (cur?.id === id && cur.trigger === trigger ? null : { id, trigger }));
  }, []);
  const close = useCallback(() => setPreview(null), []);

  const value = useMemo<HelperRefs>(
    () => ({ repo, previewId: preview?.id ?? null, openTicket, onNavigate }),
    [repo, preview?.id, openTicket, onNavigate],
  );

  return (
    <RefsContext.Provider value={value}>
      {children}
      {preview && (
        <TicketPreview
          key={preview.id}
          ticketId={preview.id}
          trigger={preview.trigger}
          onClose={close}
          onNavigate={() => {
            close();
            onNavigate();
          }}
        />
      )}
    </RefsContext.Provider>
  );
}

const FOCUS = 'outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]';
const LINK = `rounded-sm text-brand underline decoration-brand/40 underline-offset-2 hover:decoration-brand [overflow-wrap:anywhere] ${FOCUS}`;

function refPath(kind: Exclude<RefKind, 'ticket'>, id: string): string {
  return kind === 'project' ? `/projects/${encodeURIComponent(id)}` : sourceHref(id);
}

function isPlainText(node: ReactNode): boolean {
  return typeof node === 'string' || (Array.isArray(node) && node.every((c) => typeof c === 'string'));
}

function RefLink({ kind, id, children }: { kind: RefKind; id: string; children: ReactNode }) {
  const ctx = useContext(RefsContext);
  // Outside the drawer (no provider) the id stays plain text.
  if (!ctx) return <>{children}</>;
  // Bare ids read as ids; an id from inline code keeps its code well, tinted
  // and underlined so it still reads as a link.
  const mono = isPlainText(children)
    ? 'font-mono text-[0.88em]'
    : 'no-underline [&>code]:!text-brand [&>code]:!bg-[color-mix(in_oklch,var(--brand)_9%,transparent)] [&>code]:underline [&>code]:decoration-brand/40 [&>code]:underline-offset-2 hover:[&>code]:decoration-brand';
  if (kind === 'ticket') {
    const open = ctx.previewId === id;
    return (
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Preview ticket ${id}`}
        title="Preview ticket"
        onClick={(e) => ctx.openTicket(id, e.currentTarget)}
        className={`${LINK} ${mono} cursor-pointer decoration-dotted ${open ? 'decoration-solid' : ''}`}
      >
        {children}
      </button>
    );
  }
  return (
    <Link
      to={refPath(kind, id)}
      onClick={ctx.onNavigate}
      title={kind === 'project' ? 'Open project' : 'Open session'}
      className={`${LINK} ${mono}`}
    >
      {children}
    </Link>
  );
}

/** Markdown link override for `#sfs-ref-<kind>-<id>`; undefined for any other href. */
export function renderRefLink(href: string, children: ReactNode): ReactNode | undefined {
  const ref = parseRefHref(href);
  if (!ref) return undefined;
  return (
    <RefLink kind={ref.kind} id={ref.id}>
      {children}
    </RefLink>
  );
}
