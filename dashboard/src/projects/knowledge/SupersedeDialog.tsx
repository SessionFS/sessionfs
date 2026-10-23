import { useState } from 'react';

export function SupersedeDialog({
  entryId,
  onConfirm,
  onCancel,
  isPending,
}: {
  entryId: number;
  onConfirm: (supersedingId: number, reason: string) => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  const [supersedingId, setSupersedingId] = useState('');
  const [reason, setReason] = useState('');
  return (
    <div className="mt-2 p-3 rounded-lg border border-border bg-bg-primary">
      <p className="text-xs font-medium text-text-secondary mb-2">
        Supersede entry #{entryId}
      </p>
      <div className="flex gap-2 mb-2">
        <input
          type="number"
          value={supersedingId}
          onChange={(e) => setSupersedingId(e.target.value)}
          placeholder="Superseding entry ID"
          className="flex-1 px-2 py-1 text-xs bg-bg-primary border border-border rounded text-text-primary outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] placeholder:text-text-tertiary"
        />
      </div>
      <input
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Reason for supersession"
        className="w-full px-2 py-1 text-xs bg-bg-primary border border-border rounded text-text-primary outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] placeholder:text-text-tertiary mb-2"
      />
      <div className="flex gap-2 justify-end">
        <button
          onClick={onCancel}
          className="text-xs text-text-tertiary hover:text-text-secondary px-2 py-1"
        >
          Cancel
        </button>
        <button
          onClick={() => {
            const id = parseInt(supersedingId, 10);
            if (!id || !reason.trim()) return;
            onConfirm(id, reason.trim());
          }}
          disabled={isPending || !supersedingId || !reason.trim()}
          className="text-xs font-medium text-white bg-brand px-3 py-1 rounded hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
        >
          {isPending ? 'Saving…' : 'Confirm'}
        </button>
      </div>
    </div>
  );
}
