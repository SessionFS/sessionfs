import { useEffect, useRef, type ReactNode } from 'react';
import { useFocusTrap } from '../../hooks/useFocusTrap';

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  /** ID for aria-labelledby — must match a heading inside children. */
  titleId: string;
  children: ReactNode;
}

const KEYFRAME_ID = 'sfs-drawer-keyframes';

function injectKeyframes() {
  if (typeof document === 'undefined') return;
  if (document.getElementById(KEYFRAME_ID)) return;
  const style = document.createElement('style');
  style.id = KEYFRAME_ID;
  style.textContent = `
    @keyframes sfs-drawer-in {
      from { transform: translateX(100%); }
      to   { transform: translateX(0); }
    }
    @media (prefers-reduced-motion: reduce) {
      .sfs-drawer-panel { animation: none; }
    }
  `;
  document.head.appendChild(style);
}

/**
 * Right-side slide-in drawer. Reuses Dialog's focus-trap, Escape, backdrop,
 * and body-scroll-lock patterns. Anchored right, full viewport height,
 * 480 px wide (100 % on mobile via max-w-full).
 */
export function Drawer({ open, onClose, titleId, children }: DrawerProps) {
  const drawerRef = useRef<HTMLDivElement>(null);
  useFocusTrap(open ? drawerRef : { current: null });

  // Esc to close
  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  // Body scroll lock
  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden';
      return () => {
        document.body.style.overflow = '';
      };
    }
  }, [open]);

  // Inject animation keyframes once (idempotent)
  useEffect(() => {
    if (open) injectKeyframes();
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50">
      {/* Backdrop */}
      <div
        className="absolute inset-0"
        style={{ backgroundColor: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(4px)' }}
        onClick={onClose}
      />
      {/* Drawer panel */}
      <div
        ref={drawerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="sfs-drawer-panel absolute right-0 top-0 bottom-0 w-[480px] max-w-full overflow-y-auto border-l border-border bg-overlay"
        style={{ animation: 'sfs-drawer-in 150ms ease-out' }}
      >
        {/* X close button */}
        <button
          type="button"
          onClick={onClose}
          className="absolute top-3 right-3 p-2 rounded-lg text-text-tertiary hover:text-text-primary hover:bg-bg-sunken outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
          aria-label="Close drawer"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
        <div className="p-6">{children}</div>
      </div>
    </div>
  );
}
