import { createContext, useContext } from 'react';
import type { Ticket } from '../../api/client';

/**
 * Who is looking at the tickets tab. `canEdit` is the project's `can_edit`
 * flag (owner or org member); readers whose only access is a captured
 * session on the repo get `false`. The default keeps every action visible
 * (older servers, isolated component tests).
 */
export interface TicketViewer {
  canEdit: boolean;
  userId: string | null;
}

export const TicketViewerContext = createContext<TicketViewer>({ canEdit: true, userId: null });

export function useTicketViewer(): TicketViewer {
  return useContext(TicketViewerContext);
}

export type TicketAction = 'approve' | 'dismiss' | 'accept' | 'reopen' | 'close';

/**
 * Mirrors the server's ticket actor rule (routes/tickets.py
 * `_require_ticket_actor`): editors may do anything; a view-only reader may
 * dismiss or reopen tickets they created and accept them unless they also
 * resolved them, and may never approve. Closing an Issue needs project-admin
 * standing, which a reader never has.
 */
export function ticketActionAllowed(
  action: TicketAction,
  ticket: Pick<Ticket, 'created_by_user_id'> & { resolver_user_id?: string | null },
  viewer: TicketViewer,
): boolean {
  if (viewer.canEdit) return true;
  if (!viewer.userId) return false;
  const isCreator = ticket.created_by_user_id === viewer.userId;
  const isResolver = ticket.resolver_user_id === viewer.userId;
  switch (action) {
    case 'dismiss':
    case 'reopen':
      return isCreator;
    case 'accept':
      return isCreator && !isResolver;
    default:
      return false;
  }
}
