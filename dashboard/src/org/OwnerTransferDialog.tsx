import { useState } from 'react';
import { useAuth } from '../auth/AuthContext';
import { Dialog, DialogHeader, DialogFooter, Button, Select } from '../components/ui';

/**
 * Owner-only "Transfer ownership" dialog (v0.11.0).
 *
 * Starts the two-step ownership transfer:
 *   POST /api/v1/orgs/{org_id}/owner/transfer  { to_user_id }
 * The target must then ACCEPT (a separate step surfaced as a banner on their OrgPage).
 * The target can only be an existing org ADMIN — enforced server-side and mirrored here
 * by restricting the candidate list.
 *
 * NOTE: this is ORGANIZATION ownership transfer — distinct from the v0.10.0 project
 * transfer flow (projects/TransferPanel.tsx). They are unrelated.
 */

interface AdminCandidate {
  user_id: string;
  email: string;
}

interface OwnerTransferDialogProps {
  open: boolean;
  onClose: () => void;
  orgId: string;
  admins: AdminCandidate[];
  onInitiated: () => void;
}

function errorMessage(body: unknown, fallback: string): string {
  const b = body as { error?: { message?: string }; detail?: string } | null;
  return b?.error?.message || b?.detail || fallback;
}

export default function OwnerTransferDialog({
  open,
  onClose,
  orgId,
  admins,
  onInitiated,
}: OwnerTransferDialogProps) {
  const { auth } = useAuth();
  const apiBase = auth?.baseUrl || (window as any).__SFS_API_URL__ || '';
  const headers = {
    Authorization: `Bearer ${auth?.apiKey ?? ''}`,
    'Content-Type': 'application/json',
  };

  const [target, setTarget] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const titleId = 'owner-transfer-title';

  async function initiate() {
    if (!target) return;
    setError('');
    setBusy(true);
    try {
      const res = await fetch(`${apiBase}/api/v1/orgs/${orgId}/owner/transfer`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ to_user_id: target }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(errorMessage(body, 'Could not start the transfer.'));
      }
      setTarget('');
      onInitiated();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} titleId={titleId} className="max-w-md">
      <DialogHeader titleId={titleId}>Transfer ownership</DialogHeader>

      {admins.length === 0 ? (
        <p className="text-sm text-text-secondary">
          There are no other admins to transfer ownership to. Promote a member to admin first,
          then transfer ownership to them.
        </p>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-text-secondary">
            Ownership can only be transferred to an existing admin. The new owner must{' '}
            <strong>accept</strong> the transfer before it takes effect — you can cancel until
            they do. You'll become an admin once they accept.
          </p>
          <Select
            title="New owner"
            value={target}
            onValueChange={setTarget}
            placeholder="Select an admin…"
            aria-label="Select the new owner"
            options={admins.map((a) => ({ value: a.user_id, label: a.email }))}
          />
          {error && (
            <div role="alert" className="text-sm text-red-400">
              {error}
            </div>
          )}
        </div>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        {admins.length > 0 && (
          <Button onClick={initiate} disabled={!target || busy}>
            {busy ? 'Sending…' : 'Transfer ownership'}
          </Button>
        )}
      </DialogFooter>
    </Dialog>
  );
}
