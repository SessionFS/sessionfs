import type { ReactNode } from 'react';

interface KbdProps {
  children: ReactNode;
}

/** Keyboard shortcut chip — mono font, sunken background, hairline border. */
export function Kbd({ children }: KbdProps) {
  return (
    <kbd className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium text-[var(--text-tertiary)] bg-[var(--bg-sunken)] border border-[var(--border)] rounded font-mono">
      {children}
    </kbd>
  );
}

/** Convenience: ⌘ + single key. */
export function KbdShortcut({ keys }: { keys: string[] }) {
  return (
    <span className="inline-flex items-center gap-0.5">
      {keys.map((k, i) => (
        <Kbd key={i}>{k}</Kbd>
      ))}
    </span>
  );
}
