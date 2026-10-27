import { useEffect, useRef, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Button } from '../../components/ui';
import { JiraCard } from '../JiraCard';
import { ConfluenceCard } from '../ConfluenceCard';
import { useJiraConnection, useJiraHealth } from '../useJira';
import { useConfluenceConnection, useConfluenceHealth } from '../useConfluence';
import { confluenceProblems, jiraProblems, type ConfluenceSection, type JiraSection } from './health';
import { ConnectionState, SiteHost, problemSummary } from './parts';

const ENTERPRISE_TIER = 'enterprise';

type IntegrationKey = 'jira' | 'confluence';
const INTEGRATIONS: IntegrationKey[] = ['jira', 'confluence'];
const NAMES: Record<IntegrationKey, string> = { jira: 'Jira', confluence: 'Confluence' };

/**
 * Organization → Integrations. One compact row per integration (status,
 * site, problems only), with each integration's settings on its own detail
 * view: `?tab=integrations&integration=jira&section=sync`. Keeping the
 * detail in the URL makes back/forward and links to a section work.
 */
export default function IntegrationsTab({ orgId, tier }: { orgId: string; tier: string }) {
  const [params, setParams] = useSearchParams();
  const requested = params.get('integration');
  const integration = INTEGRATIONS.includes(requested as IntegrationKey) ? (requested as IntegrationKey) : null;
  const section = params.get('section');

  // Entering or leaving a detail view is a history entry; moving between its
  // sections (tabs, arrow keys, fix actions) replaces the current one.
  const open = (key: IntegrationKey | null, nextSection?: string, replace = false) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (key) next.set('integration', key);
        else next.delete('integration');
        if (nextSection) next.set('section', nextSection);
        else next.delete('section');
        return next;
      },
      { replace },
    );

  // The view swaps under the control that triggered it (Manage, Connect, the
  // breadcrumb, Back), so put focus on the new view's heading or list.
  const rootRef = useRef<HTMLDivElement>(null);
  const previous = useRef(integration);
  useEffect(() => {
    if (previous.current === integration) return;
    previous.current = integration;
    const target = integration
      ? rootRef.current?.querySelector<HTMLElement>('[data-integration-heading]')
      : rootRef.current?.querySelector<HTMLElement>('[data-integrations-list]');
    target?.focus();
  }, [integration]);

  if (integration) {
    return (
      <div ref={rootRef}>
        <nav aria-label="Breadcrumb" className="mb-3 text-sm">
          <ol className="flex items-center gap-1.5 text-text-tertiary">
            <li>
              <button
                type="button"
                onClick={() => open(null)}
                className="rounded-sm text-text-secondary hover:text-text-primary hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
              >
                Integrations
              </button>
            </li>
            <li aria-hidden="true">/</li>
            <li aria-current="page" className="text-text-primary">
              {NAMES[integration]}
            </li>
          </ol>
        </nav>
        {integration === 'jira' ? (
          <JiraCard
            orgId={orgId}
            tier={tier}
            section={section as JiraSection | null}
            onSectionChange={(s) => open('jira', s, true)}
          />
        ) : (
          <ConfluenceCard
            orgId={orgId}
            tier={tier}
            section={section as ConfluenceSection | null}
            onSectionChange={(s) => open('confluence', s, true)}
          />
        )}
      </div>
    );
  }

  const enterprise = tier === ENTERPRISE_TIER;
  return (
    <div ref={rootRef} className="space-y-3">
      <h2 className="sr-only">Integrations</h2>
      <ul
        aria-label="Integrations"
        tabIndex={-1}
        data-integrations-list
        className="divide-y divide-border rounded-lg border border-border bg-surface outline-none"
      >
        {enterprise ? (
          <>
            <JiraRow orgId={orgId} onOpen={(s) => open('jira', s)} />
            <ConfluenceRow orgId={orgId} onOpen={(s) => open('confluence', s)} />
          </>
        ) : (
          INTEGRATIONS.map((key) => (
            <OverviewRow
              key={key}
              name={NAMES[key]}
              purpose={PURPOSE[key]}
              status={<span className="text-xs text-text-tertiary">Available on Enterprise</span>}
              action={
                <Button size="sm" variant="secondary" onClick={() => open(key)} aria-label={`Learn more about ${NAMES[key]}`}>
                  Learn more
                </Button>
              }
            />
          ))
        )}
      </ul>
      <p className="text-xs text-text-tertiary">
        GitHub and GitLab connect to your own account in{' '}
        <Link to="/settings/integrations" className="text-brand underline underline-offset-2">
          Settings → Integrations
        </Link>
        .
      </p>
    </div>
  );
}

const PURPOSE: Record<IntegrationKey, string> = {
  jira: 'Promote tickets to Jira stories and sync status and comments.',
  confluence: 'Publish wiki pages and the context document to Confluence.',
};

function OverviewRow({
  name,
  purpose,
  status,
  health,
  action,
}: {
  name: string;
  purpose: string;
  status: ReactNode;
  health?: ReactNode;
  action: ReactNode;
}) {
  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3.5">
      <div className="min-w-0 flex-[1_1_16rem]">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h3 className="text-sm font-semibold text-text-primary">{name}</h3>
          {status}
        </div>
        <p className="mt-0.5 text-xs text-text-tertiary">{purpose}</p>
        {health && <div className="mt-1.5">{health}</div>}
      </div>
      <div className="shrink-0">{action}</div>
    </li>
  );
}

function HealthLine({ summary, loading, error }: { summary: string; loading: boolean; error: boolean }) {
  if (loading) return <span className="text-xs text-text-tertiary">Checking health…</span>;
  // A failed health read must never look like a clean bill of health.
  if (error) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-text-tertiary">
        <span aria-hidden="true" className="inline-block h-1.5 w-1.5 rounded-full bg-text-tertiary" />
        Health unavailable
      </span>
    );
  }
  if (!summary) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-text-secondary">
        <span aria-hidden="true" className="inline-block h-1.5 w-1.5 rounded-full bg-success" />
        All good
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-warning">
      <span aria-hidden="true" className="inline-block h-1.5 w-1.5 rounded-full bg-warning" />
      {summary}
    </span>
  );
}

/** The connection could not be read: neither connected nor safe to offer Connect. */
function ConnectionError() {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-danger">
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full bg-danger" />
      Could not load the connection
    </span>
  );
}

function ConnectedStatus({ site }: { site?: string }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 text-xs">
      <ConnectionState connected />
      {site && (
        <>
          <span className="text-text-tertiary" aria-hidden="true">·</span>
          <SiteHost url={site} />
        </>
      )}
    </span>
  );
}

function JiraRow({ orgId, onOpen }: { orgId: string; onOpen: (section?: JiraSection) => void }) {
  const connection = useJiraConnection(orgId);
  const connected = connection.data?.connected === true;
  // Health is only meaningful (and only fetched) once connected.
  const health = useJiraHealth(connected ? orgId : undefined);
  const summary = health.data ? problemSummary(jiraProblems(health.data)) : '';
  return (
    <OverviewRow
      name="Jira"
      purpose={PURPOSE.jira}
      status={
        connection.isLoading ? null : connection.isError ? (
          <ConnectionError />
        ) : connected ? (
          <ConnectedStatus site={connection.data?.site_url} />
        ) : (
          <ConnectionState connected={false} />
        )
      }
      health={connected ? <HealthLine summary={summary} loading={health.isLoading} error={health.isError} /> : undefined}
      action={
        connection.isError ? (
          <Button size="sm" variant="secondary" onClick={() => void connection.refetch()} aria-label="Retry loading Jira">
            Retry
          </Button>
        ) : connected ? (
          <Button size="sm" variant="secondary" onClick={() => onOpen()} aria-label="Manage Jira">
            Manage
          </Button>
        ) : (
          <Button size="sm" onClick={() => onOpen('connection')} aria-label="Connect Jira" disabled={connection.isLoading}>
            Connect
          </Button>
        )
      }
    />
  );
}

function ConfluenceRow({ orgId, onOpen }: { orgId: string; onOpen: (section?: ConfluenceSection) => void }) {
  const connection = useConfluenceConnection(orgId);
  const connected = connection.data?.connected === true;
  const health = useConfluenceHealth(connected ? orgId : undefined);
  const summary = health.data ? problemSummary(confluenceProblems(health.data)) : '';
  return (
    <OverviewRow
      name="Confluence"
      purpose={PURPOSE.confluence}
      status={
        connection.isLoading ? null : connection.isError ? (
          <ConnectionError />
        ) : connected ? (
          <ConnectedStatus site={connection.data?.site_url} />
        ) : (
          <ConnectionState connected={false} />
        )
      }
      health={connected ? <HealthLine summary={summary} loading={health.isLoading} error={health.isError} /> : undefined}
      action={
        connection.isError ? (
          <Button size="sm" variant="secondary" onClick={() => void connection.refetch()} aria-label="Retry loading Confluence">
            Retry
          </Button>
        ) : connected ? (
          <Button size="sm" variant="secondary" onClick={() => onOpen()} aria-label="Manage Confluence">
            Manage
          </Button>
        ) : (
          <Button size="sm" onClick={() => onOpen('connection')} aria-label="Connect Confluence" disabled={connection.isLoading}>
            Connect
          </Button>
        )
      }
    />
  );
}
