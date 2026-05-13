/**
 * v0.10.0 Phase 4 — Project transfer inbox.
 *
 * Shows pending incoming transfers (target_user_id == current user, state
 * == pending) with accept / reject controls. Outgoing pending transfers
 * surface in a secondary list with cancel controls so a user who initiated
 * a cross-org transfer can still recall it before the target acts.
 *
 * Backend invariants (Phase 2, KB 246/248/250):
 *   - Atomic state transition via UPDATE ... WHERE state='pending' with
 *     rowcount check; 409 STALE_STATE on concurrent action.
 *   - target_user_id is re-validated for current standing on accept/reject
 *     (an admin demoted between initiate and accept loses standing).
 *   - Auto-accept transfers never sit in this inbox (state='accepted' at
 *     create time).
 */

import { useToast } from '../hooks/useToast';
import {
  type TransferInfo,
  useAcceptTransfer,
  useCancelTransfer,
  useRejectTransfer,
  useTransfers,
} from './useTransfers';

function scopeLabel(scope: string): string {
  return scope === 'personal' ? 'Personal' : `Org ${scope}`;
}

function ProjectLabel({ t }: { t: TransferInfo }) {
  const name = t.project_name_snapshot ?? '(project)';
  if (t.project_id === null) {
    return (
      <span title="Source project has been deleted; this audit row survives">
        {name} <em className="text-[var(--text-tertiary)]">(deleted)</em>
      </span>
    );
  }
  return <span>{name}</span>;
}

export default function TransferInbox() {
  const incoming = useTransfers('incoming', 'pending');
  const outgoing = useTransfers('outgoing', 'pending');
  const accept = useAcceptTransfer();
  const reject = useRejectTransfer();
  const cancel = useCancelTransfer();
  const { addToast } = useToast();

  const handleAccept = (t: TransferInfo) => {
    accept.mutate(
      { transferId: t.id },
      {
        onSuccess: () =>
          addToast('success', `Accepted transfer of ${t.project_name_snapshot ?? 'project'}`),
        onError: (err) => addToast('error', `Accept failed: ${err.message}`),
      },
    );
  };

  const handleReject = (t: TransferInfo) => {
    reject.mutate(
      { transferId: t.id },
      {
        onSuccess: () =>
          addToast('success', `Rejected transfer of ${t.project_name_snapshot ?? 'project'}`),
        onError: (err) => addToast('error', `Reject failed: ${err.message}`),
      },
    );
  };

  const handleCancel = (t: TransferInfo) => {
    cancel.mutate(
      { transferId: t.id },
      {
        onSuccess: () =>
          addToast('success', `Cancelled transfer of ${t.project_name_snapshot ?? 'project'}`),
        onError: (err) => addToast('error', `Cancel failed: ${err.message}`),
      },
    );
  };

  if (incoming.isLoading || outgoing.isLoading) return <p>Loading transfers…</p>;
  if (incoming.error) return <p role="alert">Failed to load inbox: {String(incoming.error)}</p>;
  if (outgoing.error) return <p role="alert">Failed to load outbox: {String(outgoing.error)}</p>;

  const incomingRows = incoming.data?.transfers ?? [];
  const outgoingRows = outgoing.data?.transfers ?? [];
  const busy = accept.isPending || reject.isPending || cancel.isPending;

  return (
    <section aria-labelledby="transfers-heading">
      <h2 id="transfers-heading">Project transfers</h2>

      <section aria-labelledby="incoming-heading">
        <h3 id="incoming-heading">Incoming ({incomingRows.length})</h3>
        {incomingRows.length === 0 ? (
          <p className="text-[var(--text-tertiary)]">No pending incoming transfers.</p>
        ) : (
          <ul>
            {incomingRows.map((t) => (
              <li key={t.id} data-testid={`incoming-${t.id}`}>
                <div>
                  <strong><ProjectLabel t={t} /></strong>
                  <span> from {scopeLabel(t.from_scope)} → {scopeLabel(t.to_scope)}</span>
                </div>
                <div className="text-[var(--text-tertiary)] text-xs">
                  Initiated by {t.initiated_by}
                </div>
                <button
                  onClick={() => handleAccept(t)}
                  disabled={busy}
                  aria-label={`Accept transfer ${t.id}`}
                >
                  Accept
                </button>
                <button
                  onClick={() => handleReject(t)}
                  disabled={busy}
                  aria-label={`Reject transfer ${t.id}`}
                >
                  Reject
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="outgoing-heading">
        <h3 id="outgoing-heading">Outgoing ({outgoingRows.length})</h3>
        {outgoingRows.length === 0 ? (
          <p className="text-[var(--text-tertiary)]">No pending outgoing transfers.</p>
        ) : (
          <ul>
            {outgoingRows.map((t) => (
              <li key={t.id} data-testid={`outgoing-${t.id}`}>
                <div>
                  <strong><ProjectLabel t={t} /></strong>
                  <span> from {scopeLabel(t.from_scope)} → {scopeLabel(t.to_scope)}</span>
                </div>
                <div className="text-[var(--text-tertiary)] text-xs">
                  Waiting on {t.target_user_id ?? '(target removed)'}
                </div>
                <button
                  onClick={() => handleCancel(t)}
                  disabled={busy}
                  aria-label={`Cancel transfer ${t.id}`}
                >
                  Cancel
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}
