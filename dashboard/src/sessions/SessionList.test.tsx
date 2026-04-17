import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import SessionList from './SessionList';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useSessions: vi.fn(),
    useDeletedSessions: vi.fn(),
    useRestoreSession: vi.fn(),
    useFolders: vi.fn(),
    useAddBookmark: vi.fn(),
    useFolderSessions: vi.fn(),
  },
}));

vi.mock('../hooks/useSessions', () => ({
  useSessions: (...args: unknown[]) => hooks.useSessions(...args),
  useDeletedSessions: () => hooks.useDeletedSessions(),
  useRestoreSession: () => hooks.useRestoreSession(),
}));

vi.mock('../hooks/useBookmarks', () => ({
  useFolders: () => hooks.useFolders(),
  useAddBookmark: () => hooks.useAddBookmark(),
  useFolderSessions: (...args: unknown[]) => hooks.useFolderSessions(...args),
  useCreateFolder: () => ({ mutateAsync: vi.fn() }),
  useUpdateFolder: () => ({ mutateAsync: vi.fn() }),
  useDeleteFolder: () => ({ mutateAsync: vi.fn() }),
}));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    auth: {
      client: {
        listSessions: vi.fn(),
        listHandoffs: vi.fn().mockResolvedValue({ handoffs: [] }),
        setAlias: vi.fn(),
        bulkDelete: vi.fn(),
      },
    },
  }),
}));

vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: vi.fn(), removeToast: vi.fn(), toasts: [] }),
}));

vi.mock('@tanstack/react-query', async () => {
  const actual = await vi.importActual<typeof import('@tanstack/react-query')>('@tanstack/react-query');
  return {
    ...actual,
    useQuery: vi.fn().mockReturnValue({ data: null, isLoading: false }),
    useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  };
});

describe('SessionList empty state', () => {
  it('shows "Get Started" link to /getting-started when no sessions', () => {
    hooks.useSessions.mockReturnValue({
      data: { sessions: [], total: 0, page: 1, page_size: 20, has_more: false },
      isLoading: false,
      error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: [], isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolders.mockReturnValue({ data: [], isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: [], isLoading: false });

    render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );

    expect(screen.getByText('No sessions yet')).toBeInTheDocument();
    expect(screen.getByText(/captures sessions automatically/i)).toBeInTheDocument();
    const getStartedLink = screen.getByRole('link', { name: /get started/i });
    expect(getStartedLink).toHaveAttribute('href', '/getting-started');
    const helpLink = screen.getByRole('link', { name: /view help/i });
    expect(helpLink).toHaveAttribute('href', '/help');
  });
});
