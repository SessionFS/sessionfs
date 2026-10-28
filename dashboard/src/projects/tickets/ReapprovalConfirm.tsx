import { useEffect, useRef } from 'react';
import { Alert, Button } from '../../components/ui';

/** Shown in place of Save when a non-editor's change to an approved (open)
 *  ticket would send it back to suggested (ticket_writes.apply_ticket_update).
 *  Focus moves to the confirm button on open, so it never falls to the page
 *  body (where Escape would reach a surrounding drawer). */
export function ReapprovalConfirm({
  onConfirm,
  onCancel,
  pending,
}: {
  onConfirm: () => void;
  onCancel: () => void;
  pending: boolean;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    confirmRef.current?.focus();
  }, []);
  return (
    <div className="mt-2 space-y-2">
      <Alert variant="warning">
        Saving this sends the ticket back for approval: it was approved, and only a project editor can change it
        without that.
      </Alert>
      <div className="flex flex-wrap gap-1.5">
        <Button ref={confirmRef} type="button" variant="primary" size="sm" loading={pending} onClick={onConfirm}>
          Save and send back for approval
        </Button>
        <Button type="button" variant="ghost" size="sm" disabled={pending} onClick={onCancel}>
          Keep editing
        </Button>
      </div>
    </div>
  );
}
