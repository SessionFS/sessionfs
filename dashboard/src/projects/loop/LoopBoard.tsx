import { Link } from 'react-router-dom';
import { Card } from '../../components/ui';
import type { Loop, LoopChild } from '../../api/reviewLoop';
import { BudgetBar } from './Panels';
import { designProblem, forecast, isRunning, laneStages, type Stage } from './boardModel';
import { plural, storyHref } from './format';

/** One ring per review round: size scales with harmful findings, empty is clean. */
export function Ring({ round, harmful }: { round: number; harmful: number }) {
  const r = harmful > 0 ? Math.min(7 + harmful * 2, 16) : 6;
  return (
    <svg
      role="img"
      aria-label={`Round ${round}: ${plural(harmful, 'harmful finding')}`}
      width="36"
      height="36"
      viewBox="0 0 36 36"
    >
      <circle
        cx="18"
        cy="18"
        r={r}
        data-harmful={harmful > 0 ? 'true' : 'false'}
        data-radius={r}
        fill={harmful > 0 ? 'color-mix(in oklch, var(--danger) 25%, transparent)' : 'none'}
        stroke={harmful > 0 ? 'var(--danger)' : 'var(--border-strong)'}
        strokeWidth="1.75"
      />
    </svg>
  );
}

function StageNode({ stage, state, running }: { stage: Stage; state: 'done' | 'current' | 'todo'; running: boolean }) {
  const tone =
    state === 'current' ? 'font-semibold text-text-primary' : state === 'done' ? 'text-text-secondary' : 'text-text-tertiary';
  return (
    <li
      aria-current={state === 'current' ? 'step' : undefined}
      data-state={state}
      className="flex shrink-0 flex-col items-center gap-1"
    >
      {stage.round ? (
        <Ring round={stage.round.round} harmful={stage.round.harmful} />
      ) : (
        <span
          aria-hidden="true"
          data-running={state === 'current' && running ? 'true' : 'false'}
          className={`mt-3.5 mb-3.5 block h-2 w-2 rounded-full ${
            state === 'todo' ? 'border border-border-strong' : 'bg-text-tertiary'
          } ${state === 'current' && running ? 'motion-safe:animate-pulse' : ''}`}
        />
      )}
      <span className={`whitespace-nowrap text-xs ${tone}`}>
        {stage.label}
        {state === 'current' && <span className="sr-only"> (current stage)</span>}
      </span>
    </li>
  );
}

function Lane({ projectId, child }: { projectId: string; child: LoopChild }) {
  const { stages, current } = laneStages(child);
  const running = isRunning(child);
  const plan = forecast(child);
  return (
    <Card className="space-y-3 p-4">
      <Link
        to={storyHref(projectId, child.ticket_id)}
        className="block truncate text-sm font-semibold text-text-primary outline-none hover:underline focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        {child.title}
      </Link>
      <ol tabIndex={0} aria-label={`Stages for ${child.title}`} className="flex items-start gap-5 overflow-x-auto pb-1 rounded-sm focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
        {stages.map((s, i) => (
          <StageNode key={s.key} stage={s} state={i < current ? 'done' : i === current ? 'current' : 'todo'} running={running} />
        ))}
      </ol>
      {designProblem(child) && (
        <p className="text-xs text-text-secondary">
          Design problem? Three rounds without fewer findings. <Link className="text-brand underline" to={storyHref(projectId, child.ticket_id)}>Revisit the brief</Link>
        </p>
      )}
      <BudgetBar child={child} />
      {plan && <p className="text-xs text-text-tertiary">{plan}</p>}
    </Card>
  );
}

export function LoopBoard({ projectId, loops }: { projectId: string; loops: Loop[] }) {
  const lanes = loops.flatMap((l) => l.children.map((c) => ({ loop: l, child: c })));
  if (lanes.length === 0) return <p className="text-sm text-text-tertiary">No tickets in a loop yet.</p>;
  return (
    <section aria-label="Loop board" className="space-y-3">
      {loops.map((l) =>
        l.children.length === 0 ? null : (
          <div key={l.id} className="space-y-2">
            <p className="text-sm text-text-secondary">{l.title}</p>
            {l.children.map((c) => (
              <Lane key={c.ticket_id} projectId={projectId} child={c} />
            ))}
          </div>
        ),
      )}
    </section>
  );
}
