import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import PersonasTab from './PersonasTab';

const { hooks, mockAddToast } = vi.hoisted(() => ({
  hooks: {
    usePersonas: vi.fn(),
    useCreatePersona: vi.fn(),
    useUpdatePersona: vi.fn(),
    useDeletePersona: vi.fn(),
  },
  mockAddToast: vi.fn(),
}));

vi.mock('../hooks/usePersonas', () => hooks);
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

function persona(overrides: Record<string, unknown> = {}) {
  return {
    id: 'p_1',
    project_id: 'proj_1',
    name: 'atlas',
    role: 'Backend Architect',
    content: '# atlas\n\nBackend specialist',
    specializations: ['python', 'fastapi'],
    is_active: true,
    version: 1,
    created_by: 'u1',
    created_at: '2026-05-13T00:00:00Z',
    updated_at: '2026-05-14T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  hooks.usePersonas.mockReturnValue({ data: [persona()], isLoading: false, error: null });
  hooks.useCreatePersona.mockReturnValue(makeMutation());
  hooks.useUpdatePersona.mockReturnValue(makeMutation());
  hooks.useDeletePersona.mockReturnValue(makeMutation());
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('PersonasTab', () => {
  it('renders persona rows', () => {
    render(<PersonasTab projectId="proj_1" />);
    expect(screen.getByText('atlas')).toBeInTheDocument();
    expect(screen.getByText('Backend Architect')).toBeInTheDocument();
    expect(screen.getByText(/python, fastapi/)).toBeInTheDocument();
  });

  it('shows empty state when no personas', () => {
    hooks.usePersonas.mockReturnValue({ data: [], isLoading: false, error: null });
    render(<PersonasTab projectId="proj_1" />);
    expect(screen.getByText(/No personas yet/i)).toBeInTheDocument();
  });

  it('opens the create modal', () => {
    render(<PersonasTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /New persona/i }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /New persona/i })).toBeInTheDocument();
  });

  it('opens the edit modal with persona prefilled', () => {
    render(<PersonasTab projectId="proj_1" />);
    fireEvent.click(screen.getAllByRole('button', { name: /Edit/i })[0]);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    const nameField = screen.getByDisplayValue('atlas') as HTMLInputElement;
    expect(nameField.disabled).toBe(true);
  });

  it('opens the delete confirm modal', () => {
    render(<PersonasTab projectId="proj_1" />);
    fireEvent.click(screen.getAllByRole('button', { name: /Delete/i })[0]);
    expect(screen.getByText(/Delete "atlas"\?/i)).toBeInTheDocument();
    expect(screen.getByText(/Force/)).toBeInTheDocument();
  });

  it('closes the delete confirm modal on Escape', () => {
    render(<PersonasTab projectId="proj_1" />);
    fireEvent.click(screen.getAllByRole('button', { name: /Delete/i })[0]);
    expect(screen.getByText(/Delete "atlas"\?/i)).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByText(/Delete "atlas"\?/i)).toBeNull();
  });

  it('surfaces API errors as toasts', async () => {
    const create = makeMutation({
      mutateAsync: vi.fn().mockRejectedValue(new ApiError(409, 'name taken')),
    });
    hooks.useCreatePersona.mockReturnValue(create);
    render(<PersonasTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /New persona/i }));
    fireEvent.change(screen.getByLabelText(/Name/i), { target: { value: 'taken' } });
    fireEvent.change(screen.getByLabelText(/Role/i), { target: { value: 'Tester' } });
    fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
    await new Promise((r) => setTimeout(r, 0));
    expect(mockAddToast).toHaveBeenCalledWith('error', expect.stringContaining('409'));
  });
});
