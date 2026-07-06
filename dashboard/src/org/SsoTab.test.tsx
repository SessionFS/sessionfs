/**
 * UI coverage for v0.13.x SsoTab — SSO (OIDC) admin configuration panel.
 * Hooks at ./useSso + ../hooks/useToast are mocked so tests stay focused
 * on the UI and canEdit/isOwner gates.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import SsoTab from './SsoTab';

const { hooks, toastHook, toastApi } = vi.hoisted(() => {
  const toastApi = { addToast: vi.fn(), removeToast: vi.fn(), toasts: [] };
  return {
    hooks: {
      useSsoProvider: vi.fn(),
      useCreateProvider: vi.fn(),
      useUpdateProvider: vi.fn(),
      useDeleteProvider: vi.fn(),
      useDomains: vi.fn(),
      useCreateDomain: vi.fn(),
      useVerifyDomain: vi.fn(),
      useDeleteDomain: vi.fn(),
      useToggleEnforcement: vi.fn(),
      useBreakGlassGrants: vi.fn(),
      useCreateBreakGlass: vi.fn(),
      useRevokeBreakGlass: vi.fn(),
    },
    toastHook: { useToast: vi.fn() },
    toastApi,
  };
});

vi.mock('./useSso', () => hooks);
vi.mock('../hooks/useToast', () => toastHook);

function makeMutation(extra: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    isPending: false,
    isError: false,
    error: null,
    variables: undefined as unknown,
    ...extra,
  };
}

function makeQuery<T>(data: T, extra: Record<string, unknown> = {}) {
  return {
    data,
    isLoading: false,
    isError: false,
    error: null,
    isSuccess: true,
    ...extra,
  };
}

const PROVIDER = {
  id: 'p1',
  org_id: 'org_1',
  protocol: 'oidc',
  display_name: 'Okta',
  issuer: 'https://okta.example.com',
  client_id: 'client-abc',
  client_secret_ref: 'env:OKTA_SECRET',
  allowed_scopes: ['openid', 'email', 'profile'],
  enabled: true,
  enforced: false,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
};

const DOMAINS = [
  { id: 'd1', domain: 'example.com', method: 'dns', verification_token: 'tok1', status: 'verified', txt_record: '_sessionfs-challenge.example.com TXT "abc"', verified_at: '2025-01-01T00:00:00Z' },
  { id: 'd2', domain: 'other.com', method: 'dns', verification_token: 'tok2', status: 'pending', txt_record: '_sessionfs-challenge.other.com TXT "xyz"', verified_at: null },
];

const GRANTS = [
  { id: 'g1', admin_user_id: 'user-1', expires_at: '2099-12-31T23:59:59Z', revoked_at: null, created_at: '2025-01-01T00:00:00Z' },
];

function resetAllMocks() {
  for (const h of Object.values(hooks)) h.mockReset();
  toastHook.useToast.mockReset();
  toastApi.addToast.mockReset();
}

function setupDefaultMocks() {
  hooks.useSsoProvider.mockReturnValue(makeQuery(null)); // no provider
  hooks.useCreateProvider.mockReturnValue(makeMutation());
  hooks.useUpdateProvider.mockReturnValue(makeMutation());
  hooks.useDeleteProvider.mockReturnValue(makeMutation());
  hooks.useDomains.mockReturnValue(makeQuery([]));
  hooks.useCreateDomain.mockReturnValue(makeMutation());
  hooks.useVerifyDomain.mockReturnValue(makeMutation());
  hooks.useDeleteDomain.mockReturnValue(makeMutation());
  hooks.useToggleEnforcement.mockReturnValue(makeMutation());
  hooks.useBreakGlassGrants.mockReturnValue(makeQuery([]));
  hooks.useCreateBreakGlass.mockReturnValue(makeMutation());
  hooks.useRevokeBreakGlass.mockReturnValue(makeMutation());
  toastHook.useToast.mockReturnValue(toastApi);
}

beforeEach(() => {
  resetAllMocks();
  setupDefaultMocks();
});

describe('SsoTab', () => {
  /* ── Provider: empty state ── */

  it('shows loading state while provider fetches', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(null, { isLoading: true, isSuccess: false }));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    expect(screen.getByText(/loading SSO settings/i)).toBeInTheDocument();
  });

  it('shows error state when provider fails', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(null, { isError: true, error: new Error('boom'), isSuccess: false }));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    expect(screen.getByRole('alert')).toHaveTextContent(/boom/i);
  });

  it('shows empty state when no provider (404 → null)', () => {
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    expect(screen.getByTestId('sso-empty-state')).toBeInTheDocument();
    expect(screen.getByText(/SSO not configured/i)).toBeInTheDocument();
  });

  it('clicking Configure opens the create-provider form', async () => {
    const user = userEvent.setup();
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    await user.click(screen.getByRole('button', { name: /configure OIDC provider/i }));
    // Form should be visible
    expect(screen.getByLabelText(/display name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/issuer url/i)).toBeInTheDocument();
  });

  /* ── Provider: configured state ── */

  it('renders configured provider fields', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useBreakGlassGrants.mockReturnValue(makeQuery(GRANTS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={true} />);
    expect(screen.getByTestId('provider-configured')).toBeInTheDocument();
    expect(screen.getByText('Okta')).toBeInTheDocument();
    expect(screen.getByText('https://okta.example.com')).toBeInTheDocument();
    expect(screen.getByText('client-abc')).toBeInTheDocument();
    expect(screen.getByText('env:OKTA_SECRET')).toBeInTheDocument();
    expect(screen.getByText('openid, email, profile')).toBeInTheDocument();
  });

  it('does NOT query break-glass for a non-owner (owner-gated)', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    // Hook must be invoked with enabled=false so it never fetches/caches grants.
    expect(hooks.useBreakGlassGrants).toHaveBeenCalledWith('org_x', false);
  });

  it('DOES query break-glass for an owner', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useBreakGlassGrants.mockReturnValue(makeQuery(GRANTS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={true} />);
    expect(hooks.useBreakGlassGrants).toHaveBeenCalledWith('org_x', true);
  });

  it('client_secret_ref is shown as a reference, not hidden', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    // The reference text should appear as-is in the UI
    expect(screen.getByText('env:OKTA_SECRET')).toBeInTheDocument();
    // Field label says REFERENCE
    expect(screen.getByText('Client secret REFERENCE')).toBeInTheDocument();
  });

  it('shows Edit and Delete buttons when configured', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    expect(screen.getByRole('button', { name: /edit provider/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /delete provider/i })).toBeInTheDocument();
  });

  it('opens confirm dialog on delete and confirms', async () => {
    const del = makeMutation();
    del.mutate.mockImplementation((_body: unknown, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.());
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useDeleteProvider.mockReturnValue(del);

    const user = userEvent.setup();
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    await user.click(screen.getByRole('button', { name: /delete provider/i }));
    // Confirm dialog should appear
    expect(screen.getByText(/delete OIDC provider/i)).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: /confirm/i })[0]);
    expect(del.mutate).toHaveBeenCalled();
    expect(toastApi.addToast).toHaveBeenCalledWith('success', expect.stringMatching(/removed/i));
  });

  /* ── Domains ── */

  it('shows domain list with status badges', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    expect(screen.getByText('example.com')).toBeInTheDocument();
    expect(screen.getByText('other.com')).toBeInTheDocument();
    expect(screen.getByText('verified')).toBeInTheDocument();
    expect(screen.getByText('pending')).toBeInTheDocument();
  });

  it('shows Verify button for pending domains', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    // Only the pending domain row has a Verify button
    expect(screen.getByRole('button', { name: /verify domain other.com/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /verify domain example.com/i })).toBeNull();
  });

  it('adding a domain shows txt_record callout', async () => {
    const createDom = makeMutation();
    createDom.mutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: (data: unknown) => void }) =>
        opts?.onSuccess?.({ domain: 'new.com', txt_record: 'TXT=record123' }),
    );
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useCreateDomain.mockReturnValue(createDom);

    const user = userEvent.setup();
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    // Type domain and submit
    const input = screen.getByRole('textbox', { name: /add domain/i });
    await user.type(input, 'new.com');
    await user.click(screen.getByRole('button', { name: /^add$/i }));
    expect(createDom.mutate).toHaveBeenCalledWith({ domain: 'new.com' }, expect.any(Object));
    // Wait for the callout
    await waitFor(() => {
      expect(screen.getByTestId('txt-record-callout')).toBeInTheDocument();
    });
    expect(screen.getByTestId('txt-record-value')).toHaveTextContent('TXT=record123');
  });

  it('verify domain calls the mutation', async () => {
    const verify = makeMutation();
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useVerifyDomain.mockReturnValue(verify);

    const user = userEvent.setup();
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    await user.click(screen.getByRole('button', { name: /verify domain other.com/i }));
    expect(verify.mutate).toHaveBeenCalledWith('d2', expect.any(Object));
  });

  /* ── Enforcement ── */

  it('shows enforcement hint when no verified domain', () => {
    const noVerifiedDomains = [DOMAINS[1]]; // only pending
    hooks.useSsoProvider.mockReturnValue(makeQuery({ ...PROVIDER, enabled: true, enforced: false }));
    hooks.useDomains.mockReturnValue(makeQuery(noVerifiedDomains));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    expect(screen.getByTestId('enforcement-hint')).toHaveTextContent(/requires an enabled provider/i);
  });

  it('enforcement toggle enabled when provider enabled + verified domain', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery({ ...PROVIDER, enabled: true, enforced: false }));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS)); // has verified
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    const toggle = screen.getByRole('switch', { name: /toggle SSO enforcement/i });
    expect(toggle).not.toBeDisabled();
  });

  it('enforcement stays DISABLE-able when it is ON but preconditions lapse (no lockout)', () => {
    // Enforcement on, but no verified domain anymore (canEnforce=false).
    hooks.useSsoProvider.mockReturnValue(makeQuery({ ...PROVIDER, enabled: true, enforced: true }));
    hooks.useDomains.mockReturnValue(makeQuery([DOMAINS[1]])); // only a PENDING domain
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    const toggle = screen.getByRole('switch', { name: /toggle SSO enforcement/i });
    expect(toggle).not.toBeDisabled(); // can still turn enforcement OFF
  });

  it('surfaces enforcement rejection error', () => {
    const toggle = makeMutation({ isError: true, error: new Error('{"error":{"message":"No verified domains"}}') });
    hooks.useSsoProvider.mockReturnValue(makeQuery({ ...PROVIDER, enabled: true, enforced: false }));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useToggleEnforcement.mockReturnValue(toggle);
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    expect(screen.getByRole('alert')).toHaveTextContent(/No verified domains/i);
  });

  /* ── Break-glass ── */

  it('hides break-glass section for non-owners', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    expect(screen.queryByRole('heading', { name: /break-glass grants/i })).toBeNull();
  });

  it('shows break-glass section for owners', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useBreakGlassGrants.mockReturnValue(makeQuery(GRANTS));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={true} />);
    expect(screen.getByRole('heading', { name: /break-glass grants/i })).toBeInTheDocument();
    expect(screen.getByText('user-1')).toBeInTheDocument();
  });

  it('can issue a break-glass grant', async () => {
    const createG = makeMutation();
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useBreakGlassGrants.mockReturnValue(makeQuery([]));
    hooks.useCreateBreakGlass.mockReturnValue(createG);

    const user = userEvent.setup();
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={true} />);
    const input = screen.getByLabelText(/admin user ID/i);
    await user.type(input, 'user-2');
    await user.click(screen.getByRole('button', { name: /issue grant/i }));
    expect(createG.mutate).toHaveBeenCalledWith({ admin_user_id: 'user-2' }, expect.any(Object));
  });

  it('can revoke an active grant', async () => {
    const revoke = makeMutation();
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useBreakGlassGrants.mockReturnValue(makeQuery(GRANTS));
    hooks.useRevokeBreakGlass.mockReturnValue(revoke);

    const user = userEvent.setup();
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={true} />);
    await user.click(screen.getByRole('button', { name: /revoke grant for user-1/i }));
    // Confirm
    await user.click(screen.getAllByRole('button', { name: /confirm/i })[0]);
    expect(revoke.mutate).toHaveBeenCalledWith('g1', expect.any(Object));
  });

  /* ── canEdit gate ── */

  it('disables mutations when canEdit is false', () => {
    hooks.useSsoProvider.mockReturnValue(makeQuery(PROVIDER));
    hooks.useDomains.mockReturnValue(makeQuery(DOMAINS));
    hooks.useBreakGlassGrants.mockReturnValue(makeQuery(GRANTS));
    render(<SsoTab orgId="org_x" canEdit={false} isOwner={false} />);
    expect(screen.getByRole('button', { name: /edit provider/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /delete provider/i })).toBeDisabled();
  });

  /* ── SECURITY: client_secret_ref help text ── */

  it('client_secret_ref help text visible in the create-provider form', async () => {
    const user = userEvent.setup();
    hooks.useSsoProvider.mockReturnValue(makeQuery(null));
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    await user.click(screen.getByRole('button', { name: /configure OIDC provider/i }));
    // Help text should be visible once the form is open
    expect(screen.getByTestId('secret-ref-hint')).toHaveTextContent(/NOT the secret value itself/i);
  });

  it('create provider form labels client_secret_ref as a REFERENCE', async () => {
    const user = userEvent.setup();
    render(<SsoTab orgId="org_x" canEdit={true} isOwner={false} />);
    await user.click(screen.getByRole('button', { name: /configure OIDC provider/i }));
    // The field label must say REFERENCE
    const field = screen.getByLabelText(/client secret REFERENCE/i);
    expect(field).toBeInTheDocument();
    // The hint must say NOT the secret value
    expect(screen.getByTestId('secret-ref-hint')).toHaveTextContent(/NOT the secret value itself/i);
  });
});
