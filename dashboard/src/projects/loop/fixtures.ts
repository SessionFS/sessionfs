import type { ChecklistItem, Loop, LoopInsights, NeedsYouResponse, ReleaseList, ScoreboardRow, ShareLink, ShipReport, Story } from '../../api/reviewLoop';

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
  scoreboard: scoreboard(withUsd),
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
  prs: [{ id: 'pr_1', provider: 'github', repo: 'acme/app', number: 12, title: 'Record usage', state: 'merged', branch: 'feat/usage', head_sha: '3f9a2c1d44aa', merged_at: '2026-10-29T11:00:00Z', merge_commit_sha: 'ffff', external_url: 'https://github.com/acme/app/pull/12' }],
  releases: [{ version: 'v0.29.0', released_at: '2026-10-29T12:00:00Z' }],
  deployments: [{ environment: 'production', deployed_at: '2026-10-29T13:00:00Z', source: 'ci' }],
});

export const scoreboard = (withUsd = true): ScoreboardRow[] => [
  {
    implementer_model: 'deepseek-v4',
    reviewer_model: 'gpt-5.6',
    accepted_changes: 6,
    harmful_findings_per_accepted_change: 1.5,
    avg_rounds_to_clean: 2.5,
    cost_per_accepted_change: { tokens: 310_000, ...(withUsd ? { cost_usd: 0.41 } : {}) },
  },
  {
    implementer_model: 'claude-sonnet-5-5',
    reviewer_model: null,
    accepted_changes: 2,
    harmful_findings_per_accepted_change: 0,
    avg_rounds_to_clean: null,
    cost_per_accepted_change: { tokens: 520_000, ...(withUsd ? { cost_usd: 1.1 } : {}) },
  },
];

export const releaseList = (): ReleaseList => ({
  project_id: 'proj_1',
  releases: [
    { id: 'rel_2', project_id: 'proj_1', version: 'v0.29.0', tag_sha: 'abc', released_at: '2026-10-29T12:00:00Z', source: 'cli', created_at: '2026-10-29T12:00:00Z' },
    { id: 'rel_1', project_id: 'proj_1', version: 'v0.28.0', tag_sha: 'def', released_at: '2026-10-20T12:00:00Z', source: 'cli', created_at: '2026-10-20T12:00:00Z' },
  ],
});

export const shipReport = (withUsd = true): ShipReport => ({
  project_id: 'proj_1',
  version: 'v0.29.0',
  released_at: '2026-10-29T12:00:00Z',
  loops: 4,
  tickets: 9,
  review_rounds: 14,
  findings: 63,
  harmful: 19,
  harmful_by_harm: { 'data-exposed': 7, 'money-spent': 5, 'wrong-actor': 4, crash: 3 },
  all_fixed_before_merge: true,
  cost_per_accepted_change: { tokens: 310_000, ...(withUsd ? { cost_usd: 0.41 } : {}) },
  checklist_classes_learned: [
    { class: 'access-recheck', title: 'Access rechecked after a slow step', joined_at: '2026-10-14T00:00:00Z' },
    { class: 'existence-oracles', title: 'Existence oracles', joined_at: '2026-10-15T00:00:00Z' },
  ],
  gates: [
    { kind: 'tests', status: 'passed', url: null },
    { kind: 'lint', status: 'passed', url: null },
    { kind: 'security', status: 'failed', url: null },
    { kind: 'e2e', status: 'skipped', url: null },
  ],
  deployments: [{ environment: 'production', deployed_at: '2026-10-29T13:00:00Z', source: 'ci' }],
  prs: [
    { id: 'pr_1', provider: 'github', repo: 'acme/app', number: 12, title: 'Record usage', state: 'merged', external_url: 'https://github.com/acme/app/pull/12' },
    { id: 'pr_2', provider: 'github', repo: 'acme/app', number: 13, title: 'Bad link', state: 'merged', external_url: 'javascript:alert(1)' },
  ],
  markdown: '# Ship Report — v0.29.0\n\n- Loops: 4\n',
});

export const needsYou = (): NeedsYouResponse => ({
  items: [
    {
      kind: 'ready',
      project_id: 'proj_1',
      project_name: 'Acme Widgets',
      loop_id: 'tk_parent',
      ticket_id: 'tk_child1',
      ticket_title: 'Record token usage per run',
      checklist_item_id: null,
      title: 'Record token usage per run',
      reason: 'A trusted reviewer cleared commit 3f9a2c1',
      can_act: true,
    },
    {
      kind: 'needs_human',
      project_id: 'proj_1',
      project_name: 'Acme Widgets',
      loop_id: 'tk_parent',
      ticket_id: 'tk_child2',
      ticket_title: 'Expose spend endpoint',
      checklist_item_id: null,
      title: 'Expose spend endpoint',
      reason: 'Token budget hit (410,000 tokens spent)',
      can_act: false,
    },
    {
      kind: 'proposed_checklist',
      project_id: 'proj_2',
      project_name: 'Billing Service',
      loop_id: null,
      ticket_id: null,
      checklist_item_id: 'cl_1',
      title: 'Recheck access after any slow step',
      reason: 'Proposed by a reviewer in round 2',
      can_act: true,
    },
  ],
  counts: { ready: 1, needs_human: 1, proposed_checklist: 1, total: 3 },
});

export const shareLinks = (): ShareLink[] => [
  {
    id: 'lsl_1',
    kind: 'story',
    ticket_id: 'tk_child1',
    release_version: null,
    month: null,
    created_at: '2026-10-28T10:00:00Z',
    expires_at: '2036-11-27T10:00:00Z',
    revoked_at: null,
  },
  {
    id: 'lsl_2',
    kind: 'ship_report',
    ticket_id: null,
    release_version: 'v0.29.0',
    month: null,
    created_at: '2026-10-20T10:00:00Z',
    expires_at: '2026-10-27T10:00:00Z',
    revoked_at: null,
  },
];

/** The public ship report: no cost_usd, no identities. */
export const publicShipReport = (): ShipReport => shipReport(false);
