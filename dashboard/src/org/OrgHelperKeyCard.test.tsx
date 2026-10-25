/**
 * UI coverage for migration 084 OrgHelperKeyCard. Hooks at
 * `./useOrgLlmSettings` + `../hooks/useToast` are mocked so tests stay
 * focused on the form, the key-hint placeholder, and the delete gate.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import OrgHelperKeyCard from './OrgHelperKeyCard';

const { hooks, toastHook, toastApi } = vi.hoisted(() => {
  const toastApi = { addToast: vi.fn(), removeToast: vi.fn(), toasts: [] };
  return {
    hooks: {
      useOrgLlmSettings: vi.fn(),
      useSaveOrgLlmSettings: vi.fn(),
      useDeleteOrgLlmSettings: vi.fn(),
    },
    toastHook: { useToast: vi.fn() },
    toastApi,
  };
});

vi.mock('./useOrgLlmSettings', () => hooks);
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
  provider: 'anthropic',
  model: 'claude-sonnet-5-5',
  has_api_key: false,
  key_hint: null,
  base_url: null,
  enabled: true,
  plan_includes_helper: true,
};

beforeEach(() => {
  for (const h of Object.values(hooks)) h.mockReset();
  toastHook.useToast.mockReset();
  toastApi.addToast.mockReset();
  hooks.useOrgLlmSettings.mockReturnValue({ data: DEFAULTS, isLoading: false });
  hooks.useSaveOrgLlmSettings.mockReturnValue(makeMutation());
  hooks.useDeleteOrgLlmSettings.mockReturnValue(makeMutation());
  toastHook.useToast.mockReturnValue(toastApi);
});

describe('OrgHelperKeyCard', () => {
  it('shows loading state while settings fetch', () => {
    hooks.useOrgLlmSettings.mockReturnValue({ data: undefined, isLoading: true });
    render(<OrgHelperKeyCard orgId="org_x" />);
    expect(screen.getByText(/loading settings/i)).toBeInTheDocument();
  });

  it('renders provider, model, key, base URL, and enabled switch', () => {
    render(<OrgHelperKeyCard orgId="org_x" />);
    expect(screen.getByLabelText(/^Provider$/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Model$/)).toHaveValue('claude-sonnet-5-5');
    expect(screen.getByLabelText(/^API Key$/)).toHaveAttribute('placeholder', 'sk-…');
    expect(screen.getByLabelText(/^Base URL \(optional\)$/)).toBeInTheDocument();
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true');
  });

  it('shows the key-hint placeholder and delete button when a key is stored', () => {
    hooks.useOrgLlmSettings.mockReturnValue({
      data: { ...DEFAULTS, has_api_key: true, key_hint: '1234' },
      isLoading: false,
    });
    render(<OrgHelperKeyCard orgId="org_x" />);
    expect(screen.getByLabelText(/^API Key$/)).toHaveAttribute('placeholder', 'key ending in ••••1234');
    expect(screen.getByRole('button', { name: /delete key/i })).toBeInTheDocument();
  });

  it('hides the delete button when no key is stored', () => {
    render(<OrgHelperKeyCard orgId="org_x" />);
    expect(screen.queryByRole('button', { name: /delete key/i })).toBeNull();
  });

  it('submits the entered key with the other fields', async () => {
    const save = makeMutation();
    hooks.useSaveOrgLlmSettings.mockReturnValue(save);
    render(<OrgHelperKeyCard orgId="org_x" />);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^API Key$/), 'sk-org-secret');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(save.mutate).toHaveBeenCalledTimes(1);
    const body = save.mutate.mock.calls[0][0];
    expect(body.apiKey).toBe('sk-org-secret');
    expect(body.provider).toBe('anthropic');
    expect(body.model).toBe('claude-sonnet-5-5');
    expect(body.enabled).toBe(true);
  });

  it('omits the api key when the field is left blank (keeps the stored key)', async () => {
    hooks.useOrgLlmSettings.mockReturnValue({
      data: { ...DEFAULTS, has_api_key: true, key_hint: '1234' },
      isLoading: false,
    });
    const save = makeMutation();
    hooks.useSaveOrgLlmSettings.mockReturnValue(save);
    render(<OrgHelperKeyCard orgId="org_x" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(save.mutate).toHaveBeenCalledTimes(1);
    const body = save.mutate.mock.calls[0][0];
    expect(body.apiKey).toBeUndefined();
  });

  it('toggles the enabled switch into the payload', async () => {
    const save = makeMutation();
    hooks.useSaveOrgLlmSettings.mockReturnValue(save);
    render(<OrgHelperKeyCard orgId="org_x" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('switch'));
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(save.mutate).toHaveBeenCalledTimes(1);
    expect(save.mutate.mock.calls[0][0].enabled).toBe(false);
  });

  it('deletes the stored key', async () => {
    hooks.useOrgLlmSettings.mockReturnValue({
      data: { ...DEFAULTS, has_api_key: true, key_hint: '1234' },
      isLoading: false,
    });
    const remove = makeMutation();
    hooks.useDeleteOrgLlmSettings.mockReturnValue(remove);
    render(<OrgHelperKeyCard orgId="org_x" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /delete key/i }));
    expect(remove.mutate).toHaveBeenCalledTimes(1);
  });

  it('surfaces a success toast on save', async () => {
    const save = makeMutation();
    save.mutate.mockImplementation((_body: unknown, opts: { onSuccess?: () => void }) =>
      opts?.onSuccess?.(),
    );
    hooks.useSaveOrgLlmSettings.mockReturnValue(save);
    render(<OrgHelperKeyCard orgId="org_x" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^save$/i }));
    expect(toastApi.addToast).toHaveBeenCalledWith('success', expect.stringMatching(/saved/i));
  });

  it('shows a remove-or-disable state when the plan no longer includes the helper', () => {
    hooks.useOrgLlmSettings.mockReturnValue({
      data: { ...DEFAULTS, has_api_key: true, key_hint: '1234', plan_includes_helper: false },
      isLoading: false,
    });
    render(<OrgHelperKeyCard orgId="org_x" />);

    expect(screen.getByText(/plan no longer includes the session helper/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^disable$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /delete key/i })).toBeInTheDocument();
    // No editing surface in the downgraded state.
    expect(screen.queryByRole('button', { name: /^save$/i })).toBeNull();
    expect(screen.queryByLabelText(/^Provider$/)).toBeNull();
    expect(screen.queryByLabelText(/^Model$/)).toBeNull();
  });

  it('shows an upgrade notice when the plan lacks the helper and no key is stored', () => {
    hooks.useOrgLlmSettings.mockReturnValue({
      data: { ...DEFAULTS, plan_includes_helper: false },
      isLoading: false,
    });
    render(<OrgHelperKeyCard orgId="org_x" />);

    expect(screen.getByText(/requires an enterprise plan/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete key/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^save$/i })).toBeNull();
  });

  it('disables the stored key from the downgraded state', async () => {
    hooks.useOrgLlmSettings.mockReturnValue({
      data: {
        ...DEFAULTS,
        provider: 'anthropic',
        model: 'claude-sonnet-5-5',
        has_api_key: true,
        key_hint: '1234',
        plan_includes_helper: false,
      },
      isLoading: false,
    });
    const save = makeMutation();
    hooks.useSaveOrgLlmSettings.mockReturnValue(save);
    render(<OrgHelperKeyCard orgId="org_x" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^disable$/i }));

    expect(save.mutate).toHaveBeenCalledTimes(1);
    const body = save.mutate.mock.calls[0][0];
    expect(body.enabled).toBe(false);
    expect(body.apiKey).toBeUndefined();
    expect(body.provider).toBe('anthropic');
    expect(body.model).toBe('claude-sonnet-5-5');
  });
});
