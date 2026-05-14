import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AgentRunsTab from './AgentRunsTab';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useAgentRuns: vi.fn(),
  },
}));

vi.mock('../hooks/useAgentRuns', () => hooks);

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: 'run_abc',
    project_id: 'proj_1',
    persona_name: 'sentinel',
    tool: 'generic',
    trigger_source: 'ci',
    status: 'passed',
    ticket_id: 'tk_1',
    trigger_ref: 'sha123',
    ci_provider: 'github',
    ci_run_url: 'https://example.com/run',
    result_summary: 'Sentinel reviewed PR #42.',
    severity: 'low',
    findings_count: 2,
    findings: [
      { severity: 'low', title: 'unused import' },
      { severity: 'low', title: 'docstring missing' },
    ],
    fail_on: 'high',
    policy_result: 'pass',
    exit_code: 0,
    session_id: null,
    triggered_by_user_id: 'u1',
    triggered_by_persona: 'sentinel',
    created_at: '2026-05-14T00:00:00Z',
    started_at: '2026-05-14T00:00:01Z',
    completed_at: '2026-05-14T00:01:00Z',
    duration_seconds: 59,
    ...overrides,
  };
}

beforeEach(() => {
  hooks.useAgentRuns.mockReturnValue({
    data: [run()],
    isLoading: false,
    error: null,
    refetch: vi.fn(),
    isFetching: false,
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('AgentRunsTab', () => {
  it('renders run rows', () => {
    render(<AgentRunsTab projectId="proj_1" />);
    expect(screen.getByText('run_abc')).toBeInTheDocument();
    expect(screen.getByText('sentinel')).toBeInTheDocument();
    expect(screen.getByText('passed')).toBeInTheDocument();
    expect(screen.getByText(/2 findings/)).toBeInTheDocument();
    expect(screen.getByText(/exit 0/)).toBeInTheDocument();
  });

  it('shows empty state when no runs', () => {
    hooks.useAgentRuns.mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
      refetch: vi.fn(),
      isFetching: false,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    expect(screen.getByText(/No runs yet/i)).toBeInTheDocument();
  });

  it('expands run detail on click and renders findings JSON', () => {
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    // The summary appears both in the row's truncated preview AND in
    // the expanded detail panel; getAllByText to cover both occurrences.
    expect(screen.getAllByText(/Sentinel reviewed PR #42/).length).toBeGreaterThan(0);
    expect(screen.getByText(/unused import/)).toBeInTheDocument();
  });

  it('renders ci_run_url as a link', () => {
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    const link = screen.getByRole('link', { name: /github/i });
    expect(link).toHaveAttribute('href', 'https://example.com/run');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('refuses to render javascript: ci_run_url as a clickable link', () => {
    hooks.useAgentRuns.mockReturnValue({
      data: [run({ ci_run_url: 'javascript:alert(1)//' })],
      isLoading: false,
      error: null,
      refetch: vi.fn(),
      isFetching: false,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    // Provider label still renders, but NOT as an <a>.
    expect(screen.queryByRole('link', { name: /github/i })).toBeNull();
    // Plain text node falls through to "github".
    expect(screen.getAllByText('github').length).toBeGreaterThan(0);
  });

  it('refuses to render a malformed ci_run_url as a clickable link', () => {
    hooks.useAgentRuns.mockReturnValue({
      data: [run({ ci_run_url: 'not a url' })],
      isLoading: false,
      error: null,
      refetch: vi.fn(),
      isFetching: false,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    expect(screen.queryByRole('link', { name: /github/i })).toBeNull();
  });

  it('filters by status', () => {
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.change(screen.getByLabelText(/Filter by status/i), {
      target: { value: 'failed' },
    });
    expect(hooks.useAgentRuns).toHaveBeenLastCalledWith(
      'proj_1',
      expect.objectContaining({ status: 'failed' }),
    );
  });

  it('flags failed runs with the danger badge', () => {
    hooks.useAgentRuns.mockReturnValue({
      data: [run({ status: 'failed', policy_result: 'fail', exit_code: 1, severity: 'high' })],
      isLoading: false,
      error: null,
      refetch: vi.fn(),
      isFetching: false,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    expect(screen.getByText('failed')).toBeInTheDocument();
    expect(screen.getByText('fail')).toBeInTheDocument();
    expect(screen.getByText(/exit 1/)).toBeInTheDocument();
  });
});
