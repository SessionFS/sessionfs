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
let update: ReturnType<typeof makeMutation>;
let remove: ReturnType<typeof makeMutation>;

beforeEach(() => {
  create = makeMutation();
  update = makeMutation();
  remove = makeMutation();
  hooks.useOrgPersonas.mockReturnValue({ data: [orgPersona], isLoading: false, error: null });
  hooks.useCreateOrgPersona.mockReturnValue(create);
  hooks.useUpdateOrgPersona.mockReturnValue(update);
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

  // GitHub #81 — the member an org persona represents.
  describe('represented member', () => {
    const members = [
      { user_id: 'u_kelvin', email: 'kelvin@example.com' },
      { user_id: 'u_tolu', email: 'tolu@example.com' },
    ];
    const kelvinPersona = { ...orgPersona, id: 'p_k', name: 'kelvin', represented_user_id: 'u_kelvin' };

    it('lets admins choose the represented member', async () => {
      render(<OrgPersonasSection orgId="org_1" members={members} />);
      fireEvent.click(screen.getByRole('combobox', { name: /Member represented by release-governance/i }));
      fireEvent.click(screen.getByRole('option', { name: 'tolu@example.com' }));
      await new Promise((r) => setTimeout(r, 0));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        name: 'release-governance',
        body: { represented_user: 'u_tolu' },
      });
      expect(mockAddToast).toHaveBeenCalledWith('success', expect.stringContaining('tolu@example.com'));
    });

    it('lets admins clear the represented member', async () => {
      hooks.useOrgPersonas.mockReturnValue({ data: [kelvinPersona], isLoading: false, error: null });
      render(<OrgPersonasSection orgId="org_1" members={members} />);
      fireEvent.click(screen.getByRole('combobox', { name: /Member represented by kelvin/i }));
      fireEvent.click(screen.getByRole('option', { name: 'Nobody' }));
      await new Promise((r) => setTimeout(r, 0));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        name: 'kelvin',
        body: { represented_user: null },
      });
    });

    it('shows a member only their own persona, without admin actions', async () => {
      hooks.useOrgPersonas.mockReturnValue({
        data: [orgPersona, kelvinPersona],
        isLoading: false,
        error: null,
      });
      render(
        <OrgPersonasSection orgId="org_1" isAdmin={false} myUserId="u_kelvin" members={members} />,
      );
      expect(screen.getByRole('heading', { name: /Your org persona/i })).toBeInTheDocument();
      expect(screen.getByText('kelvin')).toBeInTheDocument();
      expect(screen.queryByText('release-governance')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /New org persona/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^Delete$/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('combobox')).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: /^Edit$/i }));
      fireEvent.change(screen.getByLabelText(/Role/i), { target: { value: 'Platform engineer' } });
      fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
      await new Promise((r) => setTimeout(r, 0));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        name: 'kelvin',
        body: expect.objectContaining({ role: 'Platform engineer' }),
      });
      expect(update.mutateAsync.mock.calls[0][0].body).not.toHaveProperty('represented_user');
    });

    it('renders nothing for a member no persona represents', () => {
      const { container } = render(
        <OrgPersonasSection orgId="org_1" isAdmin={false} myUserId="u_tolu" members={members} />,
      );
      expect(container).toBeEmptyDOMElement();
    });
  });
});
