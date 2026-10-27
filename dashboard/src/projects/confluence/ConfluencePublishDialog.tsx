import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type {
  ConfluenceDriftDetails,
  ConfluenceLink,
  ConfluencePreview,
  ConfluenceSection,
} from '../../api/client';
import { Alert, Badge, Button, Dialog, DialogFooter, DialogHeader, Input, Textarea } from '../../components/ui';
import {
  useConfluencePreview,
  usePublishConfluence,
  useResolveConfluenceLink,
  useUnlinkConfluence,
} from '../../hooks/useConfluencePublish';
import { useToast } from '../../hooks/useToast';
import {
  confluenceDriftDetails,
  confluenceFriendlyError,
  confluenceLinkActionError,
} from '../../utils/confluence';
import { safeHttpUrl } from '../../utils/url';

/* ── Project Confluence publish surface (Enterprise) ──
 *
 * One dialog serves both source kinds: a wiki page (`slug` set) and the
 * compiled context document (`slug` undefined). The button appears for any
 * org member on an Enterprise plan; connection/mapping state is learned from
 * the preview POST's error codes inside the dialog (the org GETs are
 * admin-only). The preview also carries the source's page link in any
 * status, so a drifted, unconfirmed or stale link can be resolved here. */

const MAX_TITLE = 255;

interface ConfluencePublishDialogProps {
  projectId: string;
  kind: 'wiki_page' | 'context';
  slug?: string;
  /** Project-admin standing (`can_admin`): only admins may resolve or unlink. */
  canAdmin?: boolean;
  onClose: () => void;
}

export function ConfluencePublishDialog({
  projectId,
  kind,
  slug,
  canAdmin = false,
  onClose,
}: ConfluencePublishDialogProps) {
  const queryClient = useQueryClient();
  const preview = useConfluencePreview(projectId, kind, slug, true);

  function handleClose() {
    // Drop the cached draft so the next open rebuilds it from the live page
    // rather than seeding the form with a now-stale preview.
    queryClient.removeQueries({ queryKey: ['confluencePreview', projectId, kind, slug ?? null] });
    onClose();
  }

  return (
    <Dialog open onClose={handleClose} titleId="confluence-publish-heading" className="max-w-3xl">
      <DialogHeader titleId="confluence-publish-heading">
        {preview.data?.existing ? 'Update Confluence page' : 'Publish to Confluence'}
      </DialogHeader>

      {preview.isLoading && <p className="text-sm text-text-tertiary">Building draft…</p>}
      {preview.isError && <Alert variant="danger">{confluenceFriendlyError(preview.error)}</Alert>}

      {preview.data && (
        <ConfluenceDraftForm
          // A fresh preview (after a link is resolved) reseeds the draft.
          key={preview.dataUpdatedAt}
          projectId={projectId}
          kind={kind}
          slug={slug}
          preview={preview.data}
          isFetching={preview.isFetching}
          canAdmin={canAdmin}
          onClose={handleClose}
        />
      )}
    </Dialog>
  );
}

const STATUS_BADGE: Record<
  ConfluenceLink['status'] | 'stale',
  { label: string; variant: 'default' | 'success' | 'warning' | 'danger' | 'info' }
> = {
  linked: { label: 'Linked', variant: 'success' },
  drifted: { label: 'Edited in Confluence', variant: 'warning' },
  unknown: { label: 'Unconfirmed', variant: 'warning' },
  unlinked: { label: 'Unlinked', variant: 'default' },
  failed: { label: 'Last publish refused', variant: 'danger' },
  stale: { label: 'Stale link', variant: 'danger' },
};

const STATUS_TEXT: Record<ConfluenceLink['status'] | 'stale', string> = {
  linked: 'This source is linked to a Confluence page; publishing updates it in place.',
  drifted:
    'The Confluence page was edited since the last publish. Overwrite it with this draft, or stop syncing to leave it as it is.',
  unknown:
    'The outcome of an earlier publish is unconfirmed; publishing will adopt the page if it was created.',
  unlinked: 'This source is no longer linked; publishing creates a new Confluence page.',
  failed:
    'Confluence refused the last publish (for example a page with this title already exists). Publishing tries to create the page again.',
  stale:
    'This link belongs to another Confluence site or organization. Publishing is blocked until it is unlinked.',
};

function ConfluenceLinkPanel({
  projectId,
  link,
  drift,
  canAdmin,
  onResolved,
}: {
  projectId: string;
  link: ConfluenceLink | null;
  drift: ConfluenceDriftDetails | null;
  canAdmin: boolean;
  onResolved: () => void;
}) {
  const resolve = useResolveConfluenceLink(projectId);
  const unlink = useUnlinkConfluence(projectId);
  const { addToast } = useToast();
  const [actionError, setActionError] = useState<string | null>(null);

  const linkId = drift?.link_id ?? link?.id;
  const state: ConfluenceLink['status'] | 'stale' = link?.stale
    ? 'stale'
    : drift
      ? 'drifted'
      : (link?.status ?? 'drifted');
  const badge = STATUS_BADGE[state];
  const pageHref = link?.page_url ? safeHttpUrl(link.page_url) : null;
  const publishing = !!link?.publishing;
  const busy = resolve.isPending || unlink.isPending;
  const canAct = !!linkId && !publishing && !busy;

  async function runResolve(action: 'overwrite' | 'unlink') {
    if (!linkId) return;
    setActionError(null);
    try {
      await resolve.mutateAsync({ linkId, action });
      addToast(
        'success',
        action === 'overwrite'
          ? 'Conflict resolved; publish again to overwrite the Confluence page.'
          : 'Stopped syncing this page with Confluence.',
      );
      onResolved();
    } catch (err: unknown) {
      setActionError(confluenceLinkActionError(err));
    }
  }

  async function runUnlink() {
    if (!linkId) return;
    setActionError(null);
    try {
      await unlink.mutateAsync(linkId);
      addToast('success', 'Unlinked from Confluence.');
      onResolved();
    } catch (err: unknown) {
      setActionError(confluenceLinkActionError(err));
    }
  }

  return (
    <section
      aria-labelledby="confluence-link-heading"
      className="rounded-lg border border-border bg-bg-secondary p-3 space-y-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="confluence-link-heading" className="text-sm font-semibold text-text-primary">
          Confluence page
        </h3>
        <Badge variant={badge.variant} label={badge.label} />
        {publishing && <Badge variant="info" label="Publishing" />}
        {pageHref && (
          <a
            href={pageHref}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-brand underline hover:no-underline ml-auto"
          >
            Open the page
          </a>
        )}
      </div>
      <p className="text-xs text-text-secondary">{STATUS_TEXT[state]}</p>

      {state === 'drifted' && drift && (drift.diff?.length ?? 0) > 0 && (
        <div>
          <p className="text-xs text-text-tertiary mb-1">
            Changed in Confluence since the last publish
            {drift.stored_version != null && drift.remote_version != null
              ? ` (version ${drift.stored_version} → ${drift.remote_version})`
              : ''}
            :
          </p>
          <pre
            aria-label="Changes made in Confluence"
            tabIndex={0}
            className="max-h-64 overflow-auto rounded-md border border-border bg-bg-sunken p-2 text-xs font-mono leading-snug"
          >
            {drift.diff!.map((line, i) => (
              <span
                key={i}
                className={`block whitespace-pre ${
                  line.startsWith('+') && !line.startsWith('+++')
                    ? 'text-success'
                    : line.startsWith('-') && !line.startsWith('---')
                      ? 'text-danger'
                      : 'text-text-secondary'
                }`}
              >
                {line || ' '}
              </span>
            ))}
          </pre>
          {drift.diff_truncated && (
            <p className="text-xs text-text-tertiary mt-1">The diff was truncated; open the page to see every change.</p>
          )}
        </div>
      )}

      {publishing && (
        <p className="text-xs text-text-tertiary">A publish is in progress; link actions are unavailable until it finishes.</p>
      )}

      {linkId && !canAdmin && (state === 'drifted' || state === 'stale') && (
        <p className="text-xs text-text-tertiary">Ask a project admin to resolve or unlink this page.</p>
      )}

      {linkId && canAdmin && (
        <div className="flex flex-wrap gap-2">
          {state === 'drifted' && (
            <>
              <Button
                size="sm"
                variant="secondary"
                disabled={!canAct}
                loading={resolve.isPending && resolve.variables?.action === 'overwrite'}
                onClick={() => runResolve('overwrite')}
              >
                Overwrite Confluence
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={!canAct}
                loading={resolve.isPending && resolve.variables?.action === 'unlink'}
                onClick={() => runResolve('unlink')}
              >
                Stop syncing
              </Button>
            </>
          )}
          {(state === 'linked' || state === 'stale') && (
            <Button size="sm" variant="ghost" disabled={!canAct} loading={unlink.isPending} onClick={runUnlink}>
              Unlink
            </Button>
          )}
        </div>
      )}

      {actionError && <Alert variant="danger">{actionError}</Alert>}
    </section>
  );
}

function ConfluenceDraftForm({
  projectId,
  kind,
  slug,
  preview,
  isFetching,
  canAdmin,
  onClose,
}: {
  projectId: string;
  kind: 'wiki_page' | 'context';
  slug?: string;
  preview: ConfluencePreview;
  isFetching?: boolean;
  canAdmin: boolean;
  onClose: () => void;
}) {
  const publish = usePublishConfluence(projectId);
  const { addToast } = useToast();
  // The form is keyed on the preview's fetch time, so `preview` is always
  // available on first render and the editable draft seeds directly from it.
  const [title, setTitle] = useState(preview.title);
  const [sections, setSections] = useState<ConfluenceSection[]>(preview.sections);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [drift, setDrift] = useState<ConfluenceDriftDetails | null>(null);

  const link = preview.link ?? null;
  const notes = preview.notes ?? [];
  const dropped = preview.dropped ?? [];
  const stale = !!link?.stale;
  const headingFor = (key: string) => preview.sections.find((s) => s.key === key)?.heading || key;
  const showLinkPanel = !!link || !!drift;

  function updateParagraph(sectionKey: string, index: number, text: string) {
    setSections((prev) =>
      prev.map((s) =>
        s.key === sectionKey
          ? {
              ...s,
              paragraphs: s.paragraphs.map((p, i) => (i === index ? { ...p, text } : p)),
            }
          : s,
      ),
    );
  }

  async function handlePublish() {
    setPublishError(null);
    setDrift(null);
    try {
      const result = await publish.mutateAsync({
        kind,
        slug,
        title,
        // The server resolves `mode` from the source's template by section key
        // and rejects unknown keys, so send only the editable shape.
        sections: sections.map((s) => ({
          key: s.key,
          heading: s.heading,
          paragraphs: s.paragraphs.map((p) => ({ text: p.text, citations: p.citations })),
        })),
        confirm: true,
        // Echo the template the draft was built from; the server refuses the
        // publish (409 template_changed) when it has since changed.
        template: preview.template.slug,
        template_version: preview.template.version ?? null,
      });
      addToast('success', 'Published to Confluence');
      // Surface the fresh page in a new tab when it came back as a URL.
      const url = safeHttpUrl(result.page_url);
      if (url) window.open(url, '_blank', 'noopener,noreferrer');
      onClose();
    } catch (err: unknown) {
      const driftDetails = confluenceDriftDetails(err);
      if (driftDetails) setDrift(driftDetails);
      else setPublishError(confluenceFriendlyError(err));
    }
  }

  return (
    <>
      <div className="space-y-4 max-h-[70vh] overflow-y-auto pr-1">
        {showLinkPanel && (
          <ConfluenceLinkPanel
            projectId={projectId}
            link={link}
            drift={drift}
            canAdmin={canAdmin}
            onResolved={() => setDrift(null)}
          />
        )}

        <p className="text-xs text-text-tertiary">
          {preview.llm_used
            ? `Rewrite sections drafted by your model (${preview.key_source}).`
            : 'Built from the template only (no model rewrite).'}
        </p>

        {notes.length > 0 && (
          <Alert variant="info" announce={false}>
            <ul className="space-y-0.5">
              {notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </Alert>
        )}

        {dropped.length > 0 && (
          <Alert
            variant="warning"
            announce={false}
            title={`${dropped.length} drafted ${dropped.length === 1 ? 'paragraph was' : 'paragraphs were'} dropped`}
          >
            <p className="mb-1">Paragraphs without a valid citation are left out of the page.</p>
            <ul className="space-y-1.5">
              {dropped.map((d, i) => (
                <li key={i}>
                  <span className="font-medium">{headingFor(d.section)}</span>
                  <span className="text-text-secondary"> · {d.reason}</span>
                  <blockquote className="mt-0.5 border-l-2 border-border pl-2 text-text-secondary line-clamp-3">
                    {d.text}
                  </blockquote>
                </li>
              ))}
            </ul>
          </Alert>
        )}

        <div>
          <label htmlFor="confluence-title" className="text-sm font-medium text-text-secondary block mb-1">
            Title
          </label>
          <Input
            id="confluence-title"
            value={title}
            maxLength={MAX_TITLE}
            onChange={(e) => setTitle(e.target.value)}
            error={title.trim() === '' ? 'Title is required' : undefined}
          />
          <p className="text-xs text-text-tertiary mt-0.5 tabular-nums text-right">
            {title.length}/{MAX_TITLE}
          </p>
        </div>

        {sections.map((section) => (
          <section key={section.key} aria-label={section.heading || section.key}>
            <div className="flex items-baseline gap-2 mb-1">
              <h3 className="text-sm font-semibold text-text-primary">{section.heading || section.key}</h3>
              <Badge
                variant={section.mode === 'verbatim' ? 'default' : 'info'}
                label={section.mode === 'verbatim' ? 'verbatim' : 'rewritten'}
              />
            </div>
            {section.paragraphs.map((p, i) => (
              <div key={i} className="mb-2">
                {section.mode === 'verbatim' ? (
                  <p className="whitespace-pre-wrap text-sm text-text-primary bg-bg-sunken border border-border rounded-lg p-2">
                    {p.text}
                  </p>
                ) : (
                  <Textarea
                    value={p.text}
                    onChange={(e) => updateParagraph(section.key, i, e.target.value)}
                    aria-label={`${section.heading || section.key} paragraph ${i + 1}`}
                  />
                )}
                {p.citations.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {p.citations.map((c) => (
                      <span
                        key={c}
                        className="text-2xs font-mono text-text-tertiary bg-surface border border-border rounded px-1 py-0.5"
                      >
                        {c}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </section>
        ))}

        {publishError && <Alert variant="danger">{publishError}</Alert>}
      </div>

      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="primary"
          disabled={publish.isPending || title.trim() === '' || isFetching || stale || !!link?.publishing}
          loading={publish.isPending}
          onClick={handlePublish}
        >
          {preview.existing ? 'Publish update' : 'Publish'}
        </Button>
      </DialogFooter>
    </>
  );
}
