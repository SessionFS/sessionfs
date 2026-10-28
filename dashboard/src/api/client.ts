import type {
  AddTrustedPublisherInput,
  ClaimPublisherInput,
  PublisherBlock,
  PublisherDetail,
  PublisherList,
  PublisherVerification,
  StartVerificationInput,
  TrustedPublisher,
} from './publisherTypes';

export interface SessionSummary {
  id: string;
  title: string | null;
  alias: string | null;
  tags: string[];
  source_tool: string;
  model_id: string | null;
  message_count: number;
  turn_count: number;
  tool_use_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  blob_size_bytes: number;
  etag: string;
  created_at: string;
  updated_at: string;
  parent_session_id: string | null;
}

export interface SessionDetail extends SessionSummary {
  original_session_id: string | null;
  source_tool_version: string | null;
  model_provider: string | null;
  duration_ms: number | null;
  parent_session_id: string | null;
  uploaded_at: string;
  git_remote_normalized?: string;
  dlp_scan_results?: string | null;
  /** Present on soft-deleted sessions */
  deleted_by?: string | null;
  delete_scope?: 'cloud' | 'both' | null;
  purge_after?: string | null;
  is_deleted?: boolean;
  deleted_at?: string | null;
}

export interface SessionListResponse {
  sessions: SessionSummary[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface SessionModelCount {
  model_id: string | null;
  count: number;
}

/** `sort` values GET /api/v1/sessions accepts (anything else is a 422). */
export type SessionListSort = 'updated_at' | 'model';

export interface SessionModelsResponse {
  models: SessionModelCount[];
}

/** Where `?around=<n>` landed (see MessageTarget in schemas/sessions.py). */
export interface MessageTarget {
  /** The requested 0-based messages.jsonl line number. */
  index: number;
  /** `hidden` = filtered out of this view (sidechain/empty); `missing` = no such message. */
  status: 'found' | 'hidden' | 'missing';
  /** The message the page shows in its place; null when the view is empty. */
  anchor_index: number | null;
}

export interface MessagesResponse {
  messages: Record<string, unknown>[];
  /** 0-based messages.jsonl line number of each message (the helper's `msg:<n>`). */
  message_indexes?: number[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  target?: MessageTarget | null;
}

export interface FileChange {
  path: string;
  operation: 'created' | 'edited' | 'deleted' | 'unknown';
  diff: string;
  added_lines: number;
  removed_lines: number;
  message_indexes: number[];
  truncated: boolean;
}

export interface ChangesResponse {
  files: FileChange[];
  git_ref: string | null;
  truncated: boolean;
}

export interface HandoffSummary {
  id: string;
  session_id: string;
  recipient_session_id: string | null;
  session_title: string | null;
  session_tool: string | null;
  session_model_id: string | null;
  session_message_count: number | null;
  session_total_tokens: number | null;
  sender_email: string;
  recipient_email: string;
  message: string | null;
  status: 'pending' | 'claimed' | 'expired';
  created_at: string;
  claimed_at: string | null;
}

export interface HandoffDetail extends HandoffSummary {
  expires_at: string;
}

export interface HandoffSessionSummary {
  session_id: string;
  title: string;
  tool: string;
  model: string | null;
  message_count: number;
  files_modified: string[];
  commands_executed: number;
  tests_run: number;
  tests_passed: number;
  tests_failed: number;
  errors_encountered: string[];
  last_assistant_messages: string[];
}

export interface HandoffListResponse {
  handoffs: HandoffSummary[];
  total: number;
}

export interface CreateHandoffResponse {
  id: string;
  recipient_email: string;
  session_id: string;
}

export interface SignupResponse {
  user_id: string;
  email: string;
  raw_key: string;
  key_id: string;
}

export interface AuditFinding {
  message_index: number;
  claim: string;
  verdict: 'verified' | 'unverified' | 'hallucination';
  severity: string;
  evidence: string;
  explanation: string;
  category?: string;
  confidence?: number;
  cwe_id?: string;
  evidence_snippets?: { text: string; message_index: number }[];
}

export interface AuditSummary {
  total_claims: number;
  verified: number;
  unverified: number;
  hallucinations: number;
  trust_score: number;
}

export interface AuditReport {
  session_id: string;
  model: string;
  timestamp: string;
  findings: AuditFinding[];
  summary: AuditSummary;
  warnings?: string[];
}

export interface JudgeSettings {
  provider: string;
  model: string;
  key_set: boolean;
  base_url: string | null;
}

export interface OrgLlmSettings {
  provider: string;
  model: string;
  has_api_key: boolean;
  key_hint: string | null;
  base_url: string | null;
  enabled: boolean;
  plan_includes_helper: boolean;
}

/** POST .../judge/models and .../llm-settings/models. */
export interface DiscoveredModels {
  models: { id: string; owned_by: string }[];
  error?: string;
  base_url?: string | null;
  provider?: string;
}

function discoverModelsBody(baseUrl: string, apiKey?: string, provider?: string): string {
  return JSON.stringify({
    ...(provider ? { provider } : { base_url: baseUrl }),
    ...(apiKey ? { api_key: apiKey } : {}),
  });
}

export interface LlmProvider {
  id: string;
  label: string;
  api_style: 'anthropic' | 'google' | 'openai';
  suggested_models: string[];
  discovery: boolean;
  /** API address shown as the Base URL placeholder (OpenAI-compatible
   *  providers only; Anthropic/Google are used with Base URL empty). */
  default_base_url?: string | null;
}

/** A transient connection-check failure returned by a settings save. */
export interface SaveWarning {
  code: string;
  message: string;
}

/** Body of POST .../test — every field optional; omitted ones fall back
 *  to the saved settings, an empty base_url means the provider's own API. */
export interface LlmConnectionTestBody {
  provider?: string;
  model?: string;
  base_url?: string | null;
  api_key?: string;
}

export interface LlmConnectionTestResult {
  ok: boolean;
  code: string;
  message: string;
  provider: string;
  model: string;
  endpoint_host: string;
  /** Null on failures against private addresses (no timing is reported). */
  latency_ms: number | null;
}

// ── Session helper (Enterprise) ──
export interface HelperStatus {
  enabled: boolean;
  key_source: string | null;
  provider: string | null;
  model: string | null;
  /** Settings a turn would use with the resolved key; null without a key. */
  effective_settings?: HelperModelSettings | null;
  has_org_instructions?: boolean;
  has_personal_instructions?: boolean;
}

export type HelperReasoningEffort = 'off' | 'low' | 'medium' | 'high';
export type HelperAnswerStyle = 'concise' | 'detailed';

/** Fully-resolved helper model settings (`helper/settings.py`). */
export interface HelperModelSettings {
  max_output_tokens: number;
  /** null = let the provider pick. */
  temperature: number | null;
  /** null = not sent. */
  top_p: number | null;
  max_tool_steps: number;
  /** null = not sent. */
  reasoning_effort: HelperReasoningEffort | null;
  answer_style: HelperAnswerStyle;
}

export interface OrgHelperModelSettings extends HelperModelSettings {
  /** Caps on the org-key path; null = no ceiling. */
  ceiling_max_output_tokens: number | null;
  ceiling_max_tool_steps: number | null;
}

/** A partial settings update: omitted keys keep their value, null resets/clears. */
export type HelperSettingsPatch = Partial<{ [K in keyof OrgHelperModelSettings]: OrgHelperModelSettings[K] | null }>;

export interface HelperTokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface HelperMemberUsage extends HelperTokenUsage {
  user_id: string;
}

/** `GET/PUT /api/v1/settings/helper`. */
export interface UserHelperSettings {
  settings: HelperModelSettings;
  instructions: string | null;
  effective_settings: HelperModelSettings | null;
  /** This UTC month, across the caller's turns on either key. */
  usage: HelperTokenUsage;
}

export interface UserHelperSettingsUpdate {
  settings?: HelperSettingsPatch | null;
  instructions?: string | null;
}

/** `GET/PUT /api/v1/orgs/{org_id}/helper-settings`. Admin-only fields are absent for members. */
export interface OrgHelperSettings {
  role: string;
  plan_includes_helper: boolean;
  effective_settings: HelperModelSettings;
  has_instructions: boolean;
  settings?: OrgHelperModelSettings;
  instructions?: string | null;
  monthly_token_budget?: number | null;
  /** Admins: org-key totals + per member. Members: their own org-key usage. */
  usage: HelperTokenUsage & { per_member?: HelperMemberUsage[] };
  /** Members only: org-key turns are refused until the month resets. */
  budget_reached?: boolean;
  /** Members only: whole percent of the budget used (capped at 100); null without a budget. */
  budget_used_pct?: number | null;
}

export interface OrgHelperSettingsUpdate {
  settings?: HelperSettingsPatch | null;
  instructions?: string | null;
  monthly_token_budget?: number | null;
}

/** A thread's stored scope. `group_id` and `label` come from servers that
 *  support group scope; older servers send only project_id/session_ids. */
export interface HelperStoredScope {
  project_id: string | null;
  group_id?: string | null;
  session_ids: string[];
  label?: string | null;
}

export interface HelperConversationSummary {
  id: string;
  org_id: string | null;
  title: string | null;
  /** The stored scope, on servers that include it in the list. */
  scope?: HelperStoredScope | null;
  created_at: string;
  updated_at: string;
}

export interface HelperCitation {
  /** Present on messages written after v0.25; omitted on older stored rows. */
  kind?: 'session';
  session_id: string;
  message_index: number | null;
  title: string | null;
  /** ISO timestamp the source data was last updated; null for old messages. */
  at?: string | null;
}

/** A docs source the helper cited with `[doc:<slug>#<anchor>]`. */
export interface HelperDocCitation {
  kind?: 'doc';
  slug: string;
  anchor: string | null;
  title: string;
  url: string;
}

/** A sessionfs ticket cited with `[ticket:tk_…]`. */
export interface HelperTicketCitation {
  kind?: 'ticket';
  ticket_id: string;
  project_id: string;
  title: string;
  at?: string | null;
}

/** A wiki page cited with `[wiki:<project_id>/<slug>]`. */
export interface HelperWikiCitation {
  kind?: 'wiki';
  project_id: string;
  slug: string;
  title: string;
  at?: string | null;
}

export type HelperCitationRef =
  | HelperCitation
  | HelperDocCitation
  | HelperTicketCitation
  | HelperWikiCitation;

export interface HelperMessage {
  id: string;
  /** `note` = a write action's outcome, written for the model; not shown. */
  role: 'user' | 'assistant' | 'tool' | 'note';
  content: string;
  tool_name: string | null;
  tool_call_id: string | null;
  tool_args: Record<string, unknown> | null;
  /** Tool rows that carried a write-action proposal: that action's id. */
  action_id?: string | null;
  citations: HelperCitationRef[] | null;
  key_source: string | null;
  input_tokens: number;
  output_tokens: number;
  usage?: { input_tokens: number; output_tokens: number };
  created_at: string;
}

export interface HelperConversation extends HelperConversationSummary {
  /** The last scope the thread was used with (project_id/session_ids), or null for personal threads. */
  scope: HelperStoredScope | null;
  messages: HelperMessage[];
}

/** A target in a helper write-action preview (`helper/tools.py`). */
export interface HelperActionTarget {
  id: string;
  name: string;
  change: 'will_change' | 'unchanged' | 'skipped';
  before?: unknown;
  after?: unknown;
  /** Why a target is skipped (not_admin, not_owned, already_trashed, …). */
  reason?: string;
  /** Session targets: the capture tool and the session date, when the server sends them. */
  tool?: string | null;
  /** Session targets: the session's model id ("unknown" when none was recorded). */
  model?: string | null;
  created_at?: string | null;
}

/** The `action_proposed` SSE event payload. */
export interface HelperActionProposal {
  id: string;
  kind: string;
  summary: string;
  targets: HelperActionTarget[];
  /** Stored proposal params (the model draft); present on the SSE event. Older
   *  streams and locally-recovered cards may not carry it until the server read. */
  params?: Record<string, unknown>;
  expires_at: string | null;
}

export type HelperActionStatus = 'pending' | 'confirmed' | 'failed' | 'cancelled' | 'expired';

export interface HelperActionTargetResult {
  id: string;
  ok: boolean;
  error: string | null;
  /** create_ticket: the new ticket's id when applied. */
  ticket_id?: string;
  /** comment_ticket: the new comment's id when applied. */
  comment_id?: string;
  /** create_ticket with publish_to_jira: the issue it was published as. */
  jira?: HelperActionJiraIssue | null;
  /** create_ticket with publish_to_jira: false when the ticket was created
   *  but the Jira publish failed, with the reason code in `jira_error`. */
  jira_published?: boolean;
  jira_error?: string | null;
  /** link_jira / jira_overwrite: the issue the ticket is linked to. */
  issue_key?: string;
  issue_url?: string;
  /** jira_overwrite from_jira: the ticket fields that changed (may be empty). */
  updated_fields?: string[];
}

export interface HelperActionJiraIssue {
  issue_key: string;
  issue_url: string;
}

/** User-edited values for an editable ticket-action confirm. Omitted fields
 *  keep the stored model draft (routes/helper.py `HelperActionConfirmBody`). */
export interface HelperActionConfirmBody {
  title?: string;
  description?: string;
  acceptance_criteria?: string[];
  priority?: string;
  /** Omit to keep the draft's assignee; `null` (or blank) leaves it unassigned. */
  assigned_to?: string | null;
  body?: string;
}

/** `GET/POST /api/v1/helper/actions/{id}[/confirm|/cancel]`. */
export interface HelperAction extends HelperActionProposal {
  status: HelperActionStatus;
  params: Record<string, unknown>;
  result: { targets?: HelperActionTargetResult[] };
  created_at: string | null;
  decided_at: string | null;
}

export interface AdminUser {
  id: string;
  email: string;
  tier: string;
  email_verified: boolean;
  is_active: boolean;
  created_at: string;
  session_count: number;
}

export interface AdminStats {
  users: { total: number; verified: number; by_tier: Record<string, number> };
  sessions: { total: number; total_size_bytes: number; by_tool: Record<string, number> };
  handoffs: { total: number; pending: number; claimed: number };
}

export interface AdminUserListResponse {
  users: AdminUser[];
  total: number;
}

export interface AdminActionLog {
  id: string;
  admin_id: string;
  action: string;
  target_type: string;
  target_id: string;
  details: Record<string, unknown> | null;
  // GH #84 (migration 081). Optional so older servers still type-check.
  reason?: string | null;
  before_state?: unknown;
  after_state?: unknown;
  // 'api' = the server performed the action; 'operator_record' = an
  // operator recorded a change made outside the API (not verified).
  source?: string;
  created_at: string;
}

export interface GitHubInstallationResponse {
  installation_id?: number | null;
  account_login: string | null;
  account_type: string | null;
  auto_comment: boolean;
  include_trust_score: boolean;
  include_session_links: boolean;
  /** True once GitHub confirmed (via OAuth) that this user controls the installation. */
  ownership_verified?: boolean;
  /** Linked before ownership checks existed; re-verify to restore trust. */
  needs_reverification?: boolean;
  claim_verified_github_login?: string | null;
  claim_role?: string | null;
  /** The link currently grants repository verification (fresh owner/admin proof). */
  repo_verification_active?: boolean;
  /** An owner/admin proof that has aged out; re-verify to restore repository verification. */
  proof_stale?: boolean;
  /** The server can run the GitHub OAuth link flow. */
  claim_available?: boolean;
  /** The server can run the one-step install-and-link flow. */
  install_available?: boolean;
}

/**
 * GitLab integration settings. The server never returns a stored token or
 * webhook secret, only whether each is set.
 */
export interface GitLabSettingsResponse {
  instance_url: string;
  has_token: boolean;
  has_webhook_secret?: boolean;
}

export interface GitLabSettingsRequest {
  instance_url: string;
  access_token: string;
  webhook_secret?: string;
}

/* ── Jira integration (Enterprise, org owner/admin) ── */

/**
 * Org Jira connection. The API token is write-only: the server reports
 * `has_token` plus a last-4 `token_hint` and never the credential itself.
 * When no connection exists the GET collapses to `{ connected: false,
 * has_token: false }`.
 */
export interface JiraConnection {
  connected: boolean;
  provider?: string;
  site_url?: string;
  account_email?: string;
  has_token: boolean;
  token_hint?: string;
  default_template?: string;
  enabled?: boolean;
  last_verified_at?: string | null;
  connected_by_user_id?: string;
  /** Two-way sync (GitHub #98 part B). */
  sync_status?: boolean;
  sync_comments?: JiraCommentSyncMode;
  status_map?: JiraStatusMap;
  has_webhook_secret?: boolean;
  webhook_secret_last4?: string | null;
  /** Canonical inbound webhook URL, `<public API origin>/api/v1/webhooks/jira/{org_id}`
   *  (GitHub #98 part B). Always use this value; never build the path client-side. */
  webhook_url?: string;
  /** The site's Agile sprint custom field (e.g. `customfield_10020`), detected
   *  on first connect or set by an admin; null when the site has none. */
  sprint_field_id?: string | null;
  /** The company-managed epic-link custom field; usually null on Jira Cloud,
   *  where the issue `parent` field is used instead. */
  epic_link_field_id?: string | null;
}

/** `POST /orgs/{org}/jira/fields/detect` — what the site exposes right now.
 *  Detection never changes the stored connection. */
export interface JiraDetectedFields {
  sprint_field_id: string | null;
  epic_link_field_id: string | null;
}

/**
 * SessionFS status → Jira transition display name (outbound) and Jira status
 * category → SessionFS action (inbound). The literal `"none"` means unmapped:
 * no outbound push for that status / no inbound action for that category.
 *
 * transitions keys: open | in_progress | blocked | review | done | cancelled | closed
 * categories keys: new | indeterminate | done → resolve | start | reopen | none
 */
export interface JiraStatusMap {
  transitions: Record<string, string>;
  categories: Record<string, string>;
}

export type JiraCommentSyncMode = 'off' | 'inbound' | 'outbound' | 'both';

/** `PATCH .../integrations/jira/sync` — any subset; omitted fields are unchanged. */
export interface JiraSyncSettingsRequest {
  sync_status?: boolean;
  sync_comments?: JiraCommentSyncMode;
  status_map?: JiraStatusMap;
  /** Sprint / epic-link custom field ids: an id overrides, null clears,
   *  omitted keeps. Must match `^customfield_[0-9]{1,10}$`. */
  sprint_field_id?: string | null;
  epic_link_field_id?: string | null;
}

export interface JiraConnectionRequest {
  site_url: string;
  account_email: string;
  api_token: string;
  default_template: string;
  /** Merged: omitted keeps the stored value, an id overrides it and an
   *  explicit null clears it. Must match `^customfield_[0-9]{1,10}$`. */
  sprint_field_id?: string | null;
  epic_link_field_id?: string | null;
}

export interface JiraProject {
  key: string;
  name: string;
  /** Empty on the list endpoint — issue types are fetched lazily per project. */
  issue_types: { name: string; id: string }[];
}

/** One page of `GET .../integrations/jira/projects?q=&limit=` (GitHub #103).
 *  Jira filters on key and name server-side; `has_more` means more projects
 *  match than were returned. */
export interface JiraProjectPage {
  projects: JiraProject[];
  has_more: boolean;
  query: string | null;
  limit: number;
}

export interface JiraIssueType {
  name: string;
  id: string;
}

export interface JiraMapping {
  project_id: string;
  jira_project_key: string;
  issue_type: string;
  template: string | null;
  done_status_name: string | null;
}

export interface JiraMappingRequest {
  jira_project_key: string;
  issue_type?: string;
  template?: string | null;
  done_status_name?: string | null;
}

export interface JiraPushFailure {
  ticket_id: string;
  issue_key: string | null;
  failed_at: string | null;
  reason: string | null;
}

export interface JiraStaleLink {
  ticket_id: string;
  issue_key: string | null;
  last_status_checked_at: string | null;
}

export interface JiraPublishActivity {
  ticket_id: string;
  started_at: string | null;
}

export interface JiraHealth {
  unmapped_project_count: number;
  last_push_failures: JiraPushFailure[];
  stale_links: JiraStaleLink[];
  publishes_in_progress: JiraPublishActivity[];
  publishes_unknown: JiraPublishActivity[];
  /** Linked tickets whose pinned custom template has since been archived. */
  archived_template_references?: {
    ticket_id: string;
    template_slug: string;
    template_version: number | null;
  }[];
  /** Mappings whose template override is an archived custom template. */
  mappings_on_archived_templates?: { project_id: string; template_slug: string }[];
  /** The connection default is an archived custom template. */
  connection_default_archived?: boolean;
  /** Two-way sync health (GitHub #98 part B). */
  last_inbound_at?: string | null;
  last_synced_at?: string | null;
  failed_sync_events_24h?: number;
  webhook_auth_failures_24h?: number;
}

/** One row of the two-way sync ledger (GitHub #98 part B). */
export interface JiraSyncEvent {
  id: string;
  ticket_id: string;
  issue_key: string | null;
  direction: 'inbound' | 'outbound';
  kind: string;
  payload: Record<string, unknown>;
  jira_event_id: string | null;
  applied: boolean;
  error: string | null;
  created_at: string | null;
  /** Delivery state: pending (queued/retrying), done, exhausted (gave up), skipped. */
  state: 'pending' | 'done' | 'exhausted' | 'skipped';
  retry_count: number;
  next_attempt_at: string | null;
  updated_at: string | null;
}

export interface JiraSyncEventsPage {
  events: JiraSyncEvent[];
}

export type JiraTemplateMode = 'verbatim' | 'rewrite';

/** One section *definition* on a template (not a rendered preview section). */
export interface JiraTemplateSection {
  key: string;
  heading: string;
  mode: JiraTemplateMode;
  /** Source-field vocabulary: title, description, acceptance_criteria, … */
  source_fields: string[];
  /** Rewrite-only; blank on verbatim sections. */
  guidance: string | null;
}

export interface JiraTemplate {
  slug: string;
  name: string;
  description: string | null;
  sections: JiraTemplateSection[];
  version: number | null;
  builtin: boolean;
  archived_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  created_by_user_id: string | null;
  updated_by_user_id: string | null;
}

export interface JiraTemplateSectionRequest {
  key: string;
  heading: string;
  mode: JiraTemplateMode;
  source_fields: string[];
  guidance?: string | null;
}

export interface JiraTemplateRequest {
  slug: string;
  name: string;
  description?: string | null;
  sections: JiraTemplateSectionRequest[];
  /** PUT only: the version being edited; a mismatch is 409 template_version_conflict. */
  expected_version?: number;
}

/** Body for the deterministic template preview (admin). */
export interface JiraTemplatePreviewRequest {
  project_id: string;
  ticket_id: string;
  /** Unsaved draft sections; omitted renders the stored template. */
  sections?: JiraTemplateSectionRequest[];
  name?: string;
}

/** A template a member may pick in the promotion dialog (no section details). */
export interface JiraTemplateChoice {
  slug: string;
  name: string;
  version: number | null;
  builtin: boolean;
  /** True only for this ticket's own pinned template after it was archived. */
  archived?: boolean;
}

/** `GET .../tickets/{tid}/jira/templates` — the per-ticket picker data. */
export interface JiraTicketTemplates {
  templates: JiraTemplateChoice[];
  inherited_default: JiraResolvedTemplate;
  pinned: { slug: string; version: number | null } | null;
}

export interface JiraTemplateVersion {
  version: number;
  sections: JiraTemplateSection[];
  created_by_user_id: string | null;
  created_at: string | null;
}

/** The template a draft/link resolved to (or is pinned at). */
export interface JiraResolvedTemplate {
  slug: string;
  name: string;
  version: number | null;
}

/** Deterministic draft from `POST .../templates/{slug}/preview` (no model call). */
export interface JiraTemplatePreview {
  ticket_id: string;
  project_id: string;
  template: JiraResolvedTemplate;
  summary: string;
  sections: JiraSection[];
}

export interface JiraParagraph {
  text: string;
  citations: string[];
}

export interface JiraSection {
  key: string;
  heading: string;
  /** Present on previews ("verbatim" vs "rewrite"); dropped on publish. */
  mode?: 'verbatim' | 'rewrite';
  paragraphs: JiraParagraph[];
}

export interface JiraPreview {
  summary: string;
  sections: JiraSection[];
  citations: string[];
  issue_type: string;
  jira_project_key: string;
  llm_used: boolean;
  key_source: string;
  ticket_id: string;
  project_id: string;
  /** Template the draft resolved to (per-ticket override → mapping → default). */
  template: JiraResolvedTemplate;
  /** Present when the ticket already links a Jira issue (re-publish). */
  existing?: {
    issue_key: string;
    issue_url: string;
    last_known_status: string | null;
    template_slug: string | null;
    template_version: number | null;
    /** Linked from Jira and never published from SessionFS: publishing
     *  overwrites the issue's summary and description. */
    attached?: boolean;
  };
  /** Unified diff against the last published text, when re-publishing. */
  diff?: string[];
}

export interface JiraPublishRequest {
  summary: string;
  sections: JiraSection[];
  confirm: boolean;
  /** Per-ticket template override; omitted falls back to mapping then default. */
  template?: string;
  /** The previewed template version, echoed back (409 template_changed on mismatch). */
  template_version?: number | null;
  /** Required (true) to publish over an issue that was linked from Jira and
   *  never published from SessionFS; else 409 jira_overwrite_required. */
  overwrite_existing?: boolean;
  /** Optional Agile sprint to place the issue in. A failed sprint set comes
   *  back as a `warnings` entry; 422 jira_sprint_field_unknown when the org
   *  connection has no sprint field. */
  sprint_id?: number;
}

/** The sprint cached on a ticket's Jira link. `name`/`state` are null until
 *  a link read or pull learns them (right after a publish only the id is known). */
export interface JiraLinkSprint {
  id: number | string;
  name: string | null;
  state: string | null;
}

/** The Jira parent (or epic) of the linked issue. `ticket_id` is set only
 *  when the viewer can read the SessionFS ticket linked to that issue. */
export interface JiraLinkParent {
  key: string;
  ticket_id: string | null;
}

/** One Agile sprint from `GET /projects/{p}/jira/sprints`. */
export interface JiraSprint {
  id: number | string;
  name: string;
  state: string;
  board_name: string | null;
  start_date: string | null;
  end_date: string | null;
}

/** Active-first sprints for the mapped Jira project (project editors only).
 *  `unavailable` is set when the site has no Jira Software (no boards). */
export interface JiraSprintList {
  sprints: JiraSprint[];
  unavailable?: string;
}

export interface JiraLink {
  ticket_id: string;
  provider: string;
  issue_key: string;
  issue_url: string;
  last_pushed_at: string | null;
  last_known_status: string | null;
  last_status_checked_at: string | null;
  payload_hash: string | null;
  /** Present on the publish response (the pinned template slug/version). */
  template?: JiraResolvedTemplate;
  /** The org's comment sync pushes SessionFS comments to Jira (outbound/both). */
  comment_sync_outbound?: boolean;
  /** Caller may send a comment to Jira: an editor, with outbound comment sync on. */
  can_send_to_jira?: boolean;
  /** The issue was linked from Jira and never published from SessionFS. */
  attached_not_published?: boolean;
  /** Cached Jira sprint (null when the issue is in no sprint). */
  sprint?: JiraLinkSprint | null;
  /** Cached Jira parent/epic (null when the issue has none). */
  parent?: JiraLinkParent | null;
  /** Publish only: what Jira refused while the publish still succeeded,
   *  e.g. `jira_sprint_not_set:move_failed`, `jira_parent_not_linked`. */
  warnings?: string[];
}

/** `POST .../tickets/{tid}/jira/pull`: the ticket after taking the Jira text. */
export interface JiraPullResult {
  ticket: Ticket;
  jira: JiraLink;
  /** Only the fields that changed ("title", "description"); may be empty. */
  updated_fields: string[];
}

/** Single-ticket link summary embedded on the ticket response. */
export interface TicketJiraLink {
  issue_key: string;
  issue_url: string;
  last_known_status: string | null;
}

/* ── Confluence integration (Enterprise, org owner/admin + project editors) ── */

/**
 * Org Confluence connection. The API token is write-only: the server reports
 * a last-4 `token_hint` and never the credential itself. When no connection
 * exists the GET collapses to `{ connected: false, has_token: false }`.
 */
export interface ConfluenceConnection {
  connected: boolean;
  provider?: string;
  site_url?: string;
  account_email?: string;
  has_token: boolean;
  token_hint?: string;
  default_template?: string;
  batch_window_minutes?: number;
  enabled?: boolean;
  last_verified_at?: string | null;
  connected_by_user_id?: string;
}

export interface ConfluenceConnectionRequest {
  site_url: string;
  account_email: string;
  api_token: string;
  default_template: string;
  batch_window_minutes?: number;
}

/** `GET .../integrations/confluence/spaces` search result. */
export interface ConfluenceSpace {
  key: string;
  name: string;
}

/** `GET .../spaces/{key}/pages` returns a bare list of pages. */
export interface ConfluenceSpacePage {
  id: string;
  title: string;
}

export interface ConfluenceSpaceSearch {
  spaces: ConfluenceSpace[];
  has_more: boolean;
  query: string | null;
  limit: number;
}

export interface ConfluenceTemplateSection {
  key: string;
  heading: string;
  mode: 'verbatim' | 'rewrite';
  source_fields: string[];
  guidance: string | null;
}

export interface ConfluenceTemplate {
  slug: string;
  name: string;
  version: number | null;
  builtin: boolean;
  sections: ConfluenceTemplateSection[];
}

export interface ConfluenceMapping {
  project_id: string;
  space_key: string;
  parent_page_id: string;
  template: string | null;
  publish_context: boolean;
  auto_publish_pages: boolean;
}

export interface ConfluenceMappingRequest {
  space_key: string;
  parent_page_id: string;
  template?: string | null;
  publish_context?: boolean;
  auto_publish_pages?: boolean;
}

export interface ConfluencePushFailure {
  project_id: string;
  source_kind: string;
  source_key: string;
  failed_at: string | null;
  reason: string | null;
}

export interface ConfluencePublishActivity {
  project_id: string;
  source_kind: string;
  source_key: string;
  started_at: string | null;
}

export interface ConfluenceDriftedLink {
  project_id: string;
  source_kind: string;
  source_key: string;
  page_id: string | null;
  stored_version: number | null;
}

export interface ConfluenceHealth {
  unmapped_project_count: number;
  last_push_failures: ConfluencePushFailure[];
  publishes_in_progress: ConfluencePublishActivity[];
  publishes_unknown: { project_id: string; source_kind: string; source_key: string }[];
  drifted_links: ConfluenceDriftedLink[];
}

export interface ConfluenceParagraph {
  text: string;
  citations: string[];
}

export interface ConfluenceSection {
  key: string;
  heading: string;
  /** Present on previews ("verbatim" vs "rewrite"); dropped on publish. */
  mode?: 'verbatim' | 'rewrite';
  paragraphs: ConfluenceParagraph[];
}

export interface ConfluenceResolvedTemplate {
  slug: string;
  name: string;
  version: number | null;
}

/** Deterministic/LLM-shaped draft from `POST .../confluence/preview`. */
export interface ConfluencePreview {
  title: string;
  sections: ConfluenceSection[];
  citations: string[];
  llm_used: boolean;
  key_source: string;
  project_id: string;
  source_kind: string;
  source_key: string;
  template: ConfluenceResolvedTemplate;
  /** Model paragraphs dropped for a missing or invalid citation. */
  dropped?: ConfluenceDroppedParagraph[];
  /** Informational notes about how the draft was built. */
  notes?: string[];
  /** The source's Confluence link in any status; null when there is none. */
  link?: ConfluenceLink | null;
  /** Present only when the source links a live (linked, not stale) page. */
  existing?: {
    page_id: string;
    page_url: string;
    version: number | null;
    template_slug: string | null;
    template_version: number | null;
  };
}

export interface ConfluenceDroppedParagraph {
  section: string;
  text: string;
  reason: string;
}

export interface ConfluencePublishRequest {
  kind: 'wiki_page' | 'context';
  slug?: string;
  title: string;
  sections: ConfluenceSection[];
  confirm: boolean;
  /** Echo of the preview's template; a mismatch is a 409 template_changed. */
  template?: string;
  /** Echo of the preview's template version (null for built-ins). */
  template_version?: number | null;
  /** An explicit template choice (not an echo of the preview). */
  template_override?: string;
}

export type ConfluenceLinkStatus = 'linked' | 'unknown' | 'unlinked' | 'drifted' | 'failed';

export interface ConfluenceLink {
  id: string;
  source_kind: string;
  source_key: string;
  page_id: string | null;
  page_url: string | null;
  version: number | null;
  status: ConfluenceLinkStatus;
  /** A live publish claim holds the link. */
  publishing: boolean;
  /** Link belongs to another org/site: only unlink works. */
  stale: boolean;
  template_slug: string | null;
  template_version: number | null;
  last_pushed_at: string | null;
  last_push_failed_at: string | null;
  last_push_failure: string | null;
}

/** The project's mapping as seen by any project member. */
export interface ConfluenceProjectMapping {
  space_key: string;
  template: string | null;
  publish_context: boolean;
  auto_publish_pages: boolean;
}

export interface ConfluenceLinksResponse {
  links: ConfluenceLink[];
  mapping: ConfluenceProjectMapping;
}

/** `details` of a 409 confluence_page_drifted error. */
export interface ConfluenceDriftDetails {
  error?: string;
  link_id?: string;
  page_id?: string | null;
  stored_version?: number | null;
  remote_version?: number | null;
  diff?: string[];
  diff_truncated?: boolean;
}

export type GitHubClaimFlow = 'install' | 'authorize';

export interface GitHubClaimChoice {
  installation_id: number;
  account_login: string | null;
  account_type: string | null;
}

export interface FolderResponse {
  id: string;
  name: string;
  color: string | null;
  bookmark_count: number;
  created_at: string;
}

export interface FolderListResponse {
  folders: FolderResponse[];
}

export interface BookmarkResponse {
  id: string;
  folder_id: string;
  session_id: string;
  created_at: string;
}

export interface FolderSessionsResponse {
  sessions: (SessionSummary & { bookmark_id: string; bookmarked_at: string })[];
  total: number;
}

export interface HelmLicense {
  id: string;
  org_name: string;
  contact_email: string;
  license_type: string;
  tier: string;
  seats_limit: number;
  status: string;
  effective_status: string;
  expires_at: string | null;
  last_validated_at: string | null;
  validation_count: number;
  created_at: string;
  notes: string | null;
}

export interface LicenseValidation {
  id: number;
  cluster_id: string | null;
  ip_address: string | null;
  result: string;
  tier: string;
  version: string | null;
  validated_at: string;
}

export interface CreateLicenseRequest {
  org_name: string;
  contact_email: string;
  license_type: 'trial' | 'paid';
  tier: string;
  seats_limit: number;
  days?: number;
  notes?: string;
}

export interface WikiPage {
  id: string;
  slug: string;
  title: string;
  page_type: string;
  content: string;
  word_count: number;
  entry_count: number;
  auto_generated: boolean;
  updated_at: string;
  /** Opt-in for the Confluence publish sweep (knowledge_pages.publish). */
  publish?: boolean;
}

export interface WikiPageDetail extends WikiPage {
  backlinks?: { source_type: string; source_id: string; link_type: string; confidence: number }[];
}

export interface WikiPageListResponse {
  pages: WikiPage[];
}

// ── Multi-Repo Projects (v0.11) ──

export interface ProjectRepoResponse {
  id: string;
  project_id: string;
  git_remote_normalized: string;
  is_primary: boolean;
  verified: boolean;
  verification_method: 'github_app' | 'owner_attested' | 'legacy_backfill' | null;
  provider: string | null;
  provider_repo_id: string | null;
  added_by_user_id: string | null;
  created_at: string;
}

export type RepoVerificationMethod = 'github_app' | 'owner_attested' | 'legacy_backfill';

export interface ProjectContext {
  id: string;
  name: string;
  git_remote_normalized: string;
  context_document: string;
  owner_id: string;
  org_id?: string | null;
  created_at: string;
  updated_at: string;
  session_count?: number;
  auto_narrative?: boolean;
  /** The plan tier that pays for this project (its org's, or its owner's).
   *  Absent on older servers — the dashboard falls back to the caller's own
   *  tier in that case. */
  effective_tier?: string | null;
  /** Whether the caller may change this project (owner or a member of its
   *  org). False for readers whose only access is a captured session on the
   *  project's repo. Absent on older servers — treat as editable. */
  can_edit?: boolean | null;
  /** Whether the caller has project-admin standing (owner, or admin/owner of
   *  its org): rename, link repos, merge, transfer. No group role grants it.
   *  Absent on older servers — treat as allowed and let the server decide. */
  can_admin?: boolean | null;
  /** Whether the caller may delete this project: the owner or a platform
   *  admin (org admins can't). Absent on older servers — show the control
   *  and let the server decide. */
  can_delete?: boolean | null;
  /** Whether the caller may start a transfer: the owner of a personal
   *  project, or an admin of the project's org (an org project's owner alone
   *  can't). Absent on older servers — fall back to can_admin. */
  can_transfer?: boolean | null;
  /** Multi-repo: linked repos (populated on detail, omitted on list). */
  repos?: ProjectRepoResponse[];
  /** Project groups containing this project (GitHub #101). Detail only, and
   *  only for members of the project's org; absent or null otherwise. */
  groups?: ProjectGroupSummary[] | null;
  /** Multi-repo: set when all repos were reclaimed (orphaned project). */
  repo_reclaimed_at?: string | null;
  /** Multi-repo: set when this project was merged into another. */
  merged_into_project_id?: string | null;
  merged_at?: string | null;
  /** Scheduled KB compile cadence (GitHub #93): off, nightly, or weekly. */
  compile_schedule?: string;
  /** Optional pending-claim threshold that compiles regardless of schedule. */
  compile_after_claims?: number | null;
  /** Last time the scheduled sweep compiled this project, if any. */
  last_scheduled_compile_at?: string | null;
}

export interface LinkRepoRequest {
  git_remote: string;
  is_primary?: boolean;
}

export interface MergeRequest {
  source_project_id: string;
  dry_run: boolean;
}

export interface MergeCollision {
  /** Persona / wiki-slug collisions. */
  old_name?: string;
  new_name?: string;
  display_note?: string;
  /** Wiki slug collisions. */
  old_slug?: string;
  new_slug?: string;
  /** Generic collision info. */
  collision_type?: string;
  /** Slug-level collisions list. */
  collisions?: Array<{ old_slug: string; new_slug: string }>;
}

export interface MergeStats {
  personas_total: number;
  personas_reassigned: number;
  persona_collisions: MergeCollision[];
  tickets_total: number;
  tickets_reassigned: number;
  knowledge_entries_total: number;
  knowledge_entries_reassigned: number;
  knowledge_entries_skipped: number;
  wiki_pages_total: number;
  wiki_pages_reassigned: number;
  slug_collisions: MergeCollision[];
  rules_action: string | null;
  source_project_name?: string;
  target_project_name?: string;
  repos_total?: number;
  repos_reassigned?: number;
}

export interface MergeDryRunResponse {
  dry_run: true;
  stats: MergeStats;
  persona_collisions: MergeCollision[];
  slug_collisions: MergeCollision[];
  ke_duplicates: number[];
}

export interface MergeExecuteResponse {
  dry_run: false;
  status: 'completed' | 'failed';
  audit_id: string;
  stats: MergeStats;
  persona_renames: MergeCollision[];
  slug_renames: MergeCollision[];
  skipped_ke_ids: number[];
  skipped_link_ids: number[];
  rules_action: string;
  error_message?: string;
}

export interface KnowledgeEntry {
  id: number;
  project_id: string;
  session_id: string;
  entry_type: string;
  content: string;
  confidence: number;
  created_at: string;
  compiled_at: string | null;
  dismissed: boolean;
  claim_class: 'evidence' | 'claim' | 'note';
  freshness_class: 'current' | 'aging' | 'stale' | 'superseded';
  entity_ref: string | null;
  entity_type: string | null;
  superseded_by: number | null;
  supersession_reason: string | null;
  promoted_at: string | null;
  retrieved_count: number;
  used_in_answer_count: number;
  compiled_count: number;
}

export interface KnowledgeEntryListResponse {
  entries: KnowledgeEntry[];
  total: number;
}

export interface ContextCompilation {
  id: number;
  entries_compiled: number;
  context_before: string;
  context_after: string;
  compiled_at: string;
  /** 'manual' (a user clicked Compile) or 'scheduled' (the KB compile sweep). */
  trigger: string;
  context_words_before: number;
  context_words_after: number;
}

export interface CompileHistoryItem {
  id: number;
  compiled_at: string;
  trigger: string;
  entries_compiled: number;
  context_words_before: number;
  context_words_after: number;
}

export interface CompileStatusResponse {
  last_compile_at: string | null;
  pending_claims: number;
  compile_needed: boolean;
  compile_schedule: string;
  compile_after_claims: number | null;
  last_scheduled_compile_at: string | null;
  next_scheduled_at: string | null;
  compile_history: CompileHistoryItem[];
}

export interface ProjectHealthResponse {
  project_id: string;
  total_entries: number;
  pending_entries: number;
  uncompiled_notes?: number;
  auto_promotable_evidence?: number;
  compiled_entries: number;
  dismissed_entries: number;
  total_compilations: number;
  last_compilation_at: string | null;
  potentially_stale: boolean;
  recommendations: string[];
  stale_entry_count: number;
  low_confidence_count: number;
  decayed_count: number;
}

// ------------------------------------------------------------------
// Rules (v0.9.9)
// ------------------------------------------------------------------

export type ToolName = 'claude-code' | 'codex' | 'cursor' | 'copilot' | 'gemini' | string;

export interface ProjectRules {
  static_rules: string;
  include_knowledge: boolean;
  knowledge_types: string[];
  knowledge_max_tokens: number;
  include_context: boolean;
  context_sections: string[];
  context_max_tokens: number;
  tool_overrides: Record<string, unknown>;
  enabled_tools: ToolName[];
  version: number;
  updated_at: string;
}

/** GET /rules returns rules plus the ETag used for optimistic concurrency. */
export interface ProjectRulesResponse {
  rules: ProjectRules;
  etag: string;
}

export interface CompiledOutput {
  /** Target filename on disk, e.g. CLAUDE.md, codex.md, .cursorrules */
  filename: string;
  /** Compiled text content */
  content: string;
  /** Approximate token count for the compiled file */
  token_count: number;
}

export interface RulesVersionSummary {
  version: number;
  compiled_at: string;
  content_hash: string;
  compiled_by: string | null;
}

export interface RulesVersion extends RulesVersionSummary {
  static_rules: string;
  /** Map of tool name -> compiled output (filename/content/token_count) */
  compiled_outputs: Record<string, CompiledOutput>;
  knowledge_snapshot?: unknown;
  context_snapshot?: unknown;
}

export interface RulesVersionListResponse {
  versions: RulesVersionSummary[];
}

/** Per-tool entry in the POST /rules/compile response. */
export interface CompileOutputEntry {
  tool: string;
  filename: string;
  content: string;
  hash: string;
  token_count: number;
}

export interface CompileRulesResponse {
  version: number;
  created_new_version: boolean;
  aggregate_hash: string;
  outputs: CompileOutputEntry[];
}

export interface RestoreResponse extends SessionDetail {
  restored_from_scope?: 'cloud' | 'both' | null;
  local_copy_may_be_missing?: boolean;
}

// ------------------------------------------------------------------
// Personas (v0.10.1, Pro+)
// ------------------------------------------------------------------

export interface Persona {
  id: string;
  /** Null for org-level personas (GH #70). */
  project_id: string | null;
  /** Set for org-level personas. Absent on older servers. */
  org_id?: string | null;
  /** 'project' | 'org'. Absent on older servers (treat as 'project'). */
  scope?: 'project' | 'org';
  name: string;
  role: string;
  content: string;
  specializations: string[];
  is_active: boolean;
  version: number;
  created_by: string;
  /** GitHub #81 — org personas: the member who may edit it. Absent on older servers. */
  represented_user_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface PersonaCreate {
  name: string;
  role: string;
  content?: string;
  specializations?: string[];
  /** Org personas only: member user id or email. */
  represented_user?: string | null;
}

export interface PersonaUpdate {
  role?: string;
  content?: string;
  specializations?: string[];
  /** Org personas, org admins only: user id or email; null clears. */
  represented_user?: string | null;
}

// ------------------------------------------------------------------
// GitHub #92 — persona roll-up + represent-history
// ------------------------------------------------------------------

export interface RollupSession {
  id: string;
  title: string | null;
  created_at: string;
  project_id: string | null;
  ticket_id: string | null;
}

export interface RollupTicket {
  id: string;
  title: string;
  status: string;
  project_id: string;
  created_at: string;
}

export interface RollupKnowledgeEntry {
  id: number;
  entry_type: string;
  content: string;
  project_id: string;
  created_at: string;
}

export interface RollupAgentRun {
  id: string;
  status: string;
  project_id: string;
  created_at: string;
}

export interface PersonaRollup {
  persona: Persona;
  recent_sessions: RollupSession[];
  open_tickets: RollupTicket[];
  last_knowledge_entry: RollupKnowledgeEntry | null;
  last_agent_run: RollupAgentRun | null;
}

export interface RepresentHistoryEntry {
  actor_user_id: string | null;
  actor_email: string | null;
  actor_role: string | null;
  from_user_id: string | null;
  to_user_id: string | null;
  created_at: string;
}

/** An org persona that represents the caller (surfaced by /auth/me). */
export interface RepresentedPersona {
  persona_id: string;
  name: string;
  role: string;
  org_id: string;
  org_name: string;
}

// ------------------------------------------------------------------
// Tickets (v0.10.1, Team+)
// ------------------------------------------------------------------

export type TicketStatus =
  | 'suggested'
  | 'open'
  | 'in_progress'
  | 'blocked'
  | 'review'
  | 'done'
  | 'cancelled';

export type TicketPriority = 'low' | 'medium' | 'high' | 'critical';

export type TicketKind = 'task' | 'issue';

export interface Ticket {
  id: string;
  project_id: string;
  title: string;
  description: string;
  priority: TicketPriority | string;
  /** Persona assignee (agent role). Never set together with `assigned_user_id`. */
  assigned_to: string | null;
  /** Person assignee. Absent on older servers. */
  assigned_user_id?: string | null;
  assigned_user?: TicketAssignedUser | null;
  created_by_user_id: string;
  created_by_session_id: string | null;
  created_by_persona: string | null;
  status: TicketStatus | string;
  kind: TicketKind | string;
  parent_ticket_id: string | null;
  child_ticket_ids: string[];
  context_refs: string[];
  file_refs: string[];
  related_sessions: string[];
  acceptance_criteria: string[];
  resolver_session_id: string | null;
  resolver_user_id: string | null;
  completion_notes: string | null;
  changed_files: string[];
  knowledge_entry_ids: string[];
  depends_on: string[];
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  /** Jira link, present once the ticket has been promoted (Enterprise). */
  jira?: TicketJiraLink | null;
  jira_publish_in_progress?: boolean;
  jira_publish_unknown?: boolean;
  /** Calendar due date, "YYYY-MM-DD". Absent on older servers. */
  due_date?: string | null;
  /** Jira duration syntax, e.g. "2d 4h". Absent on older servers. */
  estimated_effort?: string | null;
  /** Who filed the ticket (email masked for captured-session readers). */
  created_by?: TicketUserSummary | null;
  /** Who resolved it, once resolved. */
  resolved_by?: TicketUserSummary | null;
  /** Newest events (at most 10); only on the single-ticket GET. */
  recent_events?: TicketEvent[];
}

/** A user referenced by a ticket (creator / resolver). */
export interface TicketUserSummary {
  user_id: string;
  display_name: string | null;
  /** As the server sends it: masked for readers without project standing. */
  email: string;
  persona?: string | null;
  session_id?: string | null;
}

/** Who caused a ticket event. */
export interface TicketEventActor {
  type: 'user' | 'service' | 'jira' | 'system' | string;
  user_id?: string | null;
  display_name?: string | null;
  persona?: string | null;
  /** Service key name, or the Jira-side label. */
  label?: string | null;
}

/** One row of a ticket's append-only event log. `before` / `after` are
 *  bounded values: free text arrives as `{changed, length}` and reference
 *  lists as `{count, items}`. */
export interface TicketEvent {
  id: string;
  action: string;
  field?: string | null;
  before?: unknown;
  after?: unknown;
  actor: TicketEventActor;
  created_at: string;
}

/** `GET .../tickets/{tid}/events`, newest first. */
export interface TicketEventsPage {
  events: TicketEvent[];
  /** Pass as `before` for the next (older) page; null on the last page. */
  next_before: string | null;
}

/** The person a ticket is assigned to (resolved by the server). */
export interface TicketAssignedUser {
  id: string;
  display_name: string | null;
  email: string;
}

/** `GET /api/v1/projects/{id}/assignable-users` row. */
export interface AssignableUser {
  id: string;
  display_name: string | null;
  email: string;
  can_edit: boolean;
}

/** A ticket's assignee: a persona (`assigned_to`) OR a person
 *  (`assigned_user_id`), never both non-null (422 `assignee_conflict`). */
export interface TicketAssigneeFields {
  assigned_to?: string | null;
  assigned_user_id?: string | null;
}

/** `PUT /api/v1/projects/{id}/tickets/{tid}` — partial update. */
export interface TicketUpdate extends TicketAssigneeFields {
  title?: string;
  description?: string;
  priority?: TicketPriority | string;
  /** "YYYY-MM-DD"; explicit null clears it. */
  due_date?: string | null;
  /** Jira duration ("2d 4h"); explicit null clears it. */
  estimated_effort?: string | null;
  lease_epoch?: number;
}

export interface TicketCreate {
  title: string;
  description: string;
  priority?: TicketPriority | string;
  assigned_to?: string | null;
  assigned_user_id?: string | null;
  acceptance_criteria?: string[];
  context_refs?: string[];
  file_refs?: string[];
  related_sessions?: string[];
  depends_on?: string[];
  kind?: TicketKind | string;
  parent_ticket_id?: string | null;
  due_date?: string | null;
  estimated_effort?: string | null;
}

export interface TicketComment {
  id: string;
  ticket_id: string;
  author_user_id: string;
  author_persona: string | null;
  content: string;
  session_id: string | null;
  created_at: string;
  /** 'sessionfs' (in-app/CLI) or 'jira' (mirrored inbound from Jira). */
  source?: string;
}

/** One trusted review round, derived server-side from verdict comments. */
export interface TicketReviewRound {
  round: number;
  /** Alias-folded: VERIFIED-CLEAN or CHANGES_REQUESTED. */
  verdict: string;
  raw_verdict: string;
  is_strict_verified_clean: boolean;
  /** The verdict comment this round came from. */
  comment_id: string;
  timestamp: string;
  findings_raised: number;
}

/** `GET .../tickets/{id}/review-state`. Only comments the server stamped as a
 *  trusted reviewer's verdict count as rounds; null without any. */
export interface TicketReviewState {
  ticket_id: string;
  review_state: {
    open_findings: { severity: string; text: string; round: number }[];
    closed_findings: { severity: string; text: string; round: number }[];
    last_verdict: string | null;
    rounds: TicketReviewRound[];
  } | null;
}

/** `GET /api/v1/tickets/{id}`: a ticket looked up by id alone, for previews. */
export interface TicketPreview extends Ticket {
  project_name: string;
  comment_count: number;
  /** Latest comments, newest first (at most 3). */
  recent_comments: TicketComment[];
}

// ------------------------------------------------------------------
// Ticket watchers + notifications (me-scoped)
// ------------------------------------------------------------------

export interface TicketWatcher {
  user_id: string;
  persona_name: string | null;
  source: string;
  muted: boolean;
  created_at: string;
}

export type TicketNotificationKind =
  | 'your_turn'
  | 'review_verdict'
  | 'status'
  | 'comment'
  | 'assignment'
  | 'dependency_unblocked';

export interface TicketNotification {
  id: number;
  ticket_id: string;
  project_id: string;
  ticket_title: string | null;
  kind: TicketNotificationKind | string;
  summary: string;
  actor_type: 'user' | 'service_key' | string;
  actor_user_id: string | null;
  service_key_name: string | null;
  event_count: number;
  created_at: string;
  updated_at: string;
  read_at: string | null;
}

export interface TicketNotificationList {
  items: TicketNotification[];
  next_before: number | null;
}

export interface TicketNotificationSettings {
  ticket_email: 'off' | 'your_turn' | 'all';
}

// ------------------------------------------------------------------
// AgentRuns (v0.10.2, Team+)
// ------------------------------------------------------------------

export type AgentRunStatus =
  | 'queued'
  | 'running'
  | 'passed'
  | 'failed'
  | 'errored'
  | 'cancelled';

export type Severity = 'none' | 'low' | 'medium' | 'high' | 'critical';

export interface AgentRun {
  id: string;
  project_id: string;
  persona_name: string;
  tool: string;
  trigger_source: string;
  status: AgentRunStatus | string;
  ticket_id: string | null;
  trigger_ref: string | null;
  ci_provider: string | null;
  ci_run_url: string | null;
  result_summary: string | null;
  severity: Severity | string | null;
  findings_count: number;
  findings: Record<string, unknown>[];
  fail_on: Severity | string | null;
  policy_result: 'pass' | 'fail' | string | null;
  exit_code: number | null;
  session_id: string | null;
  triggered_by_user_id: string | null;
  triggered_by_persona: string | null;
  // GitHub #95 — replay provenance + edited prompt/context.
  replayed_from: string | null;
  prompt: string | null;
  context_refs: string[];
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
}

/** GitHub #95 — replay payload (omitted fields copy from the parent run). */
export interface AgentRunReplayInput {
  persona_name?: string;
  ticket_id?: string;
  prompt?: string;
  context_refs?: string[];
  fail_on?: string;
  trigger_ref?: string;
}

export interface AgentRunReplayResult {
  run: AgentRun;
  command: string;
  ci_hint: string;
}

export interface AgentRunCompareResult {
  a_id: string;
  b_id: string;
  a_findings_count: number;
  b_findings_count: number;
  a_severity_counts: Record<string, number>;
  b_severity_counts: Record<string, number>;
  added: Record<string, unknown>[];
  removed: Record<string, unknown>[];
  unchanged: Record<string, unknown>[];
  /** True when any of added/removed/unchanged was capped for the response. */
  truncated?: boolean;
}

// ------------------------------------------------------------------
// Skills and plugins marketplace (v0.16)
// ------------------------------------------------------------------

export interface MarketplaceCompatibility {
  native_skills: boolean;
  instruction_files: number;
  omitted_components: string[];
}

export interface MarketplacePackage {
  listing_id: string;
  collection: string;
  visibility: 'public' | 'user' | 'org';
  /** Set for user-private listings; the owner may delete their own versions. */
  owner_user_id?: string | null;
  org_id?: string | null;
  /** Org that owns the package (null: personal/public). Absent on older servers. */
  owner_org_id?: string | null;
  verification_state: 'verified' | 'unverified' | 'blocked';
  /** Present when the server exposes moderation state on a listing. */
  is_yanked?: boolean;
  /** Listing moderation (marketplace v2 §4.3): 'approved' is deliverable;
   *  'pending_review' / 'rejected' are shown only to the publisher. */
  moderation_state?: 'active' | 'yanked' | 'blocked' | 'approved' | 'pending_review' | 'quarantined' | 'rejected';
  /** Publisher owning the namespace (null for unclaimed per-owner namespaces). */
  publisher_id?: string | null;
  publisher?: PublisherBlock | null;
  package_id: string;
  namespace: string;
  name: string;
  display_name: string;
  description: string;
  version_id: string;
  version: string | null;
  content_hash: string;
  source: {
    type: string;
    locator: string;
    subdir: string;
    commit: string | null;
  };
  risk: {
    scripts?: boolean;
    mcp?: boolean;
    executable_extensions?: boolean;
    /** SKILL.md frontmatter `hooks:` / `allowed-tools:` — executed or pre-approved on invocation. */
    hooks?: boolean;
    allowed_tools?: string[];
    hook_events?: string[];
    disclosed_metadata?: Record<string, Record<string, string>>;
    instruction_only_load?: boolean;
  };
  compatibility: Record<string, MarketplaceCompatibility>;
  license: { declared?: boolean; value?: string };
  signature: Record<string, unknown>;
  blob_size_bytes: number;
  published_at: string;
  untrusted?: boolean;
  untrusted_preamble?: string;
  untrusted_preview?: boolean;
  instruction_preview?: { path: string; content: string }[];
  /** Capability digest (marketplace v2 §3.2); null until computed. */
  capability_digest?: string | null;
  /** Discovery (marketplace v2 §9). Absent on older servers. */
  categories?: string[];
  deprecation?: MarketplaceDeprecation | null;
  freshness?: MarketplaceFreshness;
  installs?: MarketplaceInstalls;
  /** Install disclosure card (preview responses). */
  disclosure?: MarketplaceDisclosure | null;
}

// Native per-tool catalogs + works-in (marketplace v2 §7).
export interface NativeCatalog {
  scope: 'public' | 'org' | 'user';
  scope_id: string | null;
  marketplace: string;
  clone_url: string;
  current_commit: string | null;
  generated_at: string | null;
  plugin_count: number | null;
  export_remote: string | null;
  /** Keyless public GitHub mirror (public scope, when the deployment configures one). */
  mirror?: NativeCatalogMirror | null;
  /** tool -> { add?, cli?, install?, add_hosted? } command strings (tool's own syntax). */
  tools: Record<string, Record<string, string>>;
}

export interface NativeCatalogMirror {
  /** GitHub owner/repo, e.g. SessionFS/marketplace. */
  repo: string;
  url: string;
  branch: string;
}

export interface NativeCatalogList {
  items: NativeCatalog[];
  credential_helper: string;
  vendor_docs_checked: string;
}

export type WorksInBadge =
  | 'verified'
  | 'format-valid'
  | 'regressed'
  | 'failed'
  | 'format-invalid'
  | 'not-applicable'
  | 'unverified';

export interface WorksInLevel {
  status: 'pass' | 'fail' | 'regressed' | 'skipped';
  tool_version: string;
  run_at: string | null;
  reason: string;
  projection_hash: string | null;
}

export interface WorksIn {
  version_id: string;
  content_hash: string;
  vendor_docs_checked: string;
  tools: Record<string, { badge: WorksInBadge; levels: Partial<Record<'L0' | 'L1' | 'L2', WorksInLevel>> }>;
  expected_only: Record<string, { badge: 'expected'; omitted_components: string[] }>;
}

// Install disclosure + update diff (marketplace v2 §3). Every string is
// untrusted package text: render as text, never as markup.
export interface MarketplaceToolDisclosure {
  delivery: string;
  scope: 'project' | 'user';
  summary: string;
  writes: string[];
  writes_total: number;
  runs: { scripts: number; hooks: { skill: string; event: string; matcher: string }[]; mcp_local: number };
  connects_to: string[];
  auto_allowed: string[];
  omitted: { component: string; reason: string }[];
  consent_required: string[];
}

export interface UnicodeFinding {
  path: string;
  line: number;
  column: number;
  rule_id: string;
  class: string;
  severity: string;
  code_point: string | null;
  visible: string | null;
}

export interface ShellCommandDisclosure {
  path: string;
  line: number;
  command: string;
  commands: string[];
  text: string;
}

export interface MarketplaceDisclosure {
  version_id: string | null;
  content_hash: string | null;
  capability_digest: string | null;
  capabilities: string[];
  provenance: { publisher_verified: boolean; attested: boolean };
  scan: { ruleset?: string; max_severity?: string; finding_count?: number; counts?: Record<string, number>; truncated?: boolean };
  unicode: { flagged_files: number; findings: UnicodeFinding[]; counts?: Record<string, number>; truncated?: boolean };
  incomplete?: string[];
  shell_commands: ShellCommandDisclosure[];
  network_endpoints: string[];
  /** Full count (the list above is the first MAX shown). */
  network_endpoints_total?: number;
  env_names: string[];
  mcp_servers: {
    local: { name: string; command: string; env_names: string[] }[];
    remote: { name: string; origin: string; transport?: string }[];
    declared: boolean;
  };
  allowed_tools: string[];
  persistence_targets: string[];
  executable_extensions: { path: string; kind: string }[];
  tools: Record<string, MarketplaceToolDisclosure>;
  consent_required: string[];
  consent_rule_version?: number;
  card_version?: number;
  scope?: 'project' | 'user';
  /** Digest of this whole card; approvals bind to it. */
  disclosure_digest?: string;
}

/** Re-approval acknowledgements plus the disclosure the approver saw. */
export interface ApprovalBinding {
  widening?: boolean;
  codeChange?: boolean;
  capabilityDigest?: string | null;
  consentRuleVersion?: number | null;
  /** Digest of the whole card shown (scanner, hidden characters, provenance, tools, scope). */
  disclosureDigest?: string | null;
  /** Digest of the update reviewed (prior grant, baseline, target, full diff); from the 409. */
  updateDigest?: string | null;
}

export interface CapabilityChange {
  kind: string;
  item: string;
}

export interface UpdateTextDiffs {
  entries: {
    path: string;
    change: string;
    binary: boolean;
    diff: string | null;
    truncated?: boolean;
    /** Per-file hashes from the two manifests (null for the side without the file). */
    old_sha256?: string | null;
    new_sha256?: string | null;
  }[];
  truncated: boolean;
  unavailable?: boolean;
}

/**
 * The exact build_diff() object the update_digest hashes — returned as ONE
 * nested `diff` field by the reapproval 409 and the held-updates queue. An
 * acknowledgement is valid only for the diff that was rendered, so every
 * leaf of this object must reach the screen.
 */
export interface UpdateDiffDetail {
  update_class: 'unchanged' | 'text' | 'code' | 'widening';
  baseline_known: boolean;
  /** Digest of the consented baseline (known baselines only). */
  baseline_digest?: string;
  widens: CapabilityChange[];
  narrows: CapabilityChange[];
  code_changes: { kind: string; change: string; path: string }[];
  changed_requirements: string[];
  capability_digest_changed: boolean;
  files: { added: string[]; removed: string[]; changed: string[]; unknown?: boolean };
  scan_delta: {
    old_max_severity?: string | null;
    new_max_severity?: string | null;
    old_finding_count?: number | null;
    new_finding_count?: number | null;
    old_ruleset?: string | null;
    new_ruleset?: string | null;
  };
  unicode_delta: { new_findings: UnicodeFinding[] };
  provenance_delta: { old: Record<string, boolean>; new: Record<string, boolean> };
  consent: { personal: string; org_required: string; description: string };
  text_diffs?: UpdateTextDiffs;
}

export interface MarketplaceVersionDiff extends UpdateDiffDetail {
  package_id: string;
  from_version_id: string;
  to_version_id: string;
  from_content_hash?: string;
  to_content_hash?: string;
}

export interface MarketplaceSearchResponse {
  items: MarketplacePackage[];
  /** Number of items on THIS page (not a total). */
  count: number;
  has_more: boolean;
  /** Opaque keyset cursor for the next page; null when has_more is false. */
  next_cursor: string | null;
  sort?: 'recent' | 'relevance';
}

/** A count under the display floor (10) has count=null and display "<10". */
export interface FlooredCount {
  count: number | null;
  display: string;
  /** Rounded down to a multiple of 10 ("40+") because a part of its breakdown is hidden. */
  approximate?: boolean;
}

export interface MarketplaceDeprecation {
  deprecated_at: string | null;
  message: string | null;
  /** Only present when the caller can see the successor package. */
  successor: { package_id: string; namespace: string; name: string; display_name: string } | null;
}

export type VerifyStatus = 'unknown' | 'passing' | 'failing' | 'regressed';

export interface MarketplaceToolFreshness {
  last_updated_at: string | null;
  last_verified_at: string | null;
  /** The version that last passed; a status counts only for the current version. */
  last_verified_version_id?: string | null;
  /** 'unknown' whenever the result is not about the package's current version. */
  verify_status: VerifyStatus;
  stale: boolean;
}

export interface MarketplaceFreshness {
  stale: boolean;
  unmaintained: boolean;
  deprecated: boolean;
  last_updated_at: string | null;
  computed_at: string | null;
  tools: Record<string, MarketplaceToolFreshness>;
}

export interface MarketplaceInstalls {
  window_days: number;
  total: FlooredCount;
  by_tool: Record<string, FlooredCount>;
}

export interface MarketplaceSearchFilters {
  q?: string;
  tool?: string;
  category?: string;
  publisher?: string;
  verified?: boolean;
  attested?: boolean;
  license?: string;
  max_severity?: 'none' | 'low' | 'medium' | 'high' | 'critical';
  capability?: string;
  works_in?: string;
  updated_within?: number;
  include_deprecated?: boolean;
  sort?: 'recent' | 'relevance';
  limit?: number;
  cursor?: string;
}

export interface MarketplaceCategory {
  slug: string;
  display_name: string;
}

export interface MarketplaceReadme {
  version_id: string;
  package_id: string;
  source: 'readme' | 'skill_md' | 'none';
  path: string | null;
  /** Untrusted markdown: hidden characters already replaced by ⟦U+XXXX⟧. */
  content: string;
  hidden_unicode: { line: number; column: number; code_point: string; kind: string }[];
  truncated: boolean;
  untrusted: boolean;
  image_path_prefix: string;
}

export interface PublisherAnalyticsToolRow {
  tool: string;
  installs: FlooredCount;
  active: FlooredCount;
  uninstalls: FlooredCount;
  uninstall_rate: number | null;
  translation_failures: FlooredCount;
}

export interface PublisherAnalyticsPackage {
  package_id: string;
  namespace: string;
  name: string;
  display_name: string;
  current_version_id: string | null;
  current_version: string | null;
  installs: FlooredCount;
  active_installs: FlooredCount;
  uninstalls: FlooredCount;
  uninstall_rate: number | null;
  translation_failures: FlooredCount;
  installs_by_tool: Record<string, FlooredCount>;
  translation_failures_by_tool: Record<string, FlooredCount>;
  installs_by_version: { version_id: string; version: string | null; installs: FlooredCount }[];
  works_in: { tool: string; verify_status: VerifyStatus; last_verified_at: string | null; last_updated_at: string | null; stale: boolean }[];
  works_in_regressions: string[];
  stale: boolean;
  unmaintained: boolean;
  deprecated: boolean;
  categories: string[];
  deprecation_message: string | null;
  successor_package_id: string | null;
  readme_path: string | null;
  scan: { max_severity: string | null; counts: Record<string, number> };
  open_reports: number;
}

export interface PublisherAnalytics {
  publisher_id: string;
  namespace: string;
  window_days: number;
  since: string;
  k_floor: number;
  totals: {
    installs: FlooredCount;
    active_installs: FlooredCount;
    uninstalls: FlooredCount;
    uninstall_rate: number | null;
    translation_failures: FlooredCount;
    service_installs: FlooredCount;
  };
  by_tool: PublisherAnalyticsToolRow[];
  by_day: { day: string; installs: FlooredCount }[];
  packages: PublisherAnalyticsPackage[];
}

export interface PackageSettingsInput {
  categories?: string[];
  deprecated?: boolean;
  deprecation_message?: string | null;
  successor_package_id?: string | null;
  readme_path?: string | null;
}

export interface PackageSettings {
  package_id: string;
  namespace: string;
  name: string;
  categories: string[];
  deprecated_at: string | null;
  deprecation_message: string | null;
  successor_package_id: string | null;
  readme_path: string | null;
}

export interface OrgSkillUsage {
  org_id: string;
  window_days: number;
  since: string;
  items: {
    package_id: string;
    namespace: string;
    name: string;
    display_name: string;
    installs: number;
    active: number;
    uninstalls: number;
    translation_failures: number;
    by_tool: { tool: string; installs: number; active: number; uninstalls: number; translation_failures: number }[];
    stale: boolean;
    unmaintained: boolean;
    deprecated: boolean;
  }[];
  stale_or_deprecated: string[];
}

export interface SkillInstallRequest {
  id: string;
  project_id: string;
  version_id: string;
  content_hash: string;
  requested_by_user_id: string | null;
  requested_by_session_id: string | null;
  target_user_id: string | null;
  target_device_id: string | null;
  target_scope: 'project' | 'user' | 'device';
  target_tools: string[];
  requested_capabilities: string[];
  /** Request origin, display only (caller-supplied). */
  requested_via: 'api' | 'mcp';
  /** Approving one's own request requires the emailed single-use confirmation code. */
  self_approval_requires_confirmation: boolean;
  approval_challenge_pending: boolean;
  status: 'pending' | 'approved' | 'rejected' | 'cancelled' | 'applied' | 'revoked';
  /** Concrete grants behind the requested capabilities (from the version's risk summary). */
  hook_events?: string[];
  allowed_tools?: string[];
  risk?: { scripts?: boolean; mcp?: boolean; hooks?: boolean; executable_extensions?: boolean };
  approved_by_user_id: string | null;
  decided_at: string | null;
  created_at: string;
  /** The version's capability digest (marketplace v2 §3.2). */
  capability_digest?: string | null;
}

export interface SkillAssignment {
  id: string;
  org_id: string;
  version_id: string;
  content_hash: string;
  target_type: 'org' | 'user' | 'device';
  target_id: string;
  target_user_id: string | null;
  mode: 'required' | 'optional';
  target_tools: string[];
  policy: Record<string, unknown>;
  created_at: string;
}

/** One assignment whose package has a newer version (held-updates queue). */
export interface HeldUpdate {
  assignment: SkillAssignment;
  package_id: string;
  name: string;
  from_version_id: string;
  from_version: string | null;
  to_version_id: string;
  to_version: string | null;
  to_content_hash: string;
  update_class: 'unchanged' | 'text' | 'code' | 'widening';
  widens: CapabilityChange[];
  narrows?: CapabilityChange[];
  baseline_known?: boolean;
  code_changes: { kind: string; change: string; path: string }[];
  /** The exact diff the update_digest binds (same object as the reapproval 409's details.diff). */
  diff?: UpdateDiffDetail | null;
  /** Binds an acknowledged advance to exactly this baseline, target and diff. */
  update_digest?: string | null;
  /** True when the target's manifest could not be computed: no diff, no digest, never advanceable. */
  diff_unavailable?: boolean;
  held: boolean;
  requires: 'approve_widening' | 'approve_code_change' | null;
}

export interface HeldUpdatesPage {
  items: HeldUpdate[];
  count: number;
  has_more: boolean;
  next_cursor: string | null;
}

export interface AdvanceAssignmentInput {
  to_version_id: string;
  content_hash: string;
  approve_widening?: boolean;
  approve_code_change?: boolean;
  capability_digest?: string | null;
  consent_rule_version?: number | null;
  disclosure_digest?: string | null;
  update_digest?: string | null;
}

export interface SkillRegistry {
  id: string;
  owner_type: 'user' | 'project' | 'org';
  owner_id: string;
  org_id: string | null;
  alias: string;
  source_url: string;
  refresh_commit: string | null;
  policy_state: 'approved' | 'review' | 'blocked';
  /** True when the server polls this registry (GitHub #69). */
  server_fetch_enabled: boolean;
  created_at: string;
  updated_at: string;
  // Server-side polling (older servers omit these).
  poll_interval_seconds?: number;
  tag_pattern?: string;
  branch_only_mode?: boolean;
  branch?: string | null;
  /** A reference (env:/k8s:), never a credential. */
  credential_secret_ref?: string | null;
  publish_namespace?: string | null;
  last_poll_at?: string | null;
  last_poll_status?: 'ok' | 'partial' | 'error' | null;
  last_poll_error_summary?: string | null;
  consecutive_failures?: number;
  next_poll_at?: string | null;
  poll_requested_at?: string | null;
  backoff?: boolean;
}

export interface SkillRegistryUpdate {
  policy_state?: 'approved' | 'review' | 'blocked';
  poll_interval_seconds?: number;
  tag_pattern?: string;
  branch_only_mode?: boolean;
  branch?: string;
  credential_secret_ref?: string;
  publish_namespace?: string;
}

// ------------------------------------------------------------------
// Marketplace publishing + moderation (marketplace v2)
// Thin shapes over routes/marketplace.py and routes/marketplace_moderation.py.
// Optional fields are tolerated so server additions don't break rendering.
// ------------------------------------------------------------------

export const REPORT_REASONS = [
  'malicious',
  'secrets',
  'impersonation',
  'broken',
  'license',
  'spam',
  'other',
] as const;
export type ReportReason = (typeof REPORT_REASONS)[number];

export type PublishTarget =
  | { kind: 'personal' }
  | { kind: 'org'; orgId: string; visibility: 'org' | 'public' };

export interface PublishPackageInput {
  target: PublishTarget;
  file: File | Blob;
  filename?: string;
  /** Blank/undefined = server default (`personal` or `org`). */
  namespace?: string;
  collection?: string;
}

export interface MarketplaceReport {
  id: string;
  package_id: string;
  version_id: string | null;
  reason: string;
  details: string;
  status: string;
  created_at: string;
  resolved_at: string | null;
  reporter_user_id?: string | null;
  resolved_by_user_id?: string | null;
  resolution_note?: string | null;
  /**
   * Scope the report was bound to when filed ('platform' | 'org'), when the
   * server exposes it (report scoping is being tightened server-side).
   */
  scope_kind?: string | null;
  scope_org_id?: string | null;
}

export interface MarketplaceReportList {
  items: MarketplaceReport[];
  count: number;
  has_more: boolean;
  next_cursor: string | null;
}

export type ModerationAction = 'yank' | 'block' | 'restore';

/** One critical instruction-scan finding a quarantine restore clears as a false positive. */
export interface ScanFindingKey {
  rule_id: string;
  path: string;
  line_sha: string;
}

/** Listed by the 409 `findings_required` answer to a quarantine restore. */
export interface QuarantineFinding extends ScanFindingKey {
  line: number;
}
export type ModerationTargetKind = 'versions' | 'packages';

export interface ModerationResult {
  package_id: string;
  version_id: string | null;
  content_hash: string | null;
  state: 'yanked' | 'blocked' | 'restored';
  previous_state: string;
  reason: string;
  revocation_event_ids: number[];
  reports_resolved: number;
  grants_revoked: Record<string, number>;
}

export interface RevocationEvent {
  /** Null for `kind: "content"` (hash-wide) events. */
  package_id: string | null;
  version_id: string | null;
  content_hash: string | null;
  /** "quarantined": an automated scan stopped new installs (warn only). Unknown future states are ignored. */
  state: 'yanked' | 'blocked' | 'restored' | 'quarantined' | (string & {});
  reason: string;
  changed_at: string;
  /** Absent on older servers. */
  kind?: 'version' | 'package' | 'content' | (string & {});
  block_scope?: 'global' | 'org' | 'owner' | null;
  /** The org for "org" scope, and for "owner" scope on org-owned packages (null: personal). Absent on older servers. */
  scope_org_id?: string | null;
}

export interface RevocationFeed {
  revocations: RevocationEvent[];
  next_since: string;
  /** Absent on older servers: page until an empty page instead. */
  has_more?: boolean;
}

// ── Project groups (GitHub #101) ──

/** A member's role in a project group. Org admins/owners always act as `admin`. */
export type ProjectGroupRole = 'reader' | 'editor' | 'admin';

export interface ProjectGroupSummary {
  id: string;
  org_id: string;
  slug: string;
  name: string;
  description: string | null;
  /** Explicit grants on the group (org admins' implicit standing is not counted). */
  member_count: number;
  project_count: number;
  /** The caller's effective role; null when they have no standing. */
  my_role: ProjectGroupRole | null;
}

export interface ProjectGroupMember {
  user_id: string;
  email: string;
  role: ProjectGroupRole;
  granted_by_user_id: string | null;
  created_at: string | null;
}

export interface ProjectGroupDetail {
  id: string;
  org_id: string;
  slug: string;
  name: string;
  description: string | null;
  my_role: ProjectGroupRole | null;
  members: ProjectGroupMember[];
  /** Ids of the member projects. */
  projects: string[];
}

export interface ProjectGroupCreate {
  name: string;
  slug?: string;
  description?: string | null;
}

/** Partial update: omitted keys stay; `description: null` clears it. */
export interface ProjectGroupUpdate {
  name?: string;
  slug?: string;
  description?: string | null;
}

export class ApiError extends Error {
  status: number;
  /** v0.10.x envelope code, when the response body parses cleanly. */
  code?: string;
  /** Structured details from the envelope (e.g. retry hints, bounds). */
  details?: Record<string, unknown>;
  /** Raw response body — kept for power-user diagnostics / legacy logging. */
  raw: string;
  constructor(status: number, body: string) {
    // v0.10.24 tk_e7da4c4508d94bac Codex R1 MED #2 — parse the v0.10.x
    // error envelope so toasts surface "code: message" instead of raw
    // JSON. Falls back to the body text for legacy / plain-text 5xx
    // responses so nothing regresses.
    const parsed = parseApiErrorBody(body, status);
    super(parsed.message);
    this.status = status;
    this.code = parsed.code;
    this.details = parsed.details;
    this.raw = body;
  }
}

interface ParsedApiError {
  code?: string;
  message: string;
  details?: Record<string, unknown>;
}

export function parseApiErrorBody(body: string, status: number): ParsedApiError {
  if (!body) return { message: `HTTP ${status}` };
  try {
    const json = JSON.parse(body);
    if (json && typeof json === 'object') {
      const error = (json as Record<string, unknown>).error;
      if (error && typeof error === 'object') {
        const e = error as Record<string, unknown>;
        const code = typeof e.code === 'string' ? e.code : undefined;
        const message = typeof e.message === 'string' && e.message
          ? (code ? `${code}: ${e.message}` : e.message)
          : (code ?? `HTTP ${status}`);
        const details = e.details && typeof e.details === 'object'
          ? (e.details as Record<string, unknown>)
          : undefined;
        return { code, message, details };
      }
      // Legacy `detail` shape (older FastAPI defaults).
      const detail = (json as Record<string, unknown>).detail;
      if (typeof detail === 'string' && detail) {
        return { message: detail };
      }
      if (detail && typeof detail === 'object') {
        const d = detail as Record<string, unknown>;
        const msg = (typeof d.message === 'string' && d.message)
          || (typeof d.error === 'string' && d.error);
        if (msg) return { message: msg as string };
      }
    }
  } catch {
    // body wasn't JSON — fall through to raw text
  }
  return { message: body };
}

export function createApiClient(baseUrl: string, apiKey: string) {
  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        ...init?.headers,
      },
    });
    if (!resp.ok) {
      const body = await resp.text();
      throw new ApiError(resp.status, body);
    }
    // 204 No Content (DELETE routes) and empty bodies resolve to undefined
    // instead of failing on `resp.json()` after a successful call.
    if (resp.status === 204 || resp.headers.get('content-length') === '0') {
      return undefined as T;
    }
    const text = await resp.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  /**
   * Variant of request() that also exposes response headers (needed for
   * ETag-based optimistic concurrency on the Rules endpoints).
   */
  async function requestWithEtag<T>(
    path: string,
    init?: RequestInit,
  ): Promise<{ data: T; etag: string }> {
    const resp = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        ...init?.headers,
      },
    });
    if (!resp.ok) {
      const body = await resp.text();
      throw new ApiError(resp.status, body);
    }
    const etag = resp.headers.get('ETag') || resp.headers.get('etag') || '';
    const data = (await resp.json()) as T;
    return { data, etag };
  }

  return {
    health: () => request<{ status: string }>('/health'),

    getMe: () => request<{
      user_id: string;
      email: string;
      display_name: string | null;
      email_verified: boolean;
      tier: string;
      effective_tier: string;
      org_id: string | null;
      org_name: string | null;
      org_role: string | null;
      created_at: string | null;
      last_client_version: string | null;
      last_client_platform: string | null;
      last_client_device: string | null;
      last_sync_at: string | null;
      latest_version: string;
      default_org_id: string | null;
      capture_health: {
        name: string;
        health: string;
        degraded_since: string | null;
        last_error: string | null;
        sessions_tracked: number;
      }[] | null;
      /** GitHub #92 — org personas that represent this user. */
      represented_personas: RepresentedPersona[];
    }>('/api/v1/auth/me'),

    listSessions: (params: {
      page?: number;
      page_size?: number;
      source_tool?: string;
      model?: string[];
      tag?: string;
      /** Server-side ordering; omitted = updated_at desc (the server default). */
      sort?: SessionListSort;
      order?: 'asc' | 'desc';
    } = {}) => {
      const sp = new URLSearchParams();
      if (params.page) sp.set('page', String(params.page));
      if (params.page_size) sp.set('page_size', String(params.page_size));
      if (params.source_tool) sp.set('source_tool', params.source_tool);
      if (params.model && params.model.length) {
        for (const m of params.model) sp.append('model', m);
      }
      if (params.tag) sp.set('tag', params.tag);
      if (params.sort) sp.set('sort', params.sort);
      if (params.order) sp.set('order', params.order);
      return request<SessionListResponse>(`/api/v1/sessions?${sp}`);
    },

    listSessionModels: (params: { source_tool?: string; deleted?: boolean } = {}) => {
      const sp = new URLSearchParams();
      if (params.source_tool) sp.set('source_tool', params.source_tool);
      if (params.deleted) sp.set('deleted', 'true');
      return request<SessionModelsResponse>(`/api/v1/sessions/models?${sp}`);
    },

    getSession: (id: string) =>
      request<SessionDetail>(`/api/v1/sessions/${id}`),

    deleteSession: (id: string, scope: 'cloud' | 'everywhere' = 'cloud') =>
      request<SessionDetail>(`/api/v1/sessions/${id}?scope=${scope}`, { method: 'DELETE' }),

    listDeletedSessions: () =>
      request<SessionListResponse>('/api/v1/sessions?deleted=true'),

    restoreSession: (id: string) =>
      request<RestoreResponse>(`/api/v1/sessions/${id}/restore`, { method: 'POST' }),

    adminPurgeDeleted: (sessionId?: string) =>
      request<{ purged: number; bytes_reclaimed: number }>('/api/v1/admin/purge-deleted', {
        method: 'POST',
        body: sessionId ? JSON.stringify({ session_id: sessionId }) : undefined,
      }),

    getMessages: (
      id: string,
      page = 1,
      pageSize = 50,
      order: 'oldest' | 'newest' = 'oldest',
      around?: number,
    ) =>
      request<MessagesResponse>(
        `/api/v1/sessions/${id}/messages?page=${page}&page_size=${pageSize}&order=${order}` +
          (around != null ? `&around=${around}` : ''),
      ),

    getChanges: (id: string) => request<ChangesResponse>(`/api/v1/sessions/${id}/changes`),

    search: (params: URLSearchParams) =>
      request<{
        results: {
          session_id: string;
          title: string | null;
          alias: string | null;
          source_tool: string;
          model_id: string | null;
          message_count: number;
          updated_at: string;
          matches: { snippet: string }[];
        }[];
        total: number;
        page: number;
        page_size: number;
        query: string;
      }>(`/api/v1/sessions/search?${params}`),

    createHandoff: (sessionId: string, recipientEmail: string, message?: string) =>
      request<CreateHandoffResponse>('/api/v1/handoffs', {
        method: 'POST',
        body: JSON.stringify({
          session_id: sessionId,
          recipient_email: recipientEmail,
          ...(message ? { message } : {}),
        }),
      }),

    getHandoff: (handoffId: string) =>
      request<HandoffDetail>(`/api/v1/handoffs/${handoffId}`),

    claimHandoff: (handoffId: string) =>
      request<HandoffDetail>(`/api/v1/handoffs/${handoffId}/claim`, {
        method: 'POST',
      }),

    listInbox: () =>
      request<HandoffListResponse>('/api/v1/handoffs/inbox'),

    listSent: () =>
      request<HandoffListResponse>('/api/v1/handoffs/sent'),

    getHandoffSummary: (handoffId: string) =>
      request<HandoffSessionSummary>(`/api/v1/handoffs/${handoffId}/summary`),

    downloadSession: async (id: string): Promise<Blob> => {
      const resp = await fetch(`${baseUrl}/api/v1/sessions/${id}/download`, {
        headers: { Authorization: `Bearer ${apiKey}` },
      });
      if (!resp.ok) throw new ApiError(resp.status, 'Download failed');
      return resp.blob();
    },

    runAudit: (
      sessionId: string,
      model: string,
      llmApiKey: string,
      provider?: string,
      baseUrl?: string,
    ) =>
      request<AuditReport>(`/api/v1/sessions/${sessionId}/audit`, {
        method: 'POST',
        body: JSON.stringify({
          model,
          llm_api_key: llmApiKey || undefined,
          ...(provider ? { provider } : {}),
          ...(baseUrl ? { base_url: baseUrl } : {}),
        }),
      }),

    getAudit: (sessionId: string) =>
      request<AuditReport>(`/api/v1/sessions/${sessionId}/audit`),

    getJudgeSettings: () =>
      request<JudgeSettings>('/api/v1/settings/judge'),

    saveJudgeSettings: (provider: string, model: string, apiKey: string, baseUrl?: string) =>
      request<JudgeSettings & { warnings?: SaveWarning[] }>('/api/v1/settings/judge', {
        method: 'PUT',
        body: JSON.stringify({ provider, model, api_key: apiKey, base_url: baseUrl || null }),
      }),

    clearJudgeSettings: () =>
      request<void>('/api/v1/settings/judge', {
        method: 'DELETE',
      }),

    // One minimal chat request with the form's values (or the saved key,
    // only for the provider/endpoint it was saved with).
    testJudgeConnection: (body: LlmConnectionTestBody) =>
      request<LlmConnectionTestResult>('/api/v1/settings/judge/test', {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    testOrgLlmConnection: (orgId: string, body: LlmConnectionTestBody) =>
      request<LlmConnectionTestResult>(`/api/v1/orgs/${orgId}/llm-settings/test`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    // First-class LLM providers for the judge/helper pickers.
    getLlmProviders: () =>
      request<{ providers: LlmProvider[] }>('/api/v1/llm/providers'),

    // Org-wide LLM key for the session helper (Enterprise, owner/admin).
    getOrgLlmSettings: (orgId: string) =>
      request<OrgLlmSettings>(`/api/v1/orgs/${orgId}/llm-settings`),

    saveOrgLlmSettings: (
      orgId: string,
      body: { provider: string; model: string; apiKey?: string; baseUrl?: string | null; enabled: boolean },
    ) =>
      request<OrgLlmSettings & { warnings?: SaveWarning[] }>(`/api/v1/orgs/${orgId}/llm-settings`, {
        method: 'PUT',
        body: JSON.stringify({
          provider: body.provider,
          model: body.model,
          api_key: body.apiKey ?? '',
          base_url: body.baseUrl ?? null,
          enabled: body.enabled,
        }),
      }),

    deleteOrgLlmSettings: (orgId: string) =>
      request<{ deleted: boolean }>(`/api/v1/orgs/${orgId}/llm-settings`, {
        method: 'DELETE',
      }),

    // Jira integration (Enterprise, org owner/admin). The connection GET is
    // admin-only too, since it exposes credential/config state; a non-admin
    // member learns a ticket's Jira state from the ticket response instead.
    getJiraConnection: (orgId: string) =>
      request<JiraConnection>(`/api/v1/orgs/${orgId}/integrations/jira`),

    saveJiraConnection: (orgId: string, body: JiraConnectionRequest) =>
      request<JiraConnection>(`/api/v1/orgs/${orgId}/integrations/jira`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    deleteJiraConnection: (orgId: string) =>
      request<void>(`/api/v1/orgs/${orgId}/integrations/jira`, { method: 'DELETE' }),

    // Re-detect the site's sprint / epic-link custom fields (org admin). Read
    // only: saving the connection is what stores them.
    detectJiraFields: (orgId: string) =>
      request<JiraDetectedFields>(`/api/v1/orgs/${orgId}/jira/fields/detect`, { method: 'POST' }),

    searchJiraProjects: (orgId: string, params: { q?: string; limit?: number } = {}) => {
      const qs = new URLSearchParams();
      const q = params.q?.trim();
      if (q) qs.set('q', q.slice(0, 100));
      qs.set('limit', String(params.limit ?? 50));
      return request<JiraProjectPage>(
        `/api/v1/orgs/${orgId}/integrations/jira/projects?${qs.toString()}`,
      );
    },

    listJiraIssueTypes: (orgId: string, projectKey: string) =>
      request<JiraIssueType[]>(
        `/api/v1/orgs/${orgId}/integrations/jira/projects/${encodeURIComponent(projectKey)}/issue-types`,
      ),

    listJiraMappings: (orgId: string) =>
      request<JiraMapping[]>(`/api/v1/orgs/${orgId}/integrations/jira/mappings`),

    saveJiraMapping: (orgId: string, projectId: string, body: JiraMappingRequest) =>
      request<JiraMapping>(
        `/api/v1/orgs/${orgId}/integrations/jira/mappings/${encodeURIComponent(projectId)}`,
        { method: 'PUT', body: JSON.stringify(body) },
      ),

    deleteJiraMapping: (orgId: string, projectId: string) =>
      request<void>(
        `/api/v1/orgs/${orgId}/integrations/jira/mappings/${encodeURIComponent(projectId)}`,
        { method: 'DELETE' },
      ),

    getJiraHealth: (orgId: string) =>
      request<JiraHealth>(`/api/v1/orgs/${orgId}/integrations/jira/health`),

    getJiraSyncEvents: (
      orgId: string,
      params: { limit?: number; direction?: 'inbound' | 'outbound' } = {},
    ) => {
      const qs = new URLSearchParams();
      qs.set('limit', String(params.limit ?? 50));
      if (params.direction) qs.set('direction', params.direction);
      return request<JiraSyncEventsPage>(
        `/api/v1/orgs/${orgId}/integrations/jira/sync-events?${qs.toString()}`,
      );
    },

    updateJiraSync: (orgId: string, body: JiraSyncSettingsRequest) =>
      request<JiraConnection>(`/api/v1/orgs/${orgId}/integrations/jira/sync`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      }),

    rotateJiraWebhookSecret: (orgId: string) =>
      request<{ webhook_secret: string; webhook_secret_last4: string }>(
        `/api/v1/orgs/${orgId}/integrations/jira/webhook-secret`,
        { method: 'POST' },
      ),

    // Org-custom Jira templates (admin-only). Built-ins are returned first,
    // read-only (`builtin: true`, `version: null`).
    listJiraTemplates: (orgId: string) =>
      request<JiraTemplate[]>(`/api/v1/orgs/${orgId}/integrations/jira/templates`),

    createJiraTemplate: (orgId: string, body: JiraTemplateRequest) =>
      request<JiraTemplate>(`/api/v1/orgs/${orgId}/integrations/jira/templates`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    getJiraTemplate: (orgId: string, slug: string) =>
      request<JiraTemplate>(
        `/api/v1/orgs/${orgId}/integrations/jira/templates/${encodeURIComponent(slug)}`,
      ),

    updateJiraTemplate: (orgId: string, slug: string, body: JiraTemplateRequest) =>
      request<JiraTemplate>(
        `/api/v1/orgs/${orgId}/integrations/jira/templates/${encodeURIComponent(slug)}`,
        { method: 'PUT', body: JSON.stringify(body) },
      ),

    deleteJiraTemplate: (orgId: string, slug: string) =>
      request<void>(
        `/api/v1/orgs/${orgId}/integrations/jira/templates/${encodeURIComponent(slug)}`,
        { method: 'DELETE' },
      ),

    listJiraTemplateVersions: (orgId: string, slug: string) =>
      request<JiraTemplateVersion[]>(
        `/api/v1/orgs/${orgId}/integrations/jira/templates/${encodeURIComponent(slug)}/versions`,
      ),

    unarchiveJiraTemplate: (orgId: string, slug: string) =>
      request<JiraTemplate>(
        `/api/v1/orgs/${orgId}/integrations/jira/templates/${encodeURIComponent(slug)}/unarchive`,
        { method: 'POST' },
      ),

    previewJiraTemplate: (orgId: string, slug: string, body: JiraTemplatePreviewRequest) =>
      request<JiraTemplatePreview>(
        `/api/v1/orgs/${orgId}/integrations/jira/templates/${encodeURIComponent(slug)}/preview`,
        { method: 'POST', body: JSON.stringify(body) },
      ),

    // Confluence integration (Enterprise). Org routes are owner/admin only;
    // the project routes reuse knowledge:read / knowledge:write.
    getConfluenceConnection: (orgId: string) =>
      request<ConfluenceConnection>(`/api/v1/orgs/${orgId}/integrations/confluence`),

    saveConfluenceConnection: (orgId: string, body: ConfluenceConnectionRequest) =>
      request<ConfluenceConnection>(`/api/v1/orgs/${orgId}/integrations/confluence`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    deleteConfluenceConnection: (orgId: string) =>
      request<void>(`/api/v1/orgs/${orgId}/integrations/confluence`, { method: 'DELETE' }),

    searchConfluenceSpaces: (orgId: string, params: { q?: string; limit?: number } = {}) => {
      const qs = new URLSearchParams();
      if (params.q) qs.set('q', params.q);
      if (params.limit) qs.set('limit', String(params.limit));
      return request<ConfluenceSpaceSearch>(
        `/api/v1/orgs/${orgId}/integrations/confluence/spaces?${qs.toString()}`,
      );
    },

    listConfluenceSpacePages: (
      orgId: string,
      spaceKey: string,
      params: { q?: string; limit?: number } = {},
    ) => {
      const qs = new URLSearchParams();
      if (params.q) qs.set('q', params.q);
      if (params.limit) qs.set('limit', String(params.limit));
      return request<ConfluenceSpacePage[]>(
        `/api/v1/orgs/${orgId}/integrations/confluence/spaces/${encodeURIComponent(spaceKey)}/pages?${qs.toString()}`,
      );
    },

    listConfluenceTemplates: (orgId: string) =>
      request<ConfluenceTemplate[]>(`/api/v1/orgs/${orgId}/integrations/confluence/templates`),

    listConfluenceMappings: (orgId: string) =>
      request<ConfluenceMapping[]>(`/api/v1/orgs/${orgId}/integrations/confluence/mappings`),

    saveConfluenceMapping: (orgId: string, projectId: string, body: ConfluenceMappingRequest) =>
      request<ConfluenceMapping>(
        `/api/v1/orgs/${orgId}/integrations/confluence/mappings/${encodeURIComponent(projectId)}`,
        { method: 'PUT', body: JSON.stringify(body) },
      ),

    deleteConfluenceMapping: (orgId: string, projectId: string) =>
      request<void>(
        `/api/v1/orgs/${orgId}/integrations/confluence/mappings/${encodeURIComponent(projectId)}`,
        { method: 'DELETE' },
      ),

    getConfluenceHealth: (orgId: string) =>
      request<ConfluenceHealth>(`/api/v1/orgs/${orgId}/integrations/confluence/health`),

    previewConfluence: (
      projectId: string,
      body: { kind: 'wiki_page' | 'context'; slug?: string; template_override?: string },
    ) =>
      request<ConfluencePreview>(`/api/v1/projects/${projectId}/confluence/preview`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    publishConfluence: (projectId: string, body: ConfluencePublishRequest) =>
      request<{ page_id: string; page_url: string; version: number }>(
        `/api/v1/projects/${projectId}/confluence/publish`,
        { method: 'POST', body: JSON.stringify(body) },
      ),

    listConfluenceLinks: (projectId: string) =>
      request<ConfluenceLinksResponse>(`/api/v1/projects/${projectId}/confluence/links`),

    unlinkConfluence: (projectId: string, linkId: string) =>
      request<void>(`/api/v1/projects/${projectId}/confluence/links/${encodeURIComponent(linkId)}`, {
        method: 'DELETE',
      }),

    resolveConfluenceLink: (
      projectId: string,
      linkId: string,
      body: { action: 'overwrite' | 'unlink' },
    ) =>
      request<{ id: string; status: string }>(
        `/api/v1/projects/${projectId}/confluence/links/${encodeURIComponent(linkId)}/resolve`,
        { method: 'POST', body: JSON.stringify(body) },
      ),

    // Session helper (Enterprise) — read-only conversation management + status.
    // Turn streaming bypasses request() (raw fetch + ReadableStream) because it
    // needs to read the SSE body incrementally; see helper/HelperPanel.tsx.
    getHelperStatus: (scope?: { project_id?: string; group_id?: string; session_ids?: string[] }) => {
      const sp = new URLSearchParams();
      if (scope?.group_id) sp.set('group_id', scope.group_id);
      if (scope?.project_id) sp.set('project_id', scope.project_id);
      for (const id of scope?.session_ids ?? []) sp.append('session_ids', id);
      const qs = sp.toString();
      return request<HelperStatus>(`/api/v1/helper/status${qs ? `?${qs}` : ''}`);
    },

    listHelperConversations: () =>
      request<HelperConversationSummary[]>('/api/v1/helper/conversations'),

    getHelperConversation: (conversationId: string) =>
      request<HelperConversation>(
        `/api/v1/helper/conversations/${encodeURIComponent(conversationId)}`,
      ),

    deleteHelperConversation: (conversationId: string) =>
      request<{ deleted: boolean }>(
        `/api/v1/helper/conversations/${encodeURIComponent(conversationId)}`,
        { method: 'DELETE' },
      ),

    // Helper model settings, custom instructions and usage. PUT merges: omitted
    // fields keep their value, an explicit null resets/clears.
    getUserHelperSettings: () => request<UserHelperSettings>('/api/v1/settings/helper'),

    saveUserHelperSettings: (body: UserHelperSettingsUpdate) =>
      request<UserHelperSettings>('/api/v1/settings/helper', {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    getOrgHelperSettings: (orgId: string) =>
      request<OrgHelperSettings>(`/api/v1/orgs/${encodeURIComponent(orgId)}/helper-settings`),

    saveOrgHelperSettings: (orgId: string, body: OrgHelperSettingsUpdate) =>
      request<OrgHelperSettings>(`/api/v1/orgs/${encodeURIComponent(orgId)}/helper-settings`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    // Confirm-first write actions: the helper only proposes; these apply or drop.
    getHelperAction: (actionId: string) =>
      request<HelperAction>(`/api/v1/helper/actions/${encodeURIComponent(actionId)}`),

    confirmHelperAction: (actionId: string, body?: HelperActionConfirmBody) =>
      request<HelperAction>(`/api/v1/helper/actions/${encodeURIComponent(actionId)}/confirm`, {
        method: 'POST',
        ...(body ? { body: JSON.stringify(body) } : {}),
      }),

    cancelHelperAction: (actionId: string) =>
      request<HelperAction>(`/api/v1/helper/actions/${encodeURIComponent(actionId)}/cancel`, {
        method: 'POST',
      }),

    // POST so a typed API key travels in the body, never in a URL/access log.
    // A registry provider id routes discovery to that provider's fixed
    // /models endpoint; otherwise a custom base_url is queried.
    discoverModels: (baseUrl: string, apiKey?: string, provider?: string) =>
      request<DiscoveredModels>('/api/v1/settings/judge/models', {
        method: 'POST',
        body: discoverModelsBody(baseUrl, apiKey, provider),
      }),

    // Same contract for the org helper key form (owner/admin). Without a
    // typed key the server uses the stored org key only for the exact
    // provider/endpoint it was saved with.
    discoverOrgModels: (orgId: string, baseUrl: string, apiKey?: string, provider?: string) =>
      request<DiscoveredModels>(`/api/v1/orgs/${encodeURIComponent(orgId)}/llm-settings/models`, {
        method: 'POST',
        body: discoverModelsBody(baseUrl, apiKey, provider),
      }),

    // Admin endpoints
    adminListUsers: (params: { page?: number; page_size?: number; search?: string } = {}) => {
      const sp = new URLSearchParams();
      if (params.page) sp.set('page', String(params.page));
      if (params.page_size) sp.set('page_size', String(params.page_size));
      if (params.search) sp.set('search', params.search);
      return request<AdminUserListResponse>(`/api/v1/admin/users?${sp}`);
    },

    adminGetUser: (userId: string) =>
      request<AdminUser>(`/api/v1/admin/users/${userId}`),

    adminChangeTier: (userId: string, tier: string) =>
      request<void>(`/api/v1/admin/users/${userId}/tier`, {
        method: 'PUT',
        body: JSON.stringify({ tier }),
      }),

    adminVerifyUser: (userId: string) =>
      request<void>(`/api/v1/admin/users/${userId}/verify`, {
        method: 'POST',
      }),

    adminDeleteUser: (userId: string) =>
      request<void>(`/api/v1/admin/users/${userId}`, {
        method: 'DELETE',
      }),

    adminListSessions: (params: { page?: number; page_size?: number; user_id?: string } = {}) => {
      const sp = new URLSearchParams();
      if (params.page) sp.set('page', String(params.page));
      if (params.page_size) sp.set('page_size', String(params.page_size));
      if (params.user_id) sp.set('user_id', params.user_id);
      return request<SessionListResponse>(`/api/v1/admin/sessions?${sp}`);
    },

    adminDeleteSession: (sessionId: string) =>
      request<void>(`/api/v1/admin/sessions/${sessionId}`, {
        method: 'DELETE',
      }),

    adminGetStats: () =>
      request<AdminStats>('/api/v1/admin/stats'),

    adminGetActionLog: (params: { page?: number; page_size?: number } = {}) => {
      const sp = new URLSearchParams();
      if (params.page) sp.set('page', String(params.page));
      if (params.page_size) sp.set('page_size', String(params.page_size));
      return request<{ actions: AdminActionLog[]; total: number }>(`/api/v1/admin/audit-log?${sp}`);
    },

    adminListLicenses: (status?: string) =>
      request<{ licenses: HelmLicense[] }>(`/api/v1/admin/licenses?status=${status || 'all'}`),

    adminCreateLicense: (data: CreateLicenseRequest) =>
      request<HelmLicense>('/api/v1/admin/licenses', {
        method: 'POST',
        body: JSON.stringify(data),
      }),

    adminExtendLicense: (key: string, days: number) =>
      request<HelmLicense>(`/api/v1/admin/licenses/${key}/extend`, {
        method: 'PUT',
        body: JSON.stringify({ days }),
      }),

    adminRevokeLicense: (key: string, reason: string) =>
      request<{ status: string }>(`/api/v1/admin/licenses/${key}`, {
        method: 'PUT',
        body: JSON.stringify({ status: 'revoked', notes: reason }),
      }),

    adminGetLicenseHistory: (key: string) =>
      request<{ validations: LicenseValidation[] }>(`/api/v1/admin/licenses/${key}/history`),

    setAlias: (sessionId: string, alias: string) =>
      request<SessionDetail>(`/api/v1/sessions/${sessionId}/alias`, {
        method: 'PUT',
        body: JSON.stringify({ alias }),
      }),

    clearAlias: (sessionId: string) =>
      request<SessionDetail>(`/api/v1/sessions/${sessionId}/alias`, {
        method: 'DELETE',
      }),

    updateSession: (sessionId: string, body: { title?: string; alias?: string; tags?: string[] }) =>
      request<SessionDetail>(`/api/v1/sessions/${sessionId}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      }),

    // Bookmark endpoints
    createFolder: (name: string, color?: string) =>
      request<FolderResponse>('/api/v1/bookmarks/folders', {
        method: 'POST',
        body: JSON.stringify({ name, ...(color ? { color } : {}) }),
      }),

    listFolders: () =>
      request<FolderListResponse>('/api/v1/bookmarks/folders'),

    updateFolder: (folderId: string, updates: { name?: string; color?: string }) =>
      request<FolderResponse>(`/api/v1/bookmarks/folders/${folderId}`, {
        method: 'PUT',
        body: JSON.stringify(updates),
      }),

    deleteFolder: (folderId: string) =>
      request<void>(`/api/v1/bookmarks/folders/${folderId}`, {
        method: 'DELETE',
      }),

    addBookmark: (folderId: string, sessionId: string) =>
      request<BookmarkResponse>('/api/v1/bookmarks', {
        method: 'POST',
        body: JSON.stringify({ folder_id: folderId, session_id: sessionId }),
      }),

    removeBookmark: (bookmarkId: string) =>
      request<void>(`/api/v1/bookmarks/${bookmarkId}`, {
        method: 'DELETE',
      }),

    listFolderSessions: (folderId: string) =>
      request<FolderSessionsResponse>(`/api/v1/bookmarks/folders/${folderId}/sessions`),

    // Session summary
    getSessionSummary: (sessionId: string) =>
      request<{
        session_id: string;
        title: string;
        tool: string;
        model: string | null;
        duration_minutes: number;
        message_count: number;
        tool_call_count: number;
        branch: string | null;
        commit: string | null;
        files_modified: string[];
        files_read: string[];
        commands_executed: number;
        tests_run: number;
        tests_passed: number;
        tests_failed: number;
        packages_installed: string[];
        errors_encountered: string[];
        what_happened: string | null;
        key_decisions: string[] | null;
        outcome: string | null;
        open_issues: string[] | null;
        narrative_model: string | null;
        narrative_error: { code: string; message: string; status: number | null } | null;
        generated_at: string;
      }>(`/api/v1/sessions/${sessionId}/summary`),

    generateSessionSummary: (sessionId: string) =>
      request<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/summary`, { method: 'POST' }),

    generateNarrativeSummary: (sessionId: string, body: { model?: string; provider?: string; llm_api_key?: string; base_url?: string }) =>
      request<{
        session_id: string;
        title: string;
        tool: string;
        model: string | null;
        duration_minutes: number;
        message_count: number;
        tool_call_count: number;
        branch: string | null;
        commit: string | null;
        files_modified: string[];
        files_read: string[];
        commands_executed: number;
        tests_run: number;
        tests_passed: number;
        tests_failed: number;
        packages_installed: string[];
        errors_encountered: string[];
        what_happened: string | null;
        key_decisions: string[] | null;
        outcome: string | null;
        open_issues: string[] | null;
        narrative_model: string | null;
        narrative_error: { code: string; message: string; status: number | null } | null;
        generated_at: string;
      }>(`/api/v1/sessions/${sessionId}/summary/narrative`, { method: 'POST', body: JSON.stringify(body) }),

    getAuditHistory: (sessionId: string) =>
      request<{ id: string; judge_model: string; trust_score: number; total_claims: number; contradiction_count: number; created_at: string }[]>(
        `/api/v1/sessions/${sessionId}/audits`,
      ),

    // Audit trigger settings
    getAuditTrigger: () =>
      request<{ trigger: string }>('/api/v1/settings/audit-trigger'),

    updateAuditTrigger: (trigger: string) =>
      request<{ trigger: string }>('/api/v1/settings/audit-trigger', {
        method: 'PUT',
        body: JSON.stringify({ trigger }),
      }),

    // Sync settings
    getSyncSettings: () =>
      request<{ mode: string; debounce_seconds: number }>('/api/v1/sync/settings'),

    updateSyncSettings: (mode: string, debounceSeconds?: number) =>
      request<{ mode: string; debounce_seconds: number }>('/api/v1/sync/settings', {
        method: 'PUT',
        body: JSON.stringify({ mode, debounce_seconds: debounceSeconds }),
      }),

    // Capture health — per-watcher health reported by the daemon.
    getCaptureHealth: () =>
      request<{
        watchers: {
          name: string;
          health: string;
          degraded_since: string | null;
          last_error: string | null;
          sessions_tracked: number;
        }[];
      }>('/api/v1/sync/health'),

    getSyncStatus: () =>
      request<{
        mode: string;
        total_sessions: number;
        synced_sessions: number;
        watched_sessions: number;
        queued: number;
        failed: number;
        storage_used_bytes: number;
        storage_limit_bytes: number;
      }>('/api/v1/sync/status'),

    watchSession: (sessionId: string) =>
      request<{ status: string }>(`/api/v1/sync/watch/${sessionId}`, { method: 'POST' }),

    unwatchSession: (sessionId: string) =>
      request<{ status: string }>(`/api/v1/sync/watch/${sessionId}`, { method: 'DELETE' }),

    getSyncWatchlist: () =>
      request<{ sessions: { session_id: string; status: string; last_synced_at: string | null }[] }>(
        '/api/v1/sync/watchlist',
      ),

    // GitHub integration
    getGitHubInstallation: () =>
      request<GitHubInstallationResponse>('/api/v1/settings/github'),

    updateGitHubInstallation: (updates: {
      auto_comment?: boolean;
      include_trust_score?: boolean;
      include_session_links?: boolean;
    }) =>
      request<GitHubInstallationResponse>('/api/v1/settings/github', {
        method: 'PUT',
        body: JSON.stringify(updates),
      }),

    // GitLab integration
    getGitLabSettings: () =>
      request<GitLabSettingsResponse>('/api/v1/settings/gitlab'),

    saveGitLabSettings: (body: GitLabSettingsRequest) =>
      request<GitLabSettingsResponse>('/api/v1/settings/gitlab', {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    deleteGitLabSettings: () =>
      request<void>('/api/v1/settings/gitlab', { method: 'DELETE' }),

    /**
     * Start linking a GitHub App installation through GitHub OAuth. Sent with
     * credentials so the browser stores the HttpOnly claim cookie that binds
     * the GitHub callback to this browser. Returns the GitHub URL to visit.
     */
    startGitHubClaim: async (flow: GitHubClaimFlow, installationId?: number): Promise<string> => {
      const resp = await fetch(`${baseUrl}/api/v1/settings/github/oauth/start`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          Authorization: `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(
          installationId ? { flow, installation_id: installationId } : { flow },
        ),
      });
      if (!resp.ok) {
        throw new ApiError(resp.status, await resp.text());
      }
      const data = (await resp.json()) as { redirect_url?: string };
      if (!data.redirect_url || !data.redirect_url.startsWith('https://github.com/')) {
        throw new Error('Unexpected response from the server.');
      }
      return data.redirect_url;
    },

    /** Installations the latest GitHub sign-in proved the user can link. */
    getGitHubClaimChoices: () =>
      request<{ choices: GitHubClaimChoice[] }>('/api/v1/settings/github/oauth/choices'),

    /** Link the one installation picked from the chooser. */
    chooseGitHubInstallation: (installationId: number) =>
      request<GitHubInstallationResponse>('/api/v1/settings/github/oauth/choose', {
        method: 'POST',
        body: JSON.stringify({ installation_id: installationId }),
      }),

    // Project context endpoints
    listProjects: () =>
      request<ProjectContext[]>('/api/v1/projects/'),

    getProject: (remote: string) =>
      request<ProjectContext>(`/api/v1/projects/${encodeURIComponent(remote)}`),

    createProject: (data: { name: string; git_remote_normalized: string }) =>
      request<ProjectContext>('/api/v1/projects/', {
        method: 'POST',
        body: JSON.stringify(data),
      }),

    updateProjectContext: (remote: string, doc: string) =>
      request<{ status: string }>(`/api/v1/projects/${encodeURIComponent(remote)}/context`, {
        method: 'PUT',
        body: JSON.stringify({ context_document: doc }),
      }),

    deleteProject: (id: string) =>
      request<{ status: string }>(`/api/v1/projects/${id}`, {
        method: 'DELETE',
      }),

    // Knowledge entries
    listKnowledgeEntries: async (
      projectId: string,
      params: { pending?: boolean; type?: string; limit?: number } = {},
    ): Promise<KnowledgeEntryListResponse> => {
      const sp = new URLSearchParams();
      if (params.pending) sp.set('pending', 'true');
      if (params.type) sp.set('type', params.type);
      if (params.limit) sp.set('limit', String(params.limit));
      const resp = await request<KnowledgeEntryListResponse | KnowledgeEntry[]>(
        `/api/v1/projects/${projectId}/entries?${sp}`,
      );
      // Server returns bare list; normalize to wrapper shape
      if (Array.isArray(resp)) {
        return { entries: resp, total: resp.length };
      }
      return resp;
    },

    dismissEntry: (projectId: string, entryId: number) =>
      request<KnowledgeEntry>(`/api/v1/projects/${projectId}/entries/${entryId}`, {
        method: 'PUT',
        body: JSON.stringify({ dismissed: true }),
      }),

    compileProject: (projectId: string) =>
      request<{ entries_compiled: number; compilation_id: number }>(
        `/api/v1/projects/${projectId}/compile`,
        { method: 'POST' },
      ),

    listCompilations: async (projectId: string): Promise<{ compilations: ContextCompilation[] }> => {
      const resp = await request<{ compilations: ContextCompilation[] } | ContextCompilation[]>(
        `/api/v1/projects/${projectId}/compilations`,
      );
      // Server returns bare list; normalize to wrapper shape
      if (Array.isArray(resp)) {
        return { compilations: resp };
      }
      return resp;
    },

    getCompileStatus: (projectId: string) =>
      request<CompileStatusResponse>(`/api/v1/projects/${projectId}/compile-status`),

    getProjectHealth: (projectId: string) =>
      request<ProjectHealthResponse>(`/api/v1/projects/${projectId}/health`),

    dismissStaleEntries: (projectId: string) =>
      request<{ dismissed_count: number }>(
        `/api/v1/projects/${projectId}/entries/dismiss-stale`,
        { method: 'POST' },
      ),

    promoteEntry: (projectId: string, entryId: number) =>
      request<KnowledgeEntry>(`/api/v1/projects/${projectId}/entries/${entryId}/promote`, {
        method: 'PUT',
      }),

    supersedeEntry: (projectId: string, entryId: number, body: { superseding_id: number; reason: string }) =>
      request<KnowledgeEntry>(`/api/v1/projects/${projectId}/entries/${entryId}/supersede`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    rebuildProject: (projectId: string) =>
      request<{ status: string }>(`/api/v1/projects/${projectId}/rebuild`, {
        method: 'POST',
      }),

    refreshEntry: (projectId: string, entryId: number) =>
      request<{ id: number; freshness_class: string; last_relevant_at: string }>(
        `/api/v1/projects/${projectId}/entries/${entryId}/refresh`,
        { method: 'PUT' },
      ),

    // Wiki pages
    listWikiPages: async (projectId: string): Promise<WikiPageListResponse> => {
      const resp = await request<WikiPageListResponse | WikiPage[]>(
        `/api/v1/projects/${projectId}/pages`,
      );
      // Server returns bare list; normalize to wrapper shape
      if (Array.isArray(resp)) {
        return { pages: resp };
      }
      return resp;
    },

    getWikiPage: (projectId: string, slug: string) =>
      request<WikiPageDetail>(`/api/v1/projects/${projectId}/pages/${encodeURIComponent(slug)}`),

    updateWikiPage: (projectId: string, slug: string, content: string, title?: string) =>
      request<WikiPage>(`/api/v1/projects/${projectId}/pages/${encodeURIComponent(slug)}`, {
        method: 'PUT',
        body: JSON.stringify({
          content,
          ...(title ? { title } : {}),
        }),
      }),

    /** Flip only the Confluence publish opt-in (no content write, no revision). */
    setWikiPagePublish: (projectId: string, slug: string, publish: boolean) =>
      request<{ slug: string; publish: boolean }>(
        `/api/v1/projects/${projectId}/pages/${encodeURIComponent(slug)}`,
        { method: 'PATCH', body: JSON.stringify({ publish }) },
      ),

    deleteWikiPage: (projectId: string, slug: string) =>
      request<void>(`/api/v1/projects/${projectId}/pages/${encodeURIComponent(slug)}`, {
        method: 'DELETE',
      }),

    regenerateWikiPage: (projectId: string, slug: string) =>
      request<{ status: string; slug: string; word_count: number; entries_used: number }>(
        `/api/v1/projects/${projectId}/pages/${encodeURIComponent(slug)}/regenerate`,
        { method: 'POST' },
      ),

    updateProjectSettings: (
      projectId: string,
      settings: {
        auto_narrative?: boolean;
        compile_schedule?: 'off' | 'nightly' | 'weekly';
        compile_after_claims?: number | null;
      },
    ) =>
      request<{ status: string }>(`/api/v1/projects/${projectId}/settings`, {
        method: 'PUT',
        body: JSON.stringify(settings),
      }),

    // ----------------------------------------------------------------
    // Project rules (v0.9.9)
    // ----------------------------------------------------------------
    // Rules endpoints are keyed by the project UUID (same pattern as
    // knowledge/wiki endpoints). Callers pass `project.id`, not the
    // git remote — Starlette can't route encoded slashes inside a
    // single path segment, which blocks URL-encoded remotes.
    getProjectRules: (projectId: string) =>
      requestWithEtag<ProjectRules>(
        `/api/v1/projects/${projectId}/rules`,
      ),

    updateProjectRules: (projectId: string, rules: Partial<ProjectRules>, etag: string) =>
      requestWithEtag<ProjectRules>(
        `/api/v1/projects/${projectId}/rules`,
        {
          method: 'PUT',
          headers: { 'If-Match': etag },
          body: JSON.stringify(rules),
        },
      ),

    compileRules: (projectId: string) =>
      request<CompileRulesResponse>(
        `/api/v1/projects/${projectId}/rules/compile`,
        { method: 'POST' },
      ),

    listRulesVersions: async (projectId: string): Promise<RulesVersionListResponse> => {
      const resp = await request<RulesVersionListResponse | RulesVersionSummary[]>(
        `/api/v1/projects/${projectId}/rules/versions`,
      );
      // Server may return a bare list; normalise to wrapper shape.
      if (Array.isArray(resp)) {
        return { versions: resp };
      }
      return resp;
    },

    getRulesVersion: (projectId: string, version: number) =>
      request<RulesVersion>(
        `/api/v1/projects/${projectId}/rules/versions/${version}`,
      ),

    // DLP policy
    getDLPPolicy: () =>
      request<{ enabled: boolean; mode: string; categories: string[] }>('/api/v1/dlp/policy'),

    updateDLPPolicy: (policy: { enabled?: boolean; mode?: string; categories?: string[] }) =>
      request<{ enabled: boolean; mode: string; categories: string[] }>('/api/v1/dlp/policy', {
        method: 'PUT',
        body: JSON.stringify(policy),
      }),

    // ── v0.10.1 Personas (Pro+) ──
    listPersonas: (projectId: string) =>
      request<Persona[]>(`/api/v1/projects/${projectId}/personas`),

    getPersona: (projectId: string, name: string) =>
      request<Persona>(`/api/v1/projects/${projectId}/personas/${encodeURIComponent(name)}`),

    createPersona: (projectId: string, body: PersonaCreate) =>
      request<Persona>(`/api/v1/projects/${projectId}/personas`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    updatePersona: (projectId: string, name: string, body: PersonaUpdate) =>
      request<Persona>(`/api/v1/projects/${projectId}/personas/${encodeURIComponent(name)}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    deletePersona: (projectId: string, name: string, force = false) =>
      request<{ status: string }>(
        `/api/v1/projects/${projectId}/personas/${encodeURIComponent(name)}${force ? '?force=true' : ''}`,
        { method: 'DELETE' },
      ),

    // ── GH #70 Org-level personas (org admins write; members read) ──
    listOrgPersonas: (orgId: string) =>
      request<Persona[]>(`/api/v1/orgs/${encodeURIComponent(orgId)}/personas`),

    createOrgPersona: (orgId: string, body: PersonaCreate) =>
      request<Persona>(`/api/v1/orgs/${encodeURIComponent(orgId)}/personas`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    updateOrgPersona: (orgId: string, name: string, body: PersonaUpdate) =>
      request<Persona>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/personas/${encodeURIComponent(name)}`,
        { method: 'PUT', body: JSON.stringify(body) },
      ),

    deleteOrgPersona: (orgId: string, name: string, force = false) =>
      request<{ status: string }>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/personas/${encodeURIComponent(name)}${force ? '?force=true' : ''}`,
        { method: 'DELETE' },
      ),

    // ── GitHub #92 — persona roll-up + represent-history ──
    getOrgPersonaRollup: (orgId: string, name: string) =>
      request<PersonaRollup>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/personas/${encodeURIComponent(name)}/rollup`,
      ),

    getOrgPersonaRepresentHistory: (orgId: string, name: string) =>
      request<RepresentHistoryEntry[]>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/personas/${encodeURIComponent(name)}/represent-history`,
      ),

    getProjectPersonaRollup: (projectId: string, name: string) =>
      request<PersonaRollup>(
        `/api/v1/projects/${projectId}/personas/${encodeURIComponent(name)}/rollup`,
      ),

    // ── v0.10.1 Tickets (Team+) ──
    listTickets: (
      projectId: string,
      params: {
        status?: string;
        assigned_to?: string;
        /** `me` = tickets assigned to the caller as a person. */
        assigned_user?: string;
        priority?: string;
        kind?: string;
        /** Past due and not done/closed/cancelled. */
        overdue?: boolean;
        /** "YYYY-MM-DD": only tickets due before this date. */
        due_before?: string;
        limit?: number;
      } = {},
    ) => {
      const sp = new URLSearchParams();
      if (params.status) sp.set('status', params.status);
      if (params.overdue) sp.set('overdue', 'true');
      if (params.due_before) sp.set('due_before', params.due_before);
      if (params.assigned_to) sp.set('assigned_to', params.assigned_to);
      if (params.assigned_user) sp.set('assigned_user', params.assigned_user);
      if (params.priority) sp.set('priority', params.priority);
      if (params.kind) sp.set('kind', params.kind);
      if (params.limit) sp.set('limit', String(params.limit));
      const qs = sp.toString();
      return request<Ticket[]>(
        `/api/v1/projects/${projectId}/tickets${qs ? `?${qs}` : ''}`,
      );
    },

    getTicket: (projectId: string, ticketId: string) =>
      request<Ticket>(`/api/v1/projects/${projectId}/tickets/${ticketId}`),

    listTicketEvents: (
      projectId: string,
      ticketId: string,
      params: { limit?: number; before?: string } = {},
    ) => {
      const sp = new URLSearchParams();
      if (params.limit) sp.set('limit', String(params.limit));
      if (params.before) sp.set('before', params.before);
      const qs = sp.toString();
      return request<TicketEventsPage>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/events${qs ? `?${qs}` : ''}`,
      );
    },

    getTicketById: (ticketId: string) =>
      request<TicketPreview>(`/api/v1/tickets/${encodeURIComponent(ticketId)}`),

    createTicket: (projectId: string, body: TicketCreate) =>
      request<Ticket>(`/api/v1/projects/${projectId}/tickets`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    updateTicket: (projectId: string, ticketId: string, body: TicketUpdate) =>
      request<Ticket>(`/api/v1/projects/${projectId}/tickets/${ticketId}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    listAssignableUsers: (projectId: string) =>
      request<{ users: AssignableUser[] }>(`/api/v1/projects/${projectId}/assignable-users`),

    approveTicket: (projectId: string, ticketId: string) =>
      request<Ticket>(`/api/v1/projects/${projectId}/tickets/${ticketId}/approve`, {
        method: 'POST',
      }),

    dismissTicket: (projectId: string, ticketId: string) =>
      request<Ticket>(`/api/v1/projects/${projectId}/tickets/${ticketId}/dismiss`, {
        method: 'POST',
      }),

    closeTicket: (projectId: string, ticketId: string) =>
      request<Ticket>(`/api/v1/projects/${projectId}/tickets/${ticketId}/close`, {
        method: 'POST',
      }),

    listTicketComments: (projectId: string, ticketId: string) =>
      request<TicketComment[]>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/comments`,
      ),

    addTicketComment: (
      projectId: string,
      ticketId: string,
      content: string,
      sendToJira?: boolean,
    ) =>
      request<TicketComment>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/comments`,
        {
          method: 'POST',
          body: JSON.stringify(
            sendToJira === undefined ? { content } : { content, send_to_jira: sendToJira },
          ),
        },
      ),

    // Jira promotion (any project member with ticket write access; unlink is
    // project-admin only).
    listTicketJiraTemplates: (projectId: string, ticketId: string) =>
      request<JiraTicketTemplates>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/jira/templates`,
      ),

    previewJira: (projectId: string, ticketId: string, body?: { template?: string }) =>
      request<JiraPreview>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/jira/preview`,
        { method: 'POST', body: body ? JSON.stringify(body) : undefined },
      ),

    publishJira: (projectId: string, ticketId: string, body: JiraPublishRequest) =>
      request<JiraLink>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/jira/publish`,
        { method: 'POST', body: JSON.stringify(body) },
      ),

    getJiraLink: (projectId: string, ticketId: string) =>
      request<JiraLink>(`/api/v1/projects/${projectId}/tickets/${ticketId}/jira`),

    // Active + future sprints of the project's mapped Jira project (editors).
    listJiraSprints: (projectId: string) =>
      request<JiraSprintList>(`/api/v1/projects/${projectId}/jira/sprints`),

    // Replace the ticket's title and description with the linked issue's.
    pullJira: (projectId: string, ticketId: string) =>
      request<JiraPullResult>(`/api/v1/projects/${projectId}/tickets/${ticketId}/jira/pull`, {
        method: 'POST',
        body: JSON.stringify({ confirm: true }),
      }),

    // Link an issue that already exists in Jira instead of publishing a new
    // one. Editors only; the server trims and uppercases the key.
    linkJiraIssue: (projectId: string, ticketId: string, issueKey: string) =>
      request<JiraLink>(`/api/v1/projects/${projectId}/tickets/${ticketId}/jira/link`, {
        method: 'POST',
        body: JSON.stringify({ issue_key: issueKey }),
      }),

    getTicketReviewState: (projectId: string, ticketId: string) =>
      request<TicketReviewState>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/review-state`,
      ),

    getTicketJiraSyncEvents: (projectId: string, ticketId: string) =>
      request<JiraSyncEventsPage>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/jira-sync-events`,
      ),

    unlinkJira: (projectId: string, ticketId: string) =>
      request<void>(`/api/v1/projects/${projectId}/tickets/${ticketId}/jira`, {
        method: 'DELETE',
      }),

    // ── Ticket watchers + notifications (me-scoped) ──
    listTicketNotifications: (
      params: { unread?: boolean; limit?: number; before?: number } = {},
    ) => {
      const sp = new URLSearchParams();
      if (params.unread) sp.set('unread', 'true');
      if (params.limit) sp.set('limit', String(params.limit));
      if (params.before) sp.set('before', String(params.before));
      const qs = sp.toString();
      return request<TicketNotificationList>(
        `/api/v1/me/ticket-notifications${qs ? `?${qs}` : ''}`,
      );
    },

    /**
     * Unread count for the inbox badge. Polls with an If-None-Match header;
     * a 304 resolves to `notModified` so the caller keeps its last count
     * without a full re-render.
     */
    getTicketNotificationUnreadCount: async (
      etag?: string,
    ): Promise<{ count: number; etag: string; notModified: boolean }> => {
      const resp = await fetch(
        `${baseUrl}/api/v1/me/ticket-notifications/unread-count`,
        {
          headers: {
            Authorization: `Bearer ${apiKey}`,
            ...(etag ? { 'If-None-Match': etag } : {}),
          },
        },
      );
      if (resp.status === 304) {
        return { count: 0, etag: etag ?? '', notModified: true };
      }
      if (!resp.ok) {
        throw new ApiError(resp.status, await resp.text());
      }
      const data = (await resp.json()) as { count: number };
      const newEtag = resp.headers.get('ETag') || resp.headers.get('etag') || '';
      return { count: data.count, etag: newEtag, notModified: false };
    },

    markTicketNotificationsRead: (body: { ids?: number[]; all?: boolean }) =>
      request<{ updated: number }>('/api/v1/me/ticket-notifications/read', {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    getNotificationSettings: () =>
      request<TicketNotificationSettings>('/api/v1/me/notification-settings'),

    putNotificationSettings: (body: { ticket_email: string }) =>
      request<TicketNotificationSettings>('/api/v1/me/notification-settings', {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    watchTicket: (projectId: string, ticketId: string, muted = false) =>
      request<TicketWatcher>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/watch`,
        { method: 'PUT', body: JSON.stringify({ muted }) },
      ),

    unwatchTicket: (projectId: string, ticketId: string) =>
      request<void>(`/api/v1/projects/${projectId}/tickets/${ticketId}/watch`, {
        method: 'DELETE',
      }),

    listTicketWatchers: (projectId: string, ticketId: string) =>
      request<TicketWatcher[]>(
        `/api/v1/projects/${projectId}/tickets/${ticketId}/watchers`,
      ),

    // ── v0.10.2 AgentRuns (Team+) ──
    listAgentRuns: (
      projectId: string,
      params: {
        persona_name?: string;
        status?: string;
        trigger_source?: string;
        ticket_id?: string;
        limit?: number;
      } = {},
    ) => {
      const sp = new URLSearchParams();
      if (params.persona_name) sp.set('persona_name', params.persona_name);
      if (params.status) sp.set('status', params.status);
      if (params.trigger_source) sp.set('trigger_source', params.trigger_source);
      if (params.ticket_id) sp.set('ticket_id', params.ticket_id);
      if (params.limit) sp.set('limit', String(params.limit));
      const qs = sp.toString();
      return request<AgentRun[]>(
        `/api/v1/projects/${projectId}/agent-runs${qs ? `?${qs}` : ''}`,
      );
    },

    getAgentRun: (projectId: string, runId: string) =>
      request<AgentRun>(`/api/v1/projects/${projectId}/agent-runs/${runId}`),

    replayAgentRun: (
      projectId: string,
      runId: string,
      body: AgentRunReplayInput = {},
    ) =>
      request<AgentRunReplayResult>(
        `/api/v1/projects/${projectId}/agent-runs/${runId}/replay`,
        { method: 'POST', body: JSON.stringify(body) },
      ),

    compareAgentRuns: (projectId: string, aId: string, bId: string) =>
      request<AgentRunCompareResult>(
        `/api/v1/projects/${projectId}/agent-runs/${aId}/compare/${bId}`,
      ),

    // ── v0.16 Skills and plugins marketplace ──
    searchMarketplace: (params: MarketplaceSearchFilters = {}) => {
      const sp = new URLSearchParams();
      if (params.q) sp.set('q', params.q);
      if (params.tool) sp.set('tool', params.tool);
      if (params.limit) sp.set('limit', String(params.limit));
      if (params.cursor) sp.set('cursor', params.cursor);
      // Discovery filters (marketplace v2 §9); booleans only when set.
      for (const key of ['category', 'publisher', 'license', 'max_severity', 'capability', 'works_in', 'sort'] as const) {
        const value = params[key];
        if (value) sp.set(key, value);
      }
      for (const key of ['verified', 'attested', 'include_deprecated'] as const) {
        const value = params[key];
        if (value !== undefined) sp.set(key, value ? 'true' : 'false');
      }
      if (params.updated_within) sp.set('updated_within', String(params.updated_within));
      const qs = sp.toString();
      return request<MarketplaceSearchResponse>(
        `/api/v1/marketplace/packages${qs ? `?${qs}` : ''}`,
      );
    },

    listMarketplaceCategories: () =>
      request<{ items: MarketplaceCategory[]; count: number; max_per_package: number }>(
        '/api/v1/marketplace/categories',
      ),

    getMarketplaceReadme: (packageId: string, versionId?: string) =>
      request<MarketplaceReadme>(
        `/api/v1/marketplace/packages/${encodeURIComponent(packageId)}/readme${
          versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
        }`,
      ),

    /** A raster image from inside a package (README images). Fetched with
     *  the API key and returned as a Blob so it can be shown via an object
     *  URL; remote images are never loaded. */
    getMarketplaceAsset: async (versionId: string, path: string): Promise<Blob> => {
      const resp = await fetch(
        `${baseUrl}/api/v1/marketplace/versions/${encodeURIComponent(versionId)}/assets?path=${encodeURIComponent(path)}`,
        { headers: { Authorization: `Bearer ${apiKey}` } },
      );
      if (!resp.ok) throw new ApiError(resp.status, await resp.text());
      return resp.blob();
    },

    getPublisherAnalytics: (publisherId: string, days = 30) =>
      request<PublisherAnalytics>(
        `/api/v1/publishers/${encodeURIComponent(publisherId)}/analytics?days=${days}`,
      ),

    updatePackageSettings: (publisherId: string, packageId: string, body: PackageSettingsInput) =>
      request<PackageSettings>(
        `/api/v1/publishers/${encodeURIComponent(publisherId)}/packages/${encodeURIComponent(packageId)}`,
        { method: 'PATCH', body: JSON.stringify(body) },
      ),

    getOrgSkillUsage: (orgId: string, days = 30) =>
      request<OrgSkillUsage>(`/api/v1/orgs/${encodeURIComponent(orgId)}/skills/usage?days=${days}`),

    getMarketplacePackage: (packageId: string) =>
      request<MarketplacePackage>(`/api/v1/marketplace/packages/${packageId}`),

    previewMarketplaceVersion: (versionId: string) =>
      request<MarketplacePackage>(`/api/v1/marketplace/versions/${versionId}/preview`),

    getMarketplaceDisclosure: (versionId: string, tools: string[] = [], scope: 'project' | 'user' = 'project') => {
      const params = new URLSearchParams({ scope });
      if (tools.length) params.set('tools', tools.join(','));
      return request<MarketplaceDisclosure>(
        `/api/v1/marketplace/versions/${encodeURIComponent(versionId)}/disclosure?${params.toString()}`,
      );
    },

    listHeldUpdates: (orgId: string, params: { cursor?: string; limit?: number } = {}) => {
      const query = new URLSearchParams();
      if (params.cursor) query.set('cursor', params.cursor);
      if (params.limit) query.set('limit', String(params.limit));
      const qs = query.toString();
      return request<HeldUpdatesPage>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/skills/assignments/updates${qs ? `?${qs}` : ''}`,
      );
    },

    /** How many held updates the org has, without building any diff. */
    countHeldUpdates: (orgId: string) =>
      request<{ count: number; has_more: boolean }>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/skills/assignments/updates/count`,
      ),

    advanceAssignment: (orgId: string, assignmentId: string, input: AdvanceAssignmentInput) =>
      request<SkillAssignment & { update_class: string; previous_assignment_id: string }>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/skills/assignments/${encodeURIComponent(assignmentId)}/advance`,
        { method: 'POST', body: JSON.stringify(input) },
      ),

    getMarketplaceVersionDiff: (fromVersionId: string, toVersionId: string) =>
      request<MarketplaceVersionDiff>(
        `/api/v1/marketplace/versions/${encodeURIComponent(fromVersionId)}/diff/${encodeURIComponent(toVersionId)}`,
      ),

    listNativeCatalogs: () => request<NativeCatalogList>('/api/v1/marketplace/catalogs'),

    getWorksIn: (versionId: string) =>
      request<WorksIn>(`/api/v1/marketplace/versions/${encodeURIComponent(versionId)}/works-in`),

    downloadMarketplaceVersion: async (versionId: string): Promise<Blob> => {
      const resp = await fetch(
        `${baseUrl}/api/v1/marketplace/versions/${encodeURIComponent(versionId)}/download`,
        { headers: { Authorization: `Bearer ${apiKey}` } },
      );
      if (!resp.ok) throw new ApiError(resp.status, await resp.text());
      return resp.blob();
    },

    listSkillInstallRequests: (projectId: string, status?: string) => {
      const qs = status ? `?status=${encodeURIComponent(status)}` : '';
      return request<{ items: SkillInstallRequest[]; can_approve: boolean }>(
        `/api/v1/projects/${projectId}/skill-install-requests${qs}`,
      );
    },

    createSkillInstallRequest: (
      projectId: string,
      body: {
        version_id: string;
        content_hash: string;
        target_scope: 'project' | 'user' | 'device';
        target_device_id?: string;
        target_tools: string[];
        requested_capabilities: string[];
      },
    ) => request<SkillInstallRequest>(
      `/api/v1/projects/${projectId}/skill-install-requests`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

    approveSkillInstallRequest: (
      projectId: string,
      requestId: string,
      approvedCapabilities: string[],
      confirmationCode?: string,
      acknowledgements?: ApprovalBinding,
    ) =>
      request<SkillInstallRequest>(
        `/api/v1/projects/${projectId}/skill-install-requests/${requestId}/approve`,
        {
          method: 'POST',
          body: JSON.stringify({
            approved_capabilities: approvedCapabilities,
            ...(confirmationCode ? { confirmation_code: confirmationCode } : {}),
            // Marketplace v2 D2: explicit re-approval of an update that widens
            // access or changes executable content. Never sent implicitly.
            ...(acknowledgements?.widening ? { acknowledge_widening: true } : {}),
            ...(acknowledgements?.codeChange ? { acknowledge_code_change: true } : {}),
            // The disclosure the approver saw: the server refuses with
            // 409 disclosure_changed when it is no longer current.
            ...(acknowledgements?.capabilityDigest
              ? {
                  capability_digest: acknowledgements.capabilityDigest,
                  consent_rule_version: acknowledgements.consentRuleVersion ?? 2,
                }
              : {}),
            ...(acknowledgements?.disclosureDigest ? { disclosure_digest: acknowledgements.disclosureDigest } : {}),
            ...(acknowledgements?.updateDigest ? { update_digest: acknowledgements.updateDigest } : {}),
          }),
        },
      ),

    /** Owner-only: delete one immutable personal version (409 while referenced). */
    deletePersonalSkillVersion: (packageId: string, versionId: string) =>
      request<void>(
        `/api/v1/users/me/skills/packages/${encodeURIComponent(packageId)}/versions/${encodeURIComponent(versionId)}`,
        { method: 'DELETE' },
      ),

    /** Requester or project admin: revoke an approved/applied grant (device state untouched). */
    revokeSkillInstallRequest: (projectId: string, requestId: string) =>
      request<SkillInstallRequest & { previous_status?: string; warning?: string }>(
        `/api/v1/projects/${projectId}/skill-install-requests/${requestId}/revoke`,
        { method: 'POST' },
      ),

    /** Requester-only: emails a single-use code required to approve one's own request. */
    challengeSkillInstallRequest: (projectId: string, requestId: string) =>
      request<{ id: string; status: string; delivery: 'email' | 'unavailable'; expires_at: string }>(
        `/api/v1/projects/${projectId}/skill-install-requests/${requestId}/challenge`,
        { method: 'POST' },
      ),

    cancelSkillInstallRequest: (projectId: string, requestId: string) =>
      request<SkillInstallRequest>(
        `/api/v1/projects/${projectId}/skill-install-requests/${requestId}/cancel`,
        { method: 'POST' },
      ),

    listEffectiveSkillAssignments: (params: { device_id?: string; tool?: string } = {}) => {
      const sp = new URLSearchParams();
      if (params.device_id) sp.set('device_id', params.device_id);
      if (params.tool) sp.set('tool', params.tool);
      const qs = sp.toString();
      return request<{ items: SkillAssignment[] }>(
        `/api/v1/users/me/skills/assignments${qs ? `?${qs}` : ''}`,
      );
    },

    listOrgSkillAssignments: (orgId: string) =>
      request<{ items: SkillAssignment[] }>(`/api/v1/orgs/${orgId}/skills/assignments`),

    listOrgSkillRegistries: (orgId: string) =>
      request<{ items: SkillRegistry[] }>(`/api/v1/orgs/${orgId}/skills/registries`),

    updateOrgSkillRegistry: (orgId: string, registryId: string, body: SkillRegistryUpdate) =>
      request<SkillRegistry>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/skills/registries/${encodeURIComponent(registryId)}`,
        { method: 'PATCH', body: JSON.stringify(body) },
      ),

    /** Schedules the registry for the next poller run; never fetches inline. */
    requestOrgSkillRegistryPoll: (orgId: string, registryId: string) =>
      request<SkillRegistry>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/skills/registries/${encodeURIComponent(registryId)}/poll`,
        { method: 'POST' },
      ),

    // ── Marketplace v2: publishing + moderation ──
    /**
     * Multipart upload of a pre-built .tar.gz. Goes around `request()` because
     * that helper forces a JSON Content-Type; the browser must set the
     * multipart boundary itself.
     */
    publishSkillPackage: async (input: PublishPackageInput): Promise<MarketplacePackage> => {
      const form = new FormData();
      const filename = input.filename || (input.file instanceof File ? input.file.name : 'package.tar.gz');
      form.append('package', input.file, filename);
      if (input.namespace) form.append('namespace', input.namespace);
      if (input.collection) form.append('collection', input.collection);
      if (input.target.kind === 'org') form.append('visibility', input.target.visibility);
      const path = input.target.kind === 'personal'
        ? '/api/v1/users/me/skills/packages'
        : `/api/v1/orgs/${encodeURIComponent(input.target.orgId)}/skills/packages`;
      const resp = await fetch(`${baseUrl}${path}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${apiKey}` },
        body: form,
      });
      if (!resp.ok) {
        const error = new ApiError(resp.status, await resp.text());
        const retryAfter = resp.headers?.get?.('retry-after');
        if (retryAfter && !(error.details && 'retry_after' in error.details)) {
          error.details = { ...(error.details || {}), retry_after: retryAfter };
        }
        throw error;
      }
      return resp.json();
    },

    reportMarketplacePackage: (
      packageId: string,
      body: { reason: ReportReason; details?: string; version_id?: string },
    ) => request<MarketplaceReport>(
      `/api/v1/marketplace/packages/${encodeURIComponent(packageId)}/reports`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

    listMarketplaceReports: (params: { status?: string; limit?: number; cursor?: string } = {}) => {
      const sp = new URLSearchParams();
      if (params.status) sp.set('status', params.status);
      if (params.limit) sp.set('limit', String(params.limit));
      if (params.cursor) sp.set('cursor', params.cursor);
      const qs = sp.toString();
      return request<MarketplaceReportList>(`/api/v1/marketplace/reports${qs ? `?${qs}` : ''}`);
    },

    dismissMarketplaceReport: (reportId: string, note: string) =>
      request<MarketplaceReport>(
        `/api/v1/marketplace/reports/${encodeURIComponent(reportId)}/dismiss`,
        { method: 'POST', body: JSON.stringify({ note }) },
      ),

    /**
     * Restoring a QUARANTINED version clears its critical scan findings as false
     * positives: with more than one, `findings` must list every one (the server
     * answers 409 `findings_required` with the list otherwise).
     */
    moderateMarketplace: (
      kind: ModerationTargetKind,
      targetId: string,
      action: ModerationAction,
      reason: string,
      findings?: ScanFindingKey[],
    ) =>
      request<ModerationResult>(
        `/api/v1/marketplace/${kind}/${encodeURIComponent(targetId)}/${action}`,
        { method: 'POST', body: JSON.stringify(findings ? { reason, findings } : { reason }) },
      ),

    listMarketplaceRevocations: (params: { since?: string; limit?: number } = {}) => {
      const sp = new URLSearchParams();
      if (params.since) sp.set('since', params.since);
      if (params.limit) sp.set('limit', String(params.limit));
      const qs = sp.toString();
      return request<RevocationFeed>(`/api/v1/marketplace/revocations${qs ? `?${qs}` : ''}`);
    },

    // ── Multi-Repo Projects (v0.11) ──
    listProjectRepos: (projectId: string) =>
      request<ProjectRepoResponse[]>(`/api/v1/projects/${projectId}/repos`),

    linkRepo: (projectId: string, body: LinkRepoRequest) =>
      request<ProjectRepoResponse>(`/api/v1/projects/${projectId}/repos`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    unlinkRepo: (projectId: string, repoId: string) =>
      request<{ status: string }>(`/api/v1/projects/${projectId}/repos/${repoId}`, {
        method: 'DELETE',
      }),

    mergeProject: (projectId: string, body: MergeRequest) =>
      request<MergeDryRunResponse | MergeExecuteResponse>(
        `/api/v1/projects/${projectId}/merge`,
        {
          method: 'POST',
          body: JSON.stringify(body),
        },
      ),

    /** Update project name (inline rename). */
    updateProject: (projectId: string, body: { name?: string }) =>
      request<ProjectContext>(`/api/v1/projects/${projectId}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),

    // ── Marketplace publishers + trusted publishing (marketplace v2 §4) ──
    listMyPublishers: () => request<PublisherList>('/api/v1/publishers'),

    getPublisher: (publisherId: string) =>
      request<PublisherDetail>(`/api/v1/publishers/${encodeURIComponent(publisherId)}`),

    claimPublisher: (body: ClaimPublisherInput) =>
      request<PublisherDetail>('/api/v1/publishers', { method: 'POST', body: JSON.stringify(body) }),

    startPublisherVerification: (publisherId: string, body: StartVerificationInput) =>
      request<PublisherVerification>(
        `/api/v1/publishers/${encodeURIComponent(publisherId)}/verifications`,
        { method: 'POST', body: JSON.stringify(body) },
      ),

    checkPublisherVerification: (publisherId: string, verificationId: string) =>
      request<PublisherVerification>(
        `/api/v1/publishers/${encodeURIComponent(publisherId)}/verifications/${encodeURIComponent(verificationId)}/check`,
        { method: 'POST' },
      ),

    revokePublisherVerification: (publisherId: string, verificationId: string) =>
      request<PublisherVerification>(
        `/api/v1/publishers/${encodeURIComponent(publisherId)}/verifications/${encodeURIComponent(verificationId)}`,
        { method: 'DELETE' },
      ),

    addTrustedPublisher: (publisherId: string, body: AddTrustedPublisherInput) =>
      request<TrustedPublisher>(
        `/api/v1/publishers/${encodeURIComponent(publisherId)}/trusted-publishers`,
        { method: 'POST', body: JSON.stringify(body) },
      ),

    removeTrustedPublisher: (publisherId: string, trustedId: string) =>
      request<TrustedPublisher>(
        `/api/v1/publishers/${encodeURIComponent(publisherId)}/trusted-publishers/${encodeURIComponent(trustedId)}`,
        { method: 'DELETE' },
      ),

    listPublisherListings: (publisherId: string) =>
      request<{ items: (MarketplacePackage & { moderation_reason?: string | null })[]; count: number }>(
        `/api/v1/publishers/${encodeURIComponent(publisherId)}/listings`,
      ),

    // ── Project groups (GitHub #101) ──
    listOrgGroups: (orgId: string) =>
      request<ProjectGroupSummary[]>(`/api/v1/orgs/${encodeURIComponent(orgId)}/groups`),

    createGroup: (orgId: string, body: ProjectGroupCreate) =>
      request<ProjectGroupDetail>(`/api/v1/orgs/${encodeURIComponent(orgId)}/groups`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    getGroup: (orgId: string, groupId: string) =>
      request<ProjectGroupDetail>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/groups/${encodeURIComponent(groupId)}`,
      ),

    updateGroup: (orgId: string, groupId: string, body: ProjectGroupUpdate) =>
      request<ProjectGroupDetail>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/groups/${encodeURIComponent(groupId)}`,
        { method: 'PATCH', body: JSON.stringify(body) },
      ),

    deleteGroup: (orgId: string, groupId: string) =>
      request<void>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/groups/${encodeURIComponent(groupId)}`,
        { method: 'DELETE' },
      ),

    /** Grant a member, or change their role (PUT is idempotent). */
    grantGroupMember: (orgId: string, groupId: string, userId: string, role: ProjectGroupRole) =>
      request<ProjectGroupDetail>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}`,
        { method: 'PUT', body: JSON.stringify({ role }) },
      ),

    revokeGroupMember: (orgId: string, groupId: string, userId: string) =>
      request<ProjectGroupDetail>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}`,
        { method: 'DELETE' },
      ),

    addGroupProject: (orgId: string, groupId: string, projectId: string) =>
      request<ProjectGroupDetail>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/groups/${encodeURIComponent(groupId)}/projects/${encodeURIComponent(projectId)}`,
        { method: 'PUT' },
      ),

    removeGroupProject: (orgId: string, groupId: string, projectId: string) =>
      request<ProjectGroupDetail>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/groups/${encodeURIComponent(groupId)}/projects/${encodeURIComponent(projectId)}`,
        { method: 'DELETE' },
      ),

    listProjectGroups: (projectId: string) =>
      request<ProjectGroupSummary[]>(`/api/v1/projects/${encodeURIComponent(projectId)}/groups`),

    listMyGroups: () => request<ProjectGroupSummary[]>('/api/v1/me/groups'),

    // ── Share Links (v0.14.0) ──
    createShareLink: (sessionId: string, body: { expires_in_hours?: number; password?: string }) =>
      request<{ link_id: string; url: string; public_url: string; expires_at: string; has_password: boolean }>(
        `/api/v1/sessions/${sessionId}/share`,
        { method: 'POST', body: JSON.stringify(body) },
      ),

    revokeShareLink: (sessionId: string, linkId: string) =>
      request<void>(`/api/v1/sessions/${sessionId}/share/${linkId}`, {
        method: 'DELETE',
      }),
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;

export async function signup(
  baseUrl: string,
  email: string,
): Promise<SignupResponse> {
  const resp = await fetch(`${baseUrl}/api/v1/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new ApiError(resp.status, body);
  }
  return resp.json();
}
