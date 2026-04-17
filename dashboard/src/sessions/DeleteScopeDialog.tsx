import { useState, useRef, useEffect } from 'react';

export type DeleteScope = 'cloud' | 'everywhere';

interface DeleteScopeDialogProps {
  /** Number of sessions being deleted (1 for single, N for bulk) */
  count: number;
  /** Whether the delete operation is in progress */
  isPending: boolean;
  /** Called when the user confirms the delete */
  onConfirm: (scope: DeleteScope) => void;
  /** Called when the user cancels */
  onCancel: () => void;
}

const SCOPE_OPTIONS: { value: DeleteScope; label: string; description: string }[] = [
  {
    value: 'cloud',
    label: 'Remove from cloud',
    description: 'Keeps local copies on your devices. Recoverable for 30 days.',
  },
  {
    value: 'everywhere',
    label: 'Delete everywhere',
    description: 'Removes from cloud and all synced devices. Recoverable for 30 days.',
  },
];

export default function DeleteScopeDialog({
  count,
  isPending,
  onConfirm,
  onCancel,
}: DeleteScopeDialogProps) {
  const [scope, setScope] = useState<DeleteScope>('cloud');
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') onCancel();
  }

  const title =
    count === 1 ? 'Delete session?' : `Delete ${count} sessions?`;

  return (
    <>
      <div
        className="fixed inset-0 z-50 bg-black/50"
        onClick={onCancel}
      />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pointer-events-none">
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-scope-title"
          onKeyDown={handleKeyDown}
          tabIndex={-1}
          className="pointer-events-auto w-full max-w-md bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-lg)] p-6 focus:outline-none"
        >
          <h3
            id="delete-scope-title"
            className="text-base font-semibold text-[var(--text-primary)] mb-4"
          >
            {title}
          </h3>

          <fieldset className="space-y-3 mb-6">
            <legend className="sr-only">Delete scope</legend>
            {SCOPE_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                  scope === opt.value
                    ? 'border-[var(--brand)] bg-[var(--brand)]/5'
                    : 'border-[var(--border)] hover:border-[var(--border-strong)]'
                }`}
              >
                <input
                  type="radio"
                  name="delete-scope"
                  value={opt.value}
                  checked={scope === opt.value}
                  onChange={() => setScope(opt.value)}
                  className="mt-0.5 accent-[var(--brand)]"
                />
                <div>
                  <span className="text-sm font-medium text-[var(--text-primary)]">
                    {opt.label}
                  </span>
                  <p className="text-xs text-[var(--text-tertiary)] mt-0.5">
                    {opt.description}
                  </p>
                </div>
              </label>
            ))}
          </fieldset>

          <div className="flex justify-end gap-3">
            <button
              onClick={onCancel}
              className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={() => onConfirm(scope)}
              disabled={isPending}
              className="px-4 py-1.5 text-sm bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors font-medium disabled:opacity-50"
            >
              {isPending ? 'Deleting...' : 'Delete'}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
