import { useEffect, useRef, type ReactNode } from 'react';
import { useFocusTrap } from '../../hooks/useFocusTrap';

interface DialogProps {
  open: boolean;
  onClose: () => void;
  /** ID for aria-labelledby — must match a heading inside children. */
  titleId: string;
  /** Optional className for the dialog panel (e.g. max-w-3xl for wide content). */
  className?: string;
  children: ReactNode;
}

export function Dialog({ open, onClose, titleId, className: panelClassName, children }: DialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(open ? dialogRef : { current: null });

  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden';
      return () => { document.body.style.overflow = ''; };
    }
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0"
        style={{ backgroundColor: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(4px)' }}
        onClick={onClose}
      />
      {/* Dialog */}
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`relative rounded-xl p-6 max-w-lg w-full mx-4 ${panelClassName ?? ''}`}
        style={{
          backgroundColor: 'var(--overlay)',
          border: '1px solid var(--border)',
          boxShadow: 'var(--shadow-overlay)',
          animation: 'overlayEnter 150ms ease-out both',
        }}
      >
        {children}
      </div>
    </div>
  );
}

/* ── Convenience header/footer for consistent dialog layouts ── */

export function DialogHeader({ titleId, children }: { titleId: string; children: ReactNode }) {
  return (
    <h2 id={titleId} className="text-lg font-semibold text-[var(--text-primary)] mb-4">
      {children}
    </h2>
  );
}

export function DialogFooter({ children }: { children: ReactNode }) {
  return (
    <div className="flex justify-end gap-3 mt-6">
      {children}
    </div>
  );
}
