import type { SkillRegistry } from '../api/client';
import { ApiError } from '../api/client';
import { Badge } from '../components/Badge';
import { Button } from '../components/ui';
import { useRequestRegistryPoll } from '../hooks/useMarketplace';
import { useToast } from '../hooks/useToast';

type Variant = 'default' | 'success' | 'warning' | 'danger' | 'info';

function policyVariant(value: string): Variant {
  if (value === 'approved') return 'success';
  if (value === 'blocked') return 'danger';
  if (value === 'review') return 'warning';
  return 'default';
}

/** Poll state as one badge: what the server last saw for this registry. */
export function pollBadge(registry: SkillRegistry): { label: string; variant: Variant } {
  if (!registry.server_fetch_enabled) return { label: 'Metadata only', variant: 'default' };
  switch (registry.last_poll_status) {
    case 'ok':
      return { label: 'Polling', variant: 'success' };
    case 'partial':
      return { label: 'Some packages refused', variant: 'warning' };
    case 'error':
      return { label: registry.backoff ? 'Failing · backing off' : 'Failing', variant: 'danger' };
    default:
      return { label: 'Not polled yet', variant: 'info' };
  }
}

export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  const seconds = Math.round((at - now) / 1000);
  const abs = Math.abs(seconds);
  const [value, unit] =
    abs < 60 ? [abs, 's'] : abs < 3600 ? [Math.round(abs / 60), 'min'] : abs < 86400 ? [Math.round(abs / 3600), 'h'] : [Math.round(abs / 86400), 'd'];
  if (abs < 30) return 'just now';
  return seconds < 0 ? `${value} ${unit} ago` : `in ${value} ${unit}`;
}

function intervalLabel(seconds: number | undefined): string {
  if (!seconds) return 'off';
  if (seconds % 3600 === 0) return `every ${seconds / 3600} h`;
  if (seconds % 60 === 0) return `every ${seconds / 60} min`;
  return `every ${seconds} s`;
}

function RegistryRow({ registry, orgId, canAdmin }: { registry: SkillRegistry; orgId: string; canAdmin: boolean }) {
  const poll = useRequestRegistryPoll(orgId);
  const { addToast } = useToast();
  const badge = pollBadge(registry);
  const last = relativeTime(registry.last_poll_at);
  const next = relativeTime(registry.next_poll_at);
  const failures = registry.consecutive_failures ?? 0;
  const mode = registry.branch_only_mode ? `branch ${registry.branch ?? ''}` : `tags ${registry.tag_pattern ?? ''}`;

  const pollNow = () =>
    poll.mutate(registry.id, {
      onSuccess: () => addToast('success', `${registry.alias} will be polled on the next run.`),
      onError: (error) =>
        addToast(
          'error',
          error instanceof ApiError && error.status === 429
            ? 'A poll was requested less than a minute ago.'
            : `Could not schedule a poll: ${String(error instanceof Error ? error.message : error)}`,
        ),
    });

  return (
    <li className="rounded-md border border-border bg-bg-primary/40 p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="min-w-0 truncate font-mono text-text-secondary" title={registry.source_url}>{registry.alias}</span>
        <span className="flex flex-wrap items-center gap-1.5">
          <Badge label={badge.label} variant={badge.variant} tint />
          <Badge label={registry.policy_state} variant={policyVariant(registry.policy_state)} tint />
        </span>
      </div>
      {registry.server_fetch_enabled && (
        <p className="mt-1.5 text-xs text-text-tertiary">
          <span className="font-mono">{mode}</span> · {intervalLabel(registry.poll_interval_seconds)}
          {last && <> · last polled {last}</>}
          {next && <> · next {next}</>}
          {registry.backoff && failures > 0 && <> · {failures} consecutive failure{failures === 1 ? '' : 's'}</>}
        </p>
      )}
      {registry.server_fetch_enabled && registry.last_poll_error_summary && (
        <p
          className={`mt-1 break-words text-xs ${registry.last_poll_status === 'error' ? 'text-danger' : 'text-warning'}`}
          role={registry.last_poll_status === 'error' ? 'alert' : undefined}
        >
          {registry.last_poll_error_summary}
        </p>
      )}
      {canAdmin && registry.server_fetch_enabled && (
        <div className="mt-2">
          <Button type="button" size="sm" variant="secondary" onClick={pollNow} loading={poll.isPending} aria-label={`Poll ${registry.alias} now`}>
            Poll now
          </Button>
        </div>
      )}
    </li>
  );
}

export function RegistryList({ registries, orgId, canAdmin }: { registries: SkillRegistry[]; orgId: string; canAdmin: boolean }) {
  return (
    <ul className="mt-3 space-y-2">
      {registries.map((registry) => (
        <RegistryRow key={registry.id} registry={registry} orgId={orgId} canAdmin={canAdmin} />
      ))}
    </ul>
  );
}
