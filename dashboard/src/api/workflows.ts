/**
 * Workflow templates API: types and client functions.
 *
 * Shapes follow routes/workflows.py (templates for a project and for an org,
 * POST .../workflows to start, GET .../workflows/{loop}/plan) and
 * services/workflows.py (`plan_view`).
 *
 * Workflows are record-only: SessionFS stores the template, creates the loop
 * and computes the plan. Nothing here starts an agent.
 */
import { ApiError } from './client';

export type StepRole = 'dispatcher' | 'implementer' | 'reviewer' | 'verifier' | 'documenter';
export const STEP_ROLES: StepRole[] = ['dispatcher', 'implementer', 'reviewer', 'verifier', 'documenter'];

/** `none`, `trusted_clean` or `approval_policy:<id>`. */
export type StepGate = string;

export interface StepDefinition {
  id: string;
  title: string;
  role: StepRole;
  agent: string;
  depends_on: string[];
  fan_out: number;
  gate: StepGate;
  brief_template: string;
}
export interface WorkflowDefinition {
  version: 1;
  steps: StepDefinition[];
}

export interface WorkflowTemplate {
  id: string;
  lineage_id: string;
  version: number;
  name: string;
  scope: 'project' | 'org';
  org_id: string | null;
  project_id: string | null;
  active: boolean;
  supersedes_id: string | null;
  created_by_user_id: string | null;
  actor_type: string;
  service_key_name: string | null;
  created_at: string | null;
  /** Null when the stored definition can no longer be parsed. */
  definition: WorkflowDefinition | null;
}

export interface TemplateWrite {
  name: string;
  definition: Record<string, unknown>;
}

export type PlanStepState = 'blocked' | 'ready' | 'running' | 'needs_human' | 'done';
export interface PlanChild {
  ticket_id: string;
  index: number;
  title: string;
  status: string;
  item_status: string | null;
  /** Only on `ready_children`. */
  brief_markdown?: string;
  agent?: string | null;
}
/**
 * `title`, `agent`, `gate` and `template.name` are null when the template
 * belongs to an org the caller cannot see (state and briefs still come back).
 */
export interface PlanStep {
  step_id: string;
  title: string | null;
  role: StepRole;
  agent: string | null;
  depends_on: string[];
  fan_out: number;
  gate: StepGate | null;
  state: PlanStepState;
  reason: string;
  gate_status: { passed: boolean; reason: string };
  children: PlanChild[];
  /** Ticket ids this reviewer step reviews (reviewer steps with dependencies). */
  reviews: string[];
  ready_children: PlanChild[];
}
export interface WorkflowPlan {
  loop_id: string;
  template: { id: string; lineage_id: string; name: string | null; version: number };
  steps: PlanStep[];
  ready_steps: string[];
  done: boolean;
}

export interface StartWorkflowBody {
  template_id: string;
  ticket_id?: string;
  title?: string;
  description?: string;
  reviewer_persona?: string;
  token_budget?: number;
  cost_budget_usd?: number;
  max_review_rounds?: number;
}
export interface StartedWorkflow {
  loop_id: string;
  queue_id: string;
  workflow: { template_id: string; lineage_id: string; version: number; name: string };
  steps: { step_id: string; tickets: { id: string; index: number; title: string; agent: string | null; brief_markdown: string }[] }[];
  reviewer: { persona: string; trusted: boolean };
  plan: WorkflowPlan | null;
}

export type TemplateTarget = { kind: 'project'; id: string } | { kind: 'org'; id: string };

const enc = encodeURIComponent;

export function createWorkflowApi(baseUrl: string, apiKey: string) {
  async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
    const resp = await fetch(`${baseUrl}${path}`, {
      method,
      headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) throw new ApiError(resp.status, await resp.text());
    if (resp.status === 204) return undefined as T;
    return (await resp.json()) as T;
  }
  const root = (t: TemplateTarget) =>
    t.kind === 'project' ? `/api/v1/projects/${enc(t.id)}/workflow-templates` : `/api/v1/orgs/${enc(t.id)}/workflow-templates`;
  return {
    listTemplates: (t: TemplateTarget) => send<WorkflowTemplate[]>('GET', root(t)),
    createTemplate: (t: TemplateTarget, body: TemplateWrite) => send<WorkflowTemplate>('POST', root(t), body),
    updateTemplate: (t: TemplateTarget, templateId: string, body: TemplateWrite) =>
      send<WorkflowTemplate>('PUT', `${root(t)}/${enc(templateId)}`, body),
    disableTemplate: (t: TemplateTarget, templateId: string) => send<void>('DELETE', `${root(t)}/${enc(templateId)}`),
    startWorkflow: (projectId: string, body: StartWorkflowBody) =>
      send<StartedWorkflow>('POST', `/api/v1/projects/${enc(projectId)}/workflows`, body),
    getPlan: (projectId: string, loopId: string) =>
      send<WorkflowPlan>('GET', `/api/v1/projects/${enc(projectId)}/workflows/${enc(loopId)}/plan`),
  };
}
export type WorkflowApi = ReturnType<typeof createWorkflowApi>;
