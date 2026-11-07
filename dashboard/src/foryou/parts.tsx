import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { AttentionPerson } from '../api/client';
import { hueOf, initials, type IconName } from './model';

const PATHS: Record<IconName, ReactNode> = {
  finding: <><path d="M8.5 3h7L21 8.5v7L15.5 21h-7L3 15.5v-7z" /><path d="M12 8v4.5M12 16h.01" /></>,
  reminder: <><circle cx="12" cy="13" r="7" /><path d="M12 10v3l2 2M5 4 2 7M19 4l3 3" /></>,
  approval: <><path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z" /><path d="m9 12 2 2 4-4" /></>,
  ticket: <><path d="M4 7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2a2 2 0 0 0 0-4z" /><path d="M14 6v12" strokeDasharray="2 2" /></>,
  merge: <><circle cx="6" cy="5" r="2" /><circle cx="6" cy="19" r="2" /><circle cx="18" cy="12" r="2" /><path d="M6 7v10M6 7c0 3 2 5 10 5" /></>,
  loop: <><path d="M20 12a8 8 0 0 1-14.3 4.9M4 12A8 8 0 0 1 18.3 7.1" /><path d="M18 3v4.5h-4.5M6 21v-4.5h4.5" /></>,
  meeting: <><rect x="3" y="5" width="18" height="15" rx="2" /><path d="M3 10h18M8 3v4M16 3v4M7 14h4" /></>,
  chat: <><path d="M4 5h16v11H9l-5 4z" /><path d="M8 9h8M8 12h5" /></>,
  mention: <><circle cx="12" cy="12" r="4" /><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8" /></>,
  handoff: <path d="M4 12h12M12 6l6 6-6 6" />,
  knowledge: <><path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" /><path d="M4 19V5M9 7h6" /></>,
  proof: <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
  check: <path d="m5 12 4 4 10-10" />,
  x: <path d="M6 6l12 12M18 6 6 18" />,
  link: <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />,
  chev: <path d="m6 9 6 6 6-6" />,
  suspect: <><path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4z" /><path d="M19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" /></>,
  bell: <path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0" />,
  eye: <><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></>,
  lock: <><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></>,
  pause: <path d="M9 6v12M15 6v12" />,
  play: <path d="M8 5.5v13l11-6.5z" />,
  prev: <path d="m15 6-6 6 6 6" />,
  next: <path d="m9 6 6 6-6 6" />,
  keyboard: <><rect x="2.5" y="6" width="19" height="12" rx="2" /><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10" /></>,
};

export function Icon({ name, className = '' }: { name: IconName; className?: string }) {
  return (
    <svg className={`ic ${className}`} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {PATHS[name]}
    </svg>
  );
}

export function Avatar({ person, small }: { person: Pick<AttentionPerson, 'name' | 'agent'>; small?: boolean }) {
  return (
    <span
      className={`av${person.agent ? ' agent' : ''}`}
      style={{ ['--h' as string]: String(hueOf(person.name)), ...(small ? { width: 18, height: 18 } : {}) }}
      aria-hidden="true"
      title={person.name}
    >
      {initials(person.name)}
    </span>
  );
}

/** Counts up from the previous value (or 0) with an ease-out; a bump on change. */
export function CountUp({ value, reduce, decimals = 0, prefix = '' }: { value: number; reduce: boolean; decimals?: number; prefix?: string }) {
  const [shown, setShown] = useState(reduce ? value : 0);
  const [bump, setBump] = useState(false);
  const from = useRef<number | null>(null);
  useEffect(() => {
    const start = from.current ?? 0;
    const had = from.current !== null;
    from.current = value;
    if (reduce || start === value) {
      setShown(value);
      return;
    }
    if (had) {
      setBump(true);
      const t = window.setTimeout(() => setBump(false), 450);
      void t;
    }
    let raf = 0;
    const t0 = performance.now();
    const step = (t: number) => {
      const k = 1 - Math.pow(1 - Math.min(1, (t - t0) / 520), 3);
      setShown(start + (value - start) * k);
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, reduce]);
  const text = prefix + (decimals ? shown.toFixed(decimals) : String(Math.round(shown)));
  return <span className={bump ? 'bump' : undefined} style={{ display: 'inline-block' }}>{text}</span>;
}

export function Stamp({ label, tone }: { label: string; tone: 'ok' | 'snz' | 'no' }) {
  return (
    <div className={`stamp ${tone}`} aria-hidden="true">
      <span className="stamp-in">
        <Icon name={tone === 'snz' ? 'reminder' : tone === 'no' ? 'x' : 'check'} />
        <b>{label}</b>
      </span>
    </div>
  );
}
