import { useState, useCallback } from 'react';
import { useAuth } from '../auth/AuthContext';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Dialog } from '../components/ui/Dialog';

interface ShareLinkInfo {
  link_id: string;
  url: string;
  public_url: string;
  expires_at: string;
  has_password: boolean;
}

interface Props {
  sessionId: string;
  open: boolean;
  onClose: () => void;
}

export default function ShareDialog({ sessionId, open, onClose }: Props) {
  const { auth } = useAuth();
  const [state, setState] = useState<'idle' | 'creating' | 'created' | 'error'>('idle');
  const [link, setLink] = useState<ShareLinkInfo | null>(null);
  const [error, setError] = useState('');
  const [password, setPassword] = useState('');
  const [expiresInHours, setExpiresInHours] = useState(24);
  const [copied, setCopied] = useState(false);

  const handleCreate = useCallback(async () => {
    if (!auth) return;
    try {
      setState('creating');
      setError('');
      const result = await auth.client.createShareLink(sessionId, {
        expires_in_hours: expiresInHours,
        password: password || undefined,
      });
      setLink(result);
      setState('created');
    } catch (err: unknown) {
      const e = err as { status?: number; message?: string };
      setError(e.message || 'Failed to create share link');
      setState('error');
    }
  }, [auth, sessionId, expiresInHours, password]);

  const handleCopy = useCallback((text: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  }, []);

  const handleClose = useCallback(() => {
    setState('idle');
    setLink(null);
    setError('');
    setPassword('');
    setExpiresInHours(24);
    onClose();
  }, [onClose]);

  return (
    <Dialog open={open} onClose={handleClose} titleId="share-dialog-title">
      <h2 id="share-dialog-title" className="text-lg font-semibold text-text-primary mb-4">Share Session</h2>
      <div className="space-y-4">
        {state === 'idle' && (
          <>
            <p className="text-sm text-text-secondary">
              Create a public read-only link to this session. Anyone with the link can view the transcript.
            </p>
            <div className="space-y-3">
              <label className="block">
                <span className="text-sm text-text-secondary mb-1 block">Expires in (hours)</span>
                <Input
                  type="number"
                  value={String(expiresInHours)}
                  min={1}
                  max={720}
                  onChange={(e) => setExpiresInHours(Number(e.target.value) || 24)}
                />
              </label>
              <label className="block">
                <span className="text-sm text-text-secondary mb-1 block">
                  Password <span className="text-text-tertiary">(optional)</span>
                </span>
                <Input
                  type="password"
                  placeholder="Leave empty for public access"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </label>
            </div>
            <Button onClick={handleCreate} variant="primary" className="w-full">
              Create Share Link
            </Button>
          </>
        )}

        {state === 'creating' && (
          <p className="text-sm text-text-muted text-center py-4">Creating share link…</p>
        )}

        {state === 'created' && link && (
          <>
            <div className="bg-bg-sunken border border-border rounded-lg p-3 space-y-3">
              <div>
                <span className="text-xs text-text-tertiary block mb-1">Public URL</span>
                <div className="flex items-center gap-2">
                  <code className="flex-1 text-sm text-[var(--brand)] bg-bg-primary px-2 py-1 rounded border border-border break-all">
                    {link.public_url}
                  </code>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => handleCopy(link.public_url)}
                  >
                    {copied ? 'Copied!' : 'Copy'}
                  </Button>
                </div>
              </div>
              {link.has_password && (
                <p className="text-xs text-text-tertiary">
                  🔒 Password-protected — viewers must enter the password.
                </p>
              )}
              <p className="text-xs text-text-tertiary">
                Expires: {new Date(link.expires_at).toLocaleString()}
              </p>
            </div>
            <Button onClick={handleClose} variant="ghost" className="w-full">
              Done
            </Button>
          </>
        )}

        {state === 'error' && (
          <>
            <p className="text-sm text-[var(--danger)]">{error}</p>
            <div className="flex gap-2">
              <Button onClick={() => setState('idle')} variant="secondary" className="flex-1">
                Try Again
              </Button>
              <Button onClick={handleClose} variant="ghost" className="flex-1">
                Cancel
              </Button>
            </div>
          </>
        )}
      </div>
    </Dialog>
  );
}
