import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import GettingStartedPage from './GettingStartedPage';

const mockNavigate = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    auth: {
      client: {
        listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0, page: 1, page_size: 1, has_more: false }),
        listProjects: vi.fn().mockResolvedValue([]),
      },
    },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: vi.fn().mockReturnValue({ data: null, isLoading: false }),
}));

describe('GettingStartedPage', () => {
  beforeEach(() => {
    mockNavigate.mockReset();
  });

  function renderPage() {
    return render(
      <MemoryRouter>
        <GettingStartedPage />
      </MemoryRouter>,
    );
  }

  it('renders three steps', () => {
    renderPage();
    expect(screen.getByText('Connect your AI tool')).toBeInTheDocument();
    expect(screen.getByText('Capture your first session')).toBeInTheDocument();
    expect(screen.getByText('Set up project context')).toBeInTheDocument();
  });

  it('"Skip to Dashboard" navigates to /', () => {
    renderPage();
    fireEvent.click(screen.getByText(/skip to dashboard/i));
    expect(mockNavigate).toHaveBeenCalledWith('/');
  });

  it('Step 1 shows install command for Claude Code by default', () => {
    renderPage();
    expect(screen.getByText('sfs mcp install --for claude-code')).toBeInTheDocument();
  });

  it('Step 3 "Create Project" links to /projects', () => {
    renderPage();
    const link = screen.getByRole('link', { name: /create project/i });
    expect(link).toHaveAttribute('href', '/projects');
  });
});
