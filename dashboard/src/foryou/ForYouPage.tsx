import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { AttentionFeed, AttentionGroup, AttentionItem } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { Button, Dialog, Switch, Tooltip } from '../components/ui';
import { useHelper } from '../helper/HelperProvider';
import { FOR_YOU_KEY, useForYouFeed } from '../hooks/useForYou';
import { useMe } from '../hooks/useMe';
import { errorMessage } from '../utils/errorMessage';
import CaughtUp from './CaughtUp';
import Chyron from './Chyron';
import CollabBand from './CollabBand';
import ItemCard, { type CardApi, type CardHandle, type StampState } from './ItemCard';
import SnoozeMenu, { presetCaption } from './SnoozeMenu';
import {
  ACTIONABLE,
  GROUPS,
  SECTION_CAP,
  SECTION_ORDER,
  TOGETHER_ID,
  dayLabel,
  greeting,
  hhmm,
  loadCleared,
  loadExpanded,
  localZone,
  prefersSessionsHome,
  saveCleared,
  saveExpanded,
  sectionCaption,
  setSessionsHome,
  snoozeTarget,
  useReducedMotion,
  whenLabel,
  type ClearedEntry,
  type SnoozePreset,
} from './model';
import { CountUp, Icon } from './parts';
import './foryou.css';
import { Term } from '../help/Term';

/** The Undo toast stays at least this long (and while hovered or focused). */
const UNDO_MS = 10_000;

/** Which provider sections feed which page sections (for "couldn't load"). */
const SECTION_SOURCES: Record<AttentionGroup, string[]> = {
  radar: ['reminders', 'approvals', 'harmful_findings', 'tickets'],
  waiting: ['tickets', 'loop_needs_you', 'handoffs'],
  asking: ['questions', 'reminders'],
  meetings: ['conversation_actions'],
  coming: ['reminders', 'tickets'],
  second: ['tickets', 'handoffs'],
  lately: ['knowledge_changes', 'proof'],
};

interface Gone {
  stamp: StampState | null;
  leaving: '' | 'right' | 'left' | 'down';
  hidden: boolean;
  group: AttentionGroup;
}

interface Toast {
  id: number;
  msg: string;
  undo: boolean;
}

type DeferredAction = 'done' | 'hide' | 'answered' | 'read' | 'dismiss';

const ACT_SPEC: Record<DeferredAction, { stamp: string | null; tone: StampState['tone']; dir: Gone['leaving']; msg: string; log: boolean }> = {
  done: { stamp: 'Done', tone: 'ok', dir: 'right', msg: 'Done.', log: true },
  read: { stamp: 'Read', tone: 'ok', dir: 'right', msg: 'Marked as read.', log: true },
  answered: { stamp: 'Answered', tone: 'ok', dir: 'right', msg: 'Marked as answered.', log: true },
  hide: { stamp: null, tone: 'ok', dir: 'left', msg: 'Hidden until it changes. It comes back if someone updates it.', log: false },
  dismiss: { stamp: 'Not an action', tone: 'no', dir: 'left', msg: 'Dismissed. It won’t be suggested to you again.', log: false },
};

function firstName(me: { display_name?: string | null; email?: string } | undefined): string {
  const dn = me?.display_name?.trim();
  if (dn) return dn.split(/\s+/)[0];
  const local = me?.email?.split('@')[0] ?? '';
  return local ? local.charAt(0).toUpperCase() + local.slice(1) : 'there';
}

export default function ForYouPage() {
  const { auth } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const me = useMe();
  const { openHelper } = useHelper();
  const feedQ = useForYouFeed();
  const reduce = useReducedMotion();

  const [gone, setGone] = useState<Record<string, Gone>>({});
  const [toast, setToast] = useState<Toast | null>(null);
  const [announce, setAnnounce] = useState('');
  const [menu, setMenu] = useState<{ item: AttentionItem; anchor: HTMLElement } | null>(null);
  const [kbdOpen, setKbdOpen] = useState(false);
  const [flashKey, setFlashKey] = useState<string | null>(null);
  const [cleared, setCleared] = useState<ClearedEntry[]>(() => loadCleared());
  const [ready, setReady] = useState(false);
  const [settled, setSettled] = useState(false);
  const [sessionsHome, setSessionsHomeState] = useState(prefersSessionsHome);
  const [now, setNow] = useState(() => new Date());
  const [expanded, setExpanded] = useState<Set<AttentionGroup>>(loadExpanded);

  const handles = useRef(new Map<string, CardHandle>());
  // The last action and how to reverse it (Undo).
  const lastAction = useRef<{ keys: string[]; logged: boolean; inverse: () => Promise<unknown> } | null>(null);
  const toastTimer = useRef<number>(0);
  const toastHeld = useRef(false);
  const caughtRef = useRef<HTMLElement>(null);
  const moreRefs = useRef(new Map<AttentionGroup, HTMLButtonElement>());
  // After expanding: focus the first newly shown card, or a card goto() asked for.
  const afterExpand = useRef<{ group: AttentionGroup; key: string | null; goto: boolean } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const feed: AttentionFeed | undefined = feedQ.data?.feed;

  // "Updated hh:mm" ticks when the server sends a changed feed.
  const etag = feedQ.data?.etag;
  const lastEtag = useRef<string | undefined>(undefined);
  const [ticked, setTicked] = useState(0);
  useEffect(() => {
    if (!etag) return;
    if (lastEtag.current && lastEtag.current !== etag) setTicked((n) => n + 1);
    lastEtag.current = etag;
  }, [etag]);

  // First paint: fills and count-ups run once the cards are in.
  useEffect(() => {
    if (!feed) return;
    let r2 = 0;
    const r1 = requestAnimationFrame(() => { r2 = requestAnimationFrame(() => setReady(true)); });
    const t = window.setTimeout(() => setSettled(true), 1800);
    return () => { cancelAnimationFrame(r1); cancelAnimationFrame(r2); window.clearTimeout(t); };
  }, [feed]);

  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 30_000);
    return () => window.clearInterval(t);
  }, []);

  const say = useCallback((text: string) => {
    setAnnounce('');
    window.setTimeout(() => setAnnounce(text), 30);
  }, []);

  // Toasts close on their own unless hovered or focused (then they wait).
  const armToast = useCallback((id: number, ms: number) => {
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => {
      if (toastHeld.current) {
        armToast(id, 1000);
        return;
      }
      setToast((t) => (t && t.id === id ? null : t));
    }, ms);
  }, []);
  const showToast = useCallback((msg: string, undo = false) => {
    const id = Date.now();
    setToast({ id, msg, undo });
    armToast(id, undo ? UNDO_MS : 5000);
  }, [armToast]);

  const invalidate = useCallback(() => {
    void qc.invalidateQueries({ queryKey: FOR_YOU_KEY });
    void qc.invalidateQueries({ queryKey: ['reminders'] });
  }, [qc]);

  const items = useMemo(() => feed?.items ?? [], [feed]);
  const byKey = useMemo(() => new Map(items.map((i) => [i.item_key, i])), [items]);

  // Forget optimistic state for items the server no longer returns.
  useEffect(() => {
    setGone((g) => {
      const next: Record<string, Gone> = {};
      let changed = false;
      for (const [k, v] of Object.entries(g)) {
        if (byKey.has(k)) next[k] = v;
        else changed = true;
      }
      return changed ? next : g;
    });
  }, [byKey]);

  const visible = useMemo(() => items.filter((i) => !gone[i.item_key]?.hidden), [items, gone]);
  // What each section puts on screen: every item when expanded, else the first
  // SECTION_CAP in rank order. A leaving card keeps its place until it is
  // hidden, then the next one moves up into the cap.
  const shownKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const g of GROUPS) {
      const list = visible.filter((i) => i.group === g.id);
      for (const it of expanded.has(g.id) ? list : list.slice(0, SECTION_CAP)) keys.add(it.item_key);
    }
    return keys;
  }, [visible, expanded]);
  // Read from event handlers (removeVisual), so they see this render's lists.
  const visibleRef = useRef(visible);
  const shownRef = useRef(shownKeys);
  useLayoutEffect(() => {
    visibleRef.current = visible;
    shownRef.current = shownKeys;
  }, [visible, shownKeys]);
  // A card that will move up into a collapsed section once the one leaving is gone.
  const pendingFocus = useRef<string | null>(null);
  useEffect(() => {
    const k = pendingFocus.current;
    if (!k || !shownKeys.has(k)) return;
    pendingFocus.current = null;
    const ae = document.activeElement;
    if (!ae || ae === document.body) handles.current.get(k)?.focus();
  }, [shownKeys]);
  const live = useMemo(() => items.filter((i) => !gone[i.item_key]), [items, gone]);

  const groupCount = useCallback(
    (g: AttentionGroup) => {
      const base = feed?.groups?.[g] ?? 0;
      const removed = Object.values(gone).filter((x) => x.group === g).length;
      return Math.max(0, base - removed);
    },
    [feed, gone],
  );
  const actionable = ACTIONABLE.reduce((n, g) => n + groupCount(g), 0);
  // Never claim "caught up" while a source behind those sections failed.
  const actionableBroken = ACTIONABLE.some((g) =>
    SECTION_SOURCES[g].some((src) => feed?.sections?.[src]?.error),
  );
  const caughtUp = !!feed && actionable === 0 && !actionableBroken;
  const topItem = live.find((i) => ACTIONABLE.includes(i.group));

  // ── Visual removal + undo ──

  const visibleCardEls = () =>
    Array.from(rootRef.current?.querySelectorAll<HTMLElement>('.slot:not(.leaving) .card') ?? []);

  const removeVisual = useCallback((list: AttentionItem[], stamp: StampState | null, dir: Gone['leaving']) => {
    const keys = list.map((i) => i.item_key);
    const active = document.activeElement as HTMLElement | null;
    const focused = active?.closest?.('.card') as HTMLElement | null;
    let nextFocus: HTMLElement | null = null;
    if (focused && keys.includes(focused.dataset.key ?? '')) {
      const cards = visibleCardEls();
      const idx = cards.indexOf(focused);
      const group = list.find((i) => i.item_key === focused.dataset.key)?.group;
      const after = cards.slice(idx + 1).find((c) => !keys.includes(c.dataset.key ?? ''));
      const inGroup = (el: HTMLElement | undefined) =>
        !!el && visibleRef.current.some((i) => i.item_key === el.dataset.key && i.group === group);
      // In a collapsed section the next card may still be behind "Show N more":
      // it moves up once this one has left, and focus follows it there.
      const successor = inGroup(after)
        ? undefined
        : visibleRef.current.find((i) => i.group === group && !keys.includes(i.item_key) && !shownRef.current.has(i.item_key));
      if (successor) pendingFocus.current = successor.item_key;
      else {
        nextFocus = after
          ?? cards.slice(0, idx).reverse().find((c) => !keys.includes(c.dataset.key ?? '')) ?? null;
      }
    }
    const stampMs = stamp && !reduce ? 420 : 0;
    setGone((g) => {
      const next = { ...g };
      for (const it of list) next[it.item_key] = { stamp, leaving: stampMs ? '' : dir || 'right', hidden: false, group: it.group };
      return next;
    });
    if (stampMs) {
      window.setTimeout(() => setGone((g) => {
        const next = { ...g };
        for (const k of keys) if (next[k]) next[k] = { ...next[k], leaving: dir || 'right' };
        return next;
      }), stampMs);
    }
    window.setTimeout(() => setGone((g) => {
      const next = { ...g };
      for (const k of keys) if (next[k]) next[k] = { ...next[k], hidden: true };
      return next;
    }), reduce ? 0 : stampMs + 340);
    if (nextFocus) nextFocus.focus({ preventScroll: false });
  }, [reduce]);

  const restore = useCallback((keys: string[]) => {
    setGone((g) => {
      const next = { ...g };
      for (const k of keys) delete next[k];
      return next;
    });
    if (keys[0]) {
      setFlashKey(keys[0]);
      window.setTimeout(() => setFlashKey(null), 1300);
      window.setTimeout(() => handles.current.get(keys[0])?.focus(), 40);
    }
  }, []);

  const logCleared = useCallback((list: AttentionItem[]) => {
    setCleared((c) => {
      const next = [...c, ...list.map((i) => ({ key: i.item_key, group: i.group, title: i.title, at: Date.now() }))];
      saveCleared(next);
      return next;
    });
  }, []);

  const unlog = useCallback((keys: string[]) => {
    setCleared((c) => {
      const next = [...c];
      for (const k of keys) {
        const i = next.map((e) => e.key).lastIndexOf(k);
        if (i >= 0) next.splice(i, 1);
      }
      saveCleared(next);
      return next;
    });
  }, []);

  /** Leave now, write now, and keep the inverse for Undo. Closing the tab
   *  never loses an action: the write is already done. */
  const commitNow = useCallback(async (
    list: AttentionItem[],
    spec: { stamp: StampState | null; dir: Gone['leaving']; msg: string; log: boolean },
    run: () => Promise<unknown>,
    inverse: () => Promise<unknown>,
  ) => {
    removeVisual(list, spec.stamp, spec.dir);
    if (spec.log) logCleared(list);
    const keys = list.map((i) => i.item_key);
    lastAction.current = { keys, logged: spec.log, inverse };
    showToast(spec.msg, true);
    say(spec.msg);
    try {
      await run();
      invalidate();
    } catch (e) {
      if (lastAction.current?.keys === keys) lastAction.current = null;
      restore(keys);
      if (spec.log) unlog(keys);
      showToast(`Couldn’t save that: ${errorMessage(e)}`);
    }
  }, [invalidate, logCleared, removeVisual, restore, say, showToast, unlog]);

  const undo = useCallback(async () => {
    const last = lastAction.current;
    if (!last) return;
    lastAction.current = null;
    try {
      await last.inverse();
    } catch (e) {
      showToast(`Couldn’t undo that: ${errorMessage(e)}`);
      return;
    }
    restore(last.keys);
    if (last.logged) unlog(last.keys);
    showToast(last.keys.length > 1 ? `Restored ${last.keys.length} cards.` : 'Restored.');
    say('Restored.');
    invalidate();
  }, [invalidate, restore, say, showToast, unlog]);

  const act = useCallback((item: AttentionItem, action: DeferredAction) => {
    const spec = ACT_SPEC[action];
    const client = auth!.client;
    const key = item.item_key;
    let run: () => Promise<unknown> = () => client.hideForYouItem(key);
    let inverse: () => Promise<unknown> = () => client.unhideForYouItem(key);
    let msg = spec.msg;
    if (action === 'done' && item.kind === 'reminder') {
      run = () => client.patchReminder(item.source.id, { status: 'done' });
      inverse = () => client.patchReminder(item.source.id, { status: 'open' });
      msg = 'Reminder closed as done.';
    } else if (action === 'dismiss' && item.context.meeting) {
      const m = item.context.meeting;
      run = () => client.dismissMeetingItem(m.id, m.item_id);
      inverse = () => client.restoreMeetingItem(m.id, m.item_id);
    }
    void commitNow([item], { stamp: spec.stamp ? { label: spec.stamp, tone: spec.tone } : null, dir: spec.dir, msg, log: spec.log }, run, inverse);
  }, [auth, commitNow]);

  const snooze = useCallback((item: AttentionItem, preset: SnoozePreset) => {
    const client = auth!.client;
    const key = item.item_key;
    const until = snoozeTarget(preset);
    const caption = presetCaption(preset);
    const zone = localZone();
    if (item.actions.includes('remind') && item.context.meeting) {
      const m = item.context.meeting;
      const scope = m.project_id ? { kind: 'project' as const, id: m.project_id } : { kind: 'group' as const, id: m.group_id! };
      let created: string | null = null;
      void commitNow([item], { stamp: { label: `Reminder · ${caption}`, tone: 'snz' }, dir: 'down', msg: `We’ll remind you ${preset === '1h' ? `at ${caption}` : caption}. The suggestion comes back then.`, log: false }, async () => {
        const r = await client.createMeetingItemReminder(scope, m.id, m.item_id, { due_at: until.toISOString(), ...(zone ? { timezone: zone } : {}) });
        created = r.id;
        await client.hideForYouItem(key, until.toISOString());
      }, async () => {
        if (created) await client.deleteReminder(created);
        await client.unhideForYouItem(key);
      });
      return;
    }
    const original = item.due_at;
    const reminderSnooze = item.kind === 'reminder';
    void commitNow(
      [item],
      { stamp: { label: `Snoozed · until ${caption}`, tone: 'snz' }, dir: 'down', msg: `Snoozed until ${caption}. It comes back then.`, log: false },
      reminderSnooze ? () => client.snoozeReminder(item.source.id, { preset }) : () => client.hideForYouItem(key, until.toISOString()),
      reminderSnooze && original
        ? () => client.patchReminder(item.source.id, { due_at: original })
        : () => client.unhideForYouItem(key),
    );
  }, [auth, commitNow]);

  const goto = useCallback((key: string) => {
    const it = byKey.get(key);
    // Behind "Show N more": open the section first, then go to it.
    if (it && !gone[key] && !shownKeys.has(key) && !expanded.has(it.group)) {
      afterExpand.current = { group: it.group, key, goto: true };
      setExpanded((cur) => {
        const next = new Set(cur).add(it.group);
        saveExpanded(next);
        return next;
      });
      return;
    }
    const el = document.getElementById(`card-${key}`);
    if (!it || gone[key] || !el) {
      showToast(it ? `Already handled: ${it.title}.` : 'That item is no longer on this page.');
      return;
    }
    el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
    handles.current.get(key)?.focus();
    setFlashKey(key);
    window.setTimeout(() => setFlashKey(null), 1300);
  }, [byKey, expanded, gone, reduce, shownKeys, showToast]);

  const toggleSection = useCallback((g: AttentionGroup) => {
    const opening = !expanded.has(g);
    if (opening) {
      // The first card that was hidden: focus moves there so the keyboard carries on.
      const list = visible.filter((i) => i.group === g);
      afterExpand.current = { group: g, key: list.find((i) => !shownKeys.has(i.item_key))?.item_key ?? null, goto: false };
    }
    setExpanded((cur) => {
      const next = new Set(cur);
      if (opening) next.add(g);
      else next.delete(g);
      saveExpanded(next);
      return next;
    });
    if (!opening) {
      // Focus may have been on a card that is now hidden: it goes to the toggle,
      // which moves up with the shorter list.
      const meta = GROUPS.find((x) => x.id === g);
      say(`${meta?.title ?? 'Section'}: showing the first ${SECTION_CAP}.`);
      requestAnimationFrame(() => {
        const btn = moreRefs.current.get(g);
        btn?.focus({ preventScroll: true });
        btn?.scrollIntoView({ block: 'nearest', behavior: reduce ? 'auto' : 'smooth' });
      });
    }
  }, [expanded, reduce, say, shownKeys, visible]);

  useEffect(() => {
    const pending = afterExpand.current;
    if (!pending || !expanded.has(pending.group)) return;
    afterExpand.current = null;
    if (pending.goto && pending.key) {
      goto(pending.key);
      return;
    }
    const count = visible.filter((i) => i.group === pending.group && !gone[i.item_key]).length;
    say(`Showing all ${count}.`);
    if (pending.key) handles.current.get(pending.key)?.focus();
  }, [expanded, goto, gone, say, visible]);

  const commit = useCallback<CardApi['commit']>(async (item, done, run) => {
    const result = await run();
    const custom = result && typeof result === 'object' ? (result as { stamp?: string; msg?: string }) : null;
    removeVisual([item], { label: custom?.stamp || done.label, tone: done.tone }, 'right');
    logCleared([item]);
    const msg = typeof result === 'string' ? result : custom?.msg || done.msg || 'Done.';
    showToast(msg);
    say(msg);
    invalidate();
    return true;
  }, [invalidate, logCleared, removeVisual, say, showToast]);

  const api: CardApi = useMemo(() => ({
    act,
    openSnooze: (item, anchor) => setMenu({ item, anchor }),
    open: (item) => navigate(item.link),
    commit,
    notify: (msg: string) => { showToast(msg); say(msg); },
    goto,
    askHelper: openHelper
      ? (item) => openHelper(item.project_id ? { project_id: item.project_id, label: item.context.project_name ?? undefined } : undefined)
      : undefined,
    meUserId: me.data?.user_id,
  }), [act, commit, goto, me.data?.user_id, navigate, openHelper, say, showToast]);

  const register = useCallback((key: string, h: CardHandle | null) => {
    if (h) handles.current.set(key, h);
    else handles.current.delete(key);
  }, []);

  // ── Jumping ──

  const jump = useCallback((section: string) => {
    if (caughtUp && (ACTIONABLE as string[]).includes(section)) {
      caughtRef.current?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
      caughtRef.current?.focus({ preventScroll: true });
      return;
    }
    const band = document.getElementById(`band-${section}`);
    if (!band) return;
    band.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
    const first = band.querySelector<HTMLElement>('.slot:not(.leaving) .card');
    (first ?? band.querySelector<HTMLElement>('h2'))?.focus({ preventScroll: true });
  }, [caughtUp, reduce]);

  // Caught up: play the seal once, and move focus there if it was on a card.
  const [caughtPlay, setCaughtPlay] = useState(false);
  const wasCaught = useRef(false);
  useEffect(() => {
    if (caughtUp && !wasCaught.current) {
      wasCaught.current = true;
      setCaughtPlay(true);
      if (Object.keys(gone).length > 0) {
        say('You are all caught up.');
        const ae = document.activeElement;
        if (!ae || ae === document.body || ae.closest?.('.card')) caughtRef.current?.focus({ preventScroll: true });
      }
    } else if (!caughtUp) {
      wasCaught.current = false;
      setCaughtPlay(false);
    }
  }, [caughtUp, gone, say]);

  // ── Keyboard ──

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.isContentEditable)) return;
        // A dialog or drawer locks body scroll while open; shortcuts wait.
      if (kbdOpen || menu || document.body.style.overflow === 'hidden') return;
      // Only while focus is on this page (or nowhere), never in a menu or dialog.
      const ae = document.activeElement;
      if (ae && ae !== document.body && !rootRef.current?.contains(ae)) return;
      if (t?.closest?.('[role="menu"], [role="dialog"], [role="listbox"]')) return;
      if (!rootRef.current) return;
      const k = e.key;
      const cards = visibleCardEls();
      const cur = (t?.closest?.('.card') as HTMLElement | null) ?? null;
      if (k === '?') { e.preventDefault(); setKbdOpen(true); return; }
      if (k === 'Escape') { setToast(null); return; }
      if (k === 'j' || k === 'k') {
        e.preventDefault();
        if (!cards.length) return;
        const i = cur ? cards.indexOf(cur) : -1;
        const n = k === 'j' ? Math.min(cards.length - 1, i + 1) : Math.max(0, i < 0 ? 0 : i - 1);
        cards[n].focus({ preventScroll: true });
        cards[n].scrollIntoView({ block: 'nearest', behavior: reduce ? 'auto' : 'smooth' });
        return;
      }
      if (/^[1-8]$/.test(k)) { e.preventDefault(); jump(SECTION_ORDER[Number(k) - 1]); return; }
      if (k === 'u') { e.preventDefault(); void undo(); return; }
      if (!cur || !rootRef.current.contains(cur)) {
        if ('eshabm'.includes(k) && k.length === 1 && cards[0]) {
          cards[0].focus();
          showToast('Pick a card first. j and k move between cards.');
        }
        return;
      }
      const item = byKey.get(cur.dataset.key ?? '');
      if (!item) return;
      const h = handles.current.get(item.item_key);
      const a = item.actions;
      if (k === 'e') {
        e.preventDefault();
        if (a.includes('done')) act(item, 'done');
        else if (a.includes('read')) act(item, 'read');
        else if (a.includes('answered')) act(item, 'answered');
        else showToast(`This card has no Done. ${item.kind === 'ticket' ? 'Resolve the ticket from its page.' : 'Open it to act.'}`);
      } else if (k === 's') {
        e.preventDefault();
        const btn = cur.querySelector<HTMLElement>('.actions [aria-haspopup="menu"]');
        if (btn) setMenu({ item, anchor: btn });
        else showToast('This card can’t be snoozed.');
      } else if (k === 'h') {
        e.preventDefault();
        if (a.includes('hide')) act(item, 'hide');
        else if (a.includes('dismiss')) act(item, 'dismiss');
        else showToast('This card can’t be hidden. Snooze it instead.');
      } else if (k === 'm') {
        e.preventDefault();
        h?.toggleMore();
      } else if (k === 'b') {
        e.preventDefault();
        if (!h?.toggleContext()) showToast('Before you start is on assigned ticket cards.');
      } else if (k === 'a') {
        e.preventDefault();
        h?.main();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [act, byKey, jump, kbdOpen, menu, reduce, showToast, undo]);

  // ── Render ──

  const name = firstName(me.data);
  const updatedAt = feedQ.dataUpdatedAt ? new Date(feedQ.dataUpdatedAt) : now;
  const errors = feed?.sections ?? {};
  const failed = (g: AttentionGroup) => SECTION_SOURCES[g].filter((s) => errors[s]?.error);
  const clearedKeys = useMemo(() => new Set(Object.keys(gone)), [gone]);
  const coming = live.filter((i) => i.group === 'coming').slice(0, 3);

  const captions = {
    radar: (() => {
      const n = groupCount('radar');
      if (!n) return 'All clear';
      const harm = live.filter((i) => i.group === 'radar' && i.kind === 'loop_harmful_findings').length;
      return harm ? `${harm} harmful finding${harm === 1 ? '' : 's'}` : 'Overdue or blocking';
    })(),
    waiting: (() => {
      const n = groupCount('waiting');
      if (!n) return 'All clear';
      const today = live.filter((i) => i.group === 'waiting' && /due today/.test(i.why)).length;
      return today ? `${today} due today` : 'Nothing due today';
    })(),
    asking: (() => {
      const asks = live.filter((i) => i.group === 'asking');
      if (!groupCount('asking')) return 'All clear';
      const oldest = asks.map((i) => i.occurred_at).filter(Boolean).sort()[0];
      return oldest ? `Oldest: ${whenLabel(oldest)}` : 'Waiting for you';
    })(),
    coming: (() => {
      const next = live.find((i) => i.group === 'coming');
      return next ? `Next: ${whenLabel(next.due_at || next.occurred_at)}` : 'Nothing due';
    })(),
  };
  const harmOnRadar = live.some((i) => i.group === 'radar' && i.kind === 'loop_harmful_findings');
  const total = cleared.length + actionable;
  const ringOffset = (169.65 * (1 - (total ? cleared.length / total : 1))).toFixed(2);

  let gIdx = 0;

  return (
    <div ref={rootRef} className={`fy${ready ? ' ready' : ''}${settled ? ' settled' : ''}${reduce ? ' reduce' : ''}`}>
      {feed && feed.chyron.length > 0 && (
        <Chyron entries={feed.chyron} cleared={clearedKeys} reduce={reduce} onPick={goto} />
      )}
      <div className="topbar">
        <h1>
          <Term id="for-you">For You</Term>
        </h1>
        <span className="crumb">Home</span>
        <div className="grow" />
        <Tooltip content={'Keyboard shortcuts (?)'} describe={false} side="bottom">
          <button className="iconbtn hide-sm" type="button" aria-label="Keyboard shortcuts" onClick={() => setKbdOpen(true)}>
            <Icon name="keyboard" />
          </button>
        </Tooltip>
      </div>

      {feed && (
        <nav className="secbar" aria-label="Jump to section">
          {GROUPS.map((g) => (
            <button
              key={g.id}
              type="button"
              className={g.id === 'radar' && harmOnRadar ? 'harm' : undefined}
              style={{ ['--acc' as string]: g.acc }}
              onClick={() => jump(g.id)}
            >
              <i aria-hidden="true" />{g.short}<b>{groupCount(g.id)}</b>
            </button>
          ))}
          <button type="button" style={{ ['--acc' as string]: 'var(--brand)' }} onClick={() => jump(TOGETHER_ID)}>
            <i aria-hidden="true" />Others<b>{feed.collaboration?.waiting ?? 0}</b>
          </button>
        </nav>
      )}

      <div className="page">
        <div className="feedcol" id="feed">
          <section className="summary" aria-label="Summary">
            <div className="hello">
              <h2>{greeting(now)}, <span>{name}</span></h2>
              <div className="dateline">
                <span>{dayLabel(now)}</span>
                <span aria-hidden="true">·</span>
                <span className="live"><i aria-hidden="true" key={`dot-${ticked}`} className={ticked ? 'tick' : undefined} /><span key={`t-${ticked}`} className={ticked ? 'tick' : undefined}>Updated {hhmm(updatedAt)}</span></span>
              </div>
            </div>
            {feed ? (
              <>
                <div className="tiles">
                  <div className="tile ringtile" style={{ ['--i' as string]: '0' }}>
                    <svg viewBox="0 0 64 64" aria-hidden="true">
                      <circle className="trk" cx="32" cy="32" r="27" />
                      <circle className="fg" cx="32" cy="32" r="27" style={{ strokeDashoffset: ready ? ringOffset : '169.65' }} />
                    </svg>
                    <div className="rt">
                      <span className="tl">Cleared today</span>
                      <span className="tn"><CountUp value={cleared.length} reduce={reduce} /></span>
                      <span className="tc">of {total} on this page today</span>
                    </div>
                  </div>
                  {(['radar', 'waiting', 'asking', 'coming'] as const).map((g, i) => {
                    const meta = GROUPS.find((x) => x.id === g)!;
                    return (
                      <button
                        key={g}
                        className={`tile${g === 'radar' && harmOnRadar ? ' harm' : ''}`}
                        type="button"
                        style={{ ['--i' as string]: String(i + 1), ['--acc' as string]: meta.acc }}
                        onClick={() => jump(g)}
                      >
                        <span className="tl">{meta.title}</span>
                        <span className="tn"><CountUp value={groupCount(g)} reduce={reduce} /></span>
                        <span className="tc">{captions[g]}</span>
                      </button>
                    );
                  })}
                </div>
                <div className={`focusline${topItem && topItem.kind === 'loop_harmful_findings' ? ' is-harm' : ''}${!topItem ? ' is-clear' : ''}`}>
                  <span className="lbl">{topItem ? 'Start with this' : 'All clear'}</span>
                  <p>
                    {topItem
                      ? `${topItem.title}. ${topItem.why.split(' · ').slice(0, 2).join(', ')}.`
                      : (() => {
                          const later = groupCount('coming') + groupCount('second') + groupCount('meetings');
                          return later
                            ? `Nothing needs you right now. ${later} item${later === 1 ? '' : 's'} coming up, worth a second look, or suggested from your meetings.`
                            : 'Nothing needs you, and nothing is coming up.';
                        })()}
                  </p>
                  {topItem && <button className="btn" type="button" onClick={() => goto(topItem.item_key)}>Go to it</button>}
                </div>
              </>
            ) : feedQ.isError ? null : (
              <div aria-busy="true">
                <span className="sr-only" role="status">Loading what needs you</span>
                <div className="tiles">{[0, 1, 2, 3, 4].map((i) => <div key={i} className="skcard" style={{ height: 82 }} />)}</div>
              </div>
            )}
          </section>

          {feedQ.isError && !feed && (
            <div className="caught" role="alert">
              <h2>Couldn’t load For You.</h2>
              <p className="lead">{errorMessage(feedQ.error)}</p>
              <div className="row">
                <button className="btn primary" type="button" onClick={() => void feedQ.refetch()}>Try again</button>
                <button className="btn ghost" type="button" onClick={() => navigate('/sessions')}>Open Sessions</button>
              </div>
            </div>
          )}

          {!feed && !feedQ.isError && (
            <div aria-hidden="true">{[0, 1, 2].map((i) => <div key={i} className="skcard" />)}</div>
          )}

          {feed && caughtUp && (
            <CaughtUp
              ref={caughtRef}
              cleared={cleared}
              next={coming}
              play={caughtPlay && !reduce}
              onLately={() => jump('lately')}
            />
          )}

          {feed && GROUPS.map((g) => {
            if (caughtUp && ACTIONABLE.includes(g.id)) return null;
            const list = visible.filter((i) => i.group === g.id);
            const count = groupCount(g.id);
            const broken = failed(g.id);
            const open = expanded.has(g.id);
            const shown = list.filter((i) => shownKeys.has(i.item_key));
            const liveCount = list.filter((i) => !gone[i.item_key]).length;
            const shownLive = shown.filter((i) => !gone[i.item_key]).length;
            const hiddenCount = liveCount - shownLive;
            const caption = sectionCaption(shownLive, liveCount, Math.max(count, liveCount));
            const toggle = hiddenCount > 0 || (open && liveCount > SECTION_CAP);
            return (
              <section key={g.id} className="band" data-band={g.id} id={`band-${g.id}`} aria-labelledby={`h-${g.id}`} style={{ ['--acc' as string]: g.acc }}>
                <div className="bandhead">
                  <h2 id={`h-${g.id}`} tabIndex={-1}>{g.title}</h2>
                  <span className="cnt"><CountUp value={count} reduce={reduce} /> {g.noun}</span>
                  <p className="sub">{g.sub}</p>
                </div>
                {broken.length > 0 && (
                  <div className="banderr" role="status">
                    <span>Part of {g.title} couldn’t load ({broken.join(', ').replace(/_/g, ' ')}).</span>
                    <button className="btn sm" type="button" onClick={() => void feedQ.refetch()}>Retry</button>
                  </div>
                )}
                <div className={`list${g.grid ? ' grid2' : ''}`} id={`list-${g.id}`}>
                  {shown.map((it) => {
                    const st = gone[it.item_key];
                    return (
                      <ItemCard
                        key={it.item_key}
                        item={it}
                        index={gIdx++}
                        top={!!topItem && topItem.item_key === it.item_key}
                        stamp={st?.stamp ?? null}
                        leaving={st?.leaving ?? ''}
                        flash={flashKey === it.item_key}
                        reduce={reduce}
                        api={api}
                        register={register}
                      />
                    );
                  })}
                </div>
                {(toggle || (caption && shownLive > 0)) && (
                  <div className="bandmore">
                    {toggle && (
                      <button
                        ref={(el) => {
                          if (el) moreRefs.current.set(g.id, el);
                          else moreRefs.current.delete(g.id);
                        }}
                        className="btn"
                        type="button"
                        aria-expanded={open}
                        aria-controls={`list-${g.id}`}
                        onClick={() => toggleSection(g.id)}
                      >
                        {open ? 'Show less' : `Show ${hiddenCount} more`}
                        <Icon name="chev" />
                      </button>
                    )}
                    {caption && <span className="of">{caption}</span>}
                  </div>
                )}
                {count === 0 && broken.length === 0 && <p className="bandempty">{g.empty}</p>}
              </section>
            );
          })}

          {feed && feed.collaboration && (
            <CollabBand
              projects={feed.collaboration.projects}
              waiting={feed.collaboration.waiting}
              error={feed.collaboration.error}
              reduce={reduce}
              toast={(m) => { showToast(m); say(m); }}
              onRetry={() => void feedQ.refetch()}
            />
          )}
        </div>

        <aside className="rail" aria-label="Side panel">
          <div className="rcard" style={{ ['--i' as string]: '0' }}>
            <h3>Sections</h3>
            <ul className="secnav">
              {GROUPS.map((g, i) => (
                <li key={g.id}>
                  <button type="button" onClick={() => jump(g.id)} style={{ ['--acc' as string]: g.acc }}>
                    <i aria-hidden="true" />{g.title}<b><span className="sr-only">: </span>{feed ? groupCount(g.id) : '–'}</b>
                    <kbd aria-hidden="true">{i + 1}</kbd>
                  </button>
                </li>
              ))}
              <li>
                <button type="button" onClick={() => jump(TOGETHER_ID)} style={{ ['--acc' as string]: 'var(--brand)' }}>
                  <i aria-hidden="true" />Working with others<b><span className="sr-only">: </span>{feed?.collaboration?.waiting ?? '–'}</b>
                  <kbd aria-hidden="true">8</kbd>
                </button>
              </li>
            </ul>
          </div>
          <div className="rcard kbdcard" style={{ ['--i' as string]: '1' }}>
            <h3>Keyboard</h3>
            <div className="keys">
              <span><kbd>j</kbd><kbd>k</kbd></span><span>Next and previous card</span>
              <span><kbd>e</kbd></span><span>Done</span>
              <span><kbd>s</kbd></span><span>Snooze</span>
              <span><kbd>h</kbd></span><span>Hide until it changes</span>
              <span><kbd>m</kbd></span><span>More context</span>
              <span><kbd>↵</kbd></span><span>Open</span>
              <span><kbd>?</kbd></span><span>All shortcuts</span>
            </div>
          </div>
          <div className="rcard" style={{ ['--i' as string]: '2' }}>
            <h3>Home</h3>
            <p>For You is your home page. Prefer the session list? Sessions is always one click away in the sidebar.</p>
            <Switch
              checked={sessionsHome}
              onChange={(v: boolean) => {
                setSessionsHome(v);
                setSessionsHomeState(v);
                showToast(v ? 'Sessions is now your home page.' : 'For You is your home page.');
              }}
              label="Open Sessions as my home page"
            />
          </div>
        </aside>
      </div>

      {menu && (
        <SnoozeMenu
          anchor={menu.anchor}
          title={menu.item.actions.includes('remind') ? 'Remind me' : 'Snooze until'}
          onPick={(preset) => {
            const it = menu.item;
            setMenu(null);
            snooze(it, preset);
          }}
          onClose={(restoreFocus) => {
            const anchor = menu.anchor;
            setMenu(null);
            if (restoreFocus) anchor.focus();
          }}
        />
      )}

      <Dialog open={kbdOpen} onClose={() => setKbdOpen(false)} titleId="fy-kbd-title">
        <h2 id="fy-kbd-title" className="text-lg font-semibold text-text-primary mb-1">Keyboard shortcuts</h2>
        <p className="text-sm text-text-tertiary mb-4">For You is built to be cleared from the keyboard.</p>
        <dl className="fy-keys-dl">
          <dt><kbd className="fy-kbd">j</kbd><kbd className="fy-kbd">k</kbd></dt><dd>Move to the next or previous card</dd>
          <dt><kbd className="fy-kbd">1</kbd>–<kbd className="fy-kbd">8</kbd></dt><dd>Jump to On your radar, Waiting on you, Your team is asking, From your meetings and chats, Coming up, Worth a second look, What changed this week, Working with others</dd>
          <dt><kbd className="fy-kbd">e</kbd></dt><dd>Done, where the source supports it</dd>
          <dt><kbd className="fy-kbd">s</kbd></dt><dd>Snooze for 1 hour, until tomorrow or next week</dd>
          <dt><kbd className="fy-kbd">h</kbd></dt><dd>Hide until it changes</dd>
          <dt><kbd className="fy-kbd">m</kbd></dt><dd>More context: details, related items and why it ranks here</dd>
          <dt><kbd className="fy-kbd">b</kbd></dt><dd>Before you start: related knowledge and sessions</dd>
          <dt><kbd className="fy-kbd">a</kbd></dt><dd>The card’s main action (acknowledge, reply, open)</dd>
          <dt><kbd className="fy-kbd">↵</kbd></dt><dd>Open the record</dd>
          <dt><kbd className="fy-kbd">u</kbd></dt><dd>Undo the last action</dd>
          <dt><kbd className="fy-kbd">?</kbd></dt><dd>Show this list</dd>
        </dl>
        <div className="flex justify-end mt-5">
          <Button type="button" onClick={() => setKbdOpen(false)}>Close</Button>
        </div>
      </Dialog>

      <div className="fy-toasts" aria-live="polite" aria-atomic="true">
        {toast && (
          <div
            className="fy-toast"
            key={toast.id}
            onMouseEnter={() => { toastHeld.current = true; }}
            onMouseLeave={() => { toastHeld.current = false; }}
            onFocus={() => { toastHeld.current = true; }}
            onBlur={() => { toastHeld.current = false; }}
          >
            <p>{toast.msg}</p>
            {toast.undo && <button className="btn sm" type="button" onClick={() => void undo()}>Undo <kbd>u</kbd></button>}
            <Tooltip content={'Dismiss'} describe={false}>
              <button className="iconbtn" type="button" aria-label="Dismiss" style={{ width: 28, height: 28 }} onClick={() => setToast(null)}>
                <Icon name="x" />
              </button>
            </Tooltip>
          </div>
        )}
      </div>
      <div className="sr-only" aria-live="polite">{announce}</div>
    </div>
  );
}
