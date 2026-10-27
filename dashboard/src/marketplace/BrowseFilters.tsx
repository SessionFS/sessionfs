import { useCallback, useEffect, useId, useRef, useState, type FocusEvent } from 'react';
import { createPortal } from 'react-dom';
import type { MarketplaceCategory } from '../api/client';
import { Button, Checkbox, Input, Select } from '../components/ui';
import { useFocusTrap } from '../hooks/useFocusTrap';
import type { MarketplaceFilterState } from '../hooks/useMarketplace';
import {
  CAPABILITY_OPTIONS,
  CLEARED_PANEL_FILTERS,
  SEVERITY_OPTIONS,
  SORT_OPTIONS,
  TOOL_OPTIONS,
  UPDATED_OPTIONS,
  WORKS_IN_OPTIONS,
  activeFilterChips,
} from './browseFilterOptions';
import { CloseIcon, FilterIcon, SearchIcon } from './icons';

const NARROW_QUERY = '(max-width: 639px)';

function useNarrow() {
  const [narrow, setNarrow] = useState(() => typeof window !== 'undefined' && window.matchMedia?.(NARROW_QUERY).matches === true);
  useEffect(() => {
    const mq = window.matchMedia?.(NARROW_QUERY);
    if (!mq) return;
    const onChange = () => setNarrow(mq.matches);
    mq.addEventListener?.('change', onChange);
    return () => mq.removeEventListener?.('change', onChange);
  }, []);
  return narrow;
}

function FilterFields({ filters, onChange }: { filters: MarketplaceFilterState; onChange: (next: MarketplaceFilterState) => void }) {
  const applied = filters.license || '';
  const [draft, setDraft] = useState({ applied, value: applied });
  // Keep the text box in step when the chip row clears or changes the license.
  if (draft.applied !== applied) setDraft({ applied, value: applied });
  const license = draft.applied === applied ? draft.value : applied;
  const setLicense = (value: string) => setDraft({ applied, value });
  const set = (patch: Partial<MarketplaceFilterState>) => onChange({ ...filters, ...patch });
  const toggle = (key: 'verified' | 'attested') => set({ [key]: filters[key] ? undefined : true });
  const commitLicense = () => {
    const next = license.trim() || undefined;
    if (next !== filters.license) set({ license: next });
  };
  return (
    <div className="space-y-4">
      <Select
        title="Scanner findings"
        value={filters.max_severity || ''}
        onValueChange={(value) => set({ max_severity: (value || undefined) as MarketplaceFilterState['max_severity'] })}
        options={SEVERITY_OPTIONS}
      />
      <Select
        title="Includes"
        value={filters.capability || ''}
        onValueChange={(value) => set({ capability: value || undefined })}
        options={CAPABILITY_OPTIONS}
      />
      <div className="grid grid-cols-2 gap-3">
        <Select
          title="Works in"
          value={filters.works_in || ''}
          onValueChange={(value) => set({ works_in: value || undefined })}
          options={WORKS_IN_OPTIONS}
        />
        <Select
          title="Last updated"
          value={filters.updated_within ? String(filters.updated_within) : ''}
          onValueChange={(value) => set({ updated_within: value ? Number(value) : undefined })}
          options={UPDATED_OPTIONS}
        />
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          commitLicense();
        }}
      >
        <Input title="License" placeholder="e.g. MIT" value={license} onChange={(event) => setLicense(event.target.value)} onBlur={commitLicense} maxLength={100} />
      </form>
      <fieldset className="space-y-2.5 border-t border-border pt-4">
        <legend className="sr-only">Trust</legend>
        <Checkbox label="Verified publishers only" checked={!!filters.verified} onChange={() => toggle('verified')} />
        <Checkbox
          label="Published from CI only"
          description="Released by a trusted GitHub or GitLab workflow"
          checked={!!filters.attested}
          onChange={() => toggle('attested')}
        />
        <Checkbox
          label="Hide deprecated packages"
          checked={filters.include_deprecated === false}
          onChange={() => set({ include_deprecated: filters.include_deprecated === false ? undefined : false })}
        />
      </fieldset>
    </div>
  );
}

/** "Filters" button with an active count. Opens a popover on wide screens
 *  and a bottom sheet on phones (modal there, with a focus trap). */
export function FiltersMenu({ filters, onChange }: { filters: MarketplaceFilterState; onChange: (next: MarketplaceFilterState) => void }) {
  const [open, setOpen] = useState(false);
  const narrow = useNarrow();
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const titleId = useId();
  const count = activeFilterChips(filters).length;
  // Phone sheet is modal: the page behind it can't scroll or take focus.
  // Declared before the focus trap so its cleanup runs first and the trap can
  // hand focus back to the trigger.
  useEffect(() => {
    if (!open || !narrow) return;
    const root = document.getElementById('root');
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    root?.setAttribute('inert', '');
    return () => {
      document.body.style.overflow = previousOverflow;
      root?.removeAttribute('inert');
    };
  }, [open, narrow]);
  useFocusTrap(open && narrow ? panelRef : { current: null });

  const close = useCallback((focusTrigger: boolean) => {
    setOpen(false);
    if (focusTrigger) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    function onMouseDown(event: MouseEvent) {
      const target = event.target as Node;
      if (wrapperRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      // An open Select inside claims Escape first (window capture phase).
      if (event.key === 'Escape' && !event.defaultPrevented) {
        event.preventDefault();
        close(true);
      }
    }
    document.addEventListener('mousedown', onMouseDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onMouseDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, close]);

  useEffect(() => {
    if (!open || narrow) return;
    panelRef.current?.querySelector<HTMLElement>('button, input')?.focus();
  }, [open, narrow]);

  function onBlur(event: FocusEvent<HTMLDivElement>) {
    const next = event.relatedTarget as Node | null;
    if (open && !narrow && next && wrapperRef.current && !wrapperRef.current.contains(next)) setOpen(false);
  }

  const sheet = (
    <>
      {open && narrow && (
        <div className="fixed inset-0 z-40 bg-[var(--scrim)]" aria-hidden="true" onClick={() => close(true)} />
      )}
      <div
        ref={panelRef}
        id={panelId}
        role="dialog"
        aria-labelledby={titleId}
        aria-modal={narrow ? true : undefined}
        hidden={!open}
        className="fixed inset-x-0 bottom-0 z-50 max-h-[85vh] overflow-y-auto rounded-t-xl border border-border bg-overlay p-5 sm:absolute sm:inset-x-auto sm:bottom-auto sm:right-0 sm:top-full sm:mt-2 sm:max-h-none sm:w-[22rem] sm:overflow-visible sm:rounded-xl"
        style={{ boxShadow: 'var(--shadow-overlay)' }}
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 id={titleId} className="text-base font-semibold text-text-primary">Filters</h2>
          {count > 0 && (
            <button
              type="button"
              onClick={() => onChange({ ...filters, ...CLEARED_PANEL_FILTERS })}
              className="rounded text-xs font-medium text-text-secondary hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              Reset
            </button>
          )}
        </div>
        <FilterFields filters={filters} onChange={onChange} />
        <div className="mt-5 flex justify-end">
          <Button size="sm" onClick={() => close(true)}>Done</Button>
        </div>
      </div>
    </>
  );

  return (
    <div ref={wrapperRef} className="relative" onBlur={onBlur}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
        className={`inline-flex h-full w-full items-center justify-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
          count > 0
            ? 'border-brand/50 bg-surface text-text-primary'
            : 'border-border bg-bg-sunken text-text-secondary hover:border-border-strong hover:text-text-primary'
        }`}
      >
        <FilterIcon size={15} />
        <span>Filters</span>
        {count > 0 && (
          <span className="rounded-full bg-brand px-1.5 text-2xs font-semibold leading-4 text-text-inverse">
            {count}
            <span className="sr-only"> active filters</span>
          </span>
        )}
      </button>
      {narrow ? createPortal(sheet, document.body) : sheet}
    </div>
  );
}

/** Active panel filters as removable chips, plus "Clear all". */
export function ActiveFilterChips({ filters, onChange }: { filters: MarketplaceFilterState; onChange: (next: MarketplaceFilterState) => void }) {
  const chips = activeFilterChips(filters);
  if (chips.length === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5" role="group" aria-label="Active filters">
      {chips.map((chip) => (
        <button
          key={chip.key}
          type="button"
          onClick={() => onChange({ ...filters, [chip.key]: undefined })}
          aria-label={`Remove filter: ${chip.label}`}
          className="inline-flex items-center gap-1 rounded-full border border-border bg-surface py-0.5 pl-2.5 pr-1.5 text-xs text-text-secondary transition-colors hover:border-border-strong hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
        >
          {chip.label}
          <CloseIcon size={12} />
        </button>
      ))}
      <button
        type="button"
        onClick={() => onChange({ ...filters, ...CLEARED_PANEL_FILTERS })}
        className="rounded px-1.5 py-0.5 text-xs font-medium text-text-tertiary hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        Clear all
      </button>
    </div>
  );
}

/** One quiet line of category toggles that scrolls sideways instead of wrapping. */
export function CategoryChips({
  categories,
  active,
  onChange,
}: {
  categories: MarketplaceCategory[];
  active: string | undefined;
  onChange: (category: string | undefined) => void;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  const [fade, setFade] = useState({ left: false, right: false });
  const measure = useCallback(() => {
    const el = rowRef.current;
    if (!el) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setFade((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);
  useEffect(() => {
    const el = rowRef.current;
    if (!el) return;
    measure();
    // A vertical wheel scrolls the row sideways while it still can.
    const onWheel = (event: WheelEvent) => {
      if (event.ctrlKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
      const max = el.scrollWidth - el.clientWidth;
      if (max <= 1) return;
      if ((event.deltaY < 0 && el.scrollLeft <= 0) || (event.deltaY > 0 && el.scrollLeft >= max - 1)) return;
      event.preventDefault();
      el.scrollLeft += event.deltaY;
    };
    el.addEventListener('scroll', measure, { passive: true });
    el.addEventListener('wheel', onWheel, { passive: false });
    window.addEventListener('resize', measure);
    return () => {
      el.removeEventListener('scroll', measure);
      el.removeEventListener('wheel', onWheel);
      window.removeEventListener('resize', measure);
    };
  }, [measure, categories.length]);

  if (categories.length === 0) return null;
  const chip = (selected: boolean) =>
    `shrink-0 whitespace-nowrap rounded-full border px-3 py-1 text-xs font-medium transition-colors outline-none focus-visible:shadow-[inset_0_0_0_2px_var(--brand-glow)] ${
      selected
        ? 'border-border-strong bg-surface text-text-primary'
        : 'border-transparent text-text-secondary hover:bg-surface-hover hover:text-text-primary'
    }`;
  const mask =
    fade.left || fade.right
      ? `linear-gradient(to right, ${fade.left ? 'transparent' : 'black'} 0, black ${fade.left ? 24 : 0}px, black calc(100% - ${fade.right ? 24 : 0}px), ${fade.right ? 'transparent' : 'black'} 100%)`
      : undefined;
  return (
    <nav aria-label="Browse by category" className="mt-3 min-w-0">
      <div
        ref={rowRef}
        className="flex gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        style={mask ? { maskImage: mask, WebkitMaskImage: mask } : undefined}
      >
        <button type="button" className={chip(!active)} aria-pressed={!active} onClick={() => onChange(undefined)}>All</button>
        {categories.map((category) => (
          <button
            key={category.slug}
            type="button"
            className={chip(active === category.slug)}
            aria-pressed={active === category.slug}
            onClick={() => onChange(active === category.slug ? undefined : category.slug)}
          >
            {category.display_name}
          </button>
        ))}
      </div>
    </nav>
  );
}

/** Search, tool and sort stay on the page; everything else sits behind Filters. */
export function BrowseToolbar({
  query,
  onQuery,
  tool,
  onTool,
  filters,
  onFilters,
  categories,
}: {
  query: string;
  onQuery: (value: string) => void;
  tool: string;
  onTool: (value: string) => void;
  filters: MarketplaceFilterState;
  onFilters: (next: MarketplaceFilterState) => void;
  categories: MarketplaceCategory[];
}) {
  return (
    <section aria-label="Search and filter packages" className="@container">
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-2 @2xl:grid-cols-[minmax(0,1fr)_10.5rem_9rem_auto]">
        <div className="relative col-span-3 @2xl:col-span-1">
          <SearchIcon size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary" />
          <Input
            type="search"
            aria-label="Search marketplace"
            placeholder="Search packages"
            value={query}
            onChange={(event) => onQuery(event.target.value)}
            className="!pl-9"
          />
        </div>
        <Select aria-label="Filter marketplace by tool" value={tool} onValueChange={onTool} options={TOOL_OPTIONS} />
        <Select
          aria-label="Sort"
          value={filters.sort || 'relevance'}
          onValueChange={(value) => onFilters({ ...filters, sort: value as 'recent' | 'relevance' })}
          options={SORT_OPTIONS}
        />
        <FiltersMenu filters={filters} onChange={onFilters} />
      </div>
      <CategoryChips
        categories={categories}
        active={filters.category}
        onChange={(category) => onFilters({ ...filters, category })}
      />
      <ActiveFilterChips filters={filters} onChange={onFilters} />
    </section>
  );
}
