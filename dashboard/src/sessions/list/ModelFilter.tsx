import { useEffect, useId, useRef, useState, type FocusEvent } from 'react';
import type { SessionModelCount } from '../../api/client';
import { friendlyModelName, isUnknownModelId } from '../../utils/models';
import { Badge, Checkbox } from '../../components/ui';

/** The `?model=` value the dashboard sends for the aggregated unknown bucket
 *  (sessions with a NULL, empty, literal "unknown" or `<synthetic>` model id). */
export const UNKNOWN_MODEL = 'unknown';

const baseField =
  'w-full bg-bg-sunken border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary transition-[background-color,border-color,box-shadow] duration-150 ease-out outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 disabled:cursor-not-allowed';

interface ModelFilterProps {
  models: SessionModelCount[];
  selected: string[];
  onChange: (models: string[]) => void;
  loading?: boolean;
  /** The models list failed to load; selections stay visible and clearable. */
  error?: boolean;
}

interface ModelOption {
  value: string;
  label: string;
  raw: string;
  count: number;
}

function optionFor(value: string, count: number): ModelOption {
  if (value === UNKNOWN_MODEL || isUnknownModelId(value)) {
    return { value, label: 'Unknown model', raw: 'Unknown model', count };
  }
  return { value, label: friendlyModelName(value), raw: value, count };
}

/** The `/models` list plus every selected value it doesn't contain (count 0),
 *  so a stale selection — another Tool filter, a model whose sessions were
 *  deleted, a shared link — can always be seen and unticked. */
function buildModelOptions(models: SessionModelCount[], selected: string[]): ModelOption[] {
  const options = models.map((m) => optionFor(m.model_id ?? UNKNOWN_MODEL, m.count));
  const present = new Set(options.map((o) => o.value));
  for (const value of selected) {
    if (!present.has(value)) {
      options.push(optionFor(value, 0));
      present.add(value);
    }
  }
  return options;
}

/** Multi-select model filter. Populated from `GET /sessions/models`; each
 *  option shows a friendly label plus a count badge, and the "unknown" bucket
 *  (null model id) is offered as "Unknown model". Filtering uses the raw id
 *  (or the `unknown` sentinel); the raw id is the option's title.
 *
 *  The trigger discloses a non-modal dialog holding a checkbox group
 *  (`aria-haspopup="dialog"` + `aria-controls`), not a menu: the checkboxes
 *  toggle without closing it. Escape closes it and returns focus to the
 *  trigger. */
export function ModelFilter({ models, selected, onChange, loading = false, error = false }: ModelFilterProps) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  function close({ focusTrigger }: { focusTrigger: boolean }) {
    setOpen(false);
    if (focusTrigger) triggerRef.current?.focus();
  }

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [open]);

  // Opening moves focus into the dialog (first control) so keyboard users
  // land on the checkboxes rather than having to tab past the trigger.
  useEffect(() => {
    if (!open) return;
    const first = panelRef.current?.querySelector<HTMLElement>('input, button');
    first?.focus();
  }, [open]);

  function handleBlur(e: FocusEvent<HTMLDivElement>) {
    // Tabbing to a control outside the filter closes it. A null
    // relatedTarget (a click on the panel's padding) keeps it open.
    const next = e.relatedTarget as Node | null;
    if (open && next && wrapperRef.current && !wrapperRef.current.contains(next)) {
      setOpen(false);
    }
  }

  function toggle(value: string) {
    const next = selected.includes(value)
      ? selected.filter((v) => v !== value)
      : [...selected, value];
    onChange(next);
  }

  const options = buildModelOptions(models, selected);

  const triggerLabel =
    selected.length === 0
      ? 'All Models'
      : selected.length === 1
        ? optionFor(selected[0], 0).label
        : `${selected.length} models`;

  return (
    <div ref={wrapperRef} className="relative" onBlur={handleBlur}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label="Filter by model"
        onClick={() => setOpen((v) => !v)}
        className={`${baseField} flex items-center justify-between gap-2 text-left ${selected.length === 0 ? 'text-text-tertiary' : ''}`}
      >
        <span className="truncate">{triggerLabel}</span>
        <svg
          className={`w-3.5 h-3.5 text-text-tertiary shrink-0 transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      <div
        ref={panelRef}
        id={panelId}
        role="dialog"
        aria-label="Filter by model"
        hidden={!open}
        className="absolute top-full left-0 mt-1 z-40 w-max min-w-[240px] max-w-[min(20rem,calc(100vw-2rem))] max-h-72 overflow-y-auto rounded-lg py-1 border border-border bg-overlay"
        style={{ boxShadow: 'var(--shadow-overlay)' }}
      >
        <div role="group" aria-label="Models">
          {options.map((o) => (
            <Checkbox
              key={o.value}
              checked={selected.includes(o.value)}
              onChange={() => toggle(o.value)}
              className="px-3 py-2 mx-1 rounded-sm hover:bg-surface-hover cursor-pointer [&>label]:flex-1 [&>label]:min-w-0"
              label={
                <span className="flex items-center gap-2 min-w-0">
                  <span className="flex-1 truncate" title={o.raw}>{o.label}</span>
                  {o.raw !== o.label && <span className="sr-only">({o.raw})</span>}
                  <Badge label={String(o.count)} size="sm" />
                </span>
              }
            />
          ))}
          {loading && options.length === 0 && (
            <div className="px-3 py-2 text-sm text-text-tertiary">Loading models…</div>
          )}
          {error && !loading && (
            <div className="px-3 py-2 text-sm text-danger">Couldn&rsquo;t load models</div>
          )}
          {!loading && !error && options.length === 0 && (
            <div className="px-3 py-2 text-sm text-text-tertiary">No models yet</div>
          )}
        </div>
        {selected.length > 0 && (
          <div className="border-t border-border mt-1 pt-1 px-1">
            <button
              type="button"
              onClick={() => {
                onChange([]);
                close({ focusTrigger: true });
              }}
              className="w-full text-left px-3 py-2 rounded-sm text-sm text-text-secondary hover:bg-surface-hover hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              Clear models
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
