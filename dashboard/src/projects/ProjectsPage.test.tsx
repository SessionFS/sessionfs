import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ProjectsPage from './ProjectsPage';

const { mockUseProjects } = vi.hoisted(() => ({ mockUseProjects: vi.fn() }));
vi.mock('../hooks/useProjects', () => ({ useProjects: () => mockUseProjects() }));

const projects = [
  { id: 'p1', git_remote_normalized: 'github.com/acme/one', name: 'one', context_document: 'ctx one', session_count: 2, updated_at: '2026-06-10T00:00:00Z' },
  { id: 'p2', git_remote_normalized: 'github.com/acme/two', name: 'two', context_document: 'ctx two', session_count: 5, updated_at: '2026-06-11T00:00:00Z' },
];

function renderPage() {
  return render(<MemoryRouter><ProjectsPage /></MemoryRouter>);
}

describe('ProjectsPage view toggle', () => {
  beforeEach(() => {
    localStorage.clear();
    mockUseProjects.mockReturnValue({ data: projects, isLoading: false, error: null });
  });
  afterEach(() => vi.clearAllMocks());

  it('defaults to list view', () => {
    renderPage();
    expect(screen.getByRole('button', { name: 'List view' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Grid view' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('switching to grid persists to localStorage', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('button', { name: 'Grid view' }));
    expect(screen.getByRole('button', { name: 'Grid view' })).toHaveAttribute('aria-pressed', 'true');
    expect(localStorage.getItem('sfs-projects-view')).toBe('grid');
  });

  it('reads persisted grid preference on mount', () => {
    localStorage.setItem('sfs-projects-view', 'grid');
    renderPage();
    expect(screen.getByRole('button', { name: 'Grid view' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('renders all projects in both views', async () => {
    const user = userEvent.setup();
    renderPage();
    expect(screen.getByText('one')).toBeInTheDocument();
    expect(screen.getByText('two')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Grid view' }));
    expect(screen.getByText('one')).toBeInTheDocument();
    expect(screen.getByText('two')).toBeInTheDocument();
  });
});
