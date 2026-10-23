import { useDeletedSessions, useRestoreSession } from '../../hooks/useSessions';
import type { SessionSummary, SessionDetail } from '../../api/client';
import { fullToolName } from '../../utils/models';
import RelativeDate from '../../components/RelativeDate';
import { useToast } from '../../hooks/useToast';
import { TOOL_COLORS } from '../../utils/tools';
import { Alert, Button } from '../../components/ui';
import { errorMessage } from '../../utils/errorMessage';

/* ── Trash view ── */

export function TrashView() {
  const { data, isLoading, error } = useDeletedSessions();
  const restoreSession = useRestoreSession();
  const { addToast } = useToast();

  if (isLoading) {
    return (
      <div className="py-8 text-center text-text-tertiary">
        Loading deleted sessions...
      </div>
    );
  }

  if (error) {
    return (
      <Alert variant="danger" className="mb-4">
        Failed to load trash: {errorMessage(error)}
      </Alert>
    );
  }

  const deletedSessions = data?.sessions ?? [];

  if (deletedSessions.length === 0) {
    return (
      <div className="text-center py-16">
        <p className="text-text-secondary mb-2">Trash is empty</p>
        <p className="text-text-tertiary text-sm">
          Deleted sessions appear here for 30 days before being permanently removed.
        </p>
      </div>
    );
  }

  function handleRestore(id: string) {
    restoreSession.mutate(id, {
      onSuccess: (data) => {
        if (data?.local_copy_may_be_missing) {
          addToast(
            'success',
            'Session restored on server. If this device deleted the local copy, run: sfs pull ' + id.slice(0, 12),
          );
        } else {
          addToast('success', 'Session restored');
        }
      },
      onError: (err) => addToast('error', `Restore failed: ${errorMessage(err)}`),
    });
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-text-tertiary mb-3">
        {deletedSessions.length} deleted session{deletedSessions.length !== 1 ? 's' : ''}. Sessions are permanently removed after 30 days.
      </p>
      {deletedSessions.map((s: SessionSummary) => {
        const sd = s as unknown as SessionDetail;
        const scopeLabel = sd.delete_scope === 'both' ? 'everywhere' : (sd.delete_scope || 'cloud');
        return (
          <div
            key={s.id}
            className="bg-bg-primary border border-border rounded-lg px-4 py-3 flex items-center gap-3 opacity-75 hover:opacity-100 transition-opacity"
          >
            <span
              className="w-2 h-2 rounded-full shrink-0"
              style={{ backgroundColor: TOOL_COLORS[s.source_tool] || 'var(--text-tertiary)' }}
            />
            <div className="flex-1 min-w-0">
              <span
                className="text-sm font-medium text-text-secondary truncate block"
                title="Restore to view session details"
              >
                {s.title || 'Untitled session'}
              </span>
              <div className="flex items-center gap-2 text-2xs text-text-tertiary mt-0.5">
                <span>{fullToolName(s.source_tool)}</span>
                <span className="opacity-40">&middot;</span>
                <span>Deleted <RelativeDate iso={sd.deleted_at} /></span>
                {sd.purge_after && (
                  <>
                    <span className="opacity-40">&middot;</span>
                    <span>Purges <RelativeDate iso={sd.purge_after} /></span>
                  </>
                )}
              </div>
            </div>
            <span className={`px-2 py-0.5 text-2xs font-medium rounded-full ${
              scopeLabel === 'everywhere'
                ? 'bg-danger/10 text-danger'
                : 'bg-warning/10 text-warning'
            }`}>
              {scopeLabel}
            </span>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => handleRestore(s.id)}
              loading={restoreSession.isPending}
            >
              Restore
            </Button>
          </div>
        );
      })}
    </div>
  );
}
