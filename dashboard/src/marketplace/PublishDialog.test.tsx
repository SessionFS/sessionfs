import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PublishDialog from './PublishDialog';
import { ApiError } from '../api/client';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useMe: vi.fn(),
    useMyOrgs: vi.fn(),
    publish: { mutateAsync: vi.fn(), isPending: false },
    addToast: vi.fn(),
  },
}));

vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../transfers/useTransfers', () => ({ useMyOrgs: hooks.useMyOrgs }));
vi.mock('../hooks/useMarketplace', () => ({ usePublishSkillPackage: () => hooks.publish }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));

const published = {
  listing_id: 'sml_9',
  collection: 'personal',
  visibility: 'user',
  verification_state: 'unverified',
  package_id: 'spk_9',
  namespace: 'personal',
  name: 'review-helper',
  display_name: 'Review Helper',
  description: '',
  version_id: 'skv_9',
  version: '1.0.0',
  content_hash: `sha256:${'b'.repeat(64)}`,
};

function archive(name = 'review-helper.tar.gz', size = 2048) {
  return new File([new Uint8Array(size)], name, { type: 'application/gzip' });
}

function renderDialog() {
  const onClose = vi.fn();
  render(<MemoryRouter><PublishDialog onClose={onClose} /></MemoryRouter>);
  return onClose;
}

function apiError(status: number, error: Record<string, unknown>) {
  return new ApiError(status, JSON.stringify({ error }));
}

async function chooseArchive(user: ReturnType<typeof userEvent.setup>, file = archive()) {
  await user.upload(screen.getByLabelText('Package archive (.tar.gz)'), file);
}

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'pro', effective_tier: 'pro' }, isLoading: false });
  hooks.useMyOrgs.mockReturnValue({ data: [], isLoading: false });
  hooks.publish.mutateAsync.mockResolvedValue(published);
});

describe('PublishDialog', () => {
  it('explains that folder packing is CLI-only and shows the command', () => {
    renderDialog();
    expect(screen.getByText(/Packing a folder is CLI-only/)).toBeInTheDocument();
    expect(screen.getByText('sfs skill publish <dir>')).toBeInTheDocument();
  });

  it('publishes to the personal library with server defaults when fields are blank', async () => {
    const user = userEvent.setup();
    renderDialog();
    expect(screen.getByLabelText('Namespace')).toHaveAttribute('placeholder', 'personal');
    expect(screen.getByLabelText('Collection')).toHaveAttribute('placeholder', 'personal');
    await chooseArchive(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(hooks.publish.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      target: { kind: 'personal' },
      filename: 'review-helper.tar.gz',
      namespace: undefined,
      collection: undefined,
    }));
    expect(await screen.findByText('Published Review Helper')).toBeInTheDocument();
    expect(screen.getByText(published.content_hash)).toBeInTheDocument();
  });

  it('lists only orgs the user owns or administers and defaults their fields to "org"', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'free', effective_tier: 'free' }, isLoading: false });
    hooks.useMyOrgs.mockReturnValue({
      data: [
        { org_id: 'org_owner', name: 'Owned Co', role: 'owner' },
        { org_id: 'org_admin', name: 'Admin Co', role: 'admin' },
        { org_id: 'org_member', name: 'Member Co', role: 'member' },
      ],
      isLoading: false,
    });
    renderDialog();
    expect(screen.getByText(/personal library needs the Starter plan/)).toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: /Publish to/ }));
    expect(screen.getByRole('option', { name: 'Organization: Owned Co' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Organization: Admin Co' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Organization: Member Co' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'My personal library' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: 'Organization: Admin Co' }));
    expect(screen.getByLabelText('Namespace')).toHaveAttribute('placeholder', 'org');
    expect(screen.queryByRole('combobox', { name: /Visibility/ })).not.toBeInTheDocument();
    await chooseArchive(user);
    await user.type(screen.getByLabelText('Namespace'), 'Acme.Tools');
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(hooks.publish.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      target: { kind: 'org', orgId: 'org_admin', visibility: 'org' },
      namespace: 'acme.tools',
    }));
  });

  it('lets a platform admin publish an org package publicly', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'admin', effective_tier: 'free' }, isLoading: false });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Platform', role: 'member' }], isLoading: false });
    renderDialog();
    await user.click(screen.getByRole('combobox', { name: /Publish to/ }));
    await user.click(screen.getByRole('option', { name: 'Organization: Platform' }));
    await user.click(screen.getByRole('combobox', { name: /Visibility/ }));
    await user.click(screen.getByRole('option', { name: /Public marketplace/ }));
    await chooseArchive(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(hooks.publish.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      target: { kind: 'org', orgId: 'org_1', visibility: 'public' },
    }));
  });

  it('shows an upgrade notice instead of the form when there is nowhere to publish', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'free', effective_tier: 'free' }, isLoading: false });
    renderDialog();
    expect(screen.getByText(/Publishing needs the Starter plan or above/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View plans and billing' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Publish' })).not.toBeInTheDocument();
  });

  it('refuses a non-archive locally without uploading', async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderDialog();
    await chooseArchive(user, archive('skill-folder.zip'));
    expect(screen.getByText('Choose a .tar.gz (or .tgz) archive.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(hooks.publish.mutateAsync).not.toHaveBeenCalled();
  });

  it('validates the namespace and collection locally', async () => {
    const user = userEvent.setup();
    renderDialog();
    await chooseArchive(user);
    await user.type(screen.getByLabelText('Collection'), '-bad-');
    expect(screen.getByText(/Collection must be 1-100 lowercase letters/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled();
  });

  it('renders a 422 package rejection from code + reason in plain language', async () => {
    const user = userEvent.setup();
    hooks.publish.mutateAsync.mockRejectedValue(apiError(422, {
      code: 'invalid_skill_package',
      message: 'Package content failed the DLP scan',
      details: { reason: 'dlp_findings' },
    }));
    renderDialog();
    await chooseArchive(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(await screen.findByText('The server rejected the package: Package content failed the DLP scan')).toBeInTheDocument();
    expect(screen.getByText(/Remove the secret or personal data/)).toBeInTheDocument();
    expect(screen.getByText('invalid_skill_package/dlp_findings')).toBeInTheDocument();
    expect(screen.queryByText(/\{"error"/)).not.toBeInTheDocument();
  });

  it('shows the upgrade message on 403 upgrade_required', async () => {
    const user = userEvent.setup();
    hooks.publish.mutateAsync.mockRejectedValue(apiError(403, {
      code: 'upgrade_required',
      message: 'Publishing personal skill packages requires the Starter tier or above.',
      details: { feature: 'marketplace_access', current_tier: 'free' },
    }));
    renderDialog();
    await chooseArchive(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(await screen.findByText('Personal publishing needs the Starter plan or above (you are on free).')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View plans and billing' })).toHaveAttribute('href', '/settings/billing');
  });

  it('explains a busy server and a visibility mismatch', async () => {
    const user = userEvent.setup();
    hooks.publish.mutateAsync.mockRejectedValueOnce(apiError(503, {
      code: 'publish_busy',
      message: 'Too many skill packages are being published right now; retry shortly.',
      details: { retry_after: 5 },
    }));
    renderDialog();
    await chooseArchive(user);
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(await screen.findByText(/The server is busy publishing other packages/)).toBeInTheDocument();
    expect(screen.getByText('Retry in 5 seconds: nothing was published.')).toBeInTheDocument();

    hooks.publish.mutateAsync.mockRejectedValueOnce(apiError(409, {
      code: 'visibility_mismatch',
      message: "This version is already listed in collection 'personal' with visibility 'org'.",
      details: { existing_visibility: 'org', requested_visibility: 'user' },
    }));
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(await screen.findByText(/already listed as 'org' in collection 'personal'/)).toBeInTheDocument();
  });
});
