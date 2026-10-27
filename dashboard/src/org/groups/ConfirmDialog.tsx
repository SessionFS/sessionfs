import { useId, type ReactNode } from 'react';
import { Alert, Button, Dialog, DialogFooter, DialogHeader } from '../../components/ui';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  /** Destructive actions use the danger button. */
  danger?: boolean;
  pending?: boolean;
  /** Shown inside the dialog when the action failed, so it can be retried. */
  error?: string | null;
  onConfirm: () => void;
  onClose: () => void;
}

/** A small yes/no dialog for removing, revoking, deleting and access changes. */
export default function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  danger = false,
  pending = false,
  error,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  const titleId = useId();
  return (
    <Dialog open={open} onClose={onClose} titleId={titleId}>
      <DialogHeader titleId={titleId}>{title}</DialogHeader>
      <div className="space-y-3 text-sm text-text-secondary">{children}</div>
      {error && (
        <Alert variant="danger" className="mt-4">
          {error}
        </Alert>
      )}
      <DialogFooter>
        <Button variant="secondary" onClick={onClose}>
          Cancel
        </Button>
        <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm} loading={pending}>
          {confirmLabel}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
