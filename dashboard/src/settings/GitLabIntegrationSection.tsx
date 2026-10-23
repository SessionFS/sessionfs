import { useState, type FormEvent } from 'react';
import { Card, Button, Input, Dialog, DialogHeader, DialogFooter, Alert, MonoText } from '../components/ui';
import { useGitLabSettings, useSaveGitLabSettings, useDisconnectGitLab } from '../hooks/useGitLabSettings';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';

const DEFAULT_INSTANCE = 'https://gitlab.com';

/**
 * Settings → Integrations → GitLab. The access token and webhook secret are
 * write-only: the form never pre-fills them and clears them after a save.
 */
export function GitLabIntegrationSection() {
  const { data, isLoading } = useGitLabSettings();
  const save = useSaveGitLabSettings();
  const disconnect = useDisconnectGitLab();
  const { addToast } = useToast();

  const [instanceUrl, setInstanceUrl] = useState<string | null>(null);
  const [token, setToken] = useState('');
  const [webhookSecret, setWebhookSecret] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const connected = !!data?.has_token;
  const urlValue = instanceUrl ?? (connected ? data!.instance_url : DEFAULT_INSTANCE);

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const body: { instance_url: string; access_token: string; webhook_secret?: string } = {
      instance_url: urlValue.trim(),
      access_token: token,
    };
    if (webhookSecret) body.webhook_secret = webhookSecret;
    try {
      await save.mutateAsync(body);
      setToken('');
      setWebhookSecret('');
      setInstanceUrl(null);
      addToast('success', 'GitLab settings saved');
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not save GitLab settings'));
    }
  }

  async function handleDisconnect() {
    try {
      await disconnect.mutateAsync();
      setConfirmOpen(false);
      setError(null);
      setToken('');
      setWebhookSecret('');
      setInstanceUrl(null);
      addToast('success', 'GitLab disconnected');
    } catch (err: unknown) {
      setConfirmOpen(false);
      setError(errorMessage(err, 'Could not disconnect GitLab'));
    }
  }

  return (
    <Card level="elevated" className="p-5">
      <h2 className="text-lg font-semibold text-text-primary mb-2">GitLab Integration</h2>
      <p className="text-sm text-text-tertiary mb-4">
        Post AI context comments on merge requests. Works with gitlab.com and self-managed instances.
      </p>

      {error && (
        <Alert variant="danger" className="mb-4">
          {error}
        </Alert>
      )}

      {isLoading ? (
        <div className="text-sm text-text-tertiary py-2">Loading…</div>
      ) : (
        <>
          <div className="flex items-center gap-2 mb-4 text-sm">
            <span
              className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-success' : 'bg-border'}`}
              aria-hidden="true"
            />
            {connected ? (
              <span className="text-text-primary flex items-center gap-1.5 min-w-0">
                Connected to <MonoText value={data!.instance_url} tailChars={10} />
              </span>
            ) : (
              <span className="text-text-tertiary">Not connected</span>
            )}
          </div>

          <form onSubmit={handleSave} className="space-y-3 max-w-md" autoComplete="off">
            <Input
              title="Instance URL"
              type="url"
              value={urlValue}
              onChange={(e) => setInstanceUrl(e.target.value)}
              placeholder={DEFAULT_INSTANCE}
              required
            />
            <Input
              title="Access token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={connected ? 'Enter a token to replace the stored one' : 'glpat-…'}
              autoComplete="new-password"
              required
            />
            <Input
              title="Webhook secret"
              type="password"
              value={webhookSecret}
              onChange={(e) => setWebhookSecret(e.target.value)}
              placeholder={connected ? 'Leave blank to keep the current secret' : 'Secret to set on the GitLab webhook'}
              autoComplete="new-password"
              required={!connected}
            />
            <p className="text-xs text-text-tertiary">
              The token needs the <code>api</code> scope. Stored secrets are never shown again; saving always
              asks for the token so it can be re-encrypted.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" disabled={save.isPending || !token}>
                {save.isPending ? 'Saving…' : connected ? 'Update GitLab' : 'Connect GitLab'}
              </Button>
              {connected && (
                <Button type="button" variant="ghost" onClick={() => setConfirmOpen(true)}>
                  Disconnect
                </Button>
              )}
            </div>
          </form>
        </>
      )}

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} titleId="gitlab-disconnect-heading">
        <DialogHeader titleId="gitlab-disconnect-heading">Disconnect GitLab?</DialogHeader>
        <p className="text-sm text-text-secondary">
          SessionFS will delete the stored access token and webhook secret and stop commenting on merge
          requests. Existing comments on GitLab are not removed.
        </p>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
            Cancel
          </Button>
          <Button variant="danger" disabled={disconnect.isPending} onClick={handleDisconnect}>
            {disconnect.isPending ? 'Disconnecting…' : 'Disconnect'}
          </Button>
        </DialogFooter>
      </Dialog>
    </Card>
  );
}
