import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ProjectSettings from './ProjectSettings';
import { axe } from '../test/axe';

const { hooks, mockAddToast } = vi.hoisted(() => ({
  hooks: {
    useUpdateProjectSettings: vi.fn(),
    useUpdateProjectName: vi.fn(),
  },
  mockAddToast: vi.fn(),
}));

vi.mock('../hooks/useProjects', () => hooks);
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: mockAddToast }) }));
vi.mock('../transfers/useTransfers', () => ({
  useMyOrgs: () => ({ data: [], isLoading: false, error: null }),
}));
vi.mock('./ReposTab', () => ({
  default: ({ canAdmin }: { canAdmin?: boolean }) => <div>repos body (admin: {String(canAdmin)})</div>,
}));
vi.mock('./MergeSurface', () => ({ default: () => <div>merge body</div> }));
vi.mock('./TransferPanel', () => ({ default: () => <div>transfer body</div> }));
vi.mock('./ProjectGroupsBlock', () => ({
  default: ({ groups }: { groups?: { name: string }[] | null }) => (
    <div>groups block: {(groups ?? []).map((g) => g.name).join(', ')}</div>
  ),
}));

function makeMutation(extra: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
    ...extra,
  };
}

const PROJECT = {
  id: 'proj_abc',
  name: 'SessionFS',
  git_remote_normalized: 'sessionfs/sessionfs',
  context_document: '# Context',
  owner_id: 'user_1',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-04-10T12:00:00Z',
  auto_narrative: false,
  compile_schedule: 'nightly',
  compile_after_claims: null,
};

function renderSettings() {
  return render(<ProjectSettings project={PROJECT} onRequestDelete={() => {}} />);
}

describe('ProjectSettings scheduled compile', () => {
  beforeEach(() => {
    for (const h of Object.values(hooks)) h.mockReset();
    mockAddToast.mockReset();
    hooks.useUpdateProjectSettings.mockReturnValue(makeMutation());
    hooks.useUpdateProjectName.mockReturnValue(makeMutation());
  });

  it('changing the schedule calls the settings mutation preserving auto-narrative', async () => {
    const settings = makeMutation();
    hooks.useUpdateProjectSettings.mockReturnValue(settings);
    renderSettings();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Compile schedule' }));
    await user.click(screen.getByRole('option', { name: 'Weekly' }));

    await waitFor(() => {
      expect(settings.mutate).toHaveBeenCalledWith(
        { auto_narrative: false, compile_schedule: 'weekly' },
        expect.any(Object),
      );
    });
  });

  it('applying a pending-claim threshold calls the settings mutation', async () => {
    const settings = makeMutation();
    hooks.useUpdateProjectSettings.mockReturnValue(settings);
    renderSettings();
    const user = userEvent.setup();

    const input = screen.getByLabelText('Compile after N claims');
    await user.clear(input);
    await user.type(input, '20');
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(settings.mutate).toHaveBeenCalledWith(
        { auto_narrative: false, compile_after_claims: 20 },
        expect.any(Object),
      );
    });
  });

  it('ignores a non-positive threshold', async () => {
    const settings = makeMutation();
    hooks.useUpdateProjectSettings.mockReturnValue(settings);
    renderSettings();
    const user = userEvent.setup();

    const input = screen.getByLabelText('Compile after N claims');
    await user.clear(input);
    await user.type(input, '0');
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    expect(settings.mutate).not.toHaveBeenCalled();
  });

  it('clearing the threshold input sends an explicit null to clear it', async () => {
    const settings = makeMutation();
    hooks.useUpdateProjectSettings.mockReturnValue(settings);
    render(
      <ProjectSettings
        project={{ ...PROJECT, compile_after_claims: 5 }}
        onRequestDelete={() => {}}
      />,
    );
    const user = userEvent.setup();

    const input = screen.getByLabelText('Compile after N claims');
    await user.clear(input);
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(settings.mutate).toHaveBeenCalledWith(
        { auto_narrative: false, compile_after_claims: null },
        expect.any(Object),
      );
    });
  });
});

describe('ProjectSettings for a view-only reader', () => {
  beforeEach(() => {
    for (const h of Object.values(hooks)) h.mockReset();
    hooks.useUpdateProjectSettings.mockReturnValue(makeMutation());
    hooks.useUpdateProjectName.mockReturnValue(makeMutation());
  });

  it('explains why instead of showing controls when can_edit is false', () => {
    render(<ProjectSettings project={{ ...PROJECT, can_edit: false }} onRequestDelete={() => {}} />);
    expect(screen.getByText('You can view this project but not change it')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Rename' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Compile schedule' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete project' })).not.toBeInTheDocument();
  });

  it('read-only copy for an org project covers restricted visibility', () => {
    render(<ProjectSettings project={{ ...PROJECT, org_id: 'org_1', can_edit: false }} onRequestDelete={() => {}} />);
    expect(screen.getByText(/every org member by default/)).toBeInTheDocument();
    expect(screen.getByText(/need the project owner or an admin of its organization/)).toBeInTheDocument();
    expect(screen.getByText(/restricts project visibility/)).toBeInTheDocument();
    expect(screen.queryByText(/any org member/i)).not.toBeInTheDocument();
  });

  it('read-only copy for a personal project names only the owner', () => {
    render(<ProjectSettings project={{ ...PROJECT, org_id: null, can_edit: false }} onRequestDelete={() => {}} />);
    expect(screen.getByText(/Only the project owner can change its settings/)).toBeInTheDocument();
  });

  it('shows the groups block for editors and read-only viewers', () => {
    const groups = [{ id: 'pg_1', org_id: 'org_1', slug: 'p', name: 'Payments', description: null, member_count: 1, project_count: 1, my_role: 'reader' as const }];
    const { unmount } = render(<ProjectSettings project={{ ...PROJECT, groups }} onRequestDelete={() => {}} />);
    expect(screen.getByText('groups block: Payments')).toBeInTheDocument();
    unmount();
    render(<ProjectSettings project={{ ...PROJECT, groups, can_edit: false }} onRequestDelete={() => {}} />);
    expect(screen.getByText('groups block: Payments')).toBeInTheDocument();
  });

  it('hides rename, link, merge and transfer from editors without project-admin standing', () => {
    render(<ProjectSettings project={{ ...PROJECT, org_id: 'org_1', can_edit: true, can_admin: false }} onRequestDelete={() => {}} />);
    expect(screen.getByText('Some settings need project admin rights')).toBeInTheDocument();
    expect(screen.getByText(/need the project owner or an admin of its organization/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Rename' })).not.toBeInTheDocument();
    expect(screen.getByText('repos body (admin: false)')).toBeInTheDocument();
    expect(screen.queryByText('merge body')).not.toBeInTheDocument();
    expect(screen.queryByText('transfer body')).not.toBeInTheDocument();
    // Edit-tier controls stay.
    expect(screen.getByRole('button', { name: 'Compile schedule' })).toBeInTheDocument();
  });

  it('shows the admin controls with project-admin standing', () => {
    render(<ProjectSettings project={{ ...PROJECT, org_id: 'org_1', can_edit: true, can_admin: true }} onRequestDelete={() => {}} />);
    expect(screen.queryByText('Some settings need project admin rights')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Rename' })).toBeInTheDocument();
    expect(screen.getByText('repos body (admin: true)')).toBeInTheDocument();
    expect(screen.getByText('merge body')).toBeInTheDocument();
    expect(screen.getByText('transfer body')).toBeInTheDocument();
  });

  it('hides Transfer from an org project owner who is not an org admin', () => {
    render(
      <ProjectSettings
        project={{ ...PROJECT, org_id: 'org_1', can_edit: true, can_admin: true, can_transfer: false }}
        onRequestDelete={() => {}}
      />,
    );
    expect(screen.getByText('merge body')).toBeInTheDocument();
    expect(screen.queryByText('transfer body')).not.toBeInTheDocument();
  });

  it('shows Delete only when the caller may delete', () => {
    const { unmount } = render(
      <ProjectSettings project={{ ...PROJECT, org_id: 'org_1', can_edit: true, can_admin: true, can_delete: false }} onRequestDelete={() => {}} />,
    );
    // An org admin who is not the owner: no Danger zone.
    expect(screen.queryByRole('button', { name: 'Delete project' })).not.toBeInTheDocument();
    unmount();
    const onDelete = vi.fn();
    render(<ProjectSettings project={{ ...PROJECT, can_delete: true }} onRequestDelete={onDelete} />);
    screen.getByRole('button', { name: 'Delete project' }).click();
    expect(onDelete).toHaveBeenCalled();
  });

  it('keeps Delete for older servers without can_delete, and for a platform admin who cannot edit', () => {
    const { unmount } = render(<ProjectSettings project={PROJECT} onRequestDelete={() => {}} />);
    expect(screen.getByRole('button', { name: 'Delete project' })).toBeInTheDocument();
    unmount();
    render(<ProjectSettings project={{ ...PROJECT, can_edit: false, can_delete: true }} onRequestDelete={() => {}} />);
    expect(screen.getByRole('button', { name: 'Delete project' })).toBeInTheDocument();
  });

  it('keeps the controls when can_edit is absent (older server)', () => {
    renderSettings();
    expect(screen.getByRole('button', { name: 'Rename' })).toBeInTheDocument();
  });

  it('groups the settings into a few titled sections', () => {
    renderSettings();
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(['General', 'Knowledge', 'Repositories', 'Access & ownership', 'Danger zone']);
  });

  it('keeps Rename and Apply quiet until something changes', async () => {
    renderSettings();
    const user = userEvent.setup();
    const apply = screen.getByRole('button', { name: 'Apply' });
    expect(apply).toBeDisabled();
    expect(screen.getByLabelText('Compile after N claims')).toHaveAttribute('placeholder', 'No threshold');
    await user.type(screen.getByLabelText('Compile after N claims'), '5');
    expect(apply).toBeEnabled();

    const rename = screen.getByRole('button', { name: 'Rename' });
    expect(rename).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: 'Project name' }), ' 2');
    expect(rename).toBeEnabled();
  });

  it('has no axe violations', async () => {
    const { container } = renderSettings();
    expect(await axe(container)).toHaveNoViolations();
  });
});
