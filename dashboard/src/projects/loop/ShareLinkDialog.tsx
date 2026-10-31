import { useId, useState } from 'react';
import { Alert, Badge, Button, Dialog, DialogFooter, DialogHeader, Input, Select } from '../../components/ui';
import type { CreatedShareLink, ShareLink, ShareTarget } from '../../api/reviewLoop';
import { useCreateShareLink, useRevokeShareLink, useShareLinks } from '../../hooks/useReviewLoop';
import { errorMessage } from '../../utils/errorMessage';

const EXPIRY_OPTIONS = [
  { value: '7', label: '7 days' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
];

function describe(link: ShareLink): string {
  if (link.kind === 'story') return `Story ${link.ticket_id ?? ''}`.trim();
  return link.release_version ? `Ship report ${link.release_version}` : `Ship report ${link.month ?? ''}`.trim();
}

function linkState(link: ShareLink, now = Date.now()): 'active' | 'expired' | 'revoked' {
  if (link.revoked_at) return 'revoked';
  if (link.expires_at && new Date(link.expires_at).getTime() < now) return 'expired';
  return 'active';
}

function day(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString('en', { month: 'short', day: 'numeric', year: 'numeric' }) : 'unknown';
}

function ConfirmRevoke({
  link,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  link: ShareLink;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const titleId = useId();
  return (
    <Dialog open onClose={() => !pending && onCancel()} titleId={titleId}>
      <DialogHeader titleId={titleId}>Revoke this link?</DialogHeader>
      <p className="text-sm text-text-secondary">
        {describe(link)} will stop working straight away for anyone who has the link. This cannot be undone.
      </p>
      {error && <Alert variant="danger" className="mt-3">{error}</Alert>}
      <DialogFooter>
        <Button variant="secondary" onClick={onCancel} disabled={pending}>Keep link</Button>
        <Button variant="danger" onClick={onConfirm} disabled={pending}>
          {pending ? 'Revoking…' : 'Revoke link'}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

/** Create a public, expiring link to a story or ship report, and manage the project's links. */
export function ShareLinkDialog({
  projectId,
  target,
  onClose,
}: {
  projectId: string;
  target: ShareTarget;
  onClose: () => void;
}) {
  const titleId = useId();
  const create = useCreateShareLink(projectId);
  const revoke = useRevokeShareLink(projectId);
  const links = useShareLinks(projectId);
  const [days, setDays] = useState('30');
  const [created, setCreated] = useState<CreatedShareLink | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ShareLink | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  async function makeLink() {
    if (create.isPending) return;
    setError(null);
    setNote(null);
    try {
      setCreated(await create.mutateAsync({ target, expiresDays: Number(days) }));
    } catch (err) {
      setError(errorMessage(err, 'Could not create the link'));
    }
  }

  async function copy() {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.url);
      setNote('Link copied.');
    } catch {
      setNote('Could not copy. Select the link and copy it by hand.');
    }
  }

  async function confirmRevoke() {
    if (!revoking || revoke.isPending) return;
    setRevokeError(null);
    try {
      await revoke.mutateAsync(revoking.id);
      setRevoking(null);
    } catch (err) {
      setRevokeError(errorMessage(err, 'Could not revoke the link'));
    }
  }

  const existing = (links.data ?? []).filter((l) => linkState(l) !== 'revoked');

  return (
    <>
      <Dialog open onClose={onClose} titleId={titleId} className="max-w-xl">
        <DialogHeader titleId={titleId}>Share</DialogHeader>
        <div className="space-y-4">
          <p className="text-sm text-text-secondary">
            This link shows no dollars and no emails. Anyone with it can view until it expires or you revoke it.
          </p>
          {!created ? (
            <div className="flex flex-wrap items-end gap-3">
              <Select
                title="Link expires after"
                value={days}
                onValueChange={setDays}
                options={EXPIRY_OPTIONS}
                className="w-40"
              />
              <Button onClick={() => void makeLink()} disabled={create.isPending}>
                {create.isPending ? 'Creating…' : 'Create link'}
              </Button>
            </div>
          ) : (
            <div className="space-y-2" data-testid="share-created">
              <Input title="Share link" readOnly value={created.url} onFocus={(e) => e.currentTarget.select()} />
              <div className="flex flex-wrap items-center gap-2">
                <Button variant="secondary" size="sm" onClick={() => void copy()}>Copy link</Button>
                <span role="status" className="text-xs text-text-tertiary">{note}</span>
              </div>
              <p className="text-xs text-text-tertiary">
                This is the only time the link is shown. It expires on {day(created.expires_at)}.
              </p>
            </div>
          )}
          {error && <Alert variant="danger">{error}</Alert>}
          <div className="space-y-1">
            <h3 className="text-md font-semibold text-text-primary">Links for this project</h3>
            {links.isLoading && <p className="text-sm text-text-tertiary">Loading links…</p>}
            {links.error && <Alert variant="danger">Could not load links: {errorMessage(links.error)}</Alert>}
            {!links.isLoading && !links.error && existing.length === 0 && (
              <p className="text-sm text-text-tertiary">No links yet.</p>
            )}
            {existing.length > 0 && (
              <ul className="divide-y divide-[var(--border)]" aria-label="Existing share links">
                {existing.map((l) => (
                  <li key={l.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <div className="min-w-0 text-sm">
                      <span className="text-text-primary">{describe(l)}</span>
                      <span className="ml-2 text-xs text-text-tertiary">
                        {linkState(l) === 'expired' ? `Expired ${day(l.expires_at)}` : `Expires ${day(l.expires_at)}`}
                      </span>
                      {linkState(l) === 'expired' && <span className="ml-2"><Badge label="Expired" /></span>}
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Revoke ${describe(l)}`}
                      onClick={() => { setRevokeError(null); setRevoking(l); }}
                    >
                      Revoke
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>Done</Button>
        </DialogFooter>
      </Dialog>
      {revoking && (
        <ConfirmRevoke
          link={revoking}
          pending={revoke.isPending}
          error={revokeError}
          onCancel={() => setRevoking(null)}
          onConfirm={() => void confirmRevoke()}
        />
      )}
    </>
  );
}
