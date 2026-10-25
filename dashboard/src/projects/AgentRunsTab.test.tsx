import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AgentRunsTab from './AgentRunsTab';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useAgentRuns: vi.fn(),
    useReplayAgentRun: vi.fn(),
    useCompareAgentRuns: vi.fn(),
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
    replayed_from: null,
    prompt: null,
    context_refs: [],
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
  hooks.useReplayAgentRun.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  });
  hooks.useCompareAgentRuns.mockReturnValue({
    data: null,
    isFetching: false,
    error: null,
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

  it('filters by status', async () => {
    const user = userEvent.setup();
    render(<AgentRunsTab projectId="proj_1" />);
    await user.click(screen.getByRole('button', { name: /Filter by status/i }));
    await user.click(
      screen.getByRole('option', { name: 'Failed' }),
    );
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

  it('shows a replayed-from link on child runs', () => {
    hooks.useAgentRuns.mockReturnValue({
      data: [run({ replayed_from: 'run_parent' })],
      isLoading: false,
      error: null,
      refetch: vi.fn(),
      isFetching: false,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    expect(screen.getByText('Replayed from')).toBeInTheDocument();
    const link = screen.getByRole('button', { name: 'run_parent' });
    expect(link).toBeInTheDocument();
  });

  it('opens the replay dialog and submits edited fields', async () => {
    const user = userEvent.setup();
    const mutate = vi.fn();
    hooks.useReplayAgentRun.mockReturnValue({
      mutate,
      isPending: false,
      error: null,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    await user.click(screen.getByRole('button', { name: 'Replay' }));
    expect(screen.getByText(/Replay run/)).toBeInTheDocument();

    await user.type(screen.getByLabelText('Prompt'), 'review the change');
    await user.type(screen.getByLabelText('Ticket'), 'tk_9');
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Replay' }),
    );

    expect(mutate).toHaveBeenCalledWith(
      { runId: 'run_abc', body: { prompt: 'review the change', ticket_id: 'tk_9' } },
      expect.anything(),
    );
  });

  it('shows the generated command with copy after a successful replay', async () => {
    const user = userEvent.setup();
    const mutate = vi.fn((_vars, opts) => opts.onSuccess({
      run: run({ id: 'run_new', replayed_from: 'run_abc' }),
      command: 'sfs agent run --run-id run_new',
      ci_hint: '# Replay this agent run in CI.\ngithub_actions:\n  - name: Replay agent run\n    run: sfs agent run --run-id run_new',
    }));
    hooks.useReplayAgentRun.mockReturnValue({
      mutate,
      isPending: false,
      error: null,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    await user.click(screen.getByRole('button', { name: 'Replay' }));
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Replay' }),
    );

    expect(screen.getAllByText(/sfs agent run --run-id run_new/).length).toBeGreaterThan(0);
    expect(screen.getByText(/github_actions/)).toBeInTheDocument();
  });

  it('opens the compare dialog and shows the findings diff', async () => {
    const user = userEvent.setup();
    hooks.useAgentRuns.mockReturnValue({
      data: [run(), run({ id: 'run_xyz' })],
      isLoading: false,
      error: null,
      refetch: vi.fn(),
      isFetching: false,
    });
    hooks.useCompareAgentRuns.mockReturnValue({
      data: {
        a_id: 'run_abc',
        b_id: 'run_xyz',
        a_findings_count: 2,
        b_findings_count: 1,
        a_severity_counts: { low: 2 },
        b_severity_counts: { high: 1 },
        added: [{ rule: 'missing-rule' }],
        removed: [{ title: 'docstring missing' }],
        unchanged: [{ title: 'unused import' }],
      },
      isFetching: false,
      error: null,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    await user.click(screen.getByRole('button', { name: 'Compare' }));
    expect(screen.getByText(/Compare findings/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Compare with run/i }));
    await user.click(screen.getByRole('option', { name: 'run_xyz' }));

    const dialog = within(screen.getByRole('dialog'));
    expect(dialog.getByText(/Added \(1\)/)).toBeInTheDocument();
    expect(dialog.getByText(/missing-rule/)).toBeInTheDocument();
    expect(dialog.getByText(/Removed \(1\)/)).toBeInTheDocument();
    expect(dialog.getByText(/docstring missing/)).toBeInTheDocument();
    expect(dialog.getByText(/Unchanged \(1\)/)).toBeInTheDocument();
  });

  it('flags a truncated compare diff', async () => {
    const user = userEvent.setup();
    hooks.useAgentRuns.mockReturnValue({
      data: [run(), run({ id: 'run_xyz' })],
      isLoading: false,
      error: null,
      refetch: vi.fn(),
      isFetching: false,
    });
    hooks.useCompareAgentRuns.mockReturnValue({
      data: {
        a_id: 'run_abc',
        b_id: 'run_xyz',
        a_findings_count: 600,
        b_findings_count: 600,
        a_severity_counts: { low: 600 },
        b_severity_counts: { high: 600 },
        added: [{ rule: 'x' }],
        removed: [{ rule: 'y' }],
        unchanged: [],
        truncated: true,
      },
      isFetching: false,
      error: null,
    });
    render(<AgentRunsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('run_abc'));
    await user.click(screen.getByRole('button', { name: 'Compare' }));
    await user.click(screen.getByRole('button', { name: /Compare with run/i }));
    await user.click(screen.getByRole('option', { name: 'run_xyz' }));

    expect(screen.getByText(/truncated/i)).toBeInTheDocument();
  });
});
