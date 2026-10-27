import type { ConfluenceHealth, JiraHealth } from '../../api/client';
import { plural, type IntegrationProblem } from './parts';

/* Health → the list of non-zero problems, each with a fix action that opens
 * the detail section where it can be resolved. Shared by the overview row
 * (one-line summary) and the detail's Overview tab (full list). */

export type JiraSection = 'overview' | 'connection' | 'mappings' | 'templates' | 'sync' | 'activity';
export type ConfluenceSection = 'overview' | 'connection' | 'mappings';

const mono = (s: string) => <span className="font-mono">{s}</span>;

/** Where a published Confluence source lives in the dashboard: the wiki page,
 *  or the project overview for the compiled context. A plain link, so the
 *  card also works outside a router. */
function confluenceSourceLink(src: { project_id: string; source_kind: string; source_key: string }) {
  const project = encodeURIComponent(src.project_id);
  const href =
    src.source_kind === 'wiki_page'
      ? `/projects/${project}?tab=knowledge&sub=pages&page=${encodeURIComponent(src.source_key)}`
      : `/projects/${project}`;
  return (
    <a href={href} className="font-mono text-brand underline underline-offset-2">
      {src.source_key}
    </a>
  );
}

export function jiraProblems(
  health: JiraHealth,
  go?: (section: JiraSection) => void,
): IntegrationProblem[] {
  const out: IntegrationProblem[] = [];
  const action = (label: string, section: JiraSection) => (go ? { label, onClick: () => go(section) } : undefined);

  const unmapped = health.unmapped_project_count;
  if (unmapped > 0) {
    out.push({
      key: 'unmapped',
      tone: 'warning',
      title: plural(unmapped, 'unmapped project'),
      summary: plural(unmapped, 'unmapped project'),
      body: 'Map every project to a Jira project so its tickets can be promoted.',
      action: action('Map projects', 'mappings'),
    });
  }

  const failures = health.last_push_failures;
  if (failures.length > 0) {
    out.push({
      key: 'push-failures',
      tone: 'danger',
      title: plural(failures.length, 'push failure'),
      summary: plural(failures.length, 'push failure'),
      details: failures.map((f) => (
        <>
          {mono(f.ticket_id)} → {f.issue_key ?? '—'}: {f.reason ?? 'unknown error'}
        </>
      )),
      action: action('View activity', 'activity'),
    });
  }

  const failedSync = health.failed_sync_events_24h ?? 0;
  if (failedSync > 0) {
    out.push({
      key: 'sync-failed',
      tone: 'danger',
      title: `${plural(failedSync, 'failed or refused sync event')} in the last 24 hours`,
      summary: plural(failedSync, 'sync failure'),
      body: 'Includes outbound pushes that failed and inbound Jira transitions SessionFS refused.',
      action: action('View failures', 'activity'),
    });
  }

  const rejected = health.webhook_auth_failures_24h ?? 0;
  if (rejected > 0) {
    out.push({
      key: 'webhook-rejected',
      tone: 'warning',
      title: `${plural(rejected, 'rejected webhook delivery', 'rejected webhook deliveries')} in the last 24 hours`,
      summary: plural(rejected, 'rejected webhook', 'rejected webhooks'),
      body: "Jira's signature did not match the stored secret. Rotate the secret and paste it into Jira's webhook.",
      action: action('Check webhook', 'sync'),
    });
  }

  const stale = health.stale_links;
  if (stale.length > 0) {
    out.push({
      key: 'stale',
      tone: 'warning',
      title: plural(stale.length, 'stale link'),
      summary: plural(stale.length, 'stale link'),
      details: stale.map((l) => (
        <>
          {mono(l.ticket_id)} → {l.issue_key}
        </>
      )),
    });
  }

  const unknown = health.publishes_unknown;
  if (unknown.length > 0) {
    out.push({
      key: 'unknown',
      tone: 'warning',
      title: `${plural(unknown.length, 'publish', 'publishes')} with an unknown result`,
      summary: `${plural(unknown.length, 'unconfirmed publish', 'unconfirmed publishes')}`,
      body: 'Publish the ticket again to reconcile it with Jira.',
      details: unknown.map((p) => mono(p.ticket_id)),
    });
  }

  const archivedMappings = health.mappings_on_archived_templates ?? [];
  const archivedLinks = health.archived_template_references ?? [];
  const defaultArchived = !!health.connection_default_archived;
  const archivedCount = archivedMappings.length + archivedLinks.length + (defaultArchived ? 1 : 0);
  if (archivedCount > 0) {
    out.push({
      key: 'archived',
      tone: 'warning',
      title: 'On archived templates',
      summary: `${archivedCount} on archived templates`,
      body:
        'These keep working with the archived template, but it can no longer be newly selected. Pick an active template, or unarchive it.',
      details: [
        ...(defaultArchived ? ['The connection default template'] : []),
        ...archivedMappings.map((m) => (
          <>
            Mapping {mono(m.project_id)} → {mono(m.template_slug)}
          </>
        )),
        ...archivedLinks.map((l) => (
          <>
            Ticket {mono(l.ticket_id)} published with {mono(l.template_slug)}
            {l.template_version != null ? ` v${l.template_version}` : ''}
          </>
        )),
      ],
      action: action('Review templates', 'templates'),
    });
  }

  const inProgress = health.publishes_in_progress;
  if (inProgress.length > 0) {
    // Informational: listed in the detail, never counted as a problem.
    out.push({
      key: 'in-progress',
      tone: 'info',
      title: `${plural(inProgress.length, 'publish', 'publishes')} in progress`,
      details: inProgress.map((p) => mono(p.ticket_id)),
    });
  }

  return out;
}

export function confluenceProblems(
  health: ConfluenceHealth,
  go?: (section: ConfluenceSection) => void,
): IntegrationProblem[] {
  const out: IntegrationProblem[] = [];
  const action = (label: string, section: ConfluenceSection) =>
    go ? { label, onClick: () => go(section) } : undefined;

  if (health.unmapped_project_count > 0) {
    out.push({
      key: 'unmapped',
      tone: 'warning',
      title: plural(health.unmapped_project_count, 'unmapped project'),
      summary: plural(health.unmapped_project_count, 'unmapped project'),
      body: 'Map every project to a Confluence space so its pages and context can be published.',
      action: action('Map projects', 'mappings'),
    });
  }
  if (health.last_push_failures.length > 0) {
    out.push({
      key: 'push-failures',
      tone: 'danger',
      title: plural(health.last_push_failures.length, 'push failure'),
      summary: plural(health.last_push_failures.length, 'push failure'),
      body: 'Open the page and publish it again once the cause is fixed.',
      details: health.last_push_failures.map((f) => (
        <>
          {confluenceSourceLink(f)}: {f.reason ?? 'unknown error'}
        </>
      )),
    });
  }
  if (health.drifted_links.length > 0) {
    out.push({
      key: 'drifted',
      tone: 'warning',
      title: plural(health.drifted_links.length, 'drifted page'),
      summary: plural(health.drifted_links.length, 'drifted page'),
      body: 'Someone edited these pages in Confluence. Open one and publish: the dialog shows their changes before overwriting them.',
      details: health.drifted_links.map((l) => (
        <>
          {confluenceSourceLink(l)} → page {l.page_id ?? '—'}
        </>
      )),
    });
  }
  if (health.publishes_unknown.length > 0) {
    out.push({
      key: 'unknown',
      tone: 'warning',
      title: `${plural(health.publishes_unknown.length, 'publish', 'publishes')} with an unknown result`,
      summary: plural(health.publishes_unknown.length, 'unconfirmed publish', 'unconfirmed publishes'),
      details: health.publishes_unknown.map((p) => mono(p.source_key)),
    });
  }
  if (health.publishes_in_progress.length > 0) {
    out.push({
      key: 'in-progress',
      tone: 'info',
      title: `${plural(health.publishes_in_progress.length, 'publish', 'publishes')} in progress`,
      details: health.publishes_in_progress.map((p) => mono(p.source_key)),
    });
  }
  return out;
}
