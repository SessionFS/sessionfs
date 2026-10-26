import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ProjectSettings from './ProjectSettings';

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
vi.mock('./ReposTab', () => ({ default: () => <div>repos body</div> }));
vi.mock('./MergeSurface', () => ({ default: () => <div>merge body</div> }));
vi.mock('./TransferPanel', () => ({ default: () => <div>transfer body</div> }));

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

  it('keeps the controls when can_edit is absent (older server)', () => {
    renderSettings();
    expect(screen.getByRole('button', { name: 'Rename' })).toBeInTheDocument();
  });
});
