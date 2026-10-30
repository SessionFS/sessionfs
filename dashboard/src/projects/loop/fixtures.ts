import type { ChecklistItem, Loop, LoopInsights, Story } from '../../api/reviewLoop';

export const loops = (withUsd = true): Loop[] => [
  {
    id: 'tk_parent',
    title: 'Spend tracking for agent runs',
    children: [
      {
        ticket_id: 'tk_child1',
        title: 'Record token usage per run',
        status: 'in_review',
        branch: 'feat/usage',
        head_sha: '3f9a2c1d44',
        round: 3,
        harmful_open: 0,
        findings_by_harm: {},
        rounds: [
          { round: 1, harmful: 3 },
          { round: 2, harmful: 1 },
          { round: 3, harmful: 0 },
        ],
        tokens: 240_000,
        ...(withUsd ? { cost_usd: 1.4 } : {}),
        budget: { tokens: 400_000, ...(withUsd ? { cost_usd: 5 } : {}), max_rounds: 5 },
        needs_human: null,
        ready: true,
        verdict: { reviewer: 'codex-reviewer', reviewed_commit: '3f9a2c1d44aa', at: '2026-10-29T10:00:00Z' },
        not_ready_reasons: [],
      },
      {
        ticket_id: 'tk_child2',
        title: 'Expose spend endpoint',
        status: 'in_progress',
        branch: 'feat/spend-api',
        head_sha: '9b8e7d6c55',
        round: 2,
        harmful_open: 2,
        findings_by_harm: { 'data-exposed': 2 },
        rounds: [
          { round: 1, harmful: 4 },
          { round: 2, harmful: 2 },
        ],
        tokens: 410_000,
        ...(withUsd ? { cost_usd: 3.2 } : {}),
        budget: { tokens: 400_000, ...(withUsd ? { cost_usd: 5 } : {}), max_rounds: 3 },
        needs_human: 'token budget hit (410,000 tokens spent)',
        ready: false,
        verdict: null,
        not_ready_reasons: ['no trusted strict VERIFIED-CLEAN verdict', '2 open harmful finding(s)'],
      },
    ],
  },
];

export const insights = (withUsd = true): LoopInsights => ({
  caught: [
    { finding: 'Org key readable by members', ticket_id: 'tk_child2', run_id: 'run_a1', harm: 'data-exposed', class: 'access-recheck', file: 'src/routes/keys.py', line: 88, fixed_in_sha: 'abc1234def' },
    { finding: 'Retry loop double-charges', ticket_id: 'tk_child1', run_id: 'run_a2', harm: 'money-spent', class: 'idempotency', file: 'src/billing.py', line: 12, fixed_in_sha: null },
    { finding: 'Naming nit', ticket_id: 'tk_child1', run_id: 'run_a3', harm: 'other', class: 'style', file: null, line: null, fixed_in_sha: null },
  ],
  caught_total: 3,
  cost_per_accepted_change: [
    { period: '2026-09', tokens: 900_000, ...(withUsd ? { cost_usd: 1.4 } : {}) },
    { period: '2026-10', tokens: 250_000, ...(withUsd ? { cost_usd: 0.38 } : {}) },
  ],
  learning: [
    {
      class: 'access-recheck',
      title: 'Access rechecked after a slow step',
      joined_at: '2026-10-14T00:00:00Z',
      series: [
        { week: '2026-09-28', count: 2 },
        { week: '2026-10-05', count: 4 },
        { week: '2026-10-12', count: 1 },
        { week: '2026-10-19', count: 0 },
        { week: '2026-10-26', count: 0 },
      ],
    },
    {
      // Found in runs but never accepted onto the checklist: joined_at is null.
      class: 'idempotency',
      title: 'idempotency',
      joined_at: null,
      series: [
        { week: '2026-09-28', count: 0 },
        { week: '2026-10-05', count: 1 },
        { week: '2026-10-12', count: 2 },
        { week: '2026-10-19', count: 0 },
        { week: '2026-10-26', count: 1 },
      ],
    },
  ],
});

export const checklist = (): ChecklistItem[] => [
  {
    id: 'lci_1',
    project_id: 'proj_1',
    slug: 'access-rechecked-after-slow-steps',
    title: 'Access rechecked after slow steps',
    check_text: 'Re-run the write authorization after any await that can take seconds.',
    status: 'proposed',
    source: 'seed',
    times_found: 0,
    last_found_at: null,
    accepted_at: null,
    created_at: '2026-10-28T08:00:00Z',
    updated_at: '2026-10-28T08:00:00Z',
  },
  {
    id: 'lci_2',
    project_id: 'proj_1',
    slug: 'existence-oracles',
    title: 'Existence oracles',
    check_text: 'Every miss on one route returns the byte-identical 404 body.',
    status: 'active',
    source: 'finding',
    times_found: 3,
    last_found_at: '2026-10-20T08:00:00Z',
    accepted_at: '2026-10-14T00:00:00Z',
    created_at: '2026-10-10T08:00:00Z',
    updated_at: '2026-10-14T00:00:00Z',
  },
  {
    id: 'lci_3',
    project_id: 'proj_1',
    slug: 'usage-counted-on-every-path',
    title: 'Usage counted on every path',
    check_text: 'Record usage on every path that reached the provider.',
    status: 'retired',
    source: 'seed',
    times_found: 0,
    last_found_at: null,
    accepted_at: null,
    created_at: '2026-10-10T08:00:00Z',
    updated_at: '2026-10-15T00:00:00Z',
  },
];

export const story = (): Story => ({
  ticket_id: 'tk_child1',
  title: 'Record token usage per run',
  events: [
    { kind: 'brief', at: '2026-10-28T08:00:00Z', title: 'Record token usage per run' },
    { kind: 'run', at: '2026-10-28T08:10:00Z', role: 'implementer', run_id: 'run_w1', tokens: 120_000, title: 'build' },
    { kind: 'round', at: '2026-10-28T09:00:00Z', round: 1, title: 'NEEDS-FIXES', reviewer: 'codex-reviewer' },
    { kind: 'finding', at: '2026-10-28T09:05:00Z', run_id: 'run_r1', round: 1, harm: 'money-spent', class: 'idempotency', title: 'Retry loop double-charges', file: 'src/billing.py', line: 12 },
    { kind: 'fix', at: '2026-10-28T10:00:00Z', run_id: 'run_r1', round: 1, harm: 'money-spent', class: 'idempotency', sha: 'abc1234def', title: 'Retry loop double-charges' },
    { kind: 'verdict', at: '2026-10-29T10:00:00Z', reviewer: 'codex-reviewer', sha: '3f9a2c1d44aa', title: 'VERIFIED-CLEAN' },
    { kind: 'merge', at: '2026-10-29T11:00:00Z', title: 'Merged' },
  ],
});
