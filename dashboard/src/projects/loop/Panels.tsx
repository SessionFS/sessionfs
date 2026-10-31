import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge, Button, Card, TableShell } from '../../components/ui';
import { useChecklist, usePatchChecklistItem } from '../../hooks/useReviewLoop';
import { errorMessage } from '../../utils/errorMessage';
import type {
  CaughtFinding,
  CostPoint,
  LearningClass,
  Loop,
  LoopChild,
  ScoreboardRow,
} from '../../api/reviewLoop';
import {
  HARM_LABEL,
  formatTokens,
  formatUsd,
  isHarmful,
  plural,
  runHref,
  shortSha,
  storyHref,
} from './format';
import { TrustSeal } from './TrustSeal';
import { CloseItemDialog, RaiseLimitDialog } from './LoopActions';

const SECTION_TITLE = 'text-md font-semibold text-text-primary';
const QUIET = 'text-sm text-text-tertiary';

/* ── Needs you ─────────────────────────────────────────────────────── */

export function NeedsYou({
  projectId,
  loops,
  canEdit = false,
}: {
  projectId: string;
  loops: Loop[];
  canEdit?: boolean;
}) {
  const [action, setAction] = useState<{ kind: 'raise' | 'close'; loop: Loop; child: LoopChild } | null>(null);
  const items = loops.flatMap((l) =>
    l.children
      .filter((c) => c.needs_human || c.ready)
      .map((c) => ({ child: c, loop: l })),
  );
  const proposed = (useChecklist(projectId).data ?? []).filter((i) => i.status === 'proposed');
  const patch = usePatchChecklistItem(projectId);
  const [accepting, setAccepting] = useState<string | null>(null);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  async function accept(itemId: string) {
    if (accepting) return;
    setAccepting(itemId);
    setAcceptError(null);
    try {
      await patch.mutateAsync({ itemId, status: 'active' });
    } catch (err) {
      setAcceptError(errorMessage(err, 'Could not accept this item'));
    } finally {
      setAccepting(null);
    }
  }
  const total = items.length + proposed.length;
  if (total === 0) return null;
  return (
    <section aria-labelledby="loop-needs-you" className="space-y-2">
      <h3 id="loop-needs-you" className={SECTION_TITLE}>
        Needs you <span className="text-sm font-normal text-text-tertiary">{total}</span>
      </h3>
      <Card className="divide-y divide-[var(--border)] p-0">
        <ul>
          {items.map(({ child, loop }) => (
            <li key={child.ticket_id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-text-primary">{child.title}</p>
                <p className="text-xs text-text-tertiary">
                  {child.needs_human ? child.needs_human : 'Ready to merge. You decide.'}
                  <span className="sr-only"> in {loop.title}</span>
                </p>
              </div>
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                {child.needs_human && canEdit && (
                  <>
                    <Button size="sm" variant="secondary" onClick={() => setAction({ kind: 'raise', loop, child })}>
                      Raise limit<span className="sr-only">: {child.title}</span>
                    </Button>
                    <Button size="sm" variant="secondary" onClick={() => setAction({ kind: 'close', loop, child })}>
                      Close item<span className="sr-only">: {child.title}</span>
                    </Button>
                  </>
                )}
                <Link
                  to={storyHref(projectId, child.ticket_id)}
                  className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-text-on-brand outline-none hover:bg-brand-hover focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                >
                  {child.needs_human ? 'Open' : 'Review and merge'}
                  <span className="sr-only">: {child.title}</span>
                </Link>
              </div>
            </li>
          ))}
          {proposed.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-text-primary">{item.title}</p>
                <p className="text-xs text-text-tertiary">Proposed checklist item. {item.check_text}</p>
              </div>
              {canEdit && (
                <Button size="sm" onClick={() => void accept(item.id)} loading={accepting === item.id}>
                  Accept<span className="sr-only">: {item.title}</span>
                </Button>
              )}
            </li>
          ))}
        </ul>
        {acceptError && <p role="alert" className="px-4 pb-3 text-xs text-danger">{acceptError}</p>}
      </Card>
      {action?.kind === 'raise' && (
        <RaiseLimitDialog projectId={projectId} loop={action.loop} child={action.child} onClose={() => setAction(null)} />
      )}
      {action?.kind === 'close' && (
        <CloseItemDialog projectId={projectId} loop={action.loop} child={action.child} onClose={() => setAction(null)} />
      )}
    </section>
  );
}

/* ── What the loop caught ──────────────────────────────────────────── */

export function Caught({ projectId, caught, total }: { projectId: string; caught: CaughtFinding[]; total?: number }) {
  const harmful = caught.filter((c) => isHarmful(c.harm));
  const counts = new Map<string, number>();
  for (const c of harmful) counts.set(c.harm, (counts.get(c.harm) ?? 0) + 1);
  const breakdown = [...counts.entries()].map(([h, n]) => `${n} ${(HARM_LABEL[h as keyof typeof HARM_LABEL] ?? h).toLowerCase()}`);

  return (
    <section aria-labelledby="loop-caught" className="space-y-3">
      <h3 id="loop-caught" className={SECTION_TITLE}>What the loop caught</h3>
      {harmful.length === 0 ? (
        <p className={QUIET}>
          No harmful findings recorded in this period. When a reviewer stops one before merge, it shows up here with the line and the fix.
        </p>
      ) : (
        <>
          <p className="text-sm text-text-secondary">
            The loop stopped{' '}
            <strong className="font-semibold text-danger">{plural(harmful.length, 'harmful finding')}</strong>{' '}
            before merge: {breakdown.join(', ')}.
            {total !== undefined && total > caught.length && (
              <span className="text-text-tertiary"> Showing the latest {caught.length} of {total}.</span>
            )}
          </p>
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {harmful.map((c, i) => (
              <li key={`${c.run_id}-${i}-${c.finding}`}>
                <Card className="h-full space-y-2 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="danger" tint label={HARM_LABEL[c.harm] ?? c.harm} />
                    <span className="text-xs text-text-tertiary">{c.class}</span>
                  </div>
                  <p className="text-sm text-text-primary">{c.finding}</p>
                  {c.file && (
                    <p className="truncate font-mono text-xs text-text-secondary" title={`${c.file}${c.line ? `:${c.line}` : ''}`}>
                      {c.file}
                      {c.line ? `:${c.line}` : ''}
                    </p>
                  )}
                  <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-tertiary">
                    <Link className="text-brand underline-offset-2 hover:underline" to={runHref(projectId, c.run_id)}>
                      Run{" "}<span className="sr-only">for {c.finding}</span>
                    </Link>
                    <Link className="text-brand underline-offset-2 hover:underline" to={storyHref(projectId, c.ticket_id)}>
                      Story{" "}<span className="sr-only">for {c.finding}</span>
                    </Link>
                    {c.fixed_in_sha ? (
                      <span>
                        Fixed in <code className="font-mono text-text-secondary" title={c.fixed_in_sha}>{shortSha(c.fixed_in_sha)}</code>
                      </span>
                    ) : (
                      <span>Not fixed yet</span>
                    )}
                  </p>
                </Card>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/* ── Cost per accepted change ──────────────────────────────────────── */

export function CostTrend({ points }: { points: CostPoint[] }) {
  // Dollars only when every point carries them; otherwise tokens.
  const dollars = points.length > 0 && points.every((p) => p.cost_usd !== undefined);
  const value = (p: CostPoint) => (dollars ? (p.cost_usd as number) : p.tokens);
  const fmt = (p: CostPoint) => (dollars ? formatUsd(p.cost_usd as number) : `${formatTokens(p.tokens)} tokens`);

  return (
    <section aria-labelledby="loop-cost" className="space-y-3">
      <h3 id="loop-cost" className={SECTION_TITLE}>Cost per accepted change</h3>
      {points.length === 0 ? (
        <p className={QUIET}>Appears once a change has been reviewed clean and accepted.</p>
      ) : (
        <Card className="space-y-2 p-4">
          <p className="text-sm text-text-secondary">
            {points.length > 1 ? (
              <>
                <span className="text-text-primary">{fmt(points[0])}</span> in {points[0].period}
                {' → '}
                <span className="font-semibold text-text-primary">{fmt(points[points.length - 1])}</span> in{' '}
                {points[points.length - 1].period}
              </>
            ) : (
              <>
                <span className="font-semibold text-text-primary">{fmt(points[0])}</span> in {points[0].period}
              </>
            )}
          </p>
          <TrendChart
            values={points.map(value)}
            labels={points.map((p) => `${p.period}: ${fmt(p)}`)}
          />
          <table className="sr-only">
            <caption>Cost per accepted change by period</caption>
            <thead><tr><th>Period</th><th>{dollars ? 'Dollars' : 'Tokens'}</th></tr></thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.period}><td>{p.period}</td><td>{dollars ? formatUsd(p.cost_usd as number) : p.tokens}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </section>
  );
}

function TrendChart({ values, labels }: { values: number[]; labels: string[] }) {
  const w = 320;
  const h = 64;
  const pad = 6;
  const max = Math.max(...values, 1);
  const x = (i: number) => (values.length === 1 ? w / 2 : pad + (i * (w - pad * 2)) / (values.length - 1));
  const y = (v: number) => h - pad - (v / max) * (h - pad * 2);
  const d = values.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-16 w-full" role="img" aria-label={`Trend: ${labels.join('; ')}`}>
      <line x1={pad} x2={w - pad} y1={h - pad} y2={h - pad} stroke="var(--border)" />
      {values.length > 1 && <path d={d} fill="none" stroke="var(--brand)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />}
      {values.map((v, i) => (
        <circle key={labels[i]} cx={x(i)} cy={y(v)} r={i === values.length - 1 ? 3.5 : 2.5} fill="var(--brand)" />
      ))}
    </svg>
  );
}

/* ── Your loop is learning ─────────────────────────────────────────── */

/** Index of the week that contains the join date (0 when it predates the series). */
function weekIndex(series: LearningClass['series'], joined: string): number {
  const j = joined.slice(0, 10);
  let at = 0;
  series.forEach((s, i) => {
    if (s.week.slice(0, 10) <= j) at = i;
  });
  return at;
}

export function Learning({ classes }: { classes: LearningClass[] }) {
  return (
    <section aria-labelledby="loop-learning" className="space-y-3">
      <h3 id="loop-learning" className={SECTION_TITLE}>Your loop is learning</h3>
      {classes.length === 0 ? (
        <p className={QUIET}>
          Recurring kinds of finding become checklist items that go into every brief and review. They appear here with how often each was found before and after.
        </p>
      ) : (
        <ul className="grid gap-3 xl:grid-cols-2">
          {classes.map((c) => {
            if (c.joined_at === null) {
              const found = c.series.reduce((n, s) => n + s.count, 0);
              return (
                <li key={c.class}>
                  <Card className="h-full space-y-2 p-3">
                    <p className="text-sm font-medium text-text-primary">{c.title}</p>
                    <Sparkline series={c.series} marker={null} label={`${c.title}: found ${found} times, not on the checklist`} />
                    <p className="text-xs text-text-tertiary">
                      {plural(found, 'time')} found, not on the checklist yet.
                    </p>
                  </Card>
                </li>
              );
            }
            const at = weekIndex(c.series, c.joined_at);
            const before = c.series.slice(0, at).reduce((n, s) => n + s.count, 0);
            const since = c.series.slice(at).reduce((n, s) => n + s.count, 0);
            const joined = new Date(c.joined_at).toLocaleDateString('en', { month: 'short', day: 'numeric', timeZone: 'UTC' });
            return (
              <li key={c.class}>
                <Card className="h-full space-y-2 p-3">
                  <p className="text-sm font-medium text-text-primary">{c.title}</p>
                  <Sparkline series={c.series} marker={at} label={`${c.title}: ${before} found before it joined the checklist on ${joined}, ${since} since`} />
                  <p className="text-xs text-text-tertiary">
                    {plural(before, 'time')} found before the checklist, {since} since
                    {' '}(joined {joined}).
                  </p>
                </Card>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function Sparkline({ series, marker, label }: { series: LearningClass['series']; marker: number | null; label: string }) {
  const w = 200;
  const h = 36;
  const pad = 4;
  const max = Math.max(...series.map((s) => s.count), 1);
  const x = (i: number) => (series.length <= 1 ? w / 2 : pad + (i * (w - pad * 2)) / (series.length - 1));
  const y = (v: number) => h - pad - (v / max) * (h - pad * 2);
  const d = series.map((s, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)} ${y(s.count).toFixed(1)}`).join(' ');
  const mx = marker === null ? 0 : x(marker);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-9 w-full" role="img" aria-label={label}>
      <line x1={pad} x2={w - pad} y1={h - pad} y2={h - pad} stroke="var(--border)" />
      {marker !== null && (
        <line x1={mx} x2={mx} y1={2} y2={h - 2} stroke="var(--text-tertiary)" strokeDasharray="2 3" data-testid="join-marker" />
      )}
      {series.length > 1 && <path d={d} fill="none" stroke="var(--text-secondary)" strokeWidth="1.75" strokeLinejoin="round" strokeLinecap="round" />}
    </svg>
  );
}

/* ── Active loops ──────────────────────────────────────────────────── */

export function ActiveLoops({ projectId, loops }: { projectId: string; loops: Loop[] }) {
  return (
    <section aria-labelledby="loop-active" className="space-y-3">
      <h3 id="loop-active" className={SECTION_TITLE}>Active loops</h3>
      {loops.map((l) => (
        <div key={l.id} className="space-y-2">
          <p className="text-sm text-text-secondary">{l.title}</p>
          <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {l.children.map((c) => (
              <li key={c.ticket_id}>
                <LoopCard projectId={projectId} child={c} />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}

function LoopCard({ projectId, child: c }: { projectId: string; child: LoopChild }) {
  return (
    <Card className="flex h-full flex-col gap-3 p-4">
      <div className="min-w-0">
        <Link
          to={storyHref(projectId, c.ticket_id)}
          className="block truncate text-sm font-semibold text-text-primary outline-none hover:underline focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
        >
          {c.title}
        </Link>
        <p className="truncate text-xs text-text-tertiary">
          {c.branch ? <span className="font-mono">{c.branch}</span> : c.status}
          {c.head_sha ? <> · <span className="font-mono">{shortSha(c.head_sha)}</span></> : null}
        </p>
      </div>
      <div className="flex items-center justify-between gap-3">
        <RoundRings child={c} />
        <p className={`text-xs ${c.harmful_open > 0 ? 'font-semibold text-danger' : 'text-text-tertiary'}`}>
          {c.harmful_open > 0 ? `${plural(c.harmful_open, 'harmful finding')} open` : 'No harmful findings open'}
        </p>
      </div>
      <BudgetBar child={c} />
      <TrustSeal
        compact
        readiness={{ ready: c.ready, reasons: c.not_ready_reasons ?? [], verdict: c.verdict ?? null }}
      />
    </Card>
  );
}

/** One ring per review round, sized by its harmful findings. Big, small, empty is healthy. */
export function RoundRings({ child }: { child: LoopChild }) {
  const rounds = child.rounds ?? [];
  if (rounds.length === 0) {
    return <p className="text-xs text-text-tertiary">{child.round === 0 ? 'No review yet' : `Round ${child.round}`}</p>;
  }
  const label = rounds.map((r) => `round ${r.round}: ${plural(r.harmful, 'harmful finding')}`).join(', ');
  return (
    <svg
      role="img"
      aria-label={`Review rounds, ${label}`}
      width={Math.max(rounds.length * 26, 26)}
      height="28"
      viewBox={`0 0 ${Math.max(rounds.length * 26, 26)} 28`}
    >
      {rounds.map((r, i) => {
        const radius = r.harmful > 0 ? Math.min(5 + r.harmful * 2, 12) : 4;
        return (
          <circle
            key={r.round}
            cx={13 + i * 26}
            cy={14}
            r={radius}
            data-harmful={r.harmful > 0 ? 'true' : 'false'}
            fill={r.harmful > 0 ? 'color-mix(in oklch, var(--danger) 25%, transparent)' : 'none'}
            stroke={r.harmful > 0 ? 'var(--danger)' : 'var(--border-strong)'}
            strokeWidth="1.75"
          />
        );
      })}
    </svg>
  );
}

export function BudgetBar({ child: c }: { child: LoopChild }) {
  const { budget } = c;
  // Show the cap closest to being reached. Dollars count only when both the
  // spend and its budget are present (absent for non-admins).
  const tokenCap = budget.tokens ? { usd: false, cap: budget.tokens, used: c.tokens } : null;
  const usdCap =
    c.cost_usd !== undefined && budget.cost_usd ? { usd: true, cap: budget.cost_usd, used: c.cost_usd } : null;
  const pick = [tokenCap, usdCap]
    .filter((x): x is NonNullable<typeof x> => x !== null)
    .sort((x, y) => y.used / y.cap - x.used / x.cap)[0];
  const useUsd = pick ? pick.usd : c.cost_usd !== undefined && budget.cost_usd !== undefined;
  const cap = pick?.cap;
  const used = pick ? pick.used : c.cost_usd !== undefined && useUsd ? c.cost_usd : c.tokens;
  const spent = useUsd ? formatUsd(used) : `${formatTokens(used)} tokens`;
  const roundsText = budget.max_rounds ? `round ${c.round} of ${budget.max_rounds}` : null;

  if (!cap) {
    return (
      <p className="text-xs text-text-tertiary">
        {spent} spent{roundsText ? ` · ${roundsText}` : ''}
      </p>
    );
  }
  const pct = Math.min(100, Math.round((used / cap) * 100));
  const capText = useUsd ? formatUsd(cap) : `${formatTokens(cap)} tokens`;
  return (
    <div className="space-y-1">
      <div
        role="progressbar"
        aria-label="Budget used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-valuetext={`${spent} of ${capText}`}
        className="h-1.5 overflow-hidden rounded-full bg-bg-sunken"
      >
        <div className="h-full rounded-full bg-text-tertiary" style={{ width: `${pct}%` }} />
      </div>
      <p className="text-xs text-text-tertiary">
        {spent} of {capText}{roundsText ? ` · ${roundsText}` : ''}
        {pct >= 100 ? ' · budget reached' : ''}
      </p>
    </div>
  );
}

/* ── Model scoreboard ──────────────────────────────────────────────── */

/** Which agent pairings work best, from the team's own accepted changes. Hidden when empty. */
export function Scoreboard({ rows }: { rows: ScoreboardRow[] }) {
  if (rows.length === 0) return null;
  const dollars = rows.every((r) => r.cost_per_accepted_change.cost_usd !== undefined);
  const model = (m: string | null) => m ?? 'Unknown model';
  return (
    <section aria-labelledby="loop-scoreboard" className="space-y-3">
      <h3 id="loop-scoreboard" className={SECTION_TITLE}>Model scoreboard</h3>
      <p className={QUIET}>Each implementer and reviewer pairing, over the changes it got accepted.</p>
      <TableShell label="Pairings by model" minWidth={640} bordered>
        <thead>
          <tr className="text-left text-2xs uppercase tracking-[0.04em] text-text-tertiary">
            <th scope="col" className="px-3 py-2">Implementer × reviewer</th>
            <th scope="col" className="px-3 py-2 text-right">Accepted changes</th>
            <th scope="col" className="px-3 py-2 text-right">Harmful per change</th>
            <th scope="col" className="px-3 py-2 text-right">Avg rounds to clean</th>
            <th scope="col" className="px-3 py-2 text-right">Cost per change</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.implementer_model}-${r.reviewer_model}-${i}`} className="border-t border-border text-sm text-text-secondary">
              <td className="px-3 py-2 text-text-primary">
                {model(r.implementer_model)} <span className="text-text-tertiary">×</span> {model(r.reviewer_model)}
              </td>
              <td className="px-3 py-2 text-right">{r.accepted_changes}</td>
              <td className="px-3 py-2 text-right">{r.harmful_findings_per_accepted_change}</td>
              <td className="px-3 py-2 text-right">{r.avg_rounds_to_clean ?? 'Not yet'}</td>
              <td className="px-3 py-2 text-right">
                {dollars
                  ? formatUsd(r.cost_per_accepted_change.cost_usd as number)
                  : `${formatTokens(r.cost_per_accepted_change.tokens)} tokens`}
              </td>
            </tr>
          ))}
        </tbody>
      </TableShell>
    </section>
  );
}
