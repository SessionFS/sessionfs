import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';

interface Tab {
  key: string;
  label: ReactNode;
  content?: ReactNode;
}

interface TabsProps {
  tabs: Tab[];
  activeKey: string;
  onChange: (key: string) => void;
  /** Render only the tab bar, no content panel. Use when content lives in a
   *  separate scrollable container (e.g. SessionDetail's flex-1 overflow). */
  bare?: boolean;
  /** Prefix for the tab and panel ids. Defaults to a generated one; pass an
   *  explicit value when a `TabPanel` rendered elsewhere has to point at the
   *  same tabs (bare mode). Keep it distinct per tablist on a page. */
  idPrefix?: string;
  /** Accessible name for the tablist. */
  'aria-label'?: string;
}

// Keys can hold characters that are awkward in an id.
const slug = (key: string) => key.replace(/[^A-Za-z0-9_-]/g, '_');
const tabDomId = (prefix: string, key: string) => `${prefix}-tab-${slug(key)}`;
const panelDomId = (prefix: string, key: string) => `${prefix}-panel-${slug(key)}`;

interface TabPanelProps {
  /** Same value as the `idPrefix` of the `Tabs` this panel belongs to. */
  idPrefix: string;
  /** Key of the tab this panel is the content of. */
  tabKey: string;
  className?: string;
  children?: ReactNode;
}

/** Content area for a tab, wired to it with matching ids. Tabs renders one
 *  itself unless `bare`; bare users wrap the content they show in this. */
export function TabPanel({ idPrefix, tabKey, className, children }: TabPanelProps) {
  return (
    <div
      role="tabpanel"
      id={panelDomId(idPrefix, tabKey)}
      aria-labelledby={tabDomId(idPrefix, tabKey)}
      tabIndex={0}
      className={className}
    >
      {children}
    </div>
  );
}

// Width of the fade on an edge that has more tabs past it.
const FADE_PX = 24;

/**
 * Pill-style tabs matching the Phase 1b nav pattern:
 * active tab = --surface pill + hairline border, inactive = transparent.
 *
 * The bar never wraps and never spills past its container (GitHub #86/#87):
 * tabs keep their single-line width, the bar scrolls horizontally when they
 * don't all fit, the overflowing edge fades out, and the active tab is
 * scrolled into view. The fade is a mask rather than a gradient overlay so it
 * works on any background the bar sits on.
 */
export function Tabs({
  tabs,
  activeKey,
  onChange,
  bare = false,
  idPrefix,
  'aria-label': ariaLabel,
}: TabsProps) {
  const generatedId = useId();
  const prefix = idPrefix ?? generatedId;
  const listRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setOverflow((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    measure();
    // A plain vertical wheel doesn't scroll an overflow-x box, and the
    // scrollbar is hidden, so turn it into horizontal scroll while the bar
    // can still move that way. At either end the page scrolls as usual.
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
      const max = el.scrollWidth - el.clientWidth;
      if (max <= 1) return;
      if ((e.deltaY < 0 && el.scrollLeft <= 0) || (e.deltaY > 0 && el.scrollLeft >= max - 1)) return;
      e.preventDefault();
      el.scrollLeft += e.deltaY;
      measure();
    };
    el.addEventListener('scroll', measure, { passive: true });
    el.addEventListener('wheel', onWheel, { passive: false });
    window.addEventListener('resize', measure);
    return () => {
      el.removeEventListener('scroll', measure);
      el.removeEventListener('wheel', onWheel);
      window.removeEventListener('resize', measure);
    };
  }, [measure]);

  // Watch each tab as well as the bar: a label can widen without the bar's
  // own box changing (a count badge arriving, the web font swapping in).
  useEffect(() => {
    const el = listRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    for (const child of Array.from(el.children)) observer.observe(child);
    return () => observer.disconnect();
  }, [measure, tabs]);

  // Scroll the bar just far enough to show a tab with its fade margin. Only
  // the bar scrolls (setting scrollLeft), never the page, which scrollIntoView
  // could do.
  const reveal = useCallback(
    (tab: HTMLElement | null | undefined) => {
      const el = listRef.current;
      if (!el || !tab) return;
      // The list is `relative`, so offsetLeft is measured from its content edge.
      const start = tab.offsetLeft;
      const end = start + tab.offsetWidth;
      if (start - FADE_PX < el.scrollLeft) {
        el.scrollLeft = Math.max(0, start - FADE_PX);
      } else if (end + FADE_PX > el.scrollLeft + el.clientWidth) {
        el.scrollLeft = end + FADE_PX - el.clientWidth;
      }
      measure();
    },
    [measure],
  );

  // Keep the active tab visible.
  useLayoutEffect(() => {
    reveal(listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]'));
  }, [activeKey, tabs.length, reveal]);

  // Arrow keys / Home / End move focus and select (automatic activation, same
  // as a click). Only the selected tab is in the tab order.
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.altKey || e.ctrlKey || e.metaKey || tabs.length === 0) return;
    const buttons = Array.from(
      e.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]'),
    );
    const current = buttons.indexOf(e.target as HTMLElement);
    if (current < 0) return;
    let next: number;
    switch (e.key) {
      case 'ArrowRight':
        next = (current + 1) % buttons.length;
        break;
      case 'ArrowLeft':
        next = (current - 1 + buttons.length) % buttons.length;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = buttons.length - 1;
        break;
      default:
        return;
    }
    e.preventDefault();
    const target = buttons[next];
    target.focus({ preventScroll: true });
    reveal(target);
    onChange(tabs[next].key);
  };

  const mask =
    overflow.left || overflow.right
      ? `linear-gradient(to right, ${overflow.left ? 'transparent' : 'black'} 0, black ${overflow.left ? FADE_PX : 0}px, black calc(100% - ${overflow.right ? FADE_PX : 0}px), ${overflow.right ? 'transparent' : 'black'} 100%)`
      : undefined;

  return (
    <div className="min-w-0">
      <div className="border-b border-border">
        <div
          ref={listRef}
          className="relative flex gap-1 overflow-x-auto overflow-y-hidden pb-px [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          role="tablist"
          aria-label={ariaLabel}
          onKeyDown={onKeyDown}
          data-overflow-left={overflow.left || undefined}
          data-overflow-right={overflow.right || undefined}
          style={mask ? { maskImage: mask, WebkitMaskImage: mask } : undefined}
        >
          {tabs.map((tab) => {
            const active = tab.key === activeKey;
            return (
              <button
                key={tab.key}
                role="tab"
                id={tabDomId(prefix, tab.key)}
                aria-selected={active}
                // Only the selected tab's panel is in the DOM, so only it is
                // referenced; an id pointing at nothing is flagged by checkers.
                aria-controls={active ? panelDomId(prefix, tab.key) : undefined}
                tabIndex={active ? 0 : -1}
                onClick={() => onChange(tab.key)}
                className={`shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm font-medium transition-colors cursor-pointer outline-none focus-visible:shadow-[inset_0_0_0_2px_var(--brand-glow)] ${
                  active
                    ? 'bg-surface border border-border text-text-primary'
                    : 'border border-transparent text-text-secondary hover:text-text-primary hover:bg-surface-hover'
                }`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      </div>
      {!bare && (
        <TabPanel idPrefix={prefix} tabKey={activeKey} className="pt-4">
          {tabs.find((t) => t.key === activeKey)?.content}
        </TabPanel>
      )}
    </div>
  );
}
