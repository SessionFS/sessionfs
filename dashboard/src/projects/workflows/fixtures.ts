import type { StartedWorkflow, StepDefinition, WorkflowPlan, WorkflowTemplate } from '../../api/workflows';
import type { Loop } from '../../api/reviewLoop';

const step = (s: Partial<StepDefinition> & Pick<StepDefinition, 'id' | 'title' | 'role' | 'agent'>): StepDefinition => ({
  depends_on: [],
  fan_out: 1,
  gate: 'none',
  brief_template: '',
  ...s,
});

/** Shapes follow `_template_view` in routes/workflows.py. */
export const template = (over: Partial<WorkflowTemplate> = {}): WorkflowTemplate => ({
  id: 'wft_0123456789abcdef',
  lineage_id: 'wfl_0123456789abcdef',
  version: 2,
  name: 'Ship a change',
  scope: 'project',
  org_id: null,
  project_id: 'proj_fixture1',
  active: true,
  supersedes_id: 'wft_0000000000000001',
  created_by_user_id: 'usr_fixture',
  actor_type: 'user',
  service_key_name: null,
  created_at: '2026-11-04T09:00:00+00:00',
  definition: {
    version: 1,
    steps: [
      step({ id: 'dispatch', title: 'Split the change into briefs', role: 'dispatcher', agent: 'scout', brief_template: 'Read {ticket.title}.\n\n{ticket.description}\n' }),
      step({ id: 'implement', title: 'Implement one brief', role: 'implementer', agent: 'atlas', depends_on: ['dispatch'], fan_out: 3, brief_template: 'Implement your part.\n\n{checklist}\n' }),
      step({ id: 'review', title: 'Independent code review', role: 'reviewer', agent: 'codex-reviewer', depends_on: ['implement'], gate: 'trusted_clean' }),
      step({ id: 'verify', title: 'Security verification', role: 'verifier', agent: 'sentinel', depends_on: ['review'], gate: 'approval_policy:security' }),
      step({ id: 'document', title: 'Update the docs', role: 'documenter', agent: 'scribe', depends_on: ['verify'] }),
    ],
  },
  ...over,
});

export const orgTemplate = (): WorkflowTemplate =>
  template({
    id: 'wft_org0000000000001',
    lineage_id: 'wfl_org0000000000001',
    version: 1,
    name: 'Org review pass',
    scope: 'org',
    org_id: 'org_fixture1',
    project_id: null,
    supersedes_id: null,
    definition: {
      version: 1,
      steps: [
        step({ id: 'implement', title: 'Implement', role: 'implementer', agent: 'atlas' }),
        step({ id: 'review', title: 'Review', role: 'reviewer', agent: 'codex-reviewer', depends_on: ['implement'], gate: 'trusted_clean' }),
      ],
    },
  });

export const templates = (): WorkflowTemplate[] => [template(), orgTemplate()];

/** Shapes follow `plan_view` in services/workflows.py. */
export const plan = (): WorkflowPlan => ({
  loop_id: 'tk_wfloop',
  template: { id: 'wft_0123456789abcdef', lineage_id: 'wfl_0123456789abcdef', name: 'Ship a change', version: 2 },
  steps: [
    {
      step_id: 'dispatch', title: 'Split the change into briefs', role: 'dispatcher', agent: 'scout', depends_on: [], fan_out: 1, gate: 'none',
      state: 'done', reason: 'Finished.', gate_status: { passed: true, reason: '' },
      children: [{ ticket_id: 'tk_wf_d1', index: 0, title: 'Dispatch: spend tracking', status: 'done', item_status: 'done' }],
      reviews: [], ready_children: [],
    },
    {
      step_id: 'implement', title: 'Implement one brief', role: 'implementer', agent: 'atlas', depends_on: ['dispatch'], fan_out: 3, gate: 'none',
      state: 'ready', reason: 'Every dependency is done; nothing has been reported yet.', gate_status: { passed: true, reason: '' },
      children: [
        { ticket_id: 'tk_wf_i1', index: 0, title: 'Implement 1/3: usage records', status: 'open', item_status: 'pending' },
        { ticket_id: 'tk_wf_i2', index: 1, title: 'Implement 2/3: spend endpoint', status: 'open', item_status: 'pending' },
        { ticket_id: 'tk_wf_i3', index: 2, title: 'Implement 3/3: dashboard panel', status: 'in_progress', item_status: 'pending' },
      ],
      reviews: [],
      ready_children: [
        { ticket_id: 'tk_wf_i1', index: 0, title: 'Implement 1/3: usage records', status: 'open', item_status: 'pending', agent: 'atlas', brief_markdown: 'Implement your part of **Spend tracking**.\n\n## Known traps\n- **Fail closed** — an empty set must refuse.' },
        { ticket_id: 'tk_wf_i2', index: 1, title: 'Implement 2/3: spend endpoint', status: 'open', item_status: 'pending', agent: 'atlas', brief_markdown: 'Implement the spend endpoint.' },
      ],
    },
    {
      step_id: 'review', title: 'Independent code review', role: 'reviewer', agent: 'codex-reviewer', depends_on: ['implement'], fan_out: 1, gate: 'trusted_clean',
      state: 'blocked', reason: 'Waiting for implement.', gate_status: { passed: false, reason: 'No trusted VERIFIED-CLEAN at the latest commit yet.' },
      children: [], reviews: ['tk_wf_i1', 'tk_wf_i2', 'tk_wf_i3'], ready_children: [],
    },
    {
      step_id: 'verify', title: 'Security verification', role: 'verifier', agent: 'sentinel', depends_on: ['review'], fan_out: 1, gate: 'approval_policy:security',
      state: 'blocked', reason: 'Waiting for review.', gate_status: { passed: false, reason: 'Waiting on approval: Security sign-off (0 of 1)' },
      children: [{ ticket_id: 'tk_wf_v1', index: 0, title: 'Verify: security', status: 'open', item_status: 'pending' }],
      reviews: [], ready_children: [],
    },
    {
      step_id: 'document', title: 'Update the docs', role: 'documenter', agent: 'scribe', depends_on: ['verify'], fan_out: 1, gate: 'none',
      state: 'needs_human', reason: 'Token budget hit.', gate_status: { passed: true, reason: '' },
      children: [{ ticket_id: 'tk_wf_c1', index: 0, title: 'Document: spend tracking', status: 'open', item_status: 'needs_human' }],
      reviews: [], ready_children: [],
    },
  ],
  ready_steps: ['implement'],
  done: false,
});

export const started = (): StartedWorkflow => ({
  loop_id: 'tk_wfloop',
  queue_id: 'wq_fixture',
  workflow: { template_id: 'wft_0123456789abcdef', lineage_id: 'wfl_0123456789abcdef', version: 2, name: 'Ship a change' },
  steps: [{ step_id: 'dispatch', tickets: [{ id: 'tk_wf_d1', index: 0, title: 'Dispatch', agent: 'scout', brief_markdown: 'Read it.' }] }],
  reviewer: { persona: 'codex-reviewer', trusted: true },
  plan: plan(),
});

/** A loop as `GET /loops` returns it for a workflow run (`workflow` is the pinned template). */
export const workflowLoop = (): Loop => ({
  id: 'tk_wfloop',
  title: 'Spend tracking for agent runs',
  workflow: { template_id: 'wft_0123456789abcdef', lineage_id: 'wfl_0123456789abcdef', version: 2 },
  children: [
    {
      ticket_id: 'tk_wf_i3', title: 'Implement 3/3: dashboard panel', status: 'in_progress', branch: 'feat/panel', head_sha: null,
      round: 0, harmful_open: 0, findings_by_harm: {}, rounds: [], tokens: 12_000, budget: { tokens: 400_000 },
      needs_human: null, ready: false, verdict: null, not_ready_reasons: [],
    },
  ],
});
