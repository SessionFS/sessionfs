import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { hhmm, snoozeTarget, type SnoozePreset } from './model';

const PRESETS: { id: SnoozePreset; label: string }[] = [
  { id: '1h', label: 'In 1 hour' },
  { id: 'tomorrow', label: 'Tomorrow' },
  { id: 'next_week', label: 'Next week' },
];

export function presetCaption(id: SnoozePreset, now = new Date()): string {
  const t = snoozeTarget(id, now);
  if (id === '1h') return hhmm(t);
  if (id === 'tomorrow') return `${t.toLocaleDateString('en-GB', { weekday: 'short' })} 09:00`;
  return `${t.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })}, 09:00`;
}

/** The snooze / "Remind me later" menu: anchored under its button on wide
 *  screens, a bottom sheet over a scrim on phones. Arrow keys, j/k and 1-3
 *  move and pick; Escape closes and returns focus to the button. */
export default function SnoozeMenu({
  anchor,
  title,
  onPick,
  onClose,
}: {
  anchor: HTMLElement;
  title: string;
  onPick: (preset: SnoozePreset) => void;
  onClose: (restoreFocus: boolean) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const small = typeof window !== 'undefined' && window.innerWidth <= 640;

  useLayoutEffect(() => {
    if (small || !ref.current) return;
    const r = anchor.getBoundingClientRect();
    const mw = ref.current.offsetWidth;
    const mh = ref.current.offsetHeight;
    let top = r.bottom + 6;
    if (top + mh > window.innerHeight - 8) top = r.top - mh - 6;
    setPos({ top, left: Math.max(8, Math.min(r.left, window.innerWidth - mw - 8)) });
  }, [anchor, small]);

  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>('button')?.focus();
    anchor.setAttribute('aria-expanded', 'true');
    const away = (e: MouseEvent) => {
      if (ref.current?.contains(e.target as Node) || anchor.contains(e.target as Node)) return;
      onClose(false);
    };
    const resize = () => onClose(false);
    document.addEventListener('mousedown', away);
    window.addEventListener('resize', resize);
    return () => {
      anchor.setAttribute('aria-expanded', 'false');
      document.removeEventListener('mousedown', away);
      window.removeEventListener('resize', resize);
    };
  }, [anchor, onClose]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const items = Array.from(ref.current?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    const i = items.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === 'ArrowDown' || e.key === 'j') {
      e.preventDefault();
      items[(i + 1) % items.length]?.focus();
    } else if (e.key === 'ArrowUp' || e.key === 'k') {
      e.preventDefault();
      items[(i - 1 + items.length) % items.length]?.focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onClose(true);
    } else if (e.key === 'Tab') {
      onClose(false);
    } else if (e.key === '1' || e.key === '2' || e.key === '3') {
      e.preventDefault();
      onPick(PRESETS[Number(e.key) - 1].id);
    }
  };

  return (
    <>
      {small && <div className="fy-menuscrim" onClick={() => onClose(true)} />}
      <div
        ref={ref}
        className="fy-menu"
        role="menu"
        aria-label={title}
        onKeyDown={onKeyDown}
        style={small ? undefined : { top: pos?.top ?? -9999, left: pos?.left ?? -9999 }}
      >
        <div className="mh" aria-hidden="true">{title}</div>
        {PRESETS.map((p) => (
          <button key={p.id} type="button" role="menuitem" onClick={() => onPick(p.id)}>
            {p.label}
            <span>{presetCaption(p.id)}</span>
          </button>
        ))}
      </div>
    </>
  );
}
