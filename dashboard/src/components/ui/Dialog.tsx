import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
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

/** Open dialogs, oldest first (Escape goes to the last one). Subscribers are
 *  told when it changes so each dialog knows whether it is the topmost. */
let openDialogs: symbol[] = [];
const stackListeners = new Set<() => void>();
function setOpenDialogs(next: symbol[]) {
  openDialogs = next;
  for (const l of stackListeners) l();
}
function subscribeStack(listener: () => void) {
  stackListeners.add(listener);
  return () => stackListeners.delete(listener);
}
const getStack = () => openDialogs;

export function Dialog({ open, onClose, titleId, className: panelClassName, children }: DialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(open ? dialogRef : { current: null });

  // Registered once per open (not per onClose identity) so a re-render of an
  // outer dialog never moves it above a confirm dialog opened over it.
  const [token] = useState(() => Symbol('dialog'));
  useEffect(() => {
    if (!open) return;
    setOpenDialogs([...openDialogs, token]);
    return () => setOpenDialogs(openDialogs.filter((t) => t !== token));
  }, [open, token]);
  const stack = useSyncExternalStore(subscribeStack, getStack, getStack);
  // Not registered yet (first render) counts as topmost.
  const isTop = !stack.includes(token) || stack[stack.length - 1] === token;

  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      // With a confirm dialog open over another dialog, only the topmost
      // one closes on Escape.
      if (openDialogs[openDialogs.length - 1] !== token) return;
      // A dialog sits above a drawer (and any other Escape-closing surface).
      // Capture the key first and consume it so the surface underneath does not
      // also close on the same press.
      e.preventDefault();
      e.stopPropagation();
      onClose();
    }
    document.addEventListener('keydown', handleKey, true);
    return () => document.removeEventListener('keydown', handleKey, true);
  }, [open, onClose, token]);

  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden';
      return () => {
        // Another dialog (the one a confirm was opened over) still needs it.
        if (openDialogs.length === 0) document.body.style.overflow = '';
      };
    }
  }, [open]);

  if (!open) return null;

  return (
    // A dialog with another one open over it is inert and hidden from
    // assistive tech; only the topmost is aria-modal.
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      inert={!isTop || undefined}
      aria-hidden={isTop ? undefined : true}
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0"
        style={{ backgroundColor: 'var(--scrim)', backdropFilter: 'blur(4px)' }}
        onClick={onClose}
      />
      {/* Dialog */}
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal={isTop ? true : undefined}
        aria-labelledby={titleId}
        className={`relative rounded-xl p-6 max-w-lg w-full mx-4 border border-border bg-overlay ${panelClassName ?? ''}`}
        style={{ boxShadow: 'var(--shadow-overlay)', animation: 'overlayEnter 150ms ease-out both' }}
      >
        {children}
      </div>
    </div>
  );
}

/* ── Convenience header/footer for consistent dialog layouts ── */

export function DialogHeader({ titleId, children }: { titleId: string; children: ReactNode }) {
  return (
    <h2 id={titleId} className="text-lg font-semibold text-text-primary mb-4">
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
