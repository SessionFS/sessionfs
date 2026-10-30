/**
 * Fixture data for the visual harness. Everything here is synthetic: fake
 * users on example.test, fake ids, no real project names or credentials.
 * Shapes follow the interfaces in src/api/client.ts.
 */

const NOW = '2026-10-20T12:00:00Z';
const EARLIER = '2026-10-18T09:30:00Z';
const OLD = '2026-09-02T08:15:00Z';

export const ORG_ID = 'org_fixture01';
export const PROJECT_ID = 'proj_fixture01';
export const PROJECT_REMOTE = 'acme/widgets';

export const me = {
  user_id: 'user_fixture01',
  email: 'dev@example.test',
  display_name: 'Dev Fixture',
  email_verified: true,
  tier: 'team',
  effective_tier: 'team',
  org_id: ORG_ID,
  org_name: 'Acme Fixtures',
  org_role: 'admin',
  created_at: OLD,
  last_client_version: '0.20.0',
  last_client_platform: 'darwin',
  last_client_device: 'laptop',
  last_sync_at: NOW,
  latest_version: '0.20.0',
  default_org_id: ORG_ID,
  capture_health: [
    { name: 'claude-code', health: 'ok', degraded_since: null, last_error: null, sessions_tracked: 42 },
    { name: 'codex', health: 'ok', degraded_since: null, last_error: null, sessions_tracked: 7 },
  ],
};

const tools = ['claude-code', 'codex', 'cursor', 'gemini', 'copilot'];
const titles = [
  'Refactor the billing webhook handler to be idempotent',
  'Investigate flaky integration test on the sync queue',
  'Add pagination to the sessions list endpoint with a deliberately long title that should truncate cleanly',
  'Wire the dashboard theme toggle through the shared util',
  'Spike: compare two approaches to knowledge entry decay',
  'Fix the off-by-one in the message pager',
  'Write migration for persona represented_user_id',
  'Review the marketplace publish dialog copy',
];

export const sessions = titles.map((title, i) => ({
  id: `ses_fixture${String(i + 1).padStart(2, '0')}`,
  title,
  alias: i === 1 ? 'flaky-sync' : null,
  tags: i % 3 === 0 ? ['backend', 'billing'] : [],
  source_tool: tools[i % tools.length],
  // A mix of plain, date-stamped and unknown (null) model ids for the filter.
  model_id: i === 4 ? 'claude-sonnet-4-6-20250929' : i === 5 ? null : i % 2 ? 'claude-sonnet-4' : 'gpt-5',
  message_count: 20 + i * 13,
  turn_count: 8 + i * 4,
  tool_use_count: 12 + i * 5,
  total_input_tokens: 40_000 + i * 12_345,
  total_output_tokens: 9_000 + i * 2_100,
  blob_size_bytes: 120_000 + i * 31_000,
  etag: `etag${i}`,
  created_at: i < 3 ? NOW : EARLIER,
  updated_at: i < 3 ? NOW : EARLIER,
  parent_session_id: null,
}));

export const sessionList = {
  sessions,
  total: sessions.length,
  page: 1,
  page_size: 20,
  has_more: false,
};

/** GET /api/v1/sessions/models: distinct model ids with counts (count desc,
 *  then id), null = the "Unknown model" bucket. */
export const sessionModels = {
  models: Object.entries(
    sessions.reduce<Record<string, number>>((acc, s) => {
      const key = s.model_id ?? '';
      acc[key] = (acc[key] ?? 0) + 1;
      return acc;
    }, {}),
  )
    .map(([id, count]) => ({ model_id: id === '' ? null : id, count }))
    .sort((a, b) => b.count - a.count || (a.model_id ?? '').localeCompare(b.model_id ?? '')),
};

export const projects = [
  {
    id: PROJECT_ID,
    name: 'Acme Widgets',
    git_remote_normalized: PROJECT_REMOTE,
    // Shaped like a real compiled context: a pinned block on top, one fact
    // per line, long ids and URLs, many bulleted sections.
    context_document: [
      '**Resume the main session:** `claude --resume 46ba40cb-d04a-4465-8222-9f3e679974f2`',
      'Session ID: `46ba40cb-d04a-4465-8222-9f3e679974f2` (owner request 2026-10-05).',
      'Keep this block at the TOP of this document.',
      'Start here: wiki `where-we-stopped` (current state, open work, owner rules).',
      '',
      '## Kubernetes migration — current state (2026-10-09; ticket tk_2a49cb8cd8e545c2)',
      '',
      'The backend is moving to the shared cluster (repo `acme/infra`, `k8s/widgets*`).',
      '**Dev copy LIVE:** https://widgets-dev.example.com (Firestore `dev-db`, sandbox payments).',
      '**Production:** https://widgets.example.com. Config merged but NOT switched on yet.',
      '',
      '> Real users stay on the old host until they install a build that points at the cluster.',
      '',
      '# Project Context',
      '',
      '## Known Issues & Workarounds',
      '',
      '- Scheduled jobs that should run every 10 min ran every 6–7 h (KB 3115).',
      '- SQLite is legacy; Firestore is the system of record (KB 3119).',
      '- Cluster secrets come from the vault via `create-secrets.py dev|prod`, never a committed `.env`.',
      '',
      '## Key Decisions',
      '',
      '| Decision | Why |',
      '|---|---|',
      '| Firestore over Postgres | Existing data and auth live there |',
      '| One worker per queue | Isolates slow payment callbacks |',
      '',
      '## Dependencies & Integrations',
      '',
      '- Payments: sandbox in dev, live keys only in the vault.',
      '- Push notifications through the build profile `cluster-dev`.',
      '',
      '## Recent Changes',
      '',
      '### 2026-10-23',
      '',
      '- Draft PR #237 turns the old schedule off in the same hour as cutover.',
      '',
      '### 2026-10-22',
      '',
      '- Mobile tests on the cluster dev copy use profile `cluster-dev`.',
    ].join('\n'),
    owner_id: me.user_id,
    org_id: ORG_ID,
    created_at: OLD,
    updated_at: NOW,
    session_count: 31,
    auto_narrative: true,
    repos: [
      {
        id: 'repo_fixture01',
        project_id: PROJECT_ID,
        git_remote_normalized: PROJECT_REMOTE,
        is_primary: true,
        verified: true,
        verification_method: 'github_app',
        provider: 'github',
        provider_repo_id: '1001',
        added_by_user_id: me.user_id,
        created_at: OLD,
      },
      {
        id: 'repo_fixture02',
        project_id: PROJECT_ID,
        git_remote_normalized: 'acme/widgets-infra',
        is_primary: false,
        verified: false,
        verification_method: 'owner_attested',
        provider: 'github',
        provider_repo_id: null,
        added_by_user_id: me.user_id,
        created_at: EARLIER,
      },
    ],
  },
  {
    id: 'proj_fixture02',
    name: 'Docs site',
    git_remote_normalized: 'acme/docs',
    context_document: '# Docs site\n\nPublic documentation.',
    owner_id: me.user_id,
    org_id: null,
    created_at: OLD,
    updated_at: EARLIER,
    session_count: 4,
    auto_narrative: false,
    repos: [],
  },
];

export const knowledgeEntries = [
  ['decision', 'Use optimistic concurrency (ETag + If-Match) for rules updates instead of locks.'],
  ['pattern', 'Every converter parses the native format into canonical .sfs, then writes the target.'],
  ['bug', 'SQLite WAL can corrupt after power loss; the index self-heals by rebuilding from .sfs files.'],
  ['convention', 'Ruff runs before every commit; CI fails on lint.'],
  ['discovery', 'The sidebar nav is duplicated for the mobile drawer, so landmarks need distinct labels.'],
  ['dependency', 'react-markdown renders project context; no custom parser.'],
].map(([entry_type, content], i) => ({
  id: i + 1,
  project_id: PROJECT_ID,
  session_id: sessions[i].id,
  entry_type,
  content,
  confidence: 0.6 + i * 0.06,
  created_at: EARLIER,
  compiled_at: i < 4 ? NOW : null,
  dismissed: false,
  claim_class: i < 4 ? 'claim' : 'note',
  freshness_class: i === 2 ? 'aging' : 'current',
  entity_ref: null,
  entity_type: null,
  superseded_by: null,
  supersession_reason: null,
  promoted_at: i < 4 ? NOW : null,
  retrieved_count: 3 + i,
  used_in_answer_count: i,
  compiled_count: i < 4 ? 2 : 0,
}));

export const projectHealth = {
  project_id: PROJECT_ID,
  total_entries: 6,
  pending_entries: 2,
  uncompiled_notes: 2,
  auto_promotable_evidence: 0,
  compiled_entries: 4,
  dismissed_entries: 0,
  total_compilations: 3,
  last_compilation_at: NOW,
  potentially_stale: false,
  recommendations: ['Compile to fold 2 pending notes into the context document.'],
  stale_entry_count: 0,
  low_confidence_count: 1,
  decayed_count: 0,
};

export const compilations = [
  { id: 3, entries_compiled: 4, context_before: '# Before', context_after: '# After', compiled_at: NOW },
  { id: 2, entries_compiled: 3, context_before: '# Older', context_after: '# Before', compiled_at: EARLIER },
];

export const wikiPages = [
  {
    id: 'page_1', slug: 'architecture', title: 'Architecture', page_type: 'article',
    content: '# Architecture\n\nAPI, worker and dashboard.', word_count: 120, entry_count: 4,
    auto_generated: true, updated_at: NOW,
  },
  {
    id: 'page_2', slug: 'concept-idempotency', title: 'Idempotency', page_type: 'concept',
    content: '# Idempotency\n\nWebhook handlers must be safe to retry.', word_count: 60, entry_count: 2,
    auto_generated: true, updated_at: EARLIER,
  },
];

export const personas = [
  {
    id: 'pers_1', project_id: PROJECT_ID, org_id: null, scope: 'project', name: 'atlas',
    role: 'Backend engineer', content: 'Owns the API, worker and migrations.',
    specializations: ['fastapi', 'postgres'], is_active: true, version: 3,
    created_by: me.user_id, represented_user_id: null, created_at: OLD, updated_at: NOW,
  },
  {
    id: 'pers_2', project_id: PROJECT_ID, org_id: null, scope: 'project', name: 'prism',
    role: 'Frontend engineer', content: 'Owns the dashboard and its tests.',
    specializations: ['react', 'tailwind'], is_active: true, version: 1,
    created_by: me.user_id, represented_user_id: null, created_at: OLD, updated_at: EARLIER,
  },
];

// GET /api/v1/projects/{id}/assignable-users
export const assignableUsers = {
  users: [
    { id: me.user_id, display_name: me.display_name, email: me.email, can_edit: true },
    { id: 'user_fixture02', display_name: 'Peer Fixture', email: 'peer@example.test', can_edit: true },
    { id: 'user_fixture03', display_name: null, email: 'lead@example.test', can_edit: true },
  ],
};

export const orgPersonas = [
  {
    id: 'pers_o1', project_id: null, org_id: ORG_ID, scope: 'org', name: 'sentinel',
    role: 'Security reviewer', content: 'Reviews auth and data-handling changes.',
    specializations: ['security'], is_active: true, version: 2,
    created_by: me.user_id, represented_user_id: null, created_at: OLD, updated_at: NOW,
  },
];

const statuses = ['suggested', 'open', 'in_progress', 'blocked', 'review', 'done'];
export const tickets = statuses.map((status, i) => ({
  id: `tk_fixture${i + 1}`,
  project_id: PROJECT_ID,
  title: [
    'Make the webhook handler idempotent',
    'Add retry budget to the sync worker',
    'Split ProjectDetail into section components',
    'Rotate the staging signing key',
    'Review marketplace publish copy',
    'Document the persona scope-lock protocol',
  ][i],
  description: 'Short description of the work, with enough text to wrap onto a second line in narrow viewports.',
  priority: ['low', 'medium', 'high', 'critical', 'medium', 'low'][i],
  // One ticket is assigned to a person (Peer Fixture); the rest to personas.
  assigned_to: i === 4 ? null : i % 2 ? 'atlas' : 'prism',
  assigned_user_id: i === 4 ? 'user_fixture02' : null,
  assigned_user: i === 4 ? { id: 'user_fixture02', display_name: 'Peer Fixture', email: 'peer@example.test' } : null,
  created_by_user_id: me.user_id,
  created_by_session_id: null,
  created_by_persona: null,
  status,
  kind: i === 3 ? 'issue' : 'task',
  parent_ticket_id: null,
  child_ticket_ids: [],
  context_refs: [],
  file_refs: ['src/billing/webhook.py'],
  related_sessions: [],
  acceptance_criteria: ['Replays do not double-charge', 'Covered by a regression test'],
  resolver_session_id: null,
  resolver_user_id: null,
  completion_notes: status === 'done' ? 'Shipped in the last release.' : null,
  changed_files: [],
  knowledge_entry_ids: [],
  depends_on: [],
  created_at: EARLIER,
  updated_at: NOW,
  resolved_at: status === 'done' ? NOW : null,
  // GitHub #113: the open and blocked tickets are past due, the in-progress
  // one is due later, the done one was due before it shipped.
  due_date: [null, '2026-10-01', '2026-12-15', '2026-09-30', null, '2026-10-10'][i],
  estimated_effort: [null, '3d', '2d 4h', null, '1d', '4h'][i],
  created_by: { user_id: me.user_id, display_name: me.display_name, email: me.email, persona: null, session_id: null },
  resolved_by: status === 'done' ? { user_id: me.user_id, display_name: me.display_name, email: me.email } : null,
}));

export const agentRuns = [
  {
    id: 'run_1', project_id: PROJECT_ID, persona_name: 'sentinel', tool: 'codex', trigger_source: 'ci',
    status: 'passed', ticket_id: null, trigger_ref: 'pr-128', ci_provider: 'github',
    ci_run_url: 'https://example.test/runs/1', result_summary: 'No findings.', severity: 'none',
    findings_count: 0, findings: [], fail_on: 'high', policy_result: 'pass', exit_code: 0,
    session_id: null, triggered_by_user_id: me.user_id, triggered_by_persona: null,
    replayed_from: null, prompt: null, context_refs: [],
    created_at: EARLIER, started_at: EARLIER, completed_at: NOW, duration_seconds: 94,
  },
  {
    id: 'run_2', project_id: PROJECT_ID, persona_name: 'sentinel', tool: 'codex', trigger_source: 'ci',
    status: 'failed', ticket_id: null, trigger_ref: 'pr-131', ci_provider: 'github',
    ci_run_url: 'https://example.test/runs/2', result_summary: '2 findings.', severity: 'high',
    findings_count: 2, findings: [{ title: 'Unvalidated redirect' }], fail_on: 'high', policy_result: 'fail',
    exit_code: 1, session_id: null, triggered_by_user_id: me.user_id, triggered_by_persona: null,
    replayed_from: null, prompt: null, context_refs: [],
    created_at: EARLIER, started_at: EARLIER, completed_at: NOW, duration_seconds: 131,
  },
];

export const rules = {
  rules: {
    static_rules: '# Project rules\n\n- Run ruff before committing.\n- Never mock the database in integration tests.',
    include_knowledge: true,
    knowledge_types: ['decision', 'convention'],
    knowledge_max_tokens: 2000,
    include_context: true,
    context_sections: ['architecture'],
    context_max_tokens: 4000,
    tool_overrides: {},
    enabled_tools: ['claude-code', 'codex', 'cursor'],
    version: 4,
    updated_at: NOW,
  },
  etag: 'rules-etag-4',
};

export const rulesVersions = [
  { version: 4, compiled_at: NOW, content_hash: 'sha256:4444aaaa', compiled_by: me.email },
  { version: 3, compiled_at: EARLIER, content_hash: 'sha256:3333bbbb', compiled_by: me.email },
];

function pkg(i: number, over: Record<string, unknown> = {}) {
  const names = ['commit-helper', 'migration-reviewer', 'release-notes', 'test-triage', 'pr-summarizer', 'incident-timeline'];
  const name = names[i % names.length];
  return {
    listing_id: `lst_fixture${i}`,
    collection: 'sessionfs',
    visibility: 'public',
    owner_user_id: null,
    org_id: null,
    owner_org_id: null,
    verification_state: i % 3 === 0 ? 'verified' : 'unverified',
    is_yanked: false,
    moderation_state: 'approved',
    publisher_id: 'pub_fixture01',
    publisher: { id: 'pub_fixture01', namespace: 'acme', display_name: 'Acme', owner_type: 'org', status: 'active', verified: true, badges: ['verified: acme.example.test'] },
    package_id: `spk_fixture${i}`,
    namespace: 'acme',
    name,
    display_name: name.split('-').map((w) => w[0].toUpperCase() + w.slice(1)).join(' '),
    description: 'A skill that helps with a common engineering chore. The description is long enough to wrap onto a second line in a narrow card.',
    version_id: `skv_fixture${i}`,
    version: `1.${i}.0`,
    content_hash: `sha256:${String(i).repeat(8)}`,
    source: { type: 'git', locator: 'https://example.test/acme/skills.git', subdir: name, commit: 'abc1234' },
    risk: { scripts: i % 2 === 0, mcp: false, hooks: false, executable_extensions: false, allowed_tools: [], hook_events: [] },
    compatibility: {
      'claude-code': { native_skills: true, instruction_files: 1, omitted_components: [] },
      codex: { native_skills: false, instruction_files: 1, omitted_components: ['hooks'] },
    },
    license: { declared: true, value: 'MIT' },
    signature: {},
    blob_size_bytes: 24_000 + i * 1_000,
    published_at: EARLIER,
    categories: ['workflow'],
    deprecation: null,
    freshness: { stale: false, unmaintained: false, deprecated: false, last_updated_at: EARLIER, computed_at: NOW, tools: {} },
    installs: { window_days: 30, total: { count: 120 + i, display: `${120 + i}` }, by_tool: {} },
    disclosure: null,
    ...over,
  };
}

export const marketplacePackages = {
  items: [
    pkg(0),
    pkg(1, {
      display_name: 'Database Migration Reviewer For Large Monorepos',
      risk: { scripts: true, mcp: false, hooks: true, executable_extensions: false, allowed_tools: ['allowed-tools:Bash(git diff:*)'], hook_events: ['PreToolUse'] },
    }),
    pkg(2, { visibility: 'org', org_id: ORG_ID, owner_org_id: ORG_ID, publisher: { id: 'pub_fixture02', namespace: 'acme-internal', display_name: 'Acme internal', owner_type: 'org', status: 'active', verified: false, badges: [] }, namespace: 'acme-internal' }),
    pkg(3, {
      deprecation: { deprecated_at: EARLIER, message: 'Use incident-timeline instead.', successor: { package_id: 'spk_fixture5', namespace: 'acme', name: 'incident-timeline', display_name: 'Incident Timeline' } },
      freshness: { stale: true, unmaintained: false, deprecated: true, last_updated_at: OLD, computed_at: NOW, tools: {} },
    }),
    pkg(4, { installs: { window_days: 30, total: { count: null, display: '<10' }, by_tool: {} }, description: 'Short one.' }),
    pkg(5),
  ],
  count: 6,
  has_more: false,
  next_cursor: null,
  sort: 'recent',
};

export const marketplaceCategories = [
  { slug: 'workflow', display_name: 'Workflow' },
  { slug: 'testing', display_name: 'Testing' },
  { slug: 'docs', display_name: 'Documentation' },
];

export const nativeCatalogs = {
  items: [
    {
      scope: 'public', scope_id: null, marketplace: 'sessionfs', clone_url: 'https://example.test/git/public/sessionfs.git',
      current_commit: 'abc1234', generated_at: NOW, plugin_count: 6, export_remote: null, mirror: null,
      tools: {
        'claude-code': {
          add: '/plugin marketplace add https://example.test/git/public/sessionfs.git',
          install: '/plugin install <plugin>@sessionfs',
        },
        codex: { cli: 'codex plugin marketplace add https://example.test/git/public/sessionfs.git' },
        gemini: { install: 'gemini extensions install https://example.test/git/public/plugins/<plugin>.git' },
        copilot: { add: 'copilot plugin marketplace add https://example.test/git/public/sessionfs.git' },
      },
    },
    {
      scope: 'org', scope_id: ORG_ID, marketplace: 'sessionfs-org-acme', clone_url: 'https://example.test/git/org/org_fixture01/catalog.git',
      current_commit: 'def5678', generated_at: NOW, plugin_count: 2, export_remote: null, mirror: null,
      tools: { 'claude-code': { add: '/plugin marketplace add https://example.test/git/org/org_fixture01/catalog.git' } },
    },
  ],
  credential_helper: 'sfs catalog credential-helper',
  vendor_docs_checked: '2026-10-01',
};

export const handoffInbox = {
  handoffs: [
    {
      id: 'hnd_fixture1', session_id: sessions[0].id, recipient_session_id: null,
      session_title: sessions[0].title, session_tool: 'claude-code', session_model_id: 'claude-sonnet-4',
      session_message_count: 48, session_total_tokens: 61_000, sender_email: 'lead@example.test',
      recipient_email: me.email, message: 'Picking this up tomorrow, the failing test is in the worker.',
      status: 'pending', created_at: NOW, claimed_at: null,
    },
    {
      id: 'hnd_fixture2', session_id: sessions[1].id, recipient_session_id: 'ses_copy1',
      session_title: sessions[1].title, session_tool: 'codex', session_model_id: 'gpt-5',
      session_message_count: 33, session_total_tokens: 40_000, sender_email: 'peer@example.test',
      recipient_email: me.email, message: null, status: 'claimed', created_at: EARLIER, claimed_at: NOW,
    },
  ],
  total: 2,
};

export const handoffSent = {
  handoffs: [
    {
      id: 'hnd_fixture3', session_id: sessions[2].id, recipient_session_id: null,
      session_title: sessions[2].title, session_tool: 'cursor', session_model_id: 'gpt-5',
      session_message_count: 21, session_total_tokens: 22_000, sender_email: me.email,
      recipient_email: 'peer@example.test', message: 'FYI, see the pagination work.',
      status: 'pending', created_at: EARLIER, claimed_at: null,
    },
  ],
  total: 1,
};

export const orgInfo = {
  org: {
    id: ORG_ID, name: 'Acme Fixtures', slug: 'acme-fixtures', tier: 'enterprise',
    seats_used: 3, seats_limit: 50, storage_limit_bytes: 107_374_182_400, storage_used_bytes: 5_368_709_120,
  },
  members: [
    { user_id: me.user_id, email: me.email, display_name: me.display_name, role: 'admin', joined_at: OLD },
    { user_id: 'user_fixture02', email: 'peer@example.test', display_name: 'Peer Fixture', role: 'member', joined_at: EARLIER },
    { user_id: 'user_fixture03', email: 'lead@example.test', display_name: null, role: 'owner', joined_at: OLD },
  ],
  current_user_role: 'admin',
};

export const orgInvites = {
  invites: [
    { id: 'inv_1', email: 'new@example.test', role: 'member', status: 'pending', invited_by: me.email, created_at: NOW, expires_at: '2026-10-30T12:00:00Z' },
  ],
};

export const orgSettings = {
  general: { kb_retention_days: 180, kb_max_context_words: 8000, kb_section_page_limit: 30 },
  // GET /orgs/{org}/settings is flat; the dashboard reads this key from the top.
  restrict_project_visibility: false,
  dlp: { enabled: true, mode: 'warn', patterns: ['secrets'], categories: ['secrets', 'phi'] },
};

export const ssoProvider = {
  id: 'idp_1', org_id: ORG_ID, protocol: 'oidc', display_name: 'Example IdP',
  issuer: 'https://idp.example.test', client_id: 'client-fixture', client_secret_ref: 'env:SSO_SECRET',
  allowed_scopes: ['openid', 'email'], enabled: true, enforced: false, created_at: OLD, updated_at: NOW,
};

export const ssoDomains = [
  { id: 'dom_1', domain: 'example.test', method: 'dns_txt', verification_token: 'sfs-verify-fixture', status: 'verified', txt_record: '_sfs-verify.example.test', verified_at: EARLIER },
];

export const billingStatus = {
  tier: 'team', storage_used_bytes: 5_368_709_120, storage_limit_bytes: 107_374_182_400,
  stripe_customer_id: 'cus_fixture', has_subscription: true, has_personal_subscription: false,
  is_org_member: true, org_role: 'admin', is_beta: false,
};

export const syncSettings = {
  autosync: 'selective', debounce_seconds: 30, max_member_bytes: 52_428_800,
  mode: 'selective',
};

export const syncStatus = {
  tier: 'team', session_count: 31, storage_used_bytes: 5_368_709_120, storage_limit_bytes: 107_374_182_400,
  last_sync_at: NOW,
};

export const judgeSettings = { provider: 'anthropic', model: 'claude-sonnet-4', key_set: true, base_url: null };

export const llmProviders = {
  providers: [
    {
      id: 'anthropic',
      label: 'Anthropic',
      api_style: 'anthropic',
      suggested_models: ['claude-sonnet-5-5', 'claude-haiku-5-5'],
      discovery: false,
      default_base_url: null,
    },
    {
      id: 'deepseek',
      label: 'DeepSeek',
      api_style: 'openai',
      suggested_models: ['deepseek-flash', 'deepseek-v4-pro'],
      discovery: true,
      default_base_url: 'https://api.deepseek.com',
    },
  ],
};

export const githubInstallation = {
  installation_id: 123, account_login: 'acme-fixtures', account_type: 'Organization',
  auto_comment: true, include_trust_score: true, include_session_links: false,
  ownership_verified: true, repo_verification_active: true, claim_available: true, install_available: true,
};

// The real GET also carries the stored webhook secret; the card never shows it.
export const gitlabSettings = { instance_url: 'https://gitlab.acme-fixtures.example', has_token: true };

export const dlpPolicy = { enabled: true, mode: 'warn', categories: ['secrets'], patterns: [] };

// ── Session helper settings (Assistant) ──

const helperDefaults = {
  max_output_tokens: 4096,
  temperature: 0,
  top_p: null,
  max_tool_steps: 8,
  reasoning_effort: null,
  answer_style: 'concise',
};

export const userHelperSettings = {
  settings: { ...helperDefaults, max_output_tokens: 6000, reasoning_effort: 'medium', answer_style: 'detailed' },
  instructions: 'Answer in British English. Show file paths relative to the repo root.',
  effective_settings: { ...helperDefaults, max_output_tokens: 6000, reasoning_effort: 'medium', answer_style: 'detailed' },
  usage: { input_tokens: 182_400, output_tokens: 23_950, total_tokens: 206_350 },
};

export const orgHelperSettingsAdmin = {
  role: 'admin',
  plan_includes_helper: true,
  effective_settings: { ...helperDefaults, max_output_tokens: 3000, temperature: 0.3, max_tool_steps: 6 },
  has_instructions: true,
  settings: {
    ...helperDefaults,
    max_output_tokens: 8000,
    temperature: 0.3,
    max_tool_steps: 6,
    ceiling_max_output_tokens: 3000,
    ceiling_max_tool_steps: null,
  },
  instructions:
    'Cite the newest session first. Use our ticket ids (tk_…) when you mention work items, and never paste secrets back even if a session contains them.',
  monthly_token_budget: 2_000_000,
  usage: {
    input_tokens: 1_310_000,
    output_tokens: 214_500,
    total_tokens: 1_524_500,
    per_member: [
      { user_id: me.user_id, input_tokens: 820_000, output_tokens: 131_000, total_tokens: 951_000 },
      { user_id: 'user_fixture02', input_tokens: 402_000, output_tokens: 70_500, total_tokens: 472_500 },
      { user_id: 'user_fixture03', input_tokens: 61_000, output_tokens: 9_800, total_tokens: 70_800 },
      { user_id: 'user_gone0001', input_tokens: 27_000, output_tokens: 3_200, total_tokens: 30_200 },
    ],
  },
};

export const orgHelperSettingsMember = {
  role: 'member',
  plan_includes_helper: true,
  effective_settings: orgHelperSettingsAdmin.effective_settings,
  has_instructions: true,
  usage: { input_tokens: 402_000, output_tokens: 70_500, total_tokens: 472_500 },
  budget_reached: false,
  budget_used_pct: 76,
};

export const orgLlmSettings = {
  provider: 'anthropic',
  model: 'claude-sonnet-5-5',
  has_api_key: true,
  key_hint: '7f3a',
  base_url: null,
  enabled: true,
  plan_includes_helper: true,
};

export const helperStatus = {
  enabled: true,
  key_source: 'user',
  provider: 'anthropic',
  model: 'claude-sonnet-5-5',
  effective_settings: userHelperSettings.effective_settings,
  has_org_instructions: true,
  has_personal_instructions: true,
};

// ── Jira integration (org settings → Integrations) ──

export const jiraConnection = {
  connected: true,
  has_token: true,
  site_url: 'https://acme.atlassian.net',
  account_email: 'jira@acme.example',
  token_hint: '9c2e',
  default_template: 'agile',
  last_verified_at: NOW,
  sync_status: true,
  sync_comments: 'both',
  status_map: {
    transitions: {
      open: 'To Do', in_progress: 'In Progress', blocked: 'none', review: 'Submit for Review',
      done: 'Done', cancelled: 'none', closed: 'Done',
    },
    categories: { new: 'none', indeterminate: 'start', done: 'resolve' },
  },
  has_webhook_secret: true,
  webhook_secret_last4: 'b7e1',
  webhook_url: 'https://api.example.test/api/v1/webhooks/jira/org_fixture01',
  sprint_field_id: 'customfield_10020',
  epic_link_field_id: null,
};

// POST /api/v1/orgs/{id}/jira/fields/detect
export const jiraDetectedFields = { sprint_field_id: 'customfield_10020', epic_link_field_id: 'customfield_10014' };

export const jiraHealth = {
  unmapped_project_count: 1,
  last_push_failures: [],
  stale_links: [],
  publishes_in_progress: [],
  publishes_unknown: [],
  last_inbound_at: EARLIER,
  last_synced_at: NOW,
  failed_sync_events_24h: 1,
  webhook_auth_failures_24h: 0,
};

// GET /api/v1/orgs/{id}/integrations/jira/mappings
export const jiraMappings = [
  { project_id: PROJECT_ID, jira_project_key: 'WID', issue_type: 'Story', template: 'design-lite', done_status_name: 'Done' },
];

// One org-custom story template, listed after the built-ins by the server.
export const jiraCustomTemplates = [
  {
    slug: 'design-lite',
    name: 'Design Lite',
    description: 'Short stories for UI polish work.',
    sections: [
      { key: 'summary', heading: 'Summary', mode: 'rewrite', source_fields: ['title', 'description'], guidance: null },
      { key: 'criteria', heading: 'Acceptance criteria', mode: 'verbatim', source_fields: ['acceptance_criteria'], guidance: null },
    ],
    version: 3,
    builtin: false,
    archived_at: null,
    created_at: OLD,
    updated_at: EARLIER,
    created_by_user_id: me.user_id,
    updated_by_user_id: me.user_id,
  },
];

export const confluenceConnection = {
  connected: true,
  provider: 'confluence_cloud',
  has_token: true,
  site_url: 'https://acme.atlassian.net',
  account_email: 'confluence@acme.example',
  token_hint: '4f1a',
  default_template: 'reference',
  batch_window_minutes: 15,
  enabled: true,
  last_verified_at: NOW,
};

export const confluenceMappings = [
  {
    project_id: PROJECT_ID,
    space_key: 'ENG',
    parent_page_id: '65538',
    template: 'architecture',
    publish_context: true,
    auto_publish_pages: false,
  },
];

export const confluenceHealth = {
  unmapped_project_count: 1,
  last_push_failures: [
    {
      project_id: PROJECT_ID,
      source_kind: 'wiki_page',
      source_key: 'concept-idempotency',
      failed_at: NOW,
      reason: 'Confluence is unreachable right now',
    },
  ],
  publishes_in_progress: [],
  publishes_unknown: [],
  drifted_links: [
    { project_id: PROJECT_ID, source_kind: 'wiki_page', source_key: 'architecture', page_id: '98311', stored_version: 4 },
  ],
};

export const confluenceLink = {
  id: 'cl_fixture01',
  source_kind: 'wiki_page',
  source_key: 'architecture',
  page_id: '98311',
  page_url: 'https://acme.atlassian.net/wiki/spaces/ENG/pages/98311/Architecture',
  version: 4,
  status: 'linked',
  publishing: false,
  stale: false,
  template_slug: 'architecture',
  template_version: null,
  last_pushed_at: EARLIER,
  last_push_failed_at: null,
  last_push_failure: null,
};

export const confluenceLinks = {
  links: [confluenceLink],
  mapping: { space_key: 'ENG', template: 'architecture', publish_context: true, auto_publish_pages: false },
};

export const confluencePreview = {
  title: 'Architecture',
  sections: [
    {
      key: 'summary',
      heading: 'Summary',
      mode: 'rewrite',
      paragraphs: [
        {
          text: 'The API, worker and dashboard deploy together; the worker drains one queue per payment provider so slow callbacks never block checkout.',
          citations: ['kb:3115', 'kb:3119'],
        },
      ],
    },
    {
      key: 'source',
      heading: 'Source page',
      mode: 'verbatim',
      paragraphs: [{ text: 'API, worker and dashboard.', citations: [] }],
    },
  ],
  citations: ['kb:3115', 'kb:3119'],
  llm_used: false,
  key_source: 'none',
  project_id: PROJECT_ID,
  source_kind: 'wiki_page',
  source_key: 'architecture',
  template: { slug: 'architecture', name: 'Architecture', version: null },
  dropped: [
    {
      section: 'summary',
      text: 'Checkout latency dropped by forty percent after the queue split.',
      reason: 'missing citation',
    },
  ],
  notes: ['No model key is configured; rewrite sections use the source text verbatim.'],
  link: confluenceLink,
  existing: {
    page_id: '98311',
    page_url: confluenceLink.page_url,
    version: 4,
    template_slug: 'architecture',
    template_version: null,
  },
};

export const confluenceDriftError = {
  error: {
    code: '409',
    message: 'The Confluence page was edited since the last publish',
    details: {
      error: 'confluence_page_drifted',
      link_id: 'cl_fixture01',
      page_id: '98311',
      stored_version: 4,
      remote_version: 6,
      diff: [
        '--- last published (v4)',
        '+++ confluence (v6)',
        '@@ -1,4 +1,5 @@',
        ' # Architecture',
        '-API, worker and dashboard.',
        '+API, worker, dashboard and the new reporting service, which reads from a nightly replica of the primary database so analysts never touch production.',
        '+Owner: platform team.',
        ' ',
        ' ## Deploys',
      ],
      diff_truncated: true,
    },
  },
};

/** A large Jira site: the real report had ~500 projects, A* first. */
export const jiraProjectKeys = [
  'AACD', 'AAI', 'AASPD2U', 'ACMTE', 'ACN2S', 'ACRD', 'AD', 'ADPI', 'AIDR', 'AIGPCI', 'AIML', 'AIOPS',
  'AKITABOX', 'ALGOSEC', 'AMFASSPR', 'AMLZ1T2', 'AMY',
  ...Array.from({ length: 480 }, (_, i) => `B${String(i).padStart(3, '0')}`),
  'ZULU',
];

export function jiraProjectPage(q: string, limit = 50) {
  const needle = q.trim().toLowerCase();
  const name = (k: string) => (k.startsWith('AI') ? `AI platform ${k}` : `Team ${k}`);
  const matched = jiraProjectKeys.filter(
    (k) => !needle || k.toLowerCase().includes(needle) || name(k).toLowerCase().includes(needle),
  );
  return {
    projects: matched.slice(0, limit).map((k) => ({ key: k, name: name(k), issue_types: [] })),
    has_more: matched.length > limit,
    query: needle || null,
    limit,
  };
}

// ── project groups (GitHub #101) ──
export const GROUP_ID = 'pg_fixture01';

export const projectGroups = [
  {
    id: GROUP_ID, org_id: ORG_ID, slug: 'payments-platform', name: 'Payments platform',
    description: 'Services that move money: billing, checkout and the ledger. Editors here own on-call.',
    member_count: 3, project_count: 2, my_role: 'admin',
  },
  {
    id: 'pg_fixture02', org_id: ORG_ID, slug: 'design-system', name: 'Design system',
    description: null, member_count: 1, project_count: 0, my_role: 'admin',
  },
  {
    id: 'pg_fixture03', org_id: ORG_ID, slug: 'mobile-apps-and-a-deliberately-long-slug-for-truncation',
    name: 'Mobile apps (iOS, Android and the shared React Native modules)',
    description: 'Long names and descriptions should wrap or clamp without pushing the table wider than the page.',
    member_count: 12, project_count: 7, my_role: 'admin',
  },
];

export const projectGroupDetail = {
  id: GROUP_ID, org_id: ORG_ID, slug: 'payments-platform', name: 'Payments platform',
  description: 'Services that move money: billing, checkout and the ledger. Editors here own on-call.',
  my_role: 'admin',
  members: [
    { user_id: 'user_fixture02', email: 'peer@example.test', role: 'editor', granted_by_user_id: me.user_id, created_at: EARLIER },
    { user_id: me.user_id, email: me.email, role: 'admin', granted_by_user_id: me.user_id, created_at: OLD },
    { user_id: 'user_fixture04', email: 'a-very-long-contractor-address@subsidiary.example.test', role: 'reader', granted_by_user_id: me.user_id, created_at: NOW },
  ],
  projects: [PROJECT_ID, 'proj_fixture_hidden'],
};

export const myGroups = [projectGroups[0], { ...projectGroups[1], my_role: 'reader' }];

// GET /api/v1/orgs — the caller's memberships, wrapped in `orgs` like the
// server's MyOrgsResponse.
export const myOrgs = { orgs: [{ org_id: ORG_ID, name: 'Acme Fixtures', role: 'admin' }] };

// GET /api/v1/projects/{id}/compile-status
export const compileStatus = {
  last_compile_at: NOW,
  pending_claims: 2,
  compile_needed: false,
  compile_schedule: 'nightly',
  compile_after_claims: null,
  last_scheduled_compile_at: NOW,
  next_scheduled_at: '2026-10-21T02:00:00Z',
  compile_history: [
    { id: 12, compiled_at: NOW, trigger: 'scheduled', entries_compiled: 6, context_words_before: 1180, context_words_after: 1240 },
    { id: 11, compiled_at: EARLIER, trigger: 'manual', entries_compiled: 3, context_words_before: 1100, context_words_after: 1180 },
  ],
};

// GET /api/v1/projects/{id}/rules/versions/{n}
export const rulesVersionDetail = (version: number) => ({
  id: `rv_fixture${version}`,
  rules_id: 'rules_fixture01',
  version,
  static_rules: rules.rules.static_rules,
  compiled_outputs: { 'CLAUDE.md': `${rules.rules.static_rules}\n` },
  knowledge_snapshot: [],
  context_snapshot: {},
  compiled_at: NOW,
  compiled_by: me.email,
  content_hash: `sha256:${String(version).repeat(4)}aaaa`,
});

// Built-in template lists, as the server returns them (generated from
// integrations/trackers/templates.py and integrations/docs/templates.py).
export const jiraBuiltinTemplates = [
  {
    "slug": "agile",
    "name": "Agile Standard",
    "description": null,
    "sections": [
      {
        "key": "summary",
        "heading": "Summary",
        "mode": "rewrite",
        "source_fields": [
          "title",
          "description",
          "priority",
          "kind"
        ],
        "guidance": null
      },
      {
        "key": "acceptance_criteria",
        "heading": "Acceptance Criteria",
        "mode": "verbatim",
        "source_fields": [
          "acceptance_criteria"
        ],
        "guidance": null
      },
      {
        "key": "context",
        "heading": "Context & Prior Work",
        "mode": "rewrite",
        "source_fields": [
          "description",
          "linked_sessions",
          "completion_notes"
        ],
        "guidance": null
      }
    ],
    "version": null,
    "builtin": true,
    "archived_at": null,
    "created_at": null,
    "updated_at": null,
    "created_by_user_id": null,
    "updated_by_user_id": null
  },
  {
    "slug": "compliance",
    "name": "Enterprise / Compliance",
    "description": null,
    "sections": [
      {
        "key": "summary",
        "heading": "Summary",
        "mode": "rewrite",
        "source_fields": [
          "title",
          "description"
        ],
        "guidance": null
      },
      {
        "key": "acceptance_criteria",
        "heading": "Acceptance Criteria",
        "mode": "verbatim",
        "source_fields": [
          "acceptance_criteria"
        ],
        "guidance": null
      },
      {
        "key": "impact",
        "heading": "Impact & Risk",
        "mode": "rewrite",
        "source_fields": [
          "description",
          "priority",
          "linked_sessions"
        ],
        "guidance": null
      },
      {
        "key": "evidence",
        "heading": "Completion Notes",
        "mode": "verbatim",
        "source_fields": [
          "completion_notes"
        ],
        "guidance": null
      }
    ],
    "version": null,
    "builtin": true,
    "archived_at": null,
    "created_at": null,
    "updated_at": null,
    "created_by_user_id": null,
    "updated_by_user_id": null
  },
  {
    "slug": "sre",
    "name": "Platform / SRE",
    "description": null,
    "sections": [
      {
        "key": "summary",
        "heading": "Summary",
        "mode": "rewrite",
        "source_fields": [
          "title",
          "description"
        ],
        "guidance": null
      },
      {
        "key": "acceptance_criteria",
        "heading": "Acceptance Criteria",
        "mode": "verbatim",
        "source_fields": [
          "acceptance_criteria"
        ],
        "guidance": null
      },
      {
        "key": "impact",
        "heading": "Blast Radius & Impact",
        "mode": "rewrite",
        "source_fields": [
          "description",
          "priority"
        ],
        "guidance": null
      },
      {
        "key": "related_sessions",
        "heading": "Related Sessions",
        "mode": "verbatim",
        "source_fields": [
          "linked_sessions"
        ],
        "guidance": null
      }
    ],
    "version": null,
    "builtin": true,
    "archived_at": null,
    "created_at": null,
    "updated_at": null,
    "created_by_user_id": null,
    "updated_by_user_id": null
  },
  {
    "slug": "bug",
    "name": "Bug Report",
    "description": null,
    "sections": [
      {
        "key": "summary",
        "heading": "Summary",
        "mode": "rewrite",
        "source_fields": [
          "title",
          "description"
        ],
        "guidance": null
      },
      {
        "key": "acceptance_criteria",
        "heading": "Acceptance Criteria",
        "mode": "verbatim",
        "source_fields": [
          "acceptance_criteria"
        ],
        "guidance": null
      },
      {
        "key": "reproduction",
        "heading": "Reproduction & Observed Behavior",
        "mode": "rewrite",
        "source_fields": [
          "description",
          "linked_sessions"
        ],
        "guidance": null
      },
      {
        "key": "notes",
        "heading": "Completion Notes",
        "mode": "verbatim",
        "source_fields": [
          "completion_notes"
        ],
        "guidance": null
      }
    ],
    "version": null,
    "builtin": true,
    "archived_at": null,
    "created_at": null,
    "updated_at": null,
    "created_by_user_id": null,
    "updated_by_user_id": null
  },
  {
    "slug": "design",
    "name": "Design / UX",
    "description": null,
    "sections": [
      {
        "key": "user_journey",
        "heading": "User Journey",
        "mode": "rewrite",
        "source_fields": [
          "description"
        ],
        "guidance": null
      },
      {
        "key": "hypothesis",
        "heading": "Hypothesis",
        "mode": "rewrite",
        "source_fields": [
          "description"
        ],
        "guidance": null
      },
      {
        "key": "success_metric",
        "heading": "Success Metric",
        "mode": "verbatim",
        "source_fields": [
          "acceptance_criteria"
        ],
        "guidance": null
      },
      {
        "key": "context",
        "heading": "Context",
        "mode": "rewrite",
        "source_fields": [
          "description",
          "linked_sessions"
        ],
        "guidance": null
      }
    ],
    "version": null,
    "builtin": true,
    "archived_at": null,
    "created_at": null,
    "updated_at": null,
    "created_by_user_id": null,
    "updated_by_user_id": null
  }
];

export const confluenceBuiltinTemplates = [
  {
    "slug": "reference",
    "name": "Reference",
    "version": null,
    "builtin": true,
    "sections": [
      {
        "key": "overview",
        "heading": "Overview",
        "mode": "rewrite",
        "source_fields": [
          "page_body"
        ],
        "guidance": null
      },
      {
        "key": "details",
        "heading": "Details",
        "mode": "rewrite",
        "source_fields": [
          "page_body",
          "sections"
        ],
        "guidance": null
      },
      {
        "key": "claims",
        "heading": "Claims",
        "mode": "verbatim",
        "source_fields": [
          "claims_by_type"
        ],
        "guidance": null
      },
      {
        "key": "evidence",
        "heading": "Session Evidence",
        "mode": "verbatim",
        "source_fields": [
          "session_evidence"
        ],
        "guidance": null
      },
      {
        "key": "related",
        "heading": "Related",
        "mode": "verbatim",
        "source_fields": [
          "backlinks"
        ],
        "guidance": null
      }
    ]
  },
  {
    "slug": "architecture",
    "name": "Architecture",
    "version": null,
    "builtin": true,
    "sections": [
      {
        "key": "overview",
        "heading": "Overview",
        "mode": "rewrite",
        "source_fields": [
          "page_body",
          "sections"
        ],
        "guidance": null
      },
      {
        "key": "components",
        "heading": "Components",
        "mode": "rewrite",
        "source_fields": [
          "sections"
        ],
        "guidance": null
      },
      {
        "key": "decisions",
        "heading": "Key Decisions",
        "mode": "verbatim",
        "source_fields": [
          "claims_by_type"
        ],
        "guidance": null
      },
      {
        "key": "related",
        "heading": "Related",
        "mode": "verbatim",
        "source_fields": [
          "backlinks"
        ],
        "guidance": null
      }
    ]
  },
  {
    "slug": "runbook",
    "name": "Runbook",
    "version": null,
    "builtin": true,
    "sections": [
      {
        "key": "overview",
        "heading": "Overview",
        "mode": "rewrite",
        "source_fields": [
          "page_body"
        ],
        "guidance": null
      },
      {
        "key": "steps",
        "heading": "Steps",
        "mode": "verbatim",
        "source_fields": [
          "page_body",
          "sections"
        ],
        "guidance": null
      },
      {
        "key": "known_issues",
        "heading": "Known Issues & Workarounds",
        "mode": "verbatim",
        "source_fields": [
          "claims_by_type"
        ],
        "guidance": null
      },
      {
        "key": "evidence",
        "heading": "Session Evidence",
        "mode": "verbatim",
        "source_fields": [
          "session_evidence"
        ],
        "guidance": null
      }
    ]
  },
  {
    "slug": "onboarding",
    "name": "Onboarding",
    "version": null,
    "builtin": true,
    "sections": [
      {
        "key": "overview",
        "heading": "Overview",
        "mode": "rewrite",
        "source_fields": [
          "page_body",
          "sections"
        ],
        "guidance": null
      },
      {
        "key": "conventions",
        "heading": "Coding Conventions",
        "mode": "verbatim",
        "source_fields": [
          "claims_by_type"
        ],
        "guidance": null
      },
      {
        "key": "evidence",
        "heading": "Session Evidence",
        "mode": "verbatim",
        "source_fields": [
          "session_evidence"
        ],
        "guidance": null
      },
      {
        "key": "related",
        "heading": "Related",
        "mode": "verbatim",
        "source_fields": [
          "backlinks"
        ],
        "guidance": null
      }
    ]
  },
  {
    "slug": "concept",
    "name": "Concept",
    "version": null,
    "builtin": true,
    "sections": [
      {
        "key": "overview",
        "heading": "Overview",
        "mode": "rewrite",
        "source_fields": [
          "page_body"
        ],
        "guidance": null
      },
      {
        "key": "context",
        "heading": "Context",
        "mode": "rewrite",
        "source_fields": [
          "sections",
          "compile_metadata"
        ],
        "guidance": null
      },
      {
        "key": "related",
        "heading": "Related",
        "mode": "verbatim",
        "source_fields": [
          "backlinks"
        ],
        "guidance": null
      }
    ]
  }
];
// ── marketplace detail, managed library, publishers, moderation ──

export const marketplaceWorksIn = {
  version_id: 'skv_fixture1',
  content_hash: 'sha256:11111111',
  vendor_docs_checked: '2026-10-01',
  tools: {
    'claude-code': {
      badge: 'verified',
      levels: { L1: { status: 'pass', tool_version: '2.1.240', run_at: NOW, reason: 'ok', projection_hash: null } },
    },
    codex: {
      badge: 'format-valid',
      levels: { L0: { status: 'pass', tool_version: 'docs-2026-10-01', run_at: NOW, reason: 'ok', projection_hash: null } },
    },
  },
  expected_only: { gemini: { badge: 'expected', omitted_components: ['hooks'] } },
};

export const marketplaceDisclosure = {
  version_id: 'skv_fixture1',
  content_hash: 'sha256:11111111',
  capability_digest: 'sha256:cap0001',
  capabilities: ['instructions', 'scripts', 'hooks'],
  provenance: { publisher_verified: true, attested: false },
  scan: { ruleset: '2026.10.6', max_severity: 'low', finding_count: 1, counts: { low: 1 } },
  unicode: { flagged_files: 0, findings: [], counts: {} },
  shell_commands: [{ path: 'skills/migration-reviewer/SKILL.md', line: 12, command: 'git', commands: ['git'], text: 'git diff --stat origin/main' }],
  network_endpoints: [],
  env_names: [],
  mcp_servers: { local: [], remote: [], declared: false },
  allowed_tools: ['allowed-tools:Bash(git diff:*)'],
  persistence_targets: [],
  executable_extensions: [],
  tools: {
    'claude-code': {
      delivery: 'native', scope: 'project', summary: 'Installs 1 skill with 1 script and 1 hook.',
      writes: ['.claude/skills/migration-reviewer/SKILL.md', '.claude/skills/migration-reviewer/scripts/check.sh'], writes_total: 2,
      runs: { scripts: 1, hooks: [{ skill: 'migration-reviewer', event: 'PreToolUse', matcher: 'Bash' }], mcp_local: 0 },
      connects_to: [], auto_allowed: ['Bash(git diff:*)'], omitted: [], consent_required: ['skill-scripts', 'skill-hooks'],
    },
  },
  consent_required: ['instructions', 'skill-scripts', 'skill-hooks'],
  consent_rule_version: 2,
  card_version: 2,
  scope: 'project',
  disclosure_digest: 'sha256:disc0001',
};

export const skillRegistries = {
  items: [
    {
      id: 'reg_fixture1', owner_type: 'org', owner_id: ORG_ID, org_id: ORG_ID, alias: 'acme-skills',
      source_url: 'https://example.test/acme/skills.git', refresh_commit: null, policy_state: 'approved',
      server_fetch_enabled: true, created_at: OLD, updated_at: NOW, poll_interval_seconds: 3600, tag_pattern: 'v*',
      branch_only_mode: false, last_poll_at: '2026-10-20T11:00:00Z', last_poll_status: 'ok', consecutive_failures: 0,
      next_poll_at: '2026-10-20T13:00:00Z', backoff: false,
    },
    {
      id: 'reg_fixture2', owner_type: 'org', owner_id: ORG_ID, org_id: ORG_ID, alias: 'platform-team',
      source_url: 'https://example.test/acme/platform-skills.git', refresh_commit: null, policy_state: 'review',
      server_fetch_enabled: true, created_at: OLD, updated_at: NOW, poll_interval_seconds: 3600, tag_pattern: 'release-*',
      branch_only_mode: false, last_poll_at: '2026-10-20T10:00:00Z', last_poll_status: 'partial',
      last_poll_error_summary: 'release-2.3: package refused (scanner found a critical issue)', consecutive_failures: 0, backoff: false,
    },
  ],
};

export const effectiveAssignments = {
  items: [
    { id: 'asg_fixture1', org_id: ORG_ID, version_id: 'skv_fixture0', content_hash: 'sha256:00000000', target_type: 'org', target_id: ORG_ID, target_user_id: null, mode: 'required', target_tools: ['claude-code', 'codex'], policy: {}, created_at: OLD },
    { id: 'asg_fixture2', org_id: ORG_ID, version_id: 'skv_fixture5', content_hash: 'sha256:55555555', target_type: 'user', target_id: 'user_fixture01', target_user_id: 'user_fixture01', mode: 'optional', target_tools: [], policy: {}, created_at: OLD },
  ],
  count: 2,
};

export const myPublishers = {
  items: [
    {
      id: 'pub_fixture01', namespace: 'acme', display_name: 'Acme', owner_type: 'org', status: 'active', verified: true,
      badges: ['verified: acme.example.test'], org_id: ORG_ID, created_at: OLD, public_publishing: true,
      verifications: [
        { id: 'pv_1', method: 'dns_txt', subject: 'acme.example.test', subject_display: 'acme.example.test', status: 'verified', status_reason: null, badge: 'verified: acme.example.test', verified_at: OLD, last_checked_at: NOW, last_success_at: NOW, consecutive_failures: 0, github_installation_id: null, reused_org_domain_verification: false },
        { id: 'pv_2', method: 'dns_txt', subject: 'acme-tools.example.test', subject_display: 'acme-tools.example.test', status: 'pending', status_reason: null, badge: null, verified_at: null, last_checked_at: null, last_success_at: null, consecutive_failures: 0, github_installation_id: null, reused_org_domain_verification: false, txt_record: 'sessionfs-verification=fixture0123456789abcdef', txt_host: '_sessionfs.acme-tools.example.test' },
      ],
      trusted_publishers: [
        { id: 'tp_1', provider: 'github', repository: 'acme/skills', repository_id: 101, repository_owner_id: 7, workflow: 'release.yml', reusable_workflow: null, allow_workflow_run: false, environment: 'release', ref_pattern: 'refs/tags/v*', package_name: null, created_at: OLD, revoked_at: null },
      ],
    },
  ],
  count: 1,
};

export const publisherListings = {
  items: [
    { ...pkg(0), moderation_state: 'approved' },
    { ...pkg(1), visibility: 'public', moderation_state: 'pending_review', moderation_reason: null },
  ],
  count: 2,
};

export const marketplaceReports = {
  items: [
    { id: 'spr_fixture1', package_id: 'spk_fixture1', version_id: 'skv_fixture1', reason: 'malicious', details: 'The hook runs git push on every tool call, which is not mentioned in the description.', status: 'open', created_at: EARLIER, resolved_at: null, reporter_user_id: 'user_fixture02', scope_kind: 'platform', scope_org_id: null },
    { id: 'spr_fixture2', package_id: 'spk_fixture4', version_id: null, reason: 'spam', details: '', status: 'open', created_at: OLD, resolved_at: null, reporter_user_id: 'user_fixture03', scope_kind: 'org', scope_org_id: ORG_ID },
  ],
  count: 2,
  has_more: false,
  next_cursor: null,
};

// GET /api/v1/me/ticket-notifications — the inbox list (TicketNotificationList).
export const ticketNotifications = {
  items: [
    {
      id: 104, ticket_id: tickets[4].id, project_id: PROJECT_ID, ticket_title: tickets[4].title,
      kind: 'your_turn', summary: 'Ready for your review', actor_type: 'user', actor_user_id: 'usr_peer01',
      service_key_name: null, event_count: 1, created_at: NOW, updated_at: NOW, read_at: null,
    },
    {
      id: 103, ticket_id: tickets[1].id, project_id: PROJECT_ID, ticket_title: tickets[1].title,
      kind: 'review_verdict', summary: 'Reviewer posted VERIFIED-CLEAN', actor_type: 'service_key', actor_user_id: null,
      service_key_name: 'ci-reviewer', event_count: 2, created_at: EARLIER, updated_at: NOW, read_at: null,
    },
    {
      id: 102, ticket_id: tickets[2].id, project_id: PROJECT_ID, ticket_title: tickets[2].title,
      kind: 'comment', summary: '3 new comments', actor_type: 'user', actor_user_id: 'usr_peer01',
      service_key_name: null, event_count: 3, created_at: EARLIER, updated_at: EARLIER, read_at: null,
    },
    {
      id: 101, ticket_id: tickets[3].id, project_id: PROJECT_ID, ticket_title: tickets[3].title,
      kind: 'dependency_unblocked', summary: 'A dependency was completed', actor_type: 'user', actor_user_id: me.user_id,
      service_key_name: null, event_count: 1, created_at: EARLIER, updated_at: EARLIER, read_at: EARLIER,
    },
  ],
  next_before: null,
};

// GET /api/v1/orgs/{id}/integrations/jira/sync-events — recent two-way sync
// deliveries (JiraSyncEventsPage).
export const jiraSyncEvents = {
  events: [
    {
      id: 'jse_fixture03', ticket_id: tickets[1].id, issue_key: 'WID-42', direction: 'outbound', kind: 'status',
      payload: { to_status: 'in_progress', transition: 'Start Progress' }, jira_event_id: null, applied: true, error: null, created_at: NOW,
      state: 'done', retry_count: 0, next_attempt_at: null, updated_at: NOW,
    },
    {
      id: 'jse_fixture02', ticket_id: tickets[2].id, issue_key: 'WID-43', direction: 'inbound', kind: 'comment',
      payload: { author: 'Jira user' }, jira_event_id: '10093', applied: true, error: null, created_at: EARLIER,
      state: 'done', retry_count: 0, next_attempt_at: null, updated_at: EARLIER,
    },
    {
      id: 'jse_fixture01', ticket_id: tickets[3].id, issue_key: 'WID-44', direction: 'outbound', kind: 'status',
      payload: { to_status: 'blocked', transition: 'Block' }, jira_event_id: null, applied: false, error: 'Jira returned 503',
      created_at: EARLIER, state: 'pending', retry_count: 2, next_attempt_at: NOW, updated_at: EARLIER,
    },
  ],
};

// ── Ticket detail: a Jira-linked ticket with a review thread ──
// tickets[2] (tk_fixture3, in progress) is promoted to Jira as WID-43 and has
// two review rounds from a trusted reviewer, a mirrored Jira comment and a
// system edit note.
export const LINKED_TICKET_ID = tickets[2].id;
export const linkedTicketJira = {
  issue_key: 'WID-43',
  issue_url: 'https://acme.atlassian.net/browse/WID-43',
  last_known_status: 'In Progress',
};
export const linkedTicket = {
  ...tickets[2],
  jira: linkedTicketJira,
  // A Task under the fixture Issue, whose Jira issue is the epic WID-40.
  parent_ticket_id: tickets[3].id,
  description:
    'ProjectDetail renders every tab inline, so a change to one section re-renders the whole page. Split it into one component per section and keep the URL contract (?tab=…&sub=…) unchanged.',
  acceptance_criteria: [
    'Each section is its own component',
    'Deep links keep working',
    'No new network requests on tab switch',
  ],
  depends_on: [tickets[1].id],
  created_by_persona: 'compass',
  created_at: '2026-10-17T08:00:00Z',
  created_by: { user_id: me.user_id, display_name: me.display_name, email: me.email, persona: 'compass', session_id: 'ses_fixture01' },
};
export const linkedTicketJiraLink = {
  ticket_id: LINKED_TICKET_ID,
  provider: 'jira',
  issue_key: 'WID-43',
  issue_url: 'https://acme.atlassian.net/browse/WID-43',
  last_pushed_at: '2026-10-19T10:05:00Z',
  last_known_status: 'In Progress',
  last_status_checked_at: NOW,
  payload_hash: null,
  comment_sync_outbound: true,
  can_send_to_jira: true,
  attached_not_published: false,
  sprint: { id: 37, name: 'WID Sprint 14', state: 'active' },
  parent: { key: 'WID-40', ticket_id: tickets[3].id },
};

// GET /api/v1/projects/{id}/jira/sprints — active first, then future.
export const jiraSprints = {
  sprints: [
    { id: 37, name: 'WID Sprint 14', state: 'active', board_name: 'Widgets board', start_date: '2026-10-19T09:00:00Z', end_date: '2026-11-02T09:00:00Z' },
    { id: 38, name: 'WID Sprint 15', state: 'future', board_name: 'Widgets board', start_date: '2026-11-02T09:00:00Z', end_date: '2026-11-16T09:00:00Z' },
    { id: 52, name: 'Platform hardening', state: 'future', board_name: 'Platform kanban', start_date: null, end_date: null },
  ],
};

// GET /api/v1/projects/{id}/tickets/{tid}/jira/templates
export const jiraTicketTemplates = {
  templates: [
    { slug: 'agile', name: 'Agile Standard', version: null, builtin: true },
    { slug: 'bug', name: 'Bug Report', version: null, builtin: true },
  ],
  inherited_default: { slug: 'agile', name: 'Agile Standard', version: null },
  pinned: null,
};

// POST /api/v1/projects/{id}/tickets/{tid}/jira/preview (a first publish).
export const jiraPreview = (ticketId: string) => ({
  summary: 'Add retry budget to the sync worker',
  sections: [
    {
      key: 'summary',
      heading: 'Summary',
      mode: 'rewrite',
      paragraphs: [{ text: 'Cap sync retries per worker so one failing session cannot starve the queue.', citations: [] }],
    },
    {
      key: 'acceptance_criteria',
      heading: 'Acceptance criteria',
      mode: 'verbatim',
      paragraphs: [{ text: '- Replays do not double-charge\n- Covered by a regression test', citations: [] }],
    },
  ],
  citations: [],
  issue_type: 'Story',
  jira_project_key: 'WID',
  llm_used: false,
  key_source: 'none',
  ticket_id: ticketId,
  project_id: PROJECT_ID,
  template: { slug: 'agile', name: 'Agile Standard', version: null },
});

// POST .../jira/publish: Jira took the issue but not the sprint or the due date.
export const jiraPublishWithWarnings = (ticketId: string) => ({
  ticket_id: ticketId,
  provider: 'jira',
  issue_key: 'WID-57',
  issue_url: 'https://acme.atlassian.net/browse/WID-57',
  last_pushed_at: NOW,
  last_known_status: null,
  last_status_checked_at: null,
  payload_hash: 'fixture',
  warnings: ['jira_sprint_not_set:move_failed', 'jira_parent_not_linked', 'jira_field_not_on_screen:duedate'],
  sprint: null,
  parent: null,
  template: { slug: 'agile', name: 'Agile Standard', version: null },
});
export const linkedTicketComments = [
  {
    id: 'tc_fixture1', ticket_id: LINKED_TICKET_ID, author_user_id: me.user_id, author_persona: 'prism',
    content: 'Started on this. Pulling Overview and Knowledge out first since they share the compile banner.',
    session_id: 'ses_fixture01', created_at: '2026-10-18T09:40:00Z', source: 'sessionfs',
  },
  {
    id: 'tc_fixture2', ticket_id: LINKED_TICKET_ID, author_user_id: me.user_id, author_persona: 'system',
    content:
      '**Ticket fields updated** via `update_ticket` (tk_835a876529de4551):\n- `priority`: "medium" → "high"\n\n_Full per-field old/new values preserved in `ticket_edits`._',
    session_id: null, created_at: '2026-10-18T09:42:00Z', source: 'sessionfs',
  },
  {
    id: 'tc_fixture3', ticket_id: LINKED_TICKET_ID, author_user_id: 'user_fixture02', author_persona: null,
    content: 'Priya Shah (Jira): Please keep the Settings tab last, support links to it.',
    session_id: null, created_at: '2026-10-18T14:05:00Z', source: 'jira',
  },
  {
    id: 'tc_fixture4', ticket_id: LINKED_TICKET_ID, author_user_id: 'user_fixture03', author_persona: 'codex-reviewer',
    content:
      'Codex R1 review on tk_fixture3: CHANGES_REQUESTED\n\n- [MEDIUM] KnowledgeSection refetches entries on every tab switch.\n- [LOW] The Settings panel lost its heading id.',
    session_id: null, created_at: '2026-10-19T08:10:00Z', source: 'sessionfs',
  },
  {
    id: 'tc_fixture5', ticket_id: LINKED_TICKET_ID, author_user_id: me.user_id, author_persona: 'prism',
    content: 'Both fixed: entries are cached per project, and the heading id is back.',
    session_id: 'ses_fixture02', created_at: '2026-10-19T09:55:00Z', source: 'sessionfs',
  },
  {
    id: 'tc_fixture6', ticket_id: LINKED_TICKET_ID, author_user_id: 'user_fixture03', author_persona: 'codex-reviewer',
    content: 'Codex R2 review on tk_fixture3: VERIFIED-CLEAN\n\nNo open findings.',
    session_id: null, created_at: '2026-10-19T11:20:00Z', source: 'sessionfs',
  },
];
export const linkedTicketReviewState = {
  ticket_id: LINKED_TICKET_ID,
  review_state: {
    open_findings: [],
    closed_findings: [
      { severity: 'MEDIUM', text: 'KnowledgeSection refetches entries on every tab switch.', round: 1, raised_at: '2026-10-19T08:10:00Z', raised_comment_id: 'tc_fixture4', status: 'closed', closed_round: 2 },
      { severity: 'LOW', text: 'The Settings panel lost its heading id.', round: 1, raised_at: '2026-10-19T08:10:00Z', raised_comment_id: 'tc_fixture4', status: 'closed', closed_round: 2 },
    ],
    last_verdict: 'VERIFIED-CLEAN',
    last_raw_verdict: 'VERIFIED-CLEAN',
    last_verdict_is_strict_verified_clean: true,
    severity_counts: { MEDIUM: 1, LOW: 1 },
    last_review_comment_id: 'tc_fixture6',
    last_implementer_comment_id: 'tc_fixture5',
    updated_at: '2026-10-19T11:20:00Z',
    rounds: [
      { round: 1, verdict: 'CHANGES_REQUESTED', raw_verdict: 'CHANGES_REQUESTED', is_strict_verified_clean: false, comment_id: 'tc_fixture4', timestamp: '2026-10-19T08:10:00Z', findings_raised: 2 },
      { round: 2, verdict: 'VERIFIED-CLEAN', raw_verdict: 'VERIFIED-CLEAN', is_strict_verified_clean: true, comment_id: 'tc_fixture6', timestamp: '2026-10-19T11:20:00Z', findings_raised: 0 },
    ],
  },
};
// Newest first, as the API returns them.
export const linkedTicketSyncEvents = {
  events: [
    {
      // The trusted verdict forwarded to Jira; folds onto its comment.
      id: 'tse_fixture4', ticket_id: LINKED_TICKET_ID, issue_key: 'WID-43', direction: 'outbound', kind: 'comment',
      payload: { comment_id: 'tc_fixture6', reason: 'verdict' }, jira_event_id: null, applied: false, error: 'Jira returned 503',
      created_at: '2026-10-19T11:21:00Z', state: 'pending', retry_count: 1, next_attempt_at: NOW, updated_at: '2026-10-19T11:30:00Z',
    },
    {
      id: 'tse_fixture3', ticket_id: LINKED_TICKET_ID, issue_key: 'WID-43', direction: 'inbound', kind: 'status',
      payload: { category: 'indeterminate', action: 'start', jira_status: 'In Progress', outcome: 'echo' }, jira_event_id: 'issue:20511',
      applied: false, error: null, created_at: '2026-10-18T09:41:30Z', state: 'skipped', retry_count: 0, next_attempt_at: null, updated_at: '2026-10-18T09:41:30Z',
    },
    {
      // Mirrors tc_fixture3, so the timeline shows the comment only.
      id: 'tse_fixture2', ticket_id: LINKED_TICKET_ID, issue_key: 'WID-43', direction: 'inbound', kind: 'comment',
      payload: { author: 'Priya Shah', comment_id: '10211' }, jira_event_id: 'comment:10211', applied: true, error: null,
      created_at: '2026-10-18T14:05:00Z', state: 'done', retry_count: 0, next_attempt_at: null, updated_at: '2026-10-18T14:05:00Z',
    },
    {
      id: 'tse_fixture1', ticket_id: LINKED_TICKET_ID, issue_key: 'WID-43', direction: 'outbound', kind: 'status',
      payload: { to_status: 'in_progress', transition: 'Start Progress' }, jira_event_id: null, applied: true, error: null,
      created_at: '2026-10-18T09:41:00Z', state: 'done', retry_count: 0, next_attempt_at: null, updated_at: '2026-10-18T09:41:00Z',
    },
  ],
};
export const linkedTicketWatchers = [
  { user_id: me.user_id, persona_name: null, source: 'explicit', muted: false, created_at: '2026-10-18T09:40:00Z' },
];
// The open ticket (tk_fixture2): one comment, nobody following.
export const openTicketComments = [
  {
    id: 'tc_fixture7', ticket_id: tickets[1].id, author_user_id: me.user_id, author_persona: 'atlas',
    content: 'Budget should be per destination, not global. Proposal in the linked session.',
    session_id: 'ses_fixture03', created_at: EARLIER, source: 'sessionfs',
  },
];

// GET .../tickets/{tid}/events for the linked ticket, newest first. The
// priority edit at 09:42 also left the system diff comment tc_fixture2, which
// the timeline folds away. next_before is set so "Show earlier history" shows.
const actorMe = { type: 'user', user_id: me.user_id, display_name: me.display_name, persona: null, label: null };
export const linkedTicketEvents = {
  events: [
    { id: 'evt_fixture08', action: 'jira_published', field: null, before: null, after: { issue_key: 'WID-43' }, actor: { ...actorMe, persona: 'prism' }, created_at: '2026-10-19T10:05:00Z' },
    { id: 'evt_fixture07', action: 'estimate_set', field: 'estimated_effort', before: '1d', after: '2d 4h', actor: { type: 'service', user_id: null, display_name: null, persona: null, label: 'ci-planner' }, created_at: '2026-10-19T09:00:00Z' },
    { id: 'evt_fixture06', action: 'field_edited', field: 'description', before: { changed: true, length: 120 }, after: { changed: true, length: 186 }, actor: actorMe, created_at: '2026-10-18T15:10:00Z' },
    { id: 'evt_fixture05', action: 'due_date_set', field: 'due_date', before: null, after: '2026-12-15', actor: { type: 'jira', user_id: null, display_name: null, persona: null, label: 'Priya Shah' }, created_at: '2026-10-18T14:06:00Z' },
    { id: 'evt_fixture04', action: 'field_edited', field: 'priority', before: 'medium', after: 'high', actor: { ...actorMe, persona: 'prism' }, created_at: '2026-10-18T09:42:00Z' },
    { id: 'evt_fixture03', action: 'status_changed', field: 'status', before: 'open', after: 'in_progress', actor: { ...actorMe, persona: 'prism' }, created_at: '2026-10-18T09:39:00Z' },
    { id: 'evt_fixture02', action: 'assigned', field: 'assigned_to', before: null, after: { persona: 'prism' }, actor: { type: 'system', user_id: null, display_name: null, persona: null, label: null }, created_at: '2026-10-17T08:05:00Z' },
  ],
  next_before: 'evt_fixture02',
};

// ── meeting transcripts (GitHub #116) ──
export const MEETING_ID = 'meet_f1x7ure0000000a1';
export const GROUP_MEETING_ID = 'meet_f1x7ure0000000b2';

const meetingLines = [
  'Dev Fixture: Thanks for joining. Three things today: the launch date, the rollout plan and the status page.',
  'Dev Fixture: First, the launch. Are we confident in November 10?',
  'Peer Fixture: Yes. The last two blockers closed on Friday and the load test passed at twice the expected peak.',
  'Lead: Agreed, November 10 works for support as long as the help articles are live the week before.',
  'Dev Fixture: Then November 10 it is. Let us write that down as decided.',
  'Dev Fixture: Second, the rollout plan. We need it written down before the go/no-go on the sixth.',
  'Peer Fixture: I can draft the rollout plan, including the feature-flag stages and the rollback steps.',
  'Peer Fixture: I will have a first version by Friday, October 31.',
  'Dev Fixture: Great. Include who is on call for each stage.',
  'Lead: Third, the status page. The copy still talks about the old pricing tiers.',
  'Jordan (contractor): I can take the status page copy, but I need someone to review the wording.',
  'Dev Fixture: The security reviewer should look at anything that mentions data handling.',
  'Lead: Then we keep the existing incident template and only change the pricing references.',
  'Dev Fixture: Last thing: someone needs to tell the support team about the date.',
  'Lead: I will send the support briefing on Monday.',
  'Dev Fixture: That is everything. Thanks, all.',
];

export const projectMeetings = {
  meetings: [
    {
      id: MEETING_ID, org_id: ORG_ID, project_id: PROJECT_ID, group_id: null,
      title: 'Launch readiness — weekly sync', meeting_at: '2026-10-27T00:00:00Z', duration_minutes: 30,
      source: 'file_upload', line_count: meetingLines.length, dlp_findings: 1, extraction_status: 'done',
      pending_action_count: 3, decision_count: 2,
      created_by_user_id: 'user_fixture01', created_at: NOW, updated_at: NOW, deleted_at: null,
    },
    {
      id: 'meet_f1x7ure0000000a2', org_id: ORG_ID, project_id: PROJECT_ID, group_id: null,
      title: 'Incident review: checkout timeouts on October 14, and what we change in the payment retry policy',
      meeting_at: '2026-10-15T00:00:00Z', duration_minutes: 45,
      source: 'manual_paste', line_count: 212, dlp_findings: 0, extraction_status: 'failed',
      extraction_error: 'timeout', pending_action_count: 0, decision_count: 0,
      created_by_user_id: 'user_fixture02', created_at: EARLIER, updated_at: EARLIER, deleted_at: null,
    },
    {
      id: 'meet_f1x7ure0000000a3', org_id: ORG_ID, project_id: PROJECT_ID, group_id: null,
      title: 'Design review', meeting_at: null, duration_minutes: null,
      source: 'manual_paste', line_count: 48, dlp_findings: 0, extraction_status: 'none',
      created_by_user_id: 'user_fixture01', created_at: OLD, updated_at: OLD, deleted_at: null,
    },
  ],
  next_cursor: null,
};

export const meetingDetail = {
  ...projectMeetings.meetings[0],
  content: meetingLines.join('\n'),
  content_sha256: 'f'.repeat(64),
  warnings: [],
  participants: [
    { id: 'mp_f1', name_raw: 'Dev Fixture', email_raw: 'fixture@example.test', user_id: 'user_fixture01', persona_name: null, resolution_method: 'exact_email', resolved_by_user_id: null },
    { id: 'mp_f2', name_raw: 'Peer Fixture', email_raw: null, user_id: 'user_fixture02', persona_name: null, resolution_method: 'display_name', resolved_by_user_id: null },
    { id: 'mp_f3', name_raw: 'Lead', email_raw: null, user_id: 'user_fixture03', persona_name: null, resolution_method: 'manual', resolved_by_user_id: 'user_fixture01' },
    { id: 'mp_f4', name_raw: 'security reviewer', email_raw: null, user_id: null, persona_name: 'sentinel', resolution_method: 'persona', resolved_by_user_id: null },
    { id: 'mp_f5', name_raw: 'Jordan (contractor)', email_raw: null, user_id: null, persona_name: null, resolution_method: 'unresolved', resolved_by_user_id: null },
  ],
  action_items: [
    { id: 'mai_f1', kind: 'action', description: 'Draft the rollout plan, including the feature-flag stages, rollback steps and who is on call for each stage.', owner_participant_id: 'mp_f2', due_date: '2026-10-31', line_start: 7, line_end: 9, status: 'pending', promoted_ticket_id: null, extracted_at: NOW },
    { id: 'mai_f2', kind: 'action', description: 'Update the status page copy to remove the old pricing tiers.', owner_participant_id: 'mp_f5', due_date: null, line_start: 10, line_end: 11, status: 'pending', promoted_ticket_id: null, extracted_at: NOW },
    { id: 'mai_f3', kind: 'action', description: 'Review status page wording that mentions data handling.', owner_participant_id: 'mp_f4', due_date: null, line_start: 12, line_end: 12, status: 'pending', promoted_ticket_id: null, extracted_at: NOW },
    { id: 'mai_f4', kind: 'action', description: 'Send the support team a briefing on the launch date.', owner_participant_id: 'mp_f3', due_date: '2026-11-03', line_start: 14, line_end: 15, status: 'promoted', promoted_ticket_id: 'tk_fixture2', extracted_at: NOW },
    { id: 'mai_f5', kind: 'action', description: 'Schedule a second load test.', owner_participant_id: null, due_date: null, line_start: 3, line_end: 3, status: 'dismissed', promoted_ticket_id: null, extracted_at: NOW },
    { id: 'mai_f6', kind: 'decision', description: 'Launch on November 10.', owner_participant_id: null, due_date: null, line_start: 2, line_end: 5, status: 'pending', promoted_ticket_id: null, extracted_at: NOW },
    { id: 'mai_f7', kind: 'decision', description: 'Keep the existing incident template; change only the pricing references.', owner_participant_id: null, due_date: null, line_start: 13, line_end: 13, status: 'pending', promoted_ticket_id: null, extracted_at: NOW },
  ],
};

export const groupMeetings = {
  meetings: [
    {
      ...projectMeetings.meetings[0],
      id: GROUP_MEETING_ID, project_id: null, group_id: GROUP_ID,
      title: 'Payments program review', pending_action_count: 3, decision_count: 2,
    },
  ],
  next_cursor: null,
};

export const groupMeetingDetail = {
  ...meetingDetail,
  ...groupMeetings.meetings[0],
};

export const meetingPromotePreview = {
  meeting_id: MEETING_ID,
  item_id: 'mai_f1',
  title: 'Draft the rollout plan, including the feature-flag stages, rollback steps and who is on call for each stage.',
  description: [
    'Draft the rollout plan, including the feature-flag stages, rollback steps and who is on call for each stage.',
    '',
    'From meeting *Launch readiness — weekly sync* (2026-10-27), lines 7-9:',
    `> ${meetingLines[6]}`,
    `> ${meetingLines[7]}`,
    `> ${meetingLines[8]}`,
  ].join('\n'),
  priority: 'medium',
  kind: 'task',
  assigned_to: null,
  assigned_user_id: 'user_fixture02',
  due_date: '2026-10-31',
  context_refs: [`meeting:${MEETING_ID}#mai_f1`],
  target_project_id: PROJECT_ID,
  target_project_options: null,
  // DB-only gate result from the server; the fixture project publishes to Jira.
  jira_publish_available: true,
};
