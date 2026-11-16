import { useEffect, useRef, useState } from 'react';
import {
  AlarmClock, AtSign, Bell, Book, ChartColumn, Check, ChevronDown, ChevronLeft, ChevronRight, Eye, GitMerge,
  Keyboard, Link, Lock, MessageSquareText, MessagesSquare, OctagonAlert, Pause, Play, Repeat, Send, ShieldCheck, Sparkles,
  Ticket, X, type LucideIcon,
} from 'lucide-react';
import type { AttentionPerson } from '../api/client';
import { Icon as UiIcon, type IconSize } from '../components/ui/Icon';
import { hueOf, initials, type IconName } from './model';

const GLYPHS: Record<IconName, LucideIcon> = {
  finding: OctagonAlert,
  reminder: AlarmClock,
  approval: ShieldCheck,
  ticket: Ticket,
  merge: GitMerge,
  loop: Repeat,
  meeting: MessagesSquare,
  chat: MessageSquareText,
  mention: AtSign,
  handoff: Send,
  knowledge: Book,
  proof: ChartColumn,
  check: Check,
  x: X,
  link: Link,
  chev: ChevronDown,
  suspect: Sparkles,
  bell: Bell,
  eye: Eye,
  lock: Lock,
  pause: Pause,
  play: Play,
  prev: ChevronLeft,
  next: ChevronRight,
  keyboard: Keyboard,
};

export function Icon({ name, size = 16, className = '' }: { name: IconName; size?: IconSize; className?: string }) {
  return <UiIcon icon={GLYPHS[name]} size={size} className={`ic ${className}`.trim()} />;
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
        <Icon name={tone === 'snz' ? 'reminder' : tone === 'no' ? 'x' : 'check'} size={14} />
        <b>{label}</b>
      </span>
    </div>
  );
}
