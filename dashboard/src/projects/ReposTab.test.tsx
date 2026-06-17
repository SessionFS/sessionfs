import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import ReposTab from './ReposTab';

const { hooks, mockAddToast } = vi.hoisted(() => ({
  hooks: {
    useProjectRepos: vi.fn(),
    useLinkRepo: vi.fn(),
    useUnlinkRepo: vi.fn(),
  },
  mockAddToast: vi.fn(),
}));

vi.mock('../hooks/useProjects', () => ({
  useProjectRepos: hooks.useProjectRepos,
  useLinkRepo: hooks.useLinkRepo,
  useUnlinkRepo: hooks.useUnlinkRepo,
}));
vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: mockAddToast }),
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

function repo(overrides: Record<string, unknown> = {}) {
  return {
    id: 'repo_1',
    project_id: 'proj_1',
    git_remote_normalized: 'github.com/acme/frontend',
    is_primary: true,
    verified: true,
    verification_method: 'github_app',
    provider: 'github',
    provider_repo_id: '12345',
    added_by_user_id: 'user_1',
    created_at: '2026-06-15T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  hooks.useProjectRepos.mockReturnValue({
    data: [repo()],
    isLoading: false,
    error: null,
  });
  hooks.useLinkRepo.mockReturnValue(makeMutation());
  hooks.useUnlinkRepo.mockReturnValue(makeMutation());
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('ReposTab', () => {
  it('renders repo rows with primary marker and verified badge', () => {
    render(<ReposTab projectId="proj_1" />);
    expect(screen.getByText('github.com/acme/frontend')).toBeInTheDocument();
    expect(screen.getByText('primary')).toBeInTheDocument();
    expect(screen.getByText('verified')).toBeInTheDocument();
  });

  it('shows unverified badge for owner-attested repos', () => {
    hooks.useProjectRepos.mockReturnValue({
      data: [repo({ verified: false, verification_method: 'owner_attested' })],
      isLoading: false,
      error: null,
    });
    render(<ReposTab projectId="proj_1" />);
    expect(screen.getByText('unverified')).toBeInTheDocument();
  });

  it('shows legacy badge for legacy repos', () => {
    hooks.useProjectRepos.mockReturnValue({
      data: [repo({ verified: false, verification_method: 'legacy_backfill' })],
      isLoading: false,
      error: null,
    });
    render(<ReposTab projectId="proj_1" />);
    expect(screen.getByText('legacy')).toBeInTheDocument();
  });

  it('shows empty state when no repos', () => {
    hooks.useProjectRepos.mockReturnValue({ data: [], isLoading: false, error: null });
    render(<ReposTab projectId="proj_1" />);
    expect(screen.getByText(/No repos linked/i)).toBeInTheDocument();
  });

  it('opens the add repo modal', () => {
    render(<ReposTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /Add repo/i }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Add repo to project')).toBeInTheDocument();
  });

  it('shows 409 error in modal when repo already linked', async () => {
    const linkMutate = vi.fn();
    hooks.useLinkRepo.mockReturnValue(
      makeMutation({
        mutate: linkMutate,
      }),
    );

    render(<ReposTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /Add repo/i }));

    const input = screen.getByPlaceholderText('github.com/org/repo');
    fireEvent.change(input, { target: { value: 'github.com/acme/duplicate' } });

    // Simulate error from the mutation
    linkMutate.mockImplementation((_body: unknown, opts: { onError: (e: Error) => void }) => {
      opts.onError(new ApiError(409, JSON.stringify({
        error: { code: 'repo_already_linked', message: 'This repo is already linked to another project.' },
      })));
    });

    fireEvent.click(screen.getByRole('button', { name: 'Link repo' }));
    expect(await screen.findByText(/repo_already_linked/i)).toBeInTheDocument();
  });

  it('shows cross-org error in modal', async () => {
    const linkMutate = vi.fn();
    hooks.useLinkRepo.mockReturnValue(
      makeMutation({
        mutate: linkMutate,
      }),
    );

    render(<ReposTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /Add repo/i }));

    const input = screen.getByPlaceholderText('github.com/org/repo');
    fireEvent.change(input, { target: { value: 'github.com/other-org/repo' } });

    linkMutate.mockImplementation((_body: unknown, opts: { onError: (e: Error) => void }) => {
      opts.onError(new ApiError(403, JSON.stringify({
        error: { code: 'cross_org_denied', message: 'Repo belongs to a different organization.' },
      })));
    });

    fireEvent.click(screen.getByRole('button', { name: 'Link repo' }));
    expect(await screen.findByText(/cross_org_denied/i)).toBeInTheDocument();
  });

  it('shows unlink confirmation and blocks last repo', () => {
    // Single repo — last-repo block active
    render(<ReposTab projectId="proj_1" />);
    fireEvent.click(screen.getByLabelText(/Unlink github.com\/acme\/frontend/i));
    expect(screen.getByText(/Cannot unlink the last repo/i)).toBeInTheDocument();
    // Unlink button should NOT be shown in confirm when last repo
    expect(screen.queryByRole('button', { name: 'Unlink' })).toBeNull();
  });

  it('shows unlink confirmation with unlink button for non-last repo', () => {
    hooks.useProjectRepos.mockReturnValue({
      data: [
        repo({ id: 'repo_1', is_primary: true }),
        repo({ id: 'repo_2', git_remote_normalized: 'github.com/acme/backend', is_primary: false }),
      ],
      isLoading: false,
      error: null,
    });
    render(<ReposTab projectId="proj_1" />);
    const unlinkButtons = screen.getAllByLabelText(/Unlink/);
    fireEvent.click(unlinkButtons[1]); // click the second (non-primary)

    expect(screen.getByText(/Knowledge entries and tickets from this repo stay/i)).toBeInTheDocument();
    // Unlink button should be present in the confirm
    expect(screen.getByRole('button', { name: 'Unlink' })).toBeInTheDocument();
  });

  it('shows loading state', () => {
    hooks.useProjectRepos.mockReturnValue({ data: undefined, isLoading: true, error: null });
    render(<ReposTab projectId="proj_1" />);
    expect(screen.getByText(/Loading repos/i)).toBeInTheDocument();
  });

  it('shows error state', () => {
    hooks.useProjectRepos.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('Network error'),
    });
    render(<ReposTab projectId="proj_1" />);
    expect(screen.getByText(/Failed to load repos/i)).toBeInTheDocument();
  });
});
