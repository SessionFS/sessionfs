import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import OrgPersonasSection from './OrgPersonasSection';

const { hooks, mockAddToast } = vi.hoisted(() => ({
  hooks: {
    // PersonasTab (imported for its modals) pulls the project hooks too.
    usePersonas: vi.fn(),
    useCreatePersona: vi.fn(),
    useUpdatePersona: vi.fn(),
    useDeletePersona: vi.fn(),
    useOrgPersonas: vi.fn(),
    useCreateOrgPersona: vi.fn(),
    useUpdateOrgPersona: vi.fn(),
    useDeleteOrgPersona: vi.fn(),
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

function makeMutation() {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    isPending: false,
    isError: false,
    error: null,
  };
}

const orgPersona = {
  id: 'p_org',
  project_id: null,
  org_id: 'org_1',
  scope: 'org',
  name: 'release-governance',
  role: 'Security sign-off',
  content: '# Release',
  specializations: [],
  is_active: true,
  version: 1,
  created_by: 'u1',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-02T00:00:00Z',
};

let create: ReturnType<typeof makeMutation>;
let remove: ReturnType<typeof makeMutation>;

beforeEach(() => {
  create = makeMutation();
  remove = makeMutation();
  hooks.useOrgPersonas.mockReturnValue({ data: [orgPersona], isLoading: false, error: null });
  hooks.useCreateOrgPersona.mockReturnValue(create);
  hooks.useUpdateOrgPersona.mockReturnValue(makeMutation());
  hooks.useDeleteOrgPersona.mockReturnValue(remove);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('OrgPersonasSection', () => {
  it('lists org personas', () => {
    render(<OrgPersonasSection orgId="org_1" />);
    expect(hooks.useOrgPersonas).toHaveBeenCalledWith('org_1');
    expect(screen.getByText('release-governance')).toBeInTheDocument();
    expect(screen.getByText('Security sign-off')).toBeInTheDocument();
  });

  it('shows an empty state', () => {
    hooks.useOrgPersonas.mockReturnValue({ data: [], isLoading: false, error: null });
    render(<OrgPersonasSection orgId="org_1" />);
    expect(screen.getByText(/No org personas yet/i)).toBeInTheDocument();
  });

  it('creates an org persona', async () => {
    render(<OrgPersonasSection orgId="org_1" />);
    fireEvent.click(screen.getByRole('button', { name: /New org persona/i }));
    expect(screen.getByRole('heading', { name: /New org persona/i })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Name/i), { target: { value: 'remediator' } });
    fireEvent.change(screen.getByLabelText(/Role/i), { target: { value: 'Alert responder' } });
    fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
    await new Promise((r) => setTimeout(r, 0));
    expect(create.mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'remediator', role: 'Alert responder' }),
    );
    expect(mockAddToast).toHaveBeenCalledWith('success', expect.stringContaining('remediator'));
  });

  it('deletes an org persona', async () => {
    render(<OrgPersonasSection orgId="org_1" />);
    fireEvent.click(screen.getByRole('button', { name: /^Delete$/i }));
    const dialogDelete = screen
      .getAllByRole('button', { name: /^Delete$/i })
      .find((b) => b.closest('[role="dialog"]'));
    fireEvent.click(dialogDelete!);
    await new Promise((r) => setTimeout(r, 0));
    expect(remove.mutateAsync).toHaveBeenCalledWith({ name: 'release-governance', force: false });
  });
});
