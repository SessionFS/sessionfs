import type { Ticket } from '../../api/client';
import { ContactAvatar } from '../../org/contacts/ContactChip';
import { PersonAvatar, PersonaGlyph } from './AssigneeLabel';
import { ReassignPanel } from './ReassignPanel';
import { RowPopover } from './RowPopover';
import { canReassignTicket, useTicketViewer } from './viewer';
import { assigneeName } from './views';

/* ── Row assignee ──
 *
 * The assignee cell of a row. For a viewer who may reassign it is a button
 * that opens the same reassign editor as the ticket detail (ReassignPanel:
 * AssigneePicker, Save / Unassign, and the confirm when a non-editor's
 * change would send an approved ticket back for approval). Otherwise it is
 * plain text. */

export function AssigneeFace({ ticket: t }: { ticket: Ticket | null }) {
  if (!t) return <span aria-hidden="true" className="tk-av-none" />;
  if (t.assigned_user_id) {
    const name = assigneeName(t) ?? '';
    return <PersonAvatar name={name} seed={t.assigned_user?.email ?? t.assigned_user_id} className="tk-av" />;
  }
  if (t.assigned_to) return <PersonaGlyph />;
  if (t.assigned_contact) return <ContactAvatar name={t.assigned_contact.display_name} />;
  return <span aria-hidden="true" className="tk-av-none" />;
}

export function AssigneeCell({
  ticket: t,
  projectId,
  orgId,
  isProjectAdmin,
  tabIndex,
}: {
  ticket: Ticket;
  projectId: string;
  orgId?: string | null;
  isProjectAdmin?: boolean;
  /** Roving tabindex from the row (-1 off the focused row). */
  tabIndex?: number;
}) {
  const viewer = useTicketViewer();
  const name = assigneeName(t);
  const you = !!viewer.userId && t.assigned_user_id === viewer.userId;
  const removedContact = !name && !!t.assigned_contact_id;
  const text = name ? `${name}${you ? ' (you)' : ''}` : removedContact ? 'Contact removed' : 'Unassigned';
  const kindWord = t.assigned_user_id ? 'Person' : t.assigned_to ? 'Agent' : t.assigned_contact ? 'External contact' : '';
  const face = (
    <>
      <AssigneeFace ticket={t} />
      <span className="tk-asg-name">{text}</span>
    </>
  );

  if (!canReassignTicket(t, viewer, isProjectAdmin)) {
    return (
      <span className={`tk-asg ${name ? '' : 'is-none'}`} title={kindWord ? `${kindWord}: ${text}` : text}>
        {face}
      </span>
    );
  }
  // A non-editor's change to an approved ticket sends it back to suggested.
  const needsReapproval = !viewer.canEdit && t.status === 'open';
  return (
    <RowPopover
      kind="dialog"
      label={`Reassign ${t.id}`}
      align="right"
      panelClassName="w-80"
      trigger={(props) => (
        <button
          type="button"
          {...props}
          tabIndex={tabIndex}
          data-tk-assign=""
          className={`tk-asg ${name ? '' : 'is-none'}`}
          aria-label={`Assignee: ${text}${kindWord ? ` (${kindWord.toLowerCase()})` : ''}. Reassign`}
          title={kindWord ? `${kindWord}: ${text}` : text}
        >
          {face}
        </button>
      )}
    >
      {(close) => (
        <ReassignPanel
          projectId={projectId}
          ticket={t}
          orgId={orgId}
          needsReapproval={needsReapproval}
          onDone={() => close(true)}
        />
      )}
    </RowPopover>
  );
}
