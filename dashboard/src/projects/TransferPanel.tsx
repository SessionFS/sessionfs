/**
 * v0.10.0 Phase 4 — Per-project transfer panel.
 *
 * Lives on the project settings tab. Lets the project owner / source-org
 * admin initiate a transfer either to one of their orgs or "Personal".
 * Surfaces an active pending outgoing transfer (if any) with a Cancel
 * button so the initiator can recall before the target acts.
 *
 * The backend enforces the actual authorization (Phase 2, KB 246). This
 * UI mirrors enough of it to avoid pointless API calls — but the server
 * is load-bearing.
 *
 * Phase 3 restyle: onto Select + Button + Card primitives.
 */

import { useState } from 'react';

import { useToast } from '../hooks/useToast';
import {
  type TransferInfo,
  useCancelTransfer,
  useInitiateTransfer,
  useTransfers,
} from '../transfers/useTransfers';
import { Button, Card, Select } from '../components/ui';

interface OrgOption {
  org_id: string;
  name: string;
}

interface TransferPanelProps {
  projectId: string;
  /** Current scope: "personal" or org_id. */
  currentScope: string;
  /** Orgs the current user belongs to — destination candidates. */
  availableOrgs: OrgOption[];
}

function scopeLabel(scope: string, availableOrgs: OrgOption[]): string {
  if (scope === 'personal') return 'Personal';
  const match = availableOrgs.find((o) => o.org_id === scope);
  return match ? match.name : `Org ${scope}`;
}

export default function TransferPanel({
  projectId,
  currentScope,
  availableOrgs,
}: TransferPanelProps) {
  const outgoing = useTransfers('outgoing', 'pending');
  const initiate = useInitiateTransfer(projectId);
  const cancel = useCancelTransfer();
  const { addToast } = useToast();

  // Filter the destination dropdown to scopes other than the current one.
  const destinations = [
    ...(currentScope === 'personal' ? [] : [{ value: 'personal', label: 'Personal' }]),
    ...availableOrgs
      .filter((o) => o.org_id !== currentScope)
      .map((o) => ({ value: o.org_id, label: o.name })),
  ];
  const [selectedDest, setSelectedDest] = useState(destinations[0]?.value ?? '');

  // A pending outgoing transfer for THIS project, if any.
  const pendingForThisProject: TransferInfo | undefined = outgoing.data?.transfers.find(
    (t) => t.project_id === projectId && t.state === 'pending',
  );

  const handleInitiate = () => {
    if (!selectedDest) return;
    initiate.mutate(
      { to: selectedDest },
      {
        onSuccess: (t) => {
          if (t.state === 'accepted') {
            addToast(
              'success',
              `Project moved to ${scopeLabel(t.to_scope, availableOrgs)}`,
            );
          } else {
            addToast(
              'success',
              `Transfer initiated; waiting on ${scopeLabel(t.to_scope, availableOrgs)}`,
            );
          }
        },
        onError: (err) => addToast('error', `Transfer failed: ${err.message}`),
      },
    );
  };

  const handleCancel = () => {
    if (!pendingForThisProject) return;
    cancel.mutate(
      { transferId: pendingForThisProject.id },
      {
        onSuccess: () => addToast('success', 'Transfer cancelled'),
        onError: (err) => addToast('error', `Cancel failed: ${err.message}`),
      },
    );
  };

  return (
    <section aria-labelledby="transfer-heading" className="space-y-3">
      <h3 id="transfer-heading" className="text-lg font-semibold text-text-primary">Transfer ownership</h3>
      <p className="text-sm text-text-secondary">
        Currently {scopeLabel(currentScope, availableOrgs)}.{' '}
        Transferring moves the project's org scope; sessions and audit history are preserved.
      </p>

      {pendingForThisProject ? (
        <Card className="p-4 space-y-3" role="status">
          <p className="text-sm text-text-secondary">
            Pending transfer to {scopeLabel(pendingForThisProject.to_scope, availableOrgs)}.
            Waiting on {pendingForThisProject.target_user_id ?? '(target removed)'}.
          </p>
          <Button
            variant="danger"
            size="sm"
            onClick={handleCancel}
            disabled={cancel.isPending}
            loading={cancel.isPending}
            aria-label="Cancel pending transfer"
          >
            Cancel transfer
          </Button>
        </Card>
      ) : destinations.length === 0 ? (
        <p className="text-sm text-text-tertiary">No transfer destinations available.</p>
      ) : (
        <div className="flex items-end gap-3 flex-wrap">
          <Select
            title="Destination"
            value={selectedDest}
            onChange={(e) => setSelectedDest(e.target.value)}
            options={destinations}
            aria-label="Transfer destination"
            className="w-auto min-w-[180px]"
          />
          <Button
            onClick={handleInitiate}
            disabled={initiate.isPending || !selectedDest}
            loading={initiate.isPending}
            aria-label="Initiate transfer"
          >
            Transfer project
          </Button>
        </div>
      )}
    </section>
  );
}
