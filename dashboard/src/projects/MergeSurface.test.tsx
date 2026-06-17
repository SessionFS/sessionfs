import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import MergeSurface from './MergeSurface';

const { hooks, mockAddToast, mockNavigate } = vi.hoisted(() => ({
  hooks: {
    useProjects: vi.fn(),
    useMergeProject: vi.fn(),
  },
  mockAddToast: vi.fn(),
  mockNavigate: vi.fn(),
}));

vi.mock('../hooks/useProjects', () => ({
  useProjects: hooks.useProjects,
  useMergeProject: hooks.useMergeProject,
}));
vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));
vi.mock('react-router-dom', () => ({
  useNavigate: () => mockNavigate,
}));
vi.mock('../hooks/useFocusTrap', () => ({
  useFocusTrap: vi.fn(),
}));

function makeMutation(extra: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    isPending: false,
    isError: false,
    error: null,
    ...extra,
  };
}

function project(overrides: Record<string, unknown> = {}) {
  return {
    id: 'proj_backend',
    name: 'Backend API',
    git_remote_normalized: 'github.com/acme/backend',
    context_document: '',
    owner_id: 'user_1',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-10T00:00:00Z',
    session_count: 42,
    auto_narrative: false,
    merged_into_project_id: null,
    ...overrides,
  };
}

function dryRunResponse() {
  return {
    dry_run: true as const,
    stats: {
      personas_total: 3,
      personas_reassigned: 2,
      persona_collisions: [
        {
          old_name: 'prism',
          new_name: 'prism-a1b2c3d4',
          display_note: 'Renamed from prism (source project a1b2c3d4) — collided with target persona of same name.',
        },
      ],
      tickets_total: 5,
      tickets_reassigned: 5,
      knowledge_entries_total: 47,
      knowledge_entries_reassigned: 42,
      knowledge_entries_skipped: 5,
      wiki_pages_total: 2,
      wiki_pages_reassigned: 2,
      slug_collisions: [],
      rules_action: 'archived',
    },
    persona_collisions: [
      {
        old_name: 'prism',
        new_name: 'prism-a1b2c3d4',
        display_note: 'Renamed from prism',
      },
    ],
    slug_collisions: [],
    ke_duplicates: [],
  };
}

function executeResponse(status: 'completed' | 'failed' = 'completed') {
  return {
    dry_run: false as const,
    status,
    audit_id: 'audit_123',
    stats: dryRunResponse().stats,
    persona_renames: dryRunResponse().persona_collisions,
    slug_renames: [],
    skipped_ke_ids: [],
    skipped_link_ids: [],
    rules_action: 'archived',
  };
}

beforeEach(() => {
  hooks.useProjects.mockReturnValue({
    data: [
      project({ id: 'proj_backend', name: 'Backend API' }),
      project({ id: 'proj_frontend', name: 'Frontend App', git_remote_normalized: 'github.com/acme/frontend' }),
    ],
    isLoading: false,
    error: null,
  });
  hooks.useMergeProject.mockReturnValue(makeMutation());
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('MergeSurface', () => {
  it('renders with project selector', () => {
    render(<MergeSurface projectId="proj_frontend" projectName="Frontend App" />);
    expect(screen.getByText(/Fold another project/i)).toBeInTheDocument();
    // Should show Backend API in the combo (not itself)
    expect(screen.getByText('Select a project…')).toBeInTheDocument();
  });

  it('shows empty state when no other projects', () => {
    hooks.useProjects.mockReturnValue({
      data: [project({ id: 'proj_frontend' })],
      isLoading: false,
      error: null,
    });
    render(<MergeSurface projectId="proj_frontend" projectName="Frontend App" />);
    expect(screen.getByText(/No other projects available/i)).toBeInTheDocument();
  });

  it('shows loading state', () => {
    hooks.useProjects.mockReturnValue({ data: undefined, isLoading: true, error: null });
    render(<MergeSurface projectId="proj_frontend" projectName="Frontend App" />);
    expect(screen.getByText(/Loading projects/i)).toBeInTheDocument();
  });

  it('renders dry-run plan with collisions', async () => {
    const mergeMutate = vi.fn();
    hooks.useMergeProject.mockReturnValue(
      makeMutation({
        mutate: mergeMutate,
      }),
    );

    render(<MergeSurface projectId="proj_frontend" projectName="Frontend App" />);

    // Select source project via the Select combobox
    const select = screen.getByRole('combobox', { name: 'Source project' });
    fireEvent.click(select);
    // Pick "Backend API"
    fireEvent.click(screen.getByText('Backend API'));

    // Click preview
    mergeMutate.mockImplementation((_body: unknown, opts: { onSuccess: (r: unknown) => void }) => {
      opts.onSuccess(dryRunResponse());
    });

    fireEvent.click(screen.getByRole('button', { name: /Preview merge/i }));

    await waitFor(() => {
      expect(screen.getByText(/Dry-run plan/i)).toBeInTheDocument();
    });

    // Verify collision rendering (appears in summary + Personas section)
    expect(screen.getByText(/prism-a1b2c3d4/)).toBeInTheDocument();
    const collisionEls = screen.getAllByText(/1 collision/);
    expect(collisionEls.length).toBeGreaterThanOrEqual(1);
  });

  it('shows confirm step before execute', async () => {
    const mergeMutate = vi.fn();
    hooks.useMergeProject.mockReturnValue(
      makeMutation({
        mutate: mergeMutate,
      }),
    );

    render(<MergeSurface projectId="proj_frontend" projectName="Frontend App" />);

    // Select source
    const select = screen.getByRole('combobox', { name: 'Source project' });
    fireEvent.click(select);
    fireEvent.click(screen.getByText('Backend API'));

    // Dry-run
    mergeMutate.mockImplementation((_body: unknown, opts: { onSuccess: (r: unknown) => void }) => {
      opts.onSuccess(dryRunResponse());
    });
    fireEvent.click(screen.getByRole('button', { name: /Preview merge/i }));

    await waitFor(() => {
      expect(screen.getByText(/Dry-run plan/i)).toBeInTheDocument();
    });

    // Continue to confirm
    fireEvent.click(screen.getByRole('button', { name: /Continue to execute/i }));

    await waitFor(() => {
      expect(screen.getByText(/Confirm merge execution/i)).toBeInTheDocument();
    });

    // Execute button should be disabled without checkbox
    const executeBtn = screen.getByRole('button', { name: /Execute merge/i });
    expect(executeBtn).toBeDisabled();

    // Check the confirm checkbox
    fireEvent.click(screen.getByLabelText(/I understand/i));
    expect(executeBtn).not.toBeDisabled();
  });

  it('flows from confirm to execute to result', async () => {
    const mergeMutate = vi.fn();
    hooks.useMergeProject.mockReturnValue(
      makeMutation({
        mutate: mergeMutate,
      }),
    );

    render(<MergeSurface projectId="proj_frontend" projectName="Frontend App" />);

    // Select source
    const select = screen.getByRole('combobox', { name: 'Source project' });
    fireEvent.click(select);
    fireEvent.click(screen.getByText('Backend API'));

    // Dry-run
    mergeMutate.mockImplementation((_body: unknown, opts: { onSuccess: (r: unknown) => void }) => {
      opts.onSuccess(dryRunResponse());
    });
    fireEvent.click(screen.getByRole('button', { name: /Preview merge/i }));

    await waitFor(() => {
      expect(screen.getByText(/Dry-run plan/i)).toBeInTheDocument();
    });

    // Continue to confirm
    fireEvent.click(screen.getByRole('button', { name: /Continue to execute/i }));

    await waitFor(() => {
      expect(screen.getByText(/Confirm merge execution/i)).toBeInTheDocument();
    });

    // Check and execute
    fireEvent.click(screen.getByLabelText(/I understand/i));

    mergeMutate.mockImplementation((_body: unknown, opts: { onSuccess: (r: unknown) => void }) => {
      opts.onSuccess(executeResponse('completed'));
    });
    fireEvent.click(screen.getByRole('button', { name: /Execute merge/i }));

    await waitFor(() => {
      expect(screen.getByText(/Merge completed/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/audit_123/)).toBeInTheDocument();
  });

  it('renders failed outcome', async () => {
    const mergeMutate = vi.fn();
    hooks.useMergeProject.mockReturnValue(
      makeMutation({
        mutate: mergeMutate,
      }),
    );

    render(<MergeSurface projectId="proj_frontend" projectName="Frontend App" />);

    // Select source
    const select = screen.getByRole('combobox', { name: 'Source project' });
    fireEvent.click(select);
    fireEvent.click(screen.getByText('Backend API'));

    // Dry-run
    mergeMutate.mockImplementation((_body: unknown, opts: { onSuccess: (r: unknown) => void }) => {
      opts.onSuccess(dryRunResponse());
    });
    fireEvent.click(screen.getByRole('button', { name: /Preview merge/i }));

    await waitFor(() => {
      expect(screen.getByText(/Dry-run plan/i)).toBeInTheDocument();
    });

    // Continue to confirm
    fireEvent.click(screen.getByRole('button', { name: /Continue to execute/i }));

    await waitFor(() => {
      expect(screen.getByText(/Confirm merge execution/i)).toBeInTheDocument();
    });

    // Check and execute — but it fails
    fireEvent.click(screen.getByLabelText(/I understand/i));

    mergeMutate.mockImplementation((_body: unknown, opts: { onSuccess: (r: unknown) => void }) => {
      opts.onSuccess(executeResponse('failed'));
    });
    fireEvent.click(screen.getByRole('button', { name: /Execute merge/i }));

    await waitFor(() => {
      expect(screen.getByText(/Merge failed/i)).toBeInTheDocument();
    });
  });

  it('shows back-to-projects button on success', async () => {
    const mergeMutate = vi.fn();
    hooks.useMergeProject.mockReturnValue(
      makeMutation({
        mutate: mergeMutate,
      }),
    );

    render(<MergeSurface projectId="proj_frontend" projectName="Frontend App" />);

    const select = screen.getByRole('combobox', { name: 'Source project' });
    fireEvent.click(select);
    fireEvent.click(screen.getByText('Backend API'));

    mergeMutate.mockImplementation((_body: unknown, opts: { onSuccess: (r: unknown) => void }) => {
      opts.onSuccess(dryRunResponse());
    });
    fireEvent.click(screen.getByRole('button', { name: /Preview merge/i }));

    await waitFor(() => {
      expect(screen.getByText(/Dry-run plan/i)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /Continue to execute/i }));

    await waitFor(() => {
      expect(screen.getByText(/Confirm merge execution/i)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByLabelText(/I understand/i));

    mergeMutate.mockImplementation((_body: unknown, opts: { onSuccess: (r: unknown) => void }) => {
      opts.onSuccess(executeResponse('completed'));
    });
    fireEvent.click(screen.getByRole('button', { name: /Execute merge/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Back to projects/i })).toBeInTheDocument();
    });
  });
});
