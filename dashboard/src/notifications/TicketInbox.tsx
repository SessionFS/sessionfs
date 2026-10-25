import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useMarkTicketNotificationsRead,
  useTicketNotifications,
} from '../hooks/useTicketNotifications';
import { useToast } from '../hooks/useToast';
import RelativeDate from '../components/RelativeDate';
import { Badge } from '../components/Badge';
import { Button, Card, PageContainer, PageHeader } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';
import { notificationKindLabel, notificationKindVariant } from './kind';

/**
 * The human ticket notification inbox. Defaults to unread only; "Show all"
 * reveals read rows too. Marking an item read removes it from the unread list,
 * and "Mark all read" clears the whole inbox in one call.
 */
export default function TicketInbox() {
  const [showAll, setShowAll] = useState(false);
  const { data, isLoading, error } = useTicketNotifications({
    unread: !showAll,
    limit: 100,
  });
  const markRead = useMarkTicketNotificationsRead();
  const { addToast } = useToast();

  const items = data?.items ?? [];

  function handleMarkOne(id: number) {
    markRead.mutate(
      { ids: [id] },
      {
        onError: (err) =>
          addToast('error', `Could not mark read: ${errorMessage(err)}`),
      },
    );
  }

  function handleMarkAll() {
    markRead.mutate(
      { all: true },
      {
        onSuccess: () => addToast('success', 'All notifications marked read'),
        onError: (err) =>
          addToast('error', `Could not mark all read: ${errorMessage(err)}`),
      },
    );
  }

  return (
    <PageContainer width="narrow" className="space-y-5">
      <PageHeader
        title="Inbox"
        description="Updates on tickets you follow. Summaries never include comment bodies."
        actions={
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setShowAll((v) => !v)}
            >
              {showAll ? 'Show unread' : 'Show all'}
            </Button>
            <Button
              size="sm"
              disabled={items.length === 0 || markRead.isPending}
              loading={markRead.isPending}
              onClick={handleMarkAll}
            >
              Mark all read
            </Button>
          </div>
        }
      />

      {isLoading && (
        <p className="text-text-tertiary text-sm">Loading inbox…</p>
      )}

      {error && (
        <p role="alert" className="text-danger text-sm">
          Failed to load inbox: {errorMessage(error)}
        </p>
      )}

      {!isLoading && !error && items.length === 0 && (
        <p className="text-sm text-text-tertiary">
          {showAll
            ? 'No notifications yet.'
            : 'No unread notifications.'}
        </p>
      )}

      {items.length > 0 && (
        <ul aria-label="Ticket notifications" className="space-y-2">
          {items.map((n) => {
            const unread = n.read_at === null;
            return (
              <li key={n.id}>
                <Card
                  className={`p-4 ${unread ? 'border-l-2 border-l-brand' : 'opacity-80'}`}
                  data-testid={`notification-${n.id}`}
                >
                  <div className="flex items-start gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-xs text-text-tertiary flex-wrap">
                        <Badge
                          variant={notificationKindVariant(n.kind)}
                          tint
                          label={notificationKindLabel(n.kind)}
                          size="sm"
                        />
                        {n.event_count > 1 && (
                          <span className="text-text-tertiary">
                            {n.event_count} updates
                          </span>
                        )}
                        <RelativeDate iso={n.created_at} />
                      </div>
                      <Link
                        to={`/projects/${n.project_id}?tab=work&sub=tickets&ticket=${encodeURIComponent(n.ticket_id)}`}
                        className="mt-1 block font-medium text-text-primary hover:text-brand transition-colors truncate"
                      >
                        {n.ticket_title ?? n.ticket_id}
                      </Link>
                      <p className="mt-0.5 text-sm text-text-secondary">
                        {n.summary}
                      </p>
                    </div>
                    {unread && (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={markRead.isPending}
                        onClick={() => handleMarkOne(n.id)}
                        aria-label={`Mark notification ${n.id} read`}
                      >
                        Mark read
                      </Button>
                    )}
                  </div>
                </Card>
              </li>
            );
          })}
        </ul>
      )}
    </PageContainer>
  );
}
