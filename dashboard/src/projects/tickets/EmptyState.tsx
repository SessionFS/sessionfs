
/* ── Empty state ── */

export function EmptyState({
  kindFilter,
  statusFilter,
  assignedToMe = false,
}: {
  kindFilter: string;
  statusFilter: string;
  assignedToMe?: boolean;
}) {
  if (assignedToMe) {
    const scope = [
      kindFilter ? `${kindFilter === 'issue' ? 'Issues' : 'Tasks'}` : 'tickets',
      statusFilter ? `with status "${statusFilter}"` : '',
    ]
      .filter(Boolean)
      .join(' ');
    return (
      <div className="border border-border rounded-lg p-6 text-center text-text-tertiary space-y-1">
        <p className="font-medium text-md text-text-secondary">No {scope} assigned to you.</p>
        <p className="text-sm">Tickets assigned to you as a person show up here.</p>
      </div>
    );
  }
  if (kindFilter === 'issue') {
    return (
      <div className="border border-border rounded-lg p-6 text-center text-text-tertiary space-y-1">
        <p className="font-medium text-md text-text-secondary">
          No Issues yet.
        </p>
        <p className="text-sm">
          An Issue is a PM-triaged container that rolls up one or more Tasks. File
          an Issue when a user-facing problem needs cross-team work; file a Task
          for a single executor's unit of work.
        </p>
      </div>
    );
  }
  return (
    <div className="border border-border rounded-lg p-6 text-center text-text-tertiary">
      No tickets{' '}
      {statusFilter
        ? `with status "${statusFilter}"`
        : kindFilter
          ? `of kind "${kindFilter}"`
          : 'yet'}
      .
    </div>
  );
}
