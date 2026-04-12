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
}

export interface SessionListResponse {
  sessions: SessionSummary[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface MessagesResponse {
  messages: Record<string, unknown>[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
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
  created_at: string;
}

export interface GitHubInstallationResponse {
  account_login: string | null;
  account_type: string | null;
  auto_comment: boolean;
  include_trust_score: boolean;
  include_session_links: boolean;
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
}

export interface WikiPageDetail extends WikiPage {
  backlinks?: { source_type: string; source_id: string; link_type: string; confidence: number }[];
}

export interface WikiPageListResponse {
  pages: WikiPage[];
}

export interface ProjectContext {
  id: string;
  name: string;
  git_remote_normalized: string;
  context_document: string;
  owner_id: string;
  created_at: string;
  updated_at: string;
  session_count?: number;
  auto_narrative?: boolean;
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
}

export interface ProjectHealthResponse {
  project_id: string;
  total_entries: number;
  pending_entries: number;
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

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
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
    return resp.json();
  }

  return {
    health: () => request<{ status: string }>('/health'),

    getMe: () => request<{
      user_id: string;
      email: string;
      display_name: string | null;
      email_verified: boolean;
      tier: string;
      created_at: string | null;
      last_client_version: string | null;
      last_client_platform: string | null;
      last_client_device: string | null;
      last_sync_at: string | null;
      latest_version: string;
    }>('/api/v1/auth/me'),

    listSessions: (params: {
      page?: number;
      page_size?: number;
      source_tool?: string;
      tag?: string;
    } = {}) => {
      const sp = new URLSearchParams();
      if (params.page) sp.set('page', String(params.page));
      if (params.page_size) sp.set('page_size', String(params.page_size));
      if (params.source_tool) sp.set('source_tool', params.source_tool);
      if (params.tag) sp.set('tag', params.tag);
      return request<SessionListResponse>(`/api/v1/sessions?${sp}`);
    },

    getSession: (id: string) =>
      request<SessionDetail>(`/api/v1/sessions/${id}`),

    deleteSession: (id: string) =>
      request<void>(`/api/v1/sessions/${id}`, { method: 'DELETE' }),

    getMessages: (id: string, page = 1, pageSize = 50, order: 'oldest' | 'newest' = 'oldest') =>
      request<MessagesResponse>(
        `/api/v1/sessions/${id}/messages?page=${page}&page_size=${pageSize}&order=${order}`,
      ),

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
      request<void>('/api/v1/settings/judge', {
        method: 'PUT',
        body: JSON.stringify({ provider, model, api_key: apiKey, base_url: baseUrl || null }),
      }),

    clearJudgeSettings: () =>
      request<void>('/api/v1/settings/judge', {
        method: 'DELETE',
      }),

    discoverModels: (baseUrl: string, apiKey?: string) => {
      const params = new URLSearchParams({ base_url: baseUrl });
      if (apiKey) params.set('api_key', apiKey);
      return request<{ models: { id: string; owned_by: string }[]; error?: string; base_url?: string }>(
        `/api/v1/settings/judge/models?${params}`,
      );
    },

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
      installation_id?: number;
    }) =>
      request<GitHubInstallationResponse>('/api/v1/settings/github', {
        method: 'PUT',
        body: JSON.stringify(updates),
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
        body: JSON.stringify({ content, ...(title ? { title } : {}) }),
      }),

    deleteWikiPage: (projectId: string, slug: string) =>
      request<void>(`/api/v1/projects/${projectId}/pages/${encodeURIComponent(slug)}`, {
        method: 'DELETE',
      }),

    regenerateWikiPage: (projectId: string, slug: string) =>
      request<{ status: string; slug: string; word_count: number; entries_used: number }>(
        `/api/v1/projects/${projectId}/pages/${encodeURIComponent(slug)}/regenerate`,
        { method: 'POST' },
      ),

    updateProjectSettings: (projectId: string, settings: { auto_narrative?: boolean }) =>
      request<{ status: string }>(`/api/v1/projects/${projectId}/settings`, {
        method: 'PUT',
        body: JSON.stringify(settings),
      }),

    // DLP policy
    getDLPPolicy: () =>
      request<{ enabled: boolean; mode: string; categories: string[] }>('/api/v1/dlp/policy'),

    updateDLPPolicy: (policy: { enabled?: boolean; mode?: string; categories?: string[] }) =>
      request<{ enabled: boolean; mode: string; categories: string[] }>('/api/v1/dlp/policy', {
        method: 'PUT',
        body: JSON.stringify(policy),
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
