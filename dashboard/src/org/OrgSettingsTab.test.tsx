/**
 * UI coverage for v0.10.0 Phase 6 OrgSettingsTab. Hooks at
 * `./useOrgSettings` + `../hooks/useToast` are mocked so tests stay
 * focused on the form UI and the canEdit gate.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import OrgSettingsTab from './OrgSettingsTab';

const { hooks, toastHook, toastApi, ssoHooks, digestHooks } = vi.hoisted(() => {
  const toastApi = { addToast: vi.fn(), removeToast: vi.fn(), toasts: [] };
  return {
    hooks: {
      useOrgSettings: vi.fn(),
      useUpdateOrgSettings: vi.fn(),
    },
    toastHook: { useToast: vi.fn() },
    toastApi,
    ssoHooks: { useDomains: vi.fn() },
    digestHooks: {
      useOrgDigestPrefs: vi.fn(),
      useUpdateOrgDigestPrefs: vi.fn(),
    },
  };
});

vi.mock('./useOrgSettings', () => hooks);
vi.mock('./useSso', () => ({ useDomains: ssoHooks.useDomains }));
vi.mock('./useOrgDigestPrefs', () => digestHooks);
vi.mock('../hooks/useToast', () => toastHook);

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

const DEFAULTS = {
  kb_retention_days: 90,
  kb_max_context_words: 4000,
  kb_section_page_limit: 50,
};

beforeEach(() => {
  for (const h of Object.values(hooks)) h.mockReset();
  toastHook.useToast.mockReset();
  toastApi.addToast.mockReset();
  ssoHooks.useDomains.mockReset();
  digestHooks.useOrgDigestPrefs.mockReset();
  digestHooks.useUpdateOrgDigestPrefs.mockReset();
  hooks.useOrgSettings.mockReturnValue({ data: DEFAULTS, isLoading: false, error: null });
  hooks.useUpdateOrgSettings.mockReturnValue(makeMutation());
  toastHook.useToast.mockReturnValue(toastApi);
  ssoHooks.useDomains.mockReturnValue({ data: [], isLoading: false, isSuccess: true, isError: false, error: null });
  digestHooks.useOrgDigestPrefs.mockReturnValue({
    data: { org_id: 'org_x', opt_out: false },
    isLoading: false,
    isError: false,
    error: null,
  });
  digestHooks.useUpdateOrgDigestPrefs.mockReturnValue(makeMutation());
});

describe('OrgSettingsTab', () => {
  it('shows loading state while settings fetch', () => {
    hooks.useOrgSettings.mockReturnValue({ data: undefined, isLoading: true, error: null });
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    expect(screen.getByText(/loading settings/i)).toBeInTheDocument();
  });

  it('shows error state when settings fail to load', () => {
    hooks.useOrgSettings.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('boom'),
    });
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    expect(screen.getByRole('alert')).toHaveTextContent(/boom/i);
  });

  it('renders the three kb_* fields populated from server state', async () => {
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    await waitFor(() => {
      expect(screen.getByLabelText(/^KB retention \(days\)$/)).toHaveValue(90);
    });
    expect(screen.getByLabelText(/^KB compile word budget$/)).toHaveValue(4000);
    expect(screen.getByLabelText(/^KB section page limit$/)).toHaveValue(50);
  });

  it('does NOT render the legacy retention_days or compile_model fields', () => {
    // Phase 6 Round 3 (KB entry 298) removed these inert fields. They
    // should not appear in the form until runtime consumers exist.
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    expect(screen.queryByLabelText(/^Retention \(days\)$/)).toBeNull();
    expect(screen.queryByLabelText(/^Compile model$/)).toBeNull();
  });

  it('admin submits PUT with parsed numeric values', async () => {
    const update = makeMutation();
    hooks.useUpdateOrgSettings.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();

    const kbRetention = await screen.findByLabelText(/^KB retention \(days\)$/);
    await user.clear(kbRetention);
    await user.type(kbRetention, '365');
    await user.click(screen.getByRole('button', { name: /save org settings/i }));

    expect(update.mutate).toHaveBeenCalledTimes(1);
    const body = update.mutate.mock.calls[0][0];
    expect(body.kb_retention_days).toBe(365);
    expect(body.kb_max_context_words).toBe(4000);
    expect(body.kb_section_page_limit).toBe(50);
  });

  it('empty fields submit as null (clears the override)', async () => {
    const update = makeMutation();
    hooks.useUpdateOrgSettings.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();

    await user.clear(await screen.findByLabelText(/^KB retention \(days\)$/));
    await user.clear(screen.getByLabelText(/^KB compile word budget$/));
    await user.click(screen.getByRole('button', { name: /save org settings/i }));

    const body = update.mutate.mock.calls[0][0];
    expect(body.kb_retention_days).toBeNull();
    expect(body.kb_max_context_words).toBeNull();
  });

  it('rejects non-integer numeric input with an error toast', async () => {
    const update = makeMutation();
    hooks.useUpdateOrgSettings.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();

    const field = await screen.findByLabelText(/^KB retention \(days\)$/);
    await user.clear(field);
    // Number inputs reject most non-numeric chars at the browser level
    // but jsdom is permissive; we feed an explicitly-non-integer value
    // and rely on the component's parseIntOrNull gate.
    await user.type(field, '12.5');
    await user.click(screen.getByRole('button', { name: /save org settings/i }));

    expect(update.mutate).not.toHaveBeenCalled();
    expect(toastApi.addToast).toHaveBeenCalledWith(
      'error',
      expect.stringMatching(/whole number or blank/i),
    );
  });

  it('disables every input + the save button when canEdit is false', async () => {
    render(<OrgSettingsTab orgId="org_x" canEdit={false} />);
    expect(await screen.findByLabelText(/^KB retention \(days\)$/)).toBeDisabled();
    expect(screen.getByLabelText(/^KB compile word budget$/)).toBeDisabled();
    const save = screen.getByRole('button', { name: /save org settings/i });
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute(
      'title',
      expect.stringMatching(/admins/i),
    );
  });

  it('surfaces a success toast on save', async () => {
    const update = makeMutation();
    update.mutate.mockImplementation((_body: unknown, opts: { onSuccess?: () => void }) =>
      opts?.onSuccess?.(),
    );
    hooks.useUpdateOrgSettings.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /save org settings/i }));
    expect(toastApi.addToast).toHaveBeenCalledWith(
      'success',
      expect.stringMatching(/saved/i),
    );
  });

  it('surfaces an error toast when save fails', async () => {
    const update = makeMutation();
    update.mutate.mockImplementation(
      (_body: unknown, opts: { onError?: (e: Error) => void }) =>
        opts?.onError?.(new Error('retention_days must be between 1 and 730')),
    );
    hooks.useUpdateOrgSettings.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /save org settings/i }));
    expect(toastApi.addToast).toHaveBeenCalledWith(
      'error',
      expect.stringMatching(/between 1 and 730/i),
    );
  });

  it('toggles a verified domain into auto_join_domains on submit', async () => {
    ssoHooks.useDomains.mockReturnValue({
      data: [{ id: 'odv_1', domain: 'corp.com', status: 'verified' }],
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    });
    const update = makeMutation();
    hooks.useUpdateOrgSettings.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();

    await user.click(await screen.findByLabelText(/^auto-join corp\.com$/i));
    await user.click(screen.getByRole('button', { name: /save org settings/i }));

    expect(update.mutate).toHaveBeenCalledTimes(1);
    const body = update.mutate.mock.calls[0][0];
    expect(body.auto_join_domains).toEqual(['corp.com']);
  });

  it('omits unverified domains from the auto-join control', async () => {
    ssoHooks.useDomains.mockReturnValue({
      data: [
        { id: 'odv_1', domain: 'corp.com', status: 'verified' },
        { id: 'odv_2', domain: 'pending.com', status: 'pending' },
      ],
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    });
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    expect(await screen.findByLabelText(/^auto-join corp\.com$/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^auto-join pending\.com$/i)).toBeNull();
  });

  it('does not resend unchanged auto-join domains when saving KB settings', async () => {
    hooks.useOrgSettings.mockReturnValue({
      data: { ...DEFAULTS, auto_join_domains: ['gone.com'] },
      isLoading: false,
      error: null,
    });
    const update = makeMutation();
    hooks.useUpdateOrgSettings.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();

    const kbRetention = await screen.findByLabelText(/^KB retention \(days\)$/);
    await user.clear(kbRetention);
    await user.type(kbRetention, '30');
    await user.click(screen.getByRole('button', { name: /save org settings/i }));

    const body = update.mutate.mock.calls[0][0];
    expect(body.kb_retention_days).toBe(30);
    expect('auto_join_domains' in body).toBe(false);
  });

  it('shows a configured domain that is no longer verified and lets it be removed', async () => {
    hooks.useOrgSettings.mockReturnValue({
      data: { ...DEFAULTS, auto_join_domains: ['gone.com'] },
      isLoading: false,
      error: null,
    });
    const update = makeMutation();
    hooks.useUpdateOrgSettings.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();

    expect(await screen.findByText(/gone\.com is no longer verified/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /remove gone\.com from auto-join/i }));
    await user.click(screen.getByRole('button', { name: /save org settings/i }));

    expect(update.mutate.mock.calls[0][0].auto_join_domains).toEqual([]);
  });

  it('does not flag configured domains as stale while domains load or fail', async () => {
    hooks.useOrgSettings.mockReturnValue({
      data: { ...DEFAULTS, auto_join_domains: ['corp.com'] },
      isLoading: false,
      error: null,
    });
    for (const state of [
      { data: undefined, isLoading: true, isSuccess: false, isError: false, error: null },
      { data: undefined, isLoading: false, isSuccess: false, isError: true, error: new Error('x') },
    ]) {
      ssoHooks.useDomains.mockReturnValue(state);
      const { unmount } = render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
      await screen.findByLabelText(/^KB retention \(days\)$/);
      expect(screen.queryByText(/no longer verified/i)).toBeNull();
      expect(screen.queryByRole('button', { name: /remove corp\.com/i })).toBeNull();
      unmount();
    }
  });

  it('members see configured domains read-only and never request the admin domain list', async () => {
    hooks.useOrgSettings.mockReturnValue({
      data: { ...DEFAULTS, auto_join_domains: ['corp.com'] },
      isLoading: false,
      error: null,
    });
    render(<OrgSettingsTab orgId="org_x" canEdit={false} />);
    expect(await screen.findByText('corp.com')).toBeInTheDocument();
    expect(screen.queryByText(/no longer verified/i)).toBeNull();
    expect(ssoHooks.useDomains).toHaveBeenCalledWith(undefined);
  });

  it('checks the weekly-digest box by default (opt_out false)', async () => {
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    expect(await screen.findByLabelText(/weekly activity digest/i)).toBeChecked();
  });

  it('unchecks the weekly-digest box when the admin has opted out', async () => {
    digestHooks.useOrgDigestPrefs.mockReturnValue({
      data: { org_id: 'org_x', opt_out: true },
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    expect(await screen.findByLabelText(/weekly activity digest/i)).not.toBeChecked();
  });

  it('toggling the digest box updates the opt-out preference', async () => {
    const update = makeMutation();
    digestHooks.useUpdateOrgDigestPrefs.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();

    // Starts checked (opt_out=false); unchecking opts out.
    await user.click(await screen.findByLabelText(/weekly activity digest/i));
    expect(update.mutate).toHaveBeenCalledTimes(1);
    expect(update.mutate.mock.calls[0][0]).toBe(true);
  });

  it('surfaces a success toast when the digest preference updates', async () => {
    const update = makeMutation();
    update.mutate.mockImplementation((_body: unknown, opts: { onSuccess?: () => void }) =>
      opts?.onSuccess?.(),
    );
    digestHooks.useUpdateOrgDigestPrefs.mockReturnValue(update);
    render(<OrgSettingsTab orgId="org_x" canEdit={true} />);
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText(/weekly activity digest/i));
    expect(toastApi.addToast).toHaveBeenCalledWith(
      'success',
      expect.stringMatching(/digest disabled/i),
    );
  });

  it('members see the digest box disabled and never request the pref', async () => {
    render(<OrgSettingsTab orgId="org_x" canEdit={false} />);
    expect(await screen.findByLabelText(/weekly activity digest/i)).toBeDisabled();
    expect(digestHooks.useOrgDigestPrefs).toHaveBeenCalledWith('org_x', false);
  });
});
