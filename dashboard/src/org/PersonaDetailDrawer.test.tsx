import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from '../test/axe';
import type { Persona } from '../api/client';
import PersonaDetailDrawer from './PersonaDetailDrawer';

const { useOrgPersonaRollup, useOrgPersonaRepresentHistory } = vi.hoisted(() => ({
  useOrgPersonaRollup: vi.fn(),
  useOrgPersonaRepresentHistory: vi.fn(),
}));

vi.mock('../hooks/usePersonas', () => ({
  useOrgPersonaRollup,
  useOrgPersonaRepresentHistory,
}));
vi.mock('../hooks/useFocusTrap', () => ({
  useFocusTrap: vi.fn(),
}));

const persona: Persona = {
  id: 'p_org',
  project_id: null,
  org_id: 'org_1',
  scope: 'org',
  name: 'gcp-infra',
  role: 'Infra steward',
  content: '# GCP',
  specializations: [],
  is_active: true,
  version: 1,
  created_by: 'u_admin',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-02T00:00:00Z',
};

const members = [
  { user_id: 'u_kelvin', email: 'kelvin@example.com' },
  { user_id: 'u_tolu', email: 'tolu@example.com' },
];

const rollup = {
  persona,
  recent_sessions: [
    {
      id: 's1',
      title: 'Deploy pipeline',
      created_at: '2026-10-24T00:00:00Z',
      project_id: 'proj1',
      ticket_id: null,
    },
  ],
  open_tickets: [
    {
      id: 't1',
      title: 'Rotate GCP keys',
      status: 'open',
      project_id: 'proj1',
      created_at: '2026-10-23T00:00:00Z',
    },
  ],
  last_knowledge_entry: {
    id: 1,
    entry_type: 'discovery',
    content: 'GCP IAM policy for staging',
    project_id: 'proj1',
    created_at: '2026-10-22T00:00:00Z',
  },
  last_agent_run: {
    id: 'r1',
    status: 'passed',
    project_id: 'proj1',
    created_at: '2026-10-21T00:00:00Z',
  },
};

const history = [
  {
    actor_user_id: 'u_admin',
    actor_email: 'admin@example.com',
    actor_role: 'admin',
    from_user_id: null,
    to_user_id: 'u_kelvin',
    created_at: '2026-10-20T00:00:00Z',
  },
  {
    actor_user_id: 'u_admin',
    actor_email: 'admin@example.com',
    actor_role: 'admin',
    from_user_id: 'u_kelvin',
    to_user_id: 'u_tolu',
    created_at: '2026-10-19T00:00:00Z',
  },
];

beforeEach(() => {
  useOrgPersonaRollup.mockReturnValue({ data: rollup, isLoading: false, error: null });
  useOrgPersonaRepresentHistory.mockReturnValue({ data: history, isLoading: false, error: null });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('PersonaDetailDrawer', () => {
  it('renders the persona heading and every roll-up section', () => {
    render(
      <PersonaDetailDrawer persona={persona} orgId="org_1" members={members} onClose={vi.fn()} />,
    );
    expect(screen.getByRole('heading', { name: /gcp-infra/i })).toBeInTheDocument();
    expect(screen.getByLabelText('Recent sessions')).toHaveTextContent('Deploy pipeline');
    expect(screen.getByLabelText('Open tickets')).toHaveTextContent('Rotate GCP keys');
    expect(screen.getByLabelText('Latest knowledge entry')).toHaveTextContent('GCP IAM policy');
    expect(screen.getByLabelText('Latest agent run')).toHaveTextContent('passed');
  });

  it('renders empty states when a persona has no activity', () => {
    useOrgPersonaRollup.mockReturnValue({
      data: {
        persona,
        recent_sessions: [],
        open_tickets: [],
        last_knowledge_entry: null,
        last_agent_run: null,
      },
      isLoading: false,
      error: null,
    });
    render(<PersonaDetailDrawer persona={persona} orgId="org_1" onClose={vi.fn()} />);
    expect(screen.getAllByText('None').length).toBeGreaterThanOrEqual(4);
  });

  it('shows represent history for admins', () => {
    render(
      <PersonaDetailDrawer persona={persona} orgId="org_1" members={members} onClose={vi.fn()} />,
    );
    expect(screen.getByText(/Represent history/i)).toBeInTheDocument();
    expect(screen.getByText('Nobody → kelvin@example.com')).toBeInTheDocument();
    expect(screen.getByText('kelvin@example.com → tolu@example.com')).toBeInTheDocument();
    expect(useOrgPersonaRepresentHistory).toHaveBeenCalledWith('org_1', 'gcp-infra');
  });

  it('hides represent history for non-admin members', () => {
    render(
      <PersonaDetailDrawer
        persona={persona}
        orgId="org_1"
        isAdmin={false}
        members={members}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByText(/Represent history/i)).not.toBeInTheDocument();
    expect(useOrgPersonaRepresentHistory).not.toHaveBeenCalled();
  });

  it('surfaces a load error with an alert role', () => {
    useOrgPersonaRollup.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('boom'),
    });
    render(<PersonaDetailDrawer persona={persona} orgId="org_1" onClose={vi.fn()} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load roll-up');
  });

  it('has no axe violations', async () => {
    const { container } = render(
      <PersonaDetailDrawer persona={persona} orgId="org_1" members={members} onClose={vi.fn()} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
