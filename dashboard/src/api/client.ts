export interface SessionSummary {
  id: string;
  title: string | null;
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
}

export interface SessionDetail extends SessionSummary {
  original_session_id: string | null;
  source_tool_version: string | null;
  model_provider: string | null;
  duration_ms: number | null;
  parent_session_id: string | null;
  uploaded_at: string;
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

export interface SignupResponse {
  user_id: string;
  email: string;
  raw_key: string;
  key_id: string;
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

    getMessages: (id: string, page = 1, pageSize = 50) =>
      request<MessagesResponse>(
        `/api/v1/sessions/${id}/messages?page=${page}&page_size=${pageSize}`,
      ),

    downloadSession: async (id: string): Promise<Blob> => {
      const resp = await fetch(`${baseUrl}/api/v1/sessions/${id}/download`, {
        headers: { Authorization: `Bearer ${apiKey}` },
      });
      if (!resp.ok) throw new ApiError(resp.status, 'Download failed');
      return resp.blob();
    },
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
