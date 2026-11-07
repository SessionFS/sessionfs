import { useEffect, useState } from 'react';
import type { AttentionGroup, AttentionItem, ChyronEntry } from '../api/client';
import { getItem, setItem } from '../utils/storage';

/** The page's sections, in page order (keyboard 1-7; 8 = Working with others). */
export interface GroupMeta {
  id: AttentionGroup;
  title: string;
  short: string;
  noun: string;
  acc: string;
  sub: string;
  empty: string;
  grid: boolean;
}

export const GROUPS: GroupMeta[] = [
  {
    id: 'radar', title: 'On your radar', short: 'Radar', noun: 'items', acc: 'var(--cat-urgent)', grid: false,
    sub: 'Harmful, overdue, or blocking a merge. Start here.',
    empty: 'Nothing urgent right now.',
  },
  {
    id: 'waiting', title: 'Waiting on you', short: 'Waiting', noun: 'items', acc: 'var(--brand)', grid: true,
    sub: 'Assigned to you or a persona you represent, ready for you, or paused until you decide.',
    empty: 'No work is waiting on you.',
  },
  {
    id: 'asking', title: 'Your team is asking', short: 'Asking', noun: 'questions', acc: 'var(--cat-mention)', grid: true,
    sub: 'Mentions, questions and nudges from people you work with.',
    empty: 'No open questions for you.',
  },
  {
    id: 'meetings', title: 'From your meetings and chats', short: 'Meetings', noun: 'suggestions', acc: 'var(--cat-meeting)', grid: true,
    sub: 'Things that sounded like an action for you. Acknowledge one to turn it into a ticket in the right project or group.',
    empty: 'No new suggestions from your meetings or chats.',
  },
  {
    id: 'coming', title: 'Coming up', short: 'Coming', noun: 'items', acc: 'var(--cat-reminder)', grid: true,
    sub: 'Due in the next few days, so nothing catches you out.',
    empty: 'Nothing is due in the next few days.',
  },
  {
    id: 'second', title: 'Worth a second look', short: 'Second', noun: 'items', acc: 'var(--cat-forgotten)', grid: true,
    sub: 'Important, but quiet for a while. Easy to lose track of.',
    empty: 'Nothing important has gone quiet on you.',
  },
  {
    id: 'lately', title: 'What changed this week', short: 'Lately', noun: 'updates', acc: 'var(--border-strong)', grid: true,
    sub: 'Decisions and results in projects you work in, this week.',
    empty: 'You have read everything that changed this week.',
  },
];

export const ACTIONABLE: AttentionGroup[] = ['radar', 'waiting', 'asking'];
export const TOGETHER_ID = 'together';
export const SECTION_ORDER: string[] = [...GROUPS.map((g) => g.id), TOGETHER_ID];

export type IconName =
  | 'finding' | 'reminder' | 'approval' | 'ticket' | 'merge' | 'loop' | 'meeting' | 'chat'
  | 'mention' | 'handoff' | 'knowledge' | 'proof' | 'check' | 'x' | 'link' | 'chev'
  | 'suspect' | 'bell' | 'eye' | 'lock' | 'pause' | 'play' | 'prev' | 'next' | 'keyboard';

export interface KindMeta {
  label: string;
  icon: IconName;
  acc: string;
  source: string;
}

export function kindMeta(item: AttentionItem): KindMeta {
  const k = item.kind;
  if (k === 'reminder') {
    return item.group === 'asking'
      ? { label: 'Reminder from a teammate', icon: 'bell', acc: 'var(--cat-mention)', source: 'Reminder' }
      : { label: 'Reminder', icon: 'reminder', acc: 'var(--cat-reminder)', source: 'Reminder' };
  }
  if (k === 'ticket') return { label: 'Ticket', icon: 'ticket', acc: 'var(--cat-ticket)', source: 'Ticket' };
  if (k === 'loop_harmful_findings') return { label: 'Harmful finding', icon: 'finding', acc: 'var(--cat-urgent)', source: 'Review loop' };
  if (k === 'loop_ready') return { label: 'Ready to merge', icon: 'merge', acc: 'var(--success)', source: 'Review loop' };
  if (k === 'loop_needs_human') return { label: 'Review loop paused', icon: 'loop', acc: 'var(--cat-ticket)', source: 'Review loop' };
  if (k === 'loop_checklist') return { label: 'Checklist proposal', icon: 'loop', acc: 'var(--cat-ticket)', source: 'Review loop' };
  if (k === 'meeting_action') return { label: 'Meeting action', icon: 'meeting', acc: 'var(--cat-meeting)', source: 'Meeting' };
  if (k === 'meeting_suggestion') return { label: 'Suspected action', icon: 'suspect', acc: 'var(--cat-meeting)', source: 'Meeting' };
  if (k === 'chat_action') return { label: 'Chat action', icon: 'chat', acc: 'var(--cat-meeting)', source: 'Chat' };
  if (k === 'chat_suggestion') return { label: 'Suspected action', icon: 'suspect', acc: 'var(--cat-meeting)', source: 'Chat' };
  if (k === 'ticket_question') return { label: 'Question', icon: 'mention', acc: 'var(--cat-mention)', source: 'Ticket comment' };
  if (k === 'handoff') return { label: 'Handoff', icon: 'handoff', acc: 'var(--cat-forgotten)', source: 'Handoff' };
  if (k === 'knowledge_changes') return { label: 'Knowledge', icon: 'knowledge', acc: 'var(--cat-ticket)', source: 'Knowledge base' };
  if (k === 'proof') return { label: 'This week', icon: 'proof', acc: 'var(--success)', source: 'Spend report' };
  if (k.startsWith('approval')) return { label: 'Approval', icon: 'approval', acc: 'var(--cat-approval)', source: 'Approval' };
  return { label: 'Item', icon: 'ticket', acc: 'var(--cat-ticket)', source: item.context.source_label || 'Item' };
}

export const CHYRON_LABEL: Record<ChyronEntry['category'], string> = {
  urgent: 'Urgent',
  ticket: 'Ticket',
  mention: 'Mention',
  approval: 'Approval',
  reminder: 'Reminder',
  forgotten: 'Forgotten',
  meeting: 'Meeting',
};

// ── Time ──

const pad = (n: number) => (n < 10 ? `0${n}` : String(n));

export function hhmm(d: Date): string {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function dayLabel(d: Date): string {
  return d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
}

/** "Today 07:10", "Yesterday 17:40", "Thu 5 Nov", "Mon 9 Nov, 09:00". */
export function whenLabel(iso: string | null | undefined, now = new Date(), withTime = true): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(d) - day(now)) / 86_400_000);
  const time = withTime ? ` ${hhmm(d)}` : '';
  if (diff === 0) return `Today${time}`;
  if (diff === -1) return `Yesterday${time}`;
  if (diff === 1) return `Tomorrow${time}`;
  const short = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
  return withTime && Math.abs(diff) < 8 ? `${short}, ${hhmm(d)}` : short;
}

/** "6 min", "3 h", "2 d". */
export function ago(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return '';
  const ms = now.getTime() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return '';
  const min = Math.max(0, Math.round(ms / 60_000));
  if (min < 60) return `${Math.max(1, min)} min`;
  const h = Math.round(min / 60);
  if (h < 48) return `${h} h`;
  return `${Math.round(h / 24)} d`;
}

export function greeting(now = new Date()): string {
  const h = now.getHours();
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

export function localZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

export type SnoozePreset = '1h' | 'tomorrow' | 'next_week';

export function snoozeTarget(preset: SnoozePreset, now = new Date()): Date {
  if (preset === '1h') return new Date(now.getTime() + 3_600_000);
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 9, 0, 0, 0);
  if (preset === 'tomorrow') {
    d.setDate(d.getDate() + 1);
    return d;
  }
  // Next Monday (a week ahead on Mondays).
  const days = ((8 - now.getDay()) % 7) || 7;
  d.setDate(d.getDate() + days);
  return d;
}

// ── "Cleared today" (a per-browser log of what you acted on here) ──

export interface ClearedEntry {
  key: string;
  group: AttentionGroup;
  title: string;
  at: number;
}

function clearedKey(now = new Date()): string {
  return `sfs-foryou-cleared-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

export function loadCleared(now = new Date()): ClearedEntry[] {
  try {
    const raw = getItem(clearedKey(now));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((e) => e && typeof e.key === 'string') : [];
  } catch {
    return [];
  }
}

export function saveCleared(entries: ClearedEntry[], now = new Date()): void {
  setItem(clearedKey(now), JSON.stringify(entries.slice(-200)));
}

// ── Home preference ──

export const HOME_PREF_KEY = 'sfs-home';

export function prefersSessionsHome(): boolean {
  return getItem(HOME_PREF_KEY) === 'sessions';
}

export function setSessionsHome(on: boolean): void {
  setItem(HOME_PREF_KEY, on ? 'sessions' : 'for-you');
}

// ── Reduced motion ──

export function useReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)';
  const [reduce, setReduce] = useState(
    () => typeof window !== 'undefined' && !!window.matchMedia && window.matchMedia(query).matches,
  );
  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia(query);
    const on = (e: MediaQueryListEvent) => setReduce(e.matches);
    mq.addEventListener?.('change', on);
    return () => mq.removeEventListener?.('change', on);
  }, []);
  return reduce;
}

/** Stable hue for an avatar from its name. */
export function hueOf(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return h;
}

export function initials(name: string): string {
  if (name === 'You') return 'YO';
  const parts = name.replace(/[^A-Za-z0-9 ._-]/g, ' ').split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return '·';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}
