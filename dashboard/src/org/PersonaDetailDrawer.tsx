/**
 * GitHub #92 — persona detail drawer: the roll-up of what a persona has done
 * (recent sessions, open tickets, latest KB entry and agent run) plus, for
 * org admins, the represent-history audit trail.
 */

import type { ReactNode } from 'react';
import type { Persona } from '../api/client';
import { Drawer } from '../components/ui';
import RelativeDate from '../components/RelativeDate';
import { useOrgPersonaRepresentHistory, useOrgPersonaRollup } from '../hooks/usePersonas';
import { errorMessage } from '../utils/errorMessage';
import type { OrgPersonaMember } from './OrgPersonasSection';

interface PersonaDetailDrawerProps {
  persona: Persona;
  orgId: string;
  /** Represent-history is admin-only; members see just the roll-up. */
  isAdmin?: boolean;
  /** Used to resolve member ids in the represent-history to emails. */
  members?: OrgPersonaMember[];
  onClose: () => void;
}

function memberEmail(members: OrgPersonaMember[], userId: string | null) {
  return userId ? (members.find((m) => m.user_id === userId)?.email ?? userId) : null;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wide">{title}</h3>
      <div className="mt-2 text-sm text-text-primary">{children}</div>
    </section>
  );
}

function Empty({ children = 'None' }: { children?: ReactNode }) {
  return <p className="text-text-tertiary italic">{children}</p>;
}

/**
 * Admin-only audit trail. Kept as a separate component so the history query
 * is never issued for members (who would just get a 403).
 */
function RepresentHistory({
  orgId,
  name,
  members,
}: {
  orgId: string;
  name: string;
  members: OrgPersonaMember[];
}) {
  const history = useOrgPersonaRepresentHistory(orgId, name);

  return (
    <div className="mt-6 border-t border-border pt-5">
      <h3 className="text-sm font-semibold text-text-secondary uppercase tracking-wide">
        Represent history
      </h3>
      {history.isLoading && <p className="mt-2 text-text-tertiary">Loading…</p>}
      {history.error && (
        <p role="alert" className="mt-2 text-danger">
          Failed to load represent history: {errorMessage(history.error)}
        </p>
      )}
      {history.data && (history.data.length === 0 ? (
        <div className="mt-2">
          <Empty>No representation changes recorded.</Empty>
        </div>
      ) : (
        <ul className="mt-2 space-y-2">
          {history.data.map((h, i) => (
            <li key={i} className="text-sm">
              <span className="text-text-secondary">
                {memberEmail(members, h.from_user_id) ?? 'Nobody'} →{' '}
                {memberEmail(members, h.to_user_id) ?? 'Nobody'}
              </span>
              <span className="block text-xs text-text-tertiary">
                by {h.actor_email ?? h.actor_role ?? h.actor_user_id ?? 'unknown'}{' '}
                <RelativeDate iso={h.created_at} />
              </span>
            </li>
          ))}
        </ul>
      ))}
    </div>
  );
}

export default function PersonaDetailDrawer({
  persona,
  orgId,
  isAdmin = true,
  members = [],
  onClose,
}: PersonaDetailDrawerProps) {
  const titleId = `persona-detail-${persona.id}`;
  const rollup = useOrgPersonaRollup(orgId, persona.name);

  return (
    <Drawer open onClose={onClose} titleId={titleId}>
      <h2 id={titleId} className="text-lg font-semibold text-text-primary">
        <span className="font-mono">{persona.name}</span>
      </h2>
      <p className="text-sm text-text-secondary mt-1">{persona.role}</p>

      {rollup.isLoading && <p className="mt-4 text-text-tertiary">Loading roll-up…</p>}
      {rollup.error && (
        <p role="alert" className="mt-4 text-danger">
          Failed to load roll-up: {errorMessage(rollup.error)}
        </p>
      )}
      {rollup.data && (
        <div className="mt-5 space-y-6">
          <Section title="Recent sessions">
            {rollup.data.recent_sessions.length === 0 ? (
              <Empty />
            ) : (
              <ul className="space-y-1">
                {rollup.data.recent_sessions.map((s) => (
                  <li key={s.id} className="flex items-baseline gap-2">
                    <span className="truncate text-text-primary">{s.title || s.id}</span>
                    <span className="ml-auto shrink-0 text-text-tertiary">
                      <RelativeDate iso={s.created_at} />
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Open tickets">
            {rollup.data.open_tickets.length === 0 ? (
              <Empty />
            ) : (
              <ul className="space-y-1">
                {rollup.data.open_tickets.map((t) => (
                  <li key={t.id} className="flex items-baseline gap-2">
                    <span className="truncate text-text-primary">{t.title}</span>
                    <span className="shrink-0 text-xs text-text-tertiary">{t.status}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Latest knowledge entry">
            {rollup.data.last_knowledge_entry ? (
              <p className="text-text-secondary">
                <span className="text-text-tertiary">[{rollup.data.last_knowledge_entry.entry_type}]</span>{' '}
                {rollup.data.last_knowledge_entry.content.length > 200
                  ? rollup.data.last_knowledge_entry.content.slice(0, 200) + '…'
                  : rollup.data.last_knowledge_entry.content}
              </p>
            ) : (
              <Empty />
            )}
          </Section>

          <Section title="Latest agent run">
            {rollup.data.last_agent_run ? (
              <p className="text-text-secondary">
                <span className="text-text-primary">{rollup.data.last_agent_run.status}</span>{' '}
                <span className="text-text-tertiary">
                  <RelativeDate iso={rollup.data.last_agent_run.created_at} />
                </span>
              </p>
            ) : (
              <Empty />
            )}
          </Section>
        </div>
      )}

      {isAdmin && <RepresentHistory orgId={orgId} name={persona.name} members={members} />}
    </Drawer>
  );
}
