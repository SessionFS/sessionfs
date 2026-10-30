import { useId, useState, type FormEvent } from 'react';
import { Alert, Badge, Button, Card, Dialog, DialogFooter, DialogHeader, Input } from '../../components/ui';
import type { ChecklistItem, ChecklistStatus, Loop, LoopBudgetPatch, LoopChild } from '../../api/reviewLoop';
import {
  useChecklist,
  useCloseLoopItem,
  usePatchChecklistItem,
  useSeedChecklist,
  useUpdateLoop,
} from '../../hooks/useReviewLoop';
import { errorMessage } from '../../utils/errorMessage';
import { plural } from './format';

const SECTION_TITLE = 'text-md font-semibold text-text-primary';
const QUIET = 'text-sm text-text-tertiary';

/** Blank keeps the cap as it is; anything else must be a whole number of at least 1. */
function parseWhole(raw: string, label: string): { value?: number; error?: string } {
  const t = raw.trim();
  if (t === '') return {};
  if (!/^\d+$/.test(t) || Number(t) < 1) return { error: `${label} must be a whole number of 1 or more.` };
  return { value: Number(t) };
}

/* ── Raise limit ───────────────────────────────────────────────────── */

export function RaiseLimitDialog({
  projectId,
  loop,
  child,
  onClose,
}: {
  projectId: string;
  loop: Loop;
  child: LoopChild;
  onClose: () => void;
}) {
  const titleId = useId();
  const update = useUpdateLoop(projectId);
  const pending = update.isPending;
  // Dollar caps only for callers whose responses carry dollars.
  const showCost = child.cost_usd !== undefined;
  const [tokens, setTokens] = useState('');
  const [rounds, setRounds] = useState('');
  const [cost, setCost] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const close = () => {
    if (!pending) onClose();
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pending) return;
    const t = parseWhole(tokens, 'Token limit');
    const r = parseWhole(rounds, 'Review round limit');
    const body: LoopBudgetPatch = {};
    let err = t.error ?? r.error ?? null;
    if (t.value !== undefined) body.token_budget = t.value;
    if (r.value !== undefined) body.max_review_rounds = r.value;
    if (showCost && cost.trim() !== '') {
      const n = Number(cost.trim());
      if (!Number.isFinite(n) || n <= 0) err = err ?? 'Cost limit must be more than 0.';
      else body.cost_budget_usd = n;
    }
    if (!err && Object.keys(body).length === 0) err = 'Enter at least one new limit.';
    setFieldError(err);
    if (err) return;
    try {
      await update.mutateAsync({ loopId: loop.id, body });
      onClose();
    } catch {
      /* shown below; the dialog stays open so it can be retried */
    }
  }

  return (
    <Dialog open onClose={close} titleId={titleId}>
      <form onSubmit={submit} noValidate>
        <DialogHeader titleId={titleId}>Raise limit</DialogHeader>
        <div className="space-y-4">
          <p className="text-sm text-text-secondary">
            {child.needs_human ?? 'This item reached a limit.'} The limit belongs to the whole loop, so a new value
            applies to every item in {loop.title}. Leave a field blank to keep it.
          </p>
          <Input
            title="Token limit"
            inputMode="numeric"
            placeholder={child.budget.tokens ? String(child.budget.tokens) : 'No limit'}
            value={tokens}
            onChange={(e) => setTokens(e.target.value)}
            disabled={pending}
          />
          <Input
            title="Review round limit"
            inputMode="numeric"
            placeholder={child.budget.max_rounds ? String(child.budget.max_rounds) : 'No limit'}
            value={rounds}
            onChange={(e) => setRounds(e.target.value)}
            disabled={pending}
          />
          {showCost && (
            <Input
              title="Cost limit (USD)"
              inputMode="decimal"
              placeholder={child.budget.cost_usd !== undefined ? String(child.budget.cost_usd) : 'No limit'}
              value={cost}
              onChange={(e) => setCost(e.target.value)}
              disabled={pending}
            />
          )}
        </div>
        {fieldError && <Alert variant="danger" className="mt-4">{fieldError}</Alert>}
        {update.error && (
          <Alert variant="danger" className="mt-4">{errorMessage(update.error, 'Could not raise the limit')}</Alert>
        )}
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={close} disabled={pending}>Cancel</Button>
          <Button type="submit" loading={pending}>Raise limit</Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

/* ── Close item ────────────────────────────────────────────────────── */

export function CloseItemDialog({
  projectId,
  loop,
  child,
  onClose,
}: {
  projectId: string;
  loop: Loop;
  child: LoopChild;
  onClose: () => void;
}) {
  const titleId = useId();
  const closeItem = useCloseLoopItem(projectId);
  const pending = closeItem.isPending;
  const close = () => {
    if (!pending) onClose();
  };

  async function confirm() {
    if (pending) return;
    try {
      await closeItem.mutateAsync({ loopId: loop.id, ticketId: child.ticket_id });
      onClose();
    } catch {
      /* shown below */
    }
  }

  return (
    <Dialog open onClose={close} titleId={titleId}>
      <DialogHeader titleId={titleId}>Close item</DialogHeader>
      <div className="space-y-3 text-sm text-text-secondary">
        <p>
          Mark <strong className="font-semibold text-text-primary">{child.title}</strong> as finished by you. The loop
          stops waiting on it. The ticket keeps its current status.
        </p>
      </div>
      {closeItem.error && (
        <Alert variant="danger" className="mt-4">{errorMessage(closeItem.error, 'Could not close the item')}</Alert>
      )}
      <DialogFooter>
        <Button variant="secondary" onClick={close} disabled={pending}>Cancel</Button>
        <Button onClick={confirm} loading={pending}>Close item</Button>
      </DialogFooter>
    </Dialog>
  );
}

/* ── Checklist ─────────────────────────────────────────────────────── */

const STATUS_ORDER: ChecklistStatus[] = ['proposed', 'active', 'retired'];
const STATUS_LABEL: Record<ChecklistStatus, string> = {
  proposed: 'Proposed',
  active: 'Active',
  retired: 'Retired',
};

export function ChecklistPanel({ projectId, canEdit }: { projectId: string; canEdit: boolean }) {
  const list = useChecklist(projectId);
  const seed = useSeedChecklist(projectId);
  const patch = usePatchChecklistItem(projectId);
  // One busy flag per item, settled per call.
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null);

  async function setStatus(item: ChecklistItem, status: 'active' | 'retired') {
    if (busy[item.id]) return;
    setBusy((b) => ({ ...b, [item.id]: true }));
    setRowError(null);
    try {
      await patch.mutateAsync({ itemId: item.id, status });
    } catch (err) {
      setRowError({ id: item.id, message: errorMessage(err, 'Could not update this item') });
    } finally {
      setBusy((b) => ({ ...b, [item.id]: false }));
    }
  }

  const items = list.data ?? [];
  return (
    <section aria-labelledby="loop-checklist" className="space-y-3">
      <h3 id="loop-checklist" className={SECTION_TITLE}>Checklist</h3>
      <p className={QUIET}>
        Recurring traps that go into every brief and review once accepted.
      </p>
      {list.isLoading && <p className={QUIET}>Loading the checklist…</p>}
      {list.error && <Alert variant="danger">Could not load the checklist: {errorMessage(list.error)}</Alert>}
      {list.data && items.length === 0 && (
        <Card className="flex flex-wrap items-center justify-between gap-3 p-4">
          <p className={QUIET}>
            {canEdit
              ? 'No checklist items yet. Start from six common traps and accept the ones that fit.'
              : 'No checklist items yet.'}
          </p>
          {canEdit && (
            <Button size="sm" onClick={() => seed.mutate()} loading={seed.isPending}>Seed checklist</Button>
          )}
        </Card>
      )}
      {seed.error && <Alert variant="danger">{errorMessage(seed.error, 'Could not seed the checklist')}</Alert>}
      {STATUS_ORDER.map((status) => {
        const rows = items.filter((i) => i.status === status);
        if (rows.length === 0) return null;
        return (
          <div key={status} className="space-y-2">
            <h4 className="text-sm font-medium text-text-secondary">
              {STATUS_LABEL[status]} <span className="font-normal text-text-tertiary">{rows.length}</span>
            </h4>
            <Card className="divide-y divide-[var(--border)] p-0">
              <ul aria-label={`${STATUS_LABEL[status]} checklist items`}>
                {rows.map((item) => (
                  <li key={item.id} className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
                    <div className="min-w-0 flex-1 space-y-1">
                      <p className="text-sm font-medium text-text-primary">{item.title}</p>
                      <p className="text-xs text-text-tertiary">{item.check_text}</p>
                      <p className="flex flex-wrap items-center gap-2 text-xs text-text-tertiary">
                        <Badge label={item.source} />
                        {item.times_found > 0 && <span>Found {plural(item.times_found, 'time')}</span>}
                      </p>
                      {rowError?.id === item.id && (
                        <p role="alert" className="text-xs text-danger">{rowError.message}</p>
                      )}
                    </div>
                    {canEdit && status !== 'retired' && (
                      <div className="flex shrink-0 gap-2">
                        {status === 'proposed' && (
                          <Button size="sm" onClick={() => setStatus(item, 'active')} loading={!!busy[item.id]}>
                            Accept<span className="sr-only">: {item.title}</span>
                          </Button>
                        )}
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => setStatus(item, 'retired')}
                          disabled={!!busy[item.id]}
                        >
                          Retire<span className="sr-only">: {item.title}</span>
                        </Button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          </div>
        );
      })}
    </section>
  );
}
