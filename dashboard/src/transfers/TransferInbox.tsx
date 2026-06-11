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
 *
 * Phase 3 restyle: rows onto Card + Button variants.
 */

import { useToast } from '../hooks/useToast';
import {
  type TransferInfo,
  useAcceptTransfer,
  useCancelTransfer,
  useRejectTransfer,
  useTransfers,
} from './useTransfers';
import { Button, Card } from '../components/ui';

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
  return <span className="font-semibold text-[var(--text-primary)]">{name}</span>;
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

  if (incoming.isLoading || outgoing.isLoading) return <p className="text-[var(--text-tertiary)] p-4 text-sm">Loading transfers…</p>;
  if (incoming.error) return <p role="alert" className="text-[var(--danger)] p-4 text-sm">Failed to load inbox: {String(incoming.error)}</p>;
  if (outgoing.error) return <p role="alert" className="text-[var(--danger)] p-4 text-sm">Failed to load outbox: {String(outgoing.error)}</p>;

  const incomingRows = incoming.data?.transfers ?? [];
  const outgoingRows = outgoing.data?.transfers ?? [];
  const busy = accept.isPending || reject.isPending || cancel.isPending;

  return (
    <section aria-labelledby="transfers-heading" className="space-y-5">
      <h2 id="transfers-heading" className="text-lg font-semibold text-[var(--text-primary)]">Project transfers</h2>

      <section aria-labelledby="incoming-heading">
        <h3 id="incoming-heading" className="text-micro uppercase text-[var(--text-tertiary)] mb-3">
          Incoming ({incomingRows.length})
        </h3>
        {incomingRows.length === 0 ? (
          <p className="text-[13px] text-[var(--text-tertiary)]">No pending incoming transfers.</p>
        ) : (
          <div className="space-y-2">
            {incomingRows.map((t) => (
              <Card key={t.id} className="p-4 flex items-center justify-between gap-3 flex-wrap" data-testid={`incoming-${t.id}`}>
                <div className="min-w-0">
                  <div className="text-sm">
                    <span className="font-semibold text-[var(--text-primary)]"><ProjectLabel t={t} /></span>
                    <span className="text-[var(--text-tertiary)]"> from {scopeLabel(t.from_scope)} → {scopeLabel(t.to_scope)}</span>
                  </div>
                  <div className="text-xs text-[var(--text-tertiary)] mt-0.5">
                    Initiated by {t.initiated_by}
                  </div>
                </div>
                <div className="flex gap-2 flex-shrink-0">
                  <Button size="sm" onClick={() => handleAccept(t)} disabled={busy} aria-label={`Accept transfer ${t.id}`}>
                    Accept
                  </Button>
                  <Button variant="danger" size="sm" onClick={() => handleReject(t)} disabled={busy} aria-label={`Reject transfer ${t.id}`}>
                    Reject
                  </Button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </section>

      <section aria-labelledby="outgoing-heading">
        <h3 id="outgoing-heading" className="text-micro uppercase text-[var(--text-tertiary)] mb-3">
          Outgoing ({outgoingRows.length})
        </h3>
        {outgoingRows.length === 0 ? (
          <p className="text-[13px] text-[var(--text-tertiary)]">No pending outgoing transfers.</p>
        ) : (
          <div className="space-y-2">
            {outgoingRows.map((t) => (
              <Card key={t.id} className="p-4 flex items-center justify-between gap-3 flex-wrap" data-testid={`outgoing-${t.id}`}>
                <div className="min-w-0">
                  <div className="text-sm">
                    <span className="font-semibold text-[var(--text-primary)]"><ProjectLabel t={t} /></span>
                    <span className="text-[var(--text-tertiary)]"> from {scopeLabel(t.from_scope)} → {scopeLabel(t.to_scope)}</span>
                  </div>
                  <div className="text-xs text-[var(--text-tertiary)] mt-0.5">
                    Waiting on {t.target_user_id ?? '(target removed)'}
                  </div>
                </div>
                <Button variant="danger" size="sm" onClick={() => handleCancel(t)} disabled={busy} aria-label={`Cancel transfer ${t.id}`}>
                  Cancel
                </Button>
              </Card>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
