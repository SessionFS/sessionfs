/**
 * Review Loop API: types and client functions in one module.
 *
 * Shapes follow the backend routes (routes/loops.py, review_loop.py,
 * loop_checklist.py, spend.py).
 *
 * Dollars are an admin-only field: `cost_usd` (and `budget.cost_usd`) is
 * ABSENT (not null, not zero) for everyone else, so the UI shows dollars only
 * when the key is present.
 */
import { ApiError } from './client';

export type RunRole =
  | 'implementer'
  | 'ui-implementer'
  | 'reviewer'
  | 'design-reviewer'
  | 'security-reviewer'
  | 'release';
export type Harm = 'data-exposed' | 'money-spent' | 'wrong-actor' | 'crash' | 'other' | 'none';

export interface SpendTotals {
  runs: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd?: number;
  tokens_on_plans: number;
}
export type SpendGroupBy = 'persona' | 'model' | 'ticket' | 'role' | 'day';
export interface SpendGroup extends SpendTotals {
  key: string;
}
export interface ProjectSpend {
  totals: SpendTotals;
  groups: SpendGroup[];
}
export interface TicketSpend {
  project_id: string;
  ticket_id: string;
  totals: SpendTotals;
  per_persona: SpendGroup[];
  per_model: SpendGroup[];
  review_rounds: number;
}

export interface Readiness {
  ready: boolean;
  reasons: string[];
  verdict: { reviewer: string; reviewed_commit: string | null; at: string | null } | null;
}

export interface LoopBudget {
  tokens?: number;
  cost_usd?: number;
  max_rounds?: number;
}

export interface LoopChild {
  ticket_id: string;
  title: string;
  status: string;
  branch: string | null;
  head_sha: string | null;
  /** Review rounds so far. */
  round: number;
  harmful_open: number;
  /** OPEN findings per explicit harm bucket (never includes "none"). */
  findings_by_harm: Partial<Record<Harm, number>>;
  /** Harmful findings raised per round, oldest first, drives the round rings. */
  rounds: { round: number; harmful: number }[];
  tokens: number;
  cost_usd?: number;
  budget: LoopBudget;
  needs_human: string | null;
  ready: boolean;
  verdict: Readiness['verdict'];
  not_ready_reasons: string[];
}
export interface Loop {
  id: string;
  title: string;
  children: LoopChild[];
}

export interface CaughtFinding {
  finding: string;
  ticket_id: string;
  run_id: string;
  harm: Harm;
  class: string;
  file: string | null;
  line: number | null;
  fixed_in_sha: string | null;
}
export interface CostPoint {
  period: string;
  tokens: number;
  cost_usd?: number;
}
export interface LearningClass {
  class: string;
  title: string;
  /** Null for a class that is not on the checklist (or never accepted). */
  joined_at: string | null;
  series: { week: string; count: number }[];
}
export interface LoopInsights {
  caught: CaughtFinding[];
  /** Every harmful fixed finding in the window; `caught` is the latest 100. */
  caught_total?: number;
  cost_per_accepted_change: CostPoint[];
  learning: LearningClass[];
}

export type StoryKind =
  | 'brief'
  | 'run'
  | 'round'
  | 'finding'
  | 'fix'
  | 'verdict'
  | 'merge'
  | 'pr'
  | 'release'
  | 'deploy';
export interface StoryEvent {
  kind: StoryKind;
  /** Null when the record has no timestamp. */
  at: string | null;
  title?: string;
  /** run / round / finding / fix / verdict */
  run_id?: string;
  role?: RunRole;
  round?: number | null;
  harm?: Harm | null;
  class?: string | null;
  file?: string | null;
  line?: number | null;
  sha?: string | null;
  reviewer?: string;
  url?: string | null;
  tokens?: number;
  cost_usd?: number;
  version?: string;
  environment?: string;
}
export interface Story {
  ticket_id: string;
  title: string;
  events: StoryEvent[];
}

export type ChecklistStatus = 'proposed' | 'active' | 'retired';
export interface ChecklistItem {
  id: string;
  project_id: string;
  slug: string;
  title: string;
  check_text: string;
  status: ChecklistStatus;
  source: string;
  times_found: number;
  last_found_at: string | null;
  accepted_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}
export interface SeedResult {
  created: ChecklistItem[];
  skipped: string[];
}

/** Body of PATCH /loops/{id}: a field that is absent is untouched, null clears the cap. */
export interface LoopBudgetPatch {
  token_budget?: number | null;
  max_review_rounds?: number | null;
  cost_budget_usd?: number | null;
}

export interface CloseResult {
  ticket_id: string;
  loop_id: string;
  item_status: string;
}

export interface Range {
  from?: string;
  to?: string;
}

const enc = encodeURIComponent;
function qs(params: Record<string, string | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) sp.set(k, v);
  const s = sp.toString();
  return s ? `?${s}` : '';
}

export function createReviewLoopApi(baseUrl: string, apiKey: string) {
  async function get<T>(path: string): Promise<T> {
    const resp = await fetch(`${baseUrl}${path}`, {
      headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    });
    if (!resp.ok) throw new ApiError(resp.status, await resp.text());
    return (await resp.json()) as T;
  }
  async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
    const resp = await fetch(`${baseUrl}${path}`, {
      method,
      headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) throw new ApiError(resp.status, await resp.text());
    return (await resp.json()) as T;
  }
  const base = (p: string) => `/api/v1/projects/${enc(p)}`;
  return {
    updateLoop: (projectId: string, loopId: string, body: LoopBudgetPatch) =>
      send<Loop>('PATCH', `${base(projectId)}/loops/${enc(loopId)}`, body),
    closeLoopItem: (projectId: string, loopId: string, ticketId: string) =>
      send<CloseResult>('POST', `${base(projectId)}/loops/${enc(loopId)}/children/${enc(ticketId)}/close`),
    listChecklist: (projectId: string) => get<ChecklistItem[]>(`${base(projectId)}/loop-checklist`),
    seedChecklist: (projectId: string) => send<SeedResult>('POST', `${base(projectId)}/loop-checklist/seed`),
    patchChecklistItem: (projectId: string, itemId: string, status: 'active' | 'retired') =>
      send<ChecklistItem>('PATCH', `${base(projectId)}/loop-checklist/${enc(itemId)}`, { status }),
    listLoops: (projectId: string) => get<Loop[]>(`${base(projectId)}/loops`),
    getInsights: (projectId: string, r: Range = {}) =>
      get<LoopInsights>(`${base(projectId)}/loop-insights${qs({ from: r.from, to: r.to })}`),
    getSpend: (projectId: string, groupBy: SpendGroupBy, r: Range = {}) =>
      get<ProjectSpend>(`${base(projectId)}/spend${qs({ group_by: groupBy, from: r.from, to: r.to })}`),
    getTicketSpend: (projectId: string, ticketId: string) =>
      get<TicketSpend>(`${base(projectId)}/tickets/${enc(ticketId)}/spend`),
    getReadiness: (projectId: string, ticketId: string, sha: string) =>
      get<Readiness>(`${base(projectId)}/tickets/${enc(ticketId)}/readiness${qs({ sha })}`),
    getStory: (projectId: string, ticketId: string) =>
      get<Story>(`${base(projectId)}/tickets/${enc(ticketId)}/story`),
  };
}
export type ReviewLoopApi = ReturnType<typeof createReviewLoopApi>;
