import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import type { GitHubClaimFlow, GitHubInstallationResponse } from '../api/client';
import { Card, Button } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';

const GITHUB_LINK_CONFIRMED_MESSAGE = 'GitHub installation linked and ownership verified.';

const GITHUB_LINK_FAILED_MESSAGE =
  "Couldn't link the GitHub installation. Make sure you sign in to GitHub with an account that installed the app or administers the organization, then try again.";

const GITHUB_LOGIN_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/;

/**
 * GitHub's own settings page for an existing installation (repository
 * access, suspend, uninstall). Opening it never starts an install, so it
 * never lands on the state-bound install-return endpoint.
 */
function githubInstallationSettingsUrl(gh: GitHubInstallationResponse): string | null {
  const id = gh.installation_id;
  if (!id || !Number.isSafeInteger(id) || id <= 0) return null;
  if (gh.account_type === 'Organization') {
    if (!gh.account_login || !GITHUB_LOGIN_RE.test(gh.account_login)) return null;
    return `https://github.com/organizations/${gh.account_login}/settings/installations/${id}`;
  }
  return `https://github.com/settings/installations/${id}`;
}

function reverifyCopy(gh: GitHubInstallationResponse): string {
  const base = 'PR comments keep working, but repository verification is off.';
  if (gh.needs_reverification) {
    return `This installation was linked before SessionFS checked ownership with GitHub. ${base} Sign in to GitHub to confirm you control it.`;
  }
  if (gh.proof_stale) {
    return `Your GitHub ownership check is more than 7 days old. ${base} Sign in to GitHub again to restore it.`;
  }
  return `GitHub did not confirm that you own this account or administer the organization when you linked it. ${base} Sign in to GitHub again and SessionFS will recheck your owner/admin status (for organizations this needs the app's Members permission to be approved).`;
}

export function GitHubIntegrationSection() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  // The server's GitHub OAuth callback lands the browser back here with
  // ?github=connected, ?github=failed, or ?github=choose (several
  // installations are linkable; the user picks exactly one). The old
  // ?installation_id= auto-claim is gone: a bare id proves nothing.
  const [returnOutcome] = useState(() =>
    searchParams.get('github'),
  );
  const [claimError, setClaimError] = useState<string | null>(() =>
    returnOutcome === 'failed' ? GITHUB_LINK_FAILED_MESSAGE : null,
  );
  // ?github=connected is only a hint to refetch: anyone can type it. The
  // success notice is shown only once the authenticated settings response
  // confirms a verified link (see confirmedByServer below).
  const [claimNotice, setClaimNotice] = useState<string | null>(null);
  const [choosing, setChoosing] = useState(() => returnOutcome === 'choose');
  const [starting, setStarting] = useState(false);

  const { data: choiceData, isLoading: choicesLoading } = useQuery({
    queryKey: ['github-claim-choices'],
    queryFn: () => auth!.client.getGitHubClaimChoices(),
    enabled: !!auth && choosing,
  });

  const chooseInstallation = useMutation({
    mutationFn: (installationId: number) => auth!.client.chooseGitHubInstallation(installationId),
    onSuccess: () => {
      setChoosing(false);
      setClaimError(null);
      setClaimNotice(GITHUB_LINK_CONFIRMED_MESSAGE);
      queryClient.invalidateQueries({ queryKey: ['github-installation'] });
    },
    onError: () => {
      setChoosing(false);
      setClaimError(GITHUB_LINK_FAILED_MESSAGE);
    },
  });

  const { data: ghSettings, isLoading } = useQuery({
    queryKey: ['github-installation'],
    queryFn: () => auth!.client.getGitHubInstallation(),
    enabled: !!auth,
  });

  const updateSettings = useMutation({
    mutationFn: (updates: { auto_comment?: boolean; include_trust_score?: boolean; include_session_links?: boolean }) =>
      auth!.client.updateGitHubInstallation(updates),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['github-installation'] }),
  });

  // Clean the URL so a refresh does not repeat the message.
  useEffect(() => {
    if (!returnOutcome) return;
    if (returnOutcome === 'connected') {
      queryClient.invalidateQueries({ queryKey: ['github-installation'] });
    }
    // Only the GitHub return params are dropped; anything else is kept.
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('github');
        // Stale params from GitHub's old Setup URL redirect.
        next.delete('installation_id');
        next.delete('setup_action');
        next.delete('installation_target');
        return next;
      },
      { replace: true },
    );
  }, [returnOutcome, queryClient, setSearchParams]);

  async function startClaim(flow: GitHubClaimFlow, installationId?: number) {
    if (!auth) return;
    setStarting(true);
    setClaimError(null);
    try {
      const redirectUrl =
        installationId === undefined
          ? await auth.client.startGitHubClaim(flow)
          : await auth.client.startGitHubClaim(flow, installationId);
      window.location.assign(redirectUrl);
    } catch (err: unknown) {
      const msg = errorMessage(err);
      setClaimError(`Couldn't start GitHub sign-in: ${msg}`);
      setStarting(false);
    }
  }

  const connected = ghSettings && ghSettings.account_login;
  const confirmedByServer =
    returnOutcome === 'connected' && !!ghSettings?.account_login && !!ghSettings?.ownership_verified;
  const notice = claimNotice ?? (confirmedByServer ? GITHUB_LINK_CONFIRMED_MESSAGE : null);
  const claimAvailable = !!ghSettings?.claim_available;
  const installAvailable = !!ghSettings?.install_available;

  return (
    <Card level="elevated" className="p-5">
      <h2 className="text-lg font-semibold text-text-primary mb-2">GitHub Integration</h2>
      <p className="text-sm text-text-tertiary mb-4">
        Automatically post AI context comments on pull requests showing which sessions contributed to the code.
      </p>

      {claimError && (
        <div role="alert" className="text-sm text-danger mb-3 bg-danger/10 border border-danger/30 rounded-md px-3 py-2">
          {claimError}
        </div>
      )}
      {notice && !claimError && (
        <div role="status" className="text-sm text-success mb-3 bg-success/10 border border-success/30 rounded-md px-3 py-2">
          {notice}
        </div>
      )}

      {choosing && (
        <div className="text-sm mb-4 border border-border rounded-md px-3 py-3">
          <p className="text-text-secondary mb-2">
            Your GitHub account can link more than one installation. Choose the one to link to SessionFS:
          </p>
          {choicesLoading ? (
            <p className="text-text-tertiary">Loading…</p>
          ) : choiceData && choiceData.choices.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {choiceData.choices.map((choice) => (
                <Button
                  key={choice.installation_id}
                  size="sm"
                  variant="secondary"
                  disabled={chooseInstallation.isPending}
                  onClick={() => chooseInstallation.mutate(choice.installation_id)}
                >
                  {choice.account_login ?? `Installation ${choice.installation_id}`}
                  {choice.account_type ? ` (${choice.account_type})` : ''}
                </Button>
              ))}
            </div>
          ) : (
            <p className="text-text-tertiary">This choice has expired. Start again from Connect GitHub.</p>
          )}
        </div>
      )}

      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : connected ? (
        <>
          <div className="flex items-center gap-2 mb-4">
            <span className="inline-block w-2 h-2 rounded-full bg-success" />
            <span className="text-sm text-text-primary">
              Connected to <span className="font-medium">{ghSettings.account_login}</span>
              <span className="text-text-tertiary ml-1">({ghSettings.account_type})</span>
            </span>
            {ghSettings.ownership_verified && ghSettings.claim_verified_github_login && (
              <span className="text-xs text-text-tertiary">
                verified as @{ghSettings.claim_verified_github_login}
              </span>
            )}
          </div>

          {!ghSettings.repo_verification_active && (
            <div className="text-sm mb-4 bg-warning/10 border border-warning/30 rounded-md px-3 py-2 text-text-secondary">
              <p className="mb-2">{reverifyCopy(ghSettings)}</p>
              {claimAvailable ? (
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={starting}
                  onClick={() => startClaim('authorize', ghSettings.installation_id ?? undefined)}
                >
                  {ghSettings.needs_reverification ? 'Verify ownership with GitHub' : 'Re-verify with GitHub'}
                </Button>
              ) : (
                <p className="text-text-tertiary">GitHub sign-in is not configured on this server.</p>
              )}
            </div>
          )}

          <div className="space-y-2 mb-4">
            <label className="flex items-center gap-2.5 text-sm text-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.auto_comment ?? true}
                onChange={(e) => updateSettings.mutate({ auto_comment: e.target.checked })}
                className="rounded border-border bg-surface text-brand focus-visible:ring-[var(--brand)]"
              />
              Auto-comment on PRs
            </label>
            <label className="flex items-center gap-2.5 text-sm text-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.include_trust_score ?? true}
                onChange={(e) => updateSettings.mutate({ include_trust_score: e.target.checked })}
                className="rounded border-border bg-surface text-brand focus-visible:ring-[var(--brand)]"
              />
              Include trust scores
            </label>
            <label className="flex items-center gap-2.5 text-sm text-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.include_session_links ?? true}
                onChange={(e) => updateSettings.mutate({ include_session_links: e.target.checked })}
                className="rounded border-border bg-surface text-brand focus-visible:ring-[var(--brand)]"
              />
              Include session links
            </label>
          </div>

          {updateSettings.isError && (
            <div className="text-sm text-danger mb-2">Failed to update settings.</div>
          )}

          <div className="flex flex-wrap items-center gap-4">
            {githubInstallationSettingsUrl(ghSettings) && (
              <a
                href={githubInstallationSettingsUrl(ghSettings)!}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-brand hover:underline"
              >
                Configure on GitHub
              </a>
            )}
            {installAvailable && (
              <Button size="sm" variant="ghost" disabled={starting} onClick={() => startClaim('install')}>
                Add installation
              </Button>
            )}
          </div>
          {installAvailable && (
            <p className="text-xs text-text-tertiary mt-2">
              SessionFS links one installation per account. Adding another replaces{' '}
              {ghSettings.account_login}: it is unlinked and its PR comments stop.
            </p>
          )}
        </>
      ) : (
        <div>
          <p className="text-sm text-text-tertiary mb-3">Not connected</p>
          {claimAvailable ? (
            <div className="flex flex-wrap items-center gap-3">
              {installAvailable && (
                <Button disabled={starting} onClick={() => startClaim('install')}>
                  Connect GitHub
                </Button>
              )}
              <Button
                variant={installAvailable ? 'secondary' : 'primary'}
                disabled={starting}
                onClick={() => startClaim('authorize')}
              >
                {installAvailable ? 'Link existing installation' : 'Connect GitHub'}
              </Button>
            </div>
          ) : (
            <p className="text-sm text-text-tertiary">
              GitHub linking is not configured on this server. Ask your administrator to set up the GitHub App OAuth
              credentials.
            </p>
          )}
          <p className="text-xs text-text-tertiary mt-3">
            You will sign in to GitHub so SessionFS can confirm you installed the app or administer the organization.
          </p>
        </div>
      )}
    </Card>
  );
}
