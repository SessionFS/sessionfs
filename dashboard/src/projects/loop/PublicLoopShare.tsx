import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Button, Card } from '../../components/ui';
import { resolveApiBaseUrl } from '../../auth/apiBase';
import { fetchPublicLoopShare, isPublicStory, type PublicLoopShare as Data } from '../../api/reviewLoop';
import { ShipReportCard } from './ShipReport';
import { ShippedContext, StoryTimeline } from './StoryView';

type State = { kind: 'loading' } | { kind: 'unavailable' } | { kind: 'ready'; data: Data };

/** Keep crawlers out while the page is mounted. */
function useNoIndex() {
  useEffect(() => {
    const meta = document.createElement('meta');
    meta.setAttribute('name', 'robots');
    meta.setAttribute('content', 'noindex');
    document.head.appendChild(meta);
    return () => meta.remove();
  }, []);
}

function Unavailable() {
  return (
    <Card className="mx-auto max-w-md p-8 text-center" data-testid="share-unavailable">
      <h1 className="mb-1 text-lg font-semibold text-text-primary">This link is unavailable</h1>
      <p className="text-sm text-text-tertiary">It may have expired or been revoked.</p>
    </Card>
  );
}

/** Public, read-only story or ship report behind a share link. Sends no credentials. */
export default function PublicLoopShare() {
  const { token = '' } = useParams();
  const [state, setState] = useState<State>({ kind: 'loading' });
  useNoIndex();

  useEffect(() => {
    let live = true;
    setState({ kind: 'loading' });
    fetchPublicLoopShare(resolveApiBaseUrl(), token)
      .then((data) => live && setState({ kind: 'ready', data }))
      .catch(() => live && setState({ kind: 'unavailable' }));
    return () => {
      live = false;
    };
  }, [token]);

  return (
    <main className="mx-auto min-h-screen max-w-3xl space-y-4 bg-bg-primary px-4 py-8">
      {state.kind === 'loading' && <p className="text-center text-text-tertiary">Loading…</p>}
      {state.kind === 'unavailable' && <Unavailable />}
      {state.kind === 'ready' &&
        (isPublicStory(state.data) ? (
          <section aria-labelledby="public-story-heading" className="space-y-4">
            <h1 id="public-story-heading" className="text-lg font-semibold text-text-primary">{state.data.title}</h1>
            <h2 className="sr-only">Change details</h2>
            <ShippedContext story={state.data} />
            {state.data.events.length === 0 ? (
              <Card className="p-6 text-center text-sm text-text-tertiary">Nothing recorded for this change yet.</Card>
            ) : (
              <Card className="p-4 sm:p-5"><StoryTimeline story={state.data} projectId="" publicView /></Card>
            )}
          </section>
        ) : (
          <section aria-labelledby="public-ship-heading" className="space-y-4">
            <h1 id="public-ship-heading" className="text-xs uppercase tracking-[0.04em] text-text-tertiary">Ship report</h1>
            <h2 className="sr-only">Summary</h2>
            <ShipReportCard report={state.data} />
            <CopyMarkdown markdown={state.data.markdown} />
          </section>
        ))}
      <p className="text-center text-xs text-text-tertiary">Counts of stored records. Recorded by SessionFS.</p>
    </main>
  );
}

function CopyMarkdown({ markdown }: { markdown: string }) {
  const [note, setNote] = useState<string | null>(null);
  return (
    <div className="flex items-center gap-2">
      <Button
        variant="secondary"
        size="sm"
        onClick={() => {
          navigator.clipboard.writeText(markdown).then(
            () => setNote('Markdown copied.'),
            () => setNote('Could not copy. Your browser blocked clipboard access.'),
          );
        }}
      >
        Copy markdown
      </Button>
      <span role="status" className="text-xs text-text-tertiary">{note}</span>
    </div>
  );
}
