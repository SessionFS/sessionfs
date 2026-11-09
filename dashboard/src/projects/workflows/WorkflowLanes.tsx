import { Link } from 'react-router-dom';
import { Alert, Badge, Card, Markdown } from '../../components/ui';
import type { PlanChild, PlanStep, PlanStepState } from '../../api/workflows';
import { useWorkflowPlan } from '../../hooks/useWorkflows';
import { errorMessage } from '../../utils/errorMessage';
import { storyHref } from '../loop/format';
import { gateLabel } from './graph';
import { Term } from '../../help/Term';

const STATE_LABEL: Record<PlanStepState, string> = {
  blocked: 'Blocked',
  ready: 'Ready',
  running: 'Running',
  needs_human: 'Needs a person',
  done: 'Done',
};
const STATE_VARIANT: Record<PlanStepState, 'default' | 'info' | 'warning' | 'success'> = {
  blocked: 'default',
  ready: 'info',
  running: 'info',
  needs_human: 'warning',
  done: 'success',
};

function ChildRow({ projectId, child, highlight }: { projectId: string; child: PlanChild; highlight: boolean }) {
  return (
    <li
      data-ready={highlight ? 'true' : 'false'}
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2 py-1.5 text-sm ${
        highlight ? 'border border-brand bg-bg-sunken' : ''
      }`}
    >
      <Link
        to={storyHref(projectId, child.ticket_id)}
        className="min-w-0 flex-1 truncate text-text-primary outline-none hover:underline focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        {child.title}
      </Link>
      <span className="text-xs text-text-tertiary">
        {child.status.replace(/_/g, ' ')}
        {child.item_status === 'needs_human' && ' · needs a person'}
      </span>
    </li>
  );
}

function Lane({ projectId, step }: { projectId: string; step: PlanStep }) {
  const gate = step.gate ? gateLabel(step.gate) : null;
  const name = step.title ?? step.step_id;
  const readyIds = new Set(step.ready_children.map((c) => c.ticket_id));
  const briefs = step.ready_children.filter((c) => c.brief_markdown);
  return (
    <Card
      className={`space-y-3 p-4 ${step.state === 'ready' ? 'border-brand' : ''}`}
      data-testid={`lane-${step.step_id}`}
      data-state={step.state}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="min-w-0 flex-1 text-sm font-semibold text-text-primary">{name}</h3>
        {step.state === 'needs_human' ? (
          <Term id="needs-human">
            <Badge variant={STATE_VARIANT[step.state]} label={STATE_LABEL[step.state]} tint />
          </Term>
        ) : (
          <Badge variant={STATE_VARIANT[step.state]} label={STATE_LABEL[step.state]} tint />
        )}
      </div>
      <p className="text-xs text-text-tertiary">
        {step.role}
        {step.agent && ` · ${step.agent}`}
        {step.fan_out > 1 && (
          <>
            {' · '}
            <Term id="fan-out">×{step.fan_out}</Term>
          </>
        )}
        {step.depends_on.length > 0 && ` · after ${step.depends_on.join(', ')}`}
      </p>
      {gate && (
        <p className="text-xs text-text-secondary">
          <Term id="workflow-gate">{gate}</Term>:{' '}
          <span className={step.gate_status.passed ? 'text-text-secondary' : 'text-text-primary'}>
            {step.gate_status.passed ? 'passed' : `not passed${step.gate_status.reason ? `, ${step.gate_status.reason}` : ''}`}
          </span>
        </p>
      )}
      {step.reason && <p className="text-xs text-text-secondary">{step.reason}</p>}
      {step.reviews.length > 0 && (
        <p className="text-xs text-text-tertiary">Reviews {step.reviews.length} {step.reviews.length === 1 ? 'ticket' : 'tickets'} from earlier steps.</p>
      )}
      {step.children.length > 0 && (
        <ul aria-label={`Tickets for ${name}`} className="space-y-1">
          {step.children.map((c) => (
            <ChildRow key={c.ticket_id} projectId={projectId} child={c} highlight={readyIds.has(c.ticket_id)} />
          ))}
        </ul>
      )}
      {briefs.map((c) => (
        <details key={c.ticket_id} className="rounded-md border border-border bg-bg-sunken px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
            Brief for {c.title}
            {c.agent ? ` (${c.agent})` : ''}
          </summary>
          <div className="mt-2 text-sm">
            <Markdown>{c.brief_markdown ?? ''}</Markdown>
          </div>
        </details>
      ))}
    </Card>
  );
}

/**
 * One lane per template step for a loop started from a workflow: state, gate,
 * the plan's reason and the step's tickets. Tickets the plan says can run now
 * are highlighted, with their brief a click away. Computed from stored rows
 * on every call, so there is nothing here to configure.
 */
export function WorkflowLanes({ projectId, loopId, title }: { projectId: string; loopId: string; title: string }) {
  const plan = useWorkflowPlan(projectId, loopId);
  return (
    <section aria-label={`Workflow lanes for ${title}`} className="space-y-2" data-testid="workflow-lanes">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <p className="text-sm text-text-secondary">{title}</p>
        {plan.data && (
          <span className="text-xs text-text-tertiary">
            {plan.data.template.name ?? 'Workflow'} v{plan.data.template.version}
            {plan.data.done ? ' · all steps done' : plan.data.ready_steps.length > 0 ? ` · ready: ${plan.data.ready_steps.join(', ')}` : ''}
          </span>
        )}
      </div>
      {plan.isLoading && <p className="text-sm text-text-tertiary">Loading the plan…</p>}
      {plan.error && <Alert variant="danger">Could not load the workflow plan: {errorMessage(plan.error)}</Alert>}
      {plan.data && (
        <div className="space-y-3">
          {plan.data.steps.map((s) => (
            <Lane key={s.step_id} projectId={projectId} step={s} />
          ))}
        </div>
      )}
    </section>
  );
}
