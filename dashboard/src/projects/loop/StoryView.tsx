import { useState } from 'react';
import { Link } from 'react-router-dom';
import RelativeDate from '../../components/RelativeDate';
import { Alert, Badge, Button, Card } from '../../components/ui';
import type { Harm, Loop, Readiness, Story, StoryEvent } from '../../api/reviewLoop';
import { useStory, useTicketSpend } from '../../hooks/useReviewLoop';
import { errorMessage } from '../../utils/errorMessage';
import { safeHttpUrl } from '../../utils/url';
import { HARM_LABEL, formatTokens, formatUsd, isHarmful, plural, runHref, shortSha, ticketHref } from './format';
import { TrustSeal } from './TrustSeal';
import { ShareLinkDialog } from './ShareLinkDialog';

const KIND_LABEL: Record<StoryEvent['kind'], string> = {
  brief: 'Brief',
  run: 'Worker run',
  round: 'Review round',
  finding: 'Finding',
  fix: 'Fix',
  verdict: 'Verdict',
  merge: 'Merged',
  pr: 'Pull request',
  release: 'Release',
  deploy: 'Deploy',
};

function EventBody({ e, projectId, publicView }: { e: StoryEvent; projectId: string; publicView: boolean }) {
  const link = safeHttpUrl(e.url);
  const run = e.run_id && !publicView ? (
    <Link className="text-brand underline" to={runHref(projectId, e.run_id)}>
      Run record
    </Link>
  ) : null;
  const sha = e.sha ? (
    <code className="font-mono text-text-secondary" title={e.sha}>{shortSha(e.sha)}</code>
  ) : null;
  switch (e.kind) {
    case 'finding':
      return (
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            {e.harm && isHarmful(e.harm) ? (
              <Badge variant="danger" tint label={HARM_LABEL[e.harm as Harm] ?? e.harm} />
            ) : (
              <Badge label={e.harm ? (HARM_LABEL[e.harm as Harm] ?? e.harm) : 'Finding'} />
            )}
            {e.class && <span className="text-xs text-text-tertiary">{e.class}</span>}
          </div>
          <p className="text-sm text-text-primary">{e.title}</p>
          {e.file && (
            <p className="font-mono text-xs text-text-secondary">{e.file}{e.line ? `:${e.line}` : ''}</p>
          )}
          {run}
        </div>
      );
    case 'fix':
      return (
        <p className="text-sm text-text-primary">
          {e.title ?? 'Fix committed'} {sha && <>in {sha}</>}
        </p>
      );
    case 'run':
      return (
        <div className="space-y-0.5">
          <p className="text-sm text-text-primary">
            {e.role ?? 'Worker'} run{e.title ? `: ${e.title}` : ''}
          </p>
          <p className="text-xs text-text-tertiary">
            {e.tokens !== undefined && <>{formatTokens(e.tokens)} tokens</>}
            {e.cost_usd !== undefined && <> · {formatUsd(e.cost_usd)}</>}
          </p>
          {run}
        </div>
      );
    case 'round':
      return <p className="text-sm text-text-primary">{e.title ?? `Round ${e.round ?? ''}`}</p>;
    case 'verdict':
      return (
        <p className="text-sm text-text-primary">
          {e.reviewer ? `${e.reviewer}: ` : ''}{e.title ?? 'Verdict recorded'} {sha && <>on {sha}</>}
        </p>
      );
    case 'release':
      return <p className="text-sm text-text-primary">{e.version ? `Shipped in ${e.version}` : e.title ?? 'Released'}</p>;
    case 'deploy':
      return <p className="text-sm text-text-primary">{e.environment ? `Deployed to ${e.environment}` : e.title ?? 'Deployed'}</p>;
    default:
      return (
        <p className="text-sm text-text-primary">
          {e.title ?? KIND_LABEL[e.kind]} {sha}
          {link && (
            <>
              {' '}
              <a className="text-brand underline" href={link} target="_blank" rel="noopener noreferrer">
                Open{" "}<span className="sr-only">{KIND_LABEL[e.kind]}</span>
              </a>
            </>
          )}
        </p>
      );
  }
}

/** Dot on the rail: loud only for a harmful finding. */
function Dot({ e }: { e: StoryEvent }) {
  const loud = e.kind === 'finding' && isHarmful(e.harm);
  return (
    <span
      aria-hidden="true"
      data-harm={loud ? 'true' : 'false'}
      className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full border-2"
      style={{
        borderColor: loud ? 'var(--danger)' : 'var(--border-strong)',
        backgroundColor: loud ? 'var(--danger)' : 'var(--bg-primary)',
      }}
    />
  );
}

/** Where this change shipped: PRs, releases and deployments (absent on older servers). */
export function ShippedContext({ story }: { story: Story }) {
  const prs = story.prs ?? [];
  const rels = story.releases ?? [];
  const deps = story.deployments ?? [];
  if (prs.length === 0 && rels.length === 0 && deps.length === 0) return null;
  return (
    <Card className="space-y-3 p-4" data-testid="story-shipped">
      <h3 className="text-md font-semibold text-text-primary">Where it shipped</h3>
      <dl className="grid gap-3 sm:grid-cols-3">
        {prs.length > 0 && (
          <div>
            <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Pull requests</dt>
            <dd>
              <ul className="space-y-0.5 text-sm">
                {prs.map((p) => {
                  const href = safeHttpUrl(p.external_url);
                  const label = `${p.repo ?? p.provider}${p.number ? ` #${p.number}` : ''}${p.state ? ` (${p.state})` : ''}`;
                  return (
                    <li key={p.id}>
                      {href ? (
                        <a className="text-brand underline" href={href} target="_blank" rel="noopener noreferrer">
                          {label}<span className="sr-only"> (opens in a new tab)</span>
                        </a>
                      ) : (
                        <span className="text-text-secondary">{label}</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </dd>
          </div>
        )}
        {rels.length > 0 && (
          <div>
            <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Releases</dt>
            <dd>
              <ul className="space-y-0.5 text-sm text-text-secondary">
                {rels.map((r) => (
                  <li key={r.version}>
                    <span className="font-mono text-text-primary">{r.version}</span>
                    {r.released_at && <> · <RelativeDate iso={r.released_at} /></>}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
        {deps.length > 0 && (
          <div>
            <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Deployments</dt>
            <dd>
              <ul className="space-y-0.5 text-sm text-text-secondary">
                {deps.map((d, i) => (
                  <li key={`${d.environment}-${i}`}>
                    {d.environment}
                    {d.deployed_at && <> · <RelativeDate iso={d.deployed_at} /></>}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
      </dl>
    </Card>
  );
}

export function StoryTimeline({
  story,
  projectId,
  readiness,
  publicView = false,
}: {
  story: Story;
  projectId: string;
  /** Readiness for the head commit, from the loop board (the story has none). */
  readiness?: Readiness;
  /** The shared page: no links into the signed-in app. */
  publicView?: boolean;
}) {
  const events = [...story.events].sort((a, b) => (a.at ?? '').localeCompare(b.at ?? ''));
  const hasMerge = events.some((e) => e.kind === 'merge');
  return (
    <ol className="ml-1 border-l border-border" aria-label="Story of this change">
      {events.map((e, i) => (
        <li key={`${e.kind}-${e.at}-${i}`} className="relative pb-5 pl-5 last:pb-0">
          <Dot e={e} />
          <div className="mb-0.5 flex flex-wrap items-baseline gap-2">
            <span className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">{KIND_LABEL[e.kind]}</span>
            <span className="text-xs text-text-tertiary">{e.at ? <RelativeDate iso={e.at} /> : 'Time not recorded'}</span>
          </div>
          <EventBody e={e} projectId={projectId} publicView={publicView} />
          {e.kind === 'merge' && readiness && (
            <div className="mt-2"><TrustSeal readiness={readiness} /></div>
          )}
        </li>
      ))}
      {!hasMerge && readiness && (
        <li className="relative pl-5">
          <span aria-hidden="true" className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-border-strong bg-bg-primary" />
          <span className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Merge point</span>
          <div className="mt-1.5"><TrustSeal readiness={readiness} /></div>
        </li>
      )}
    </ol>
  );
}

/** One ticket's change from brief to deploy. URL-addressable via ?story=<ticket id>. */
export function StoryView({
  projectId,
  ticketId,
  loops = [],
  onBack,
  canEdit = false,
}: {
  projectId: string;
  ticketId: string;
  loops?: Loop[];
  onBack: () => void;
  /** Editors may share the story through a public link. */
  canEdit?: boolean;
}) {
  const [sharing, setSharing] = useState(false);
  const child = loops.flatMap((l) => l.children).find((c) => c.ticket_id === ticketId);
  const readiness: Readiness | undefined = child
    ? { ready: child.ready, reasons: child.not_ready_reasons ?? [], verdict: child.verdict ?? null }
    : undefined;
  const { data: story, isLoading, error } = useStory(projectId, ticketId);
  const { data: spend } = useTicketSpend(projectId, ticketId);

  return (
    <section aria-labelledby="story-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button variant="ghost" size="sm" onClick={onBack}>All loops</Button>
        {canEdit && story && (
          <Button variant="secondary" size="sm" onClick={() => setSharing(true)}>Share</Button>
        )}
      </div>
      {sharing && (
        <ShareLinkDialog projectId={projectId} target={{ kind: 'story', ticketId }} onClose={() => setSharing(false)} />
      )}
      {isLoading && <p className="text-text-tertiary">Loading the story…</p>}
      {error && <Alert variant="danger">Could not load this story: {errorMessage(error)}</Alert>}
      {story && (
        <>
          <div className="space-y-1">
            <h2 id="story-heading" className="text-lg font-semibold text-text-primary">{story.title}</h2>
            <p className="text-sm text-text-tertiary">
              <Link className="font-mono underline" to={ticketHref(projectId, ticketId)}>{ticketId}</Link>
              {spend && (
                <>
                  {' · '}
                  {plural(spend.review_rounds, 'review round')}
                  {' · '}
                  {formatTokens(spend.totals.input_tokens + spend.totals.output_tokens)} tokens
                  {spend.totals.cost_usd !== undefined && <> · {formatUsd(spend.totals.cost_usd)}</>}
                </>
              )}
            </p>
          </div>
          <ShippedContext story={story} />
          {story.events.length === 0 ? (
            <Card className="p-6 text-center text-sm text-text-tertiary">Nothing recorded for this change yet.</Card>
          ) : (
            <Card className="p-4 sm:p-5"><StoryTimeline story={story} projectId={projectId} readiness={readiness} /></Card>
          )}
        </>
      )}
    </section>
  );
}
