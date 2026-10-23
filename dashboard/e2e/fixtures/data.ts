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
  model_id: i % 2 ? 'claude-sonnet-4' : 'gpt-5',
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
    created_by: me.user_id, created_at: OLD, updated_at: NOW,
  },
  {
    id: 'pers_2', project_id: PROJECT_ID, org_id: null, scope: 'project', name: 'prism',
    role: 'Frontend engineer', content: 'Owns the dashboard and its tests.',
    specializations: ['react', 'tailwind'], is_active: true, version: 1,
    created_by: me.user_id, created_at: OLD, updated_at: EARLIER,
  },
];

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
  assigned_to: i % 2 ? 'atlas' : 'prism',
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
}));

export const agentRuns = [
  {
    id: 'run_1', project_id: PROJECT_ID, persona_name: 'sentinel', tool: 'codex', trigger_source: 'ci',
    status: 'passed', ticket_id: null, trigger_ref: 'pr-128', ci_provider: 'github',
    ci_run_url: 'https://example.test/runs/1', result_summary: 'No findings.', severity: 'none',
    findings_count: 0, findings: [], fail_on: 'high', policy_result: 'pass', exit_code: 0,
    session_id: null, triggered_by_user_id: me.user_id, triggered_by_persona: null,
    created_at: EARLIER, started_at: EARLIER, completed_at: NOW, duration_seconds: 94,
  },
  {
    id: 'run_2', project_id: PROJECT_ID, persona_name: 'sentinel', tool: 'codex', trigger_source: 'ci',
    status: 'failed', ticket_id: null, trigger_ref: 'pr-131', ci_provider: 'github',
    ci_run_url: 'https://example.test/runs/2', result_summary: '2 findings.', severity: 'high',
    findings_count: 2, findings: [{ title: 'Unvalidated redirect' }], fail_on: 'high', policy_result: 'fail',
    exit_code: 1, session_id: null, triggered_by_user_id: me.user_id, triggered_by_persona: null,
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
    publisher: { id: 'pub_fixture01', namespace: 'acme', display_name: 'Acme', owner_type: 'org', status: 'active', verified: true, badges: ['dns'] },
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
  items: [0, 1, 2, 3, 4, 5].map((i) => pkg(i)),
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
      tools: { 'claude-code': { add: '/plugin marketplace add https://example.test/git/public/sessionfs.git' } },
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

export const githubInstallation = {
  installation_id: 123, account_login: 'acme-fixtures', account_type: 'Organization',
  auto_comment: true, include_trust_score: true, include_session_links: false,
  ownership_verified: true, repo_verification_active: true, claim_available: true, install_available: true,
};

// The real GET also carries the stored webhook secret; the card never shows it.
export const gitlabSettings = { instance_url: 'https://gitlab.acme-fixtures.example', has_token: true };

export const dlpPolicy = { enabled: true, mode: 'warn', categories: ['secrets'], patterns: [] };
