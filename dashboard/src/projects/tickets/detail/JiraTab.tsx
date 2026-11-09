import type { Ticket } from '../../../api/client';
import { JiraTicketSection } from '../JiraPromote';
import { SectionLabel } from './OverviewTab';
import type { TimelineItem } from './timeline';
import { TimelineRow } from './TimelineRow';

/** The Jira surface (unchanged JiraTicketSection) and, for a linked ticket,
 *  the sync log the activity timeline also shows. */
export function JiraTab({
  projectId,
  ticket,
  orgId,
  isProjectAdmin,
  isEnterprise,
  canEdit,
  onNavigateTicket,
  syncItems,
}: {
  projectId: string;
  ticket: Ticket;
  orgId: string;
  isProjectAdmin: boolean;
  isEnterprise: boolean;
  canEdit: boolean;
  onNavigateTicket: (id: string) => void;
  syncItems: TimelineItem[];
}) {
  return (
    <div className="space-y-6 text-sm">
      <JiraTicketSection
        projectId={projectId}
        ticket={ticket}
        orgId={orgId}
        isProjectAdmin={isProjectAdmin}
        isEnterprise={isEnterprise}
        canEdit={canEdit}
        onNavigateTicket={onNavigateTicket}
      />
      {ticket.jira && (
        <section aria-label="Jira sync log">
          <SectionLabel>Sync log</SectionLabel>
          {syncItems.length > 0 ? (
            <ol className="relative ml-1.5 border-l border-border">
              {syncItems.map((item) => (
                <TimelineRow key={item.key} item={item} />
              ))}
            </ol>
          ) : (
            <p className="text-xs text-text-tertiary">Nothing synced yet.</p>
          )}
        </section>
      )}
    </div>
  );
}
