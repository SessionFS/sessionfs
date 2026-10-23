import type { ReactNode } from 'react';
import type { CapabilityChange, MarketplaceDisclosure, UpdateDiffDetail } from '../api/client';
import { Badge } from '../components/Badge';

/**
 * Install disclosure card (marketplace v2 §3.3): the same JSON the CLI and
 * MCP render, and the content the approval's disclosure_digest covers. EVERY
 * security-relevant field is rendered (provenance, scanner ruleset/counts,
 * hidden characters, persistence, executable extensions, MCP, tools, scope,
 * writes, shell commands, truncation), so nothing approved is off-screen.
 * Every value is untrusted package text and is rendered as text.
 */

const CONSENT_LABELS: Record<string, string> = {
  'skill-scripts': 'scripts',
  'skill-hooks': 'hooks / pre-approved tools',
  'executable-extensions': 'executable extensions',
  'hidden-unicode': 'hidden characters',
  'scan-findings': 'scanner findings',
  'mcp-review': 'MCP servers (review)',
};

function Field({ label, children, testId }: { label: string; children: ReactNode; testId: string }) {
  return (
    <div data-testid={testId}>
      <dt className="text-text-tertiary">{label}</dt>
      <dd className="break-all font-mono text-text-primary">{children}</dd>
    </div>
  );
}

function more(total: number | undefined, shown: number): string {
  const rest = (total ?? shown) - shown;
  return rest > 0 ? ` (+${rest} more not listed)` : '';
}

/** Every leaf of the card as [path, text] (same shape as the CLI's card_leaves). */
export function cardLeaves(value: unknown, path = ''): [string, string][] {
  if (Array.isArray(value)) {
    if (value.length === 0) return [[path, '[]']];
    return value.flatMap((item, index) => cardLeaves(item, `${path}[${index}]`));
  }
  if (value !== null && typeof value === 'object') {
    const keys = Object.keys(value as Record<string, unknown>).sort();
    if (keys.length === 0) return [[path, '{}']];
    return keys.flatMap((key) => cardLeaves((value as Record<string, unknown>)[key], path ? `${path}.${key}` : key));
  }
  if (typeof value === 'string') return [[path, value]];
  return [[path, JSON.stringify(value)]];
}

/** Exhaustive, schema-free rendering of the whole card (approval surfaces). */
function DisclosureFields({ disclosure }: { disclosure: MarketplaceDisclosure }) {
  const leaves = cardLeaves(disclosure);
  return (
    <div className="mt-3" data-testid="disclosure-fields">
      <p className="text-xs font-semibold text-text-primary">Every disclosed field ({leaves.length})</p>
      <ul className="mt-1 max-h-64 overflow-auto rounded-lg bg-bg-elevated p-2 font-mono text-2xs text-text-secondary">
        {leaves.map(([path, value]) => (
          <li key={path} data-leaf={path} className="break-all">
            {path} = {value}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function DisclosureCard({ disclosure }: { disclosure: MarketplaceDisclosure }) {
  const tools = Object.entries(disclosure.tools || {});
  const consent = (disclosure.consent_required || []).filter((item) => item !== 'instructions');
  const unicode = disclosure.unicode?.findings || [];
  const unicodeCounts = disclosure.unicode?.counts || {};
  const unicodeTotal = Object.values(unicodeCounts).reduce((sum, value) => sum + Number(value || 0), 0);
  const allowed = (disclosure.allowed_tools || [])
    .filter((entry) => entry.startsWith('allowed-tools:'))
    .map((entry) => entry.slice('allowed-tools:'.length));
  const hooks = tools[0]?.[1].runs.hooks || [];
  const scan = disclosure.scan || {};
  const counts = (scan as { counts?: Record<string, number> }).counts || {};
  return (
    <section
      aria-labelledby="disclosure-heading"
      className="rounded-xl border border-border bg-bg-sunken p-4"
      data-testid="disclosure-card"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="disclosure-heading" className="text-sm font-semibold text-text-primary">
          What this package can do
        </h3>
        <div className="flex flex-wrap gap-1">
          {(disclosure.capabilities || []).map((capability) => (
            <Badge
              key={capability}
              label={capability}
              variant={capability === 'instructions' || capability === 'assets' ? 'default' : 'warning'}
              tint
            />
          ))}
        </div>
      </div>
      <p className="mt-1 text-xs text-text-secondary" data-testid="disclosure-target">
        For {tools.map(([tool]) => tool).join(', ') || 'no tools'} · {disclosure.scope || 'project'} scope
        {disclosure.card_version ? ` · card v${disclosure.card_version}` : ''}
      </p>
      {(disclosure.incomplete || []).length > 0 && (
        <p role="alert" className="mt-2 text-xs font-semibold text-danger" data-testid="disclosure-incomplete">
          Not fully analyzed: {(disclosure.incomplete || []).join(', ')}
        </p>
      )}
      {(unicode.length > 0 || unicodeTotal > 0 || disclosure.unicode?.truncated) && (
        <div role="alert" className="mt-3 rounded-lg border border-danger/40 bg-danger/6 p-3" data-testid="disclosure-unicode">
          <p className="text-sm font-semibold text-danger">
            Invisible characters in {disclosure.unicode.flagged_files} file{disclosure.unicode.flagged_files === 1 ? '' : 's'}
            {unicodeTotal ? ` (${Object.entries(unicodeCounts).filter(([, v]) => v).map(([k, v]) => `${v} ${k}`).join(', ')})` : ''}
            {disclosure.unicode?.truncated ? ' · scan truncated' : ''}
          </p>
          <ul className="mt-1 space-y-0.5 font-mono text-xs text-text-secondary">
            {unicode.map((finding) => (
              <li key={`${finding.path}:${finding.line}:${finding.column}:${finding.rule_id}`}>
                {finding.path}:{finding.line}:{finding.column} {finding.visible || ''} {finding.class} ({finding.severity})
              </li>
            ))}
          </ul>
          {unicodeTotal > unicode.length && (
            <p className="mt-1 text-xs text-danger">+{unicodeTotal - unicode.length} more not listed</p>
          )}
        </div>
      )}
      <dl className="mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2">
        <Field label="Provenance" testId="disclosure-provenance">
          publisher {disclosure.provenance?.publisher_verified ? 'verified' : 'not verified'} · release{' '}
          {disclosure.provenance?.attested ? 'attested' : 'not attested'}
        </Field>
        <Field label="Scanner" testId="disclosure-scan">
          ruleset {scan.ruleset || 'unknown'} · {scan.finding_count || 0} finding{scan.finding_count === 1 ? '' : 's'}
          {scan.max_severity ? ` · worst ${scan.max_severity}` : ''}
          {Object.entries(counts).filter(([, v]) => v).map(([k, v]) => ` · ${v} ${k}`).join('')}
          {(scan as { truncated?: boolean }).truncated ? ' · truncated' : ''}
        </Field>
        {hooks.length > 0 && (
          <Field label="Hooks (run without asking)" testId="disclosure-hooks">
            {hooks.map((hook) => `${hook.skill}: ${hook.event} (${hook.matcher})`).join(', ')}
          </Field>
        )}
        {allowed.length > 0 && <Field label="Pre-approved tools" testId="disclosure-allowed">{allowed.join(', ')}</Field>}
        {disclosure.mcp_servers.local.length > 0 && (
          <Field label="Local MCP servers" testId="disclosure-mcp-local">
            {disclosure.mcp_servers.local.map((server) => `${server.name}: ${server.command}`).join(', ')}
          </Field>
        )}
        {disclosure.mcp_servers.remote.length > 0 && (
          <Field label="Remote MCP servers" testId="disclosure-mcp-remote">
            {disclosure.mcp_servers.remote.map((server) => `${server.name}: ${server.origin}${server.transport ? ` (${server.transport})` : ''}`).join(', ')}
          </Field>
        )}
        {(disclosure.executable_extensions || []).length > 0 && (
          <Field label="Executable extensions" testId="disclosure-executables">
            {disclosure.executable_extensions.map((item) => `${item.path} (${item.kind})`).join(', ')}
          </Field>
        )}
        {(disclosure.persistence_targets || []).length > 0 && (
          <Field label="Persistence" testId="disclosure-persistence">{disclosure.persistence_targets.join(', ')}</Field>
        )}
        {disclosure.network_endpoints.length > 0 && (
          <Field label="Network endpoints referenced" testId="disclosure-network">
            {disclosure.network_endpoints.join(', ')}
            {more(disclosure.network_endpoints_total, disclosure.network_endpoints.length)}
          </Field>
        )}
        {disclosure.env_names.length > 0 && (
          <Field label="Environment variables" testId="disclosure-env">{disclosure.env_names.join(', ')}</Field>
        )}
      </dl>
      {tools.map(([tool, item]) => (
        <div key={tool} className="mt-3 text-xs" data-testid={`disclosure-tool-${tool}`}>
          <p className="font-semibold text-text-primary">{tool}: <span className="font-normal">{item.summary}</span></p>
          <ul className="mt-1 max-h-40 overflow-auto font-mono text-2xs text-text-tertiary">
            {item.writes.map((path) => <li key={path}>writes {path}</li>)}
          </ul>
          {item.writes_total > item.writes.length && (
            <p className="text-2xs text-warning">+{item.writes_total - item.writes.length} more files not listed</p>
          )}
          {item.omitted.map((omitted) => (
            <p key={omitted.component} className="mt-1 text-warning">Not installed: {omitted.component} ({omitted.reason})</p>
          ))}
        </div>
      ))}
      {disclosure.shell_commands.length > 0 && (
        <div className="mt-3" data-testid="disclosure-shell">
          <p className="text-xs text-text-tertiary">Shell commands the instructions tell the agent to run</p>
          <ul className="mt-1 max-h-40 overflow-auto font-mono text-2xs text-text-secondary">
            {disclosure.shell_commands.map((command) => (
              <li key={`${command.path}:${command.line}`}>{command.path}:{command.line} {command.text}</li>
            ))}
          </ul>
        </div>
      )}
      {consent.length > 0 && (
        <p className="mt-3 text-xs font-semibold text-text-primary">
          Consent required: {consent.map((item) => CONSENT_LABELS[item] || item).join(', ')}
        </p>
      )}
      <DisclosureFields disclosure={disclosure} />
    </section>
  );
}

function ChangeList({ items, tone }: { items: CapabilityChange[]; tone: 'danger' | 'accent' }) {
  return (
    <ul className={`mt-1 space-y-0.5 font-mono text-xs ${tone === 'danger' ? 'text-danger' : 'text-accent'}`}>
      {items.map((item) => <li key={`${item.kind}:${item.item}`}>{tone === 'danger' ? '+' : '-'} {item.kind}: {item.item}</li>)}
    </ul>
  );
}

const CLASS_COPY: Record<string, { label: string; variant: 'success' | 'warning' | 'danger' | 'default' }> = {
  unchanged: { label: 'unchanged', variant: 'default' },
  text: { label: 'text-only update', variant: 'success' },
  code: { label: 'executable content changed', variant: 'warning' },
  widening: { label: 'asks for more access', variant: 'danger' },
};

/**
 * Update diff (marketplace v2 §3.4): renders the exact object the
 * update_digest binds — class, baseline, capability delta, every changed
 * file path, and every scan / unicode / provenance delta leaf — so an
 * acknowledgement never covers something that was off-screen.
 */
export function UpdateDiff({ diff }: { diff: UpdateDiffDetail }) {
  const copy = CLASS_COPY[diff.update_class] || CLASS_COPY.widening;
  const files = diff.files;
  // Every leaf of the three delta objects, schema-free (same walk as the
  // disclosure card's card_leaves): a field the server adds later appears
  // here automatically instead of being silently hidden.
  const deltaLeaves = cardLeaves({
    scan_delta: diff.scan_delta ?? {},
    unicode_delta: diff.unicode_delta ?? {},
    provenance_delta: diff.provenance_delta ?? {},
  });
  return (
    <section aria-label="Update diff" className="rounded-xl border border-border bg-bg-sunken p-4 text-sm" data-testid="update-diff">
      <div className="flex items-center gap-2">
        <span className="font-semibold text-text-primary">Update</span>
        <Badge label={copy.label} variant={copy.variant} tint />
      </div>
      {diff.consent?.description && <p className="mt-1 text-xs text-text-secondary">{diff.consent.description}</p>}
      <p className="mt-1 text-xs text-text-tertiary" data-testid="update-baseline">
        {diff.baseline_known
          ? `Baseline digest ${diff.baseline_digest || 'unknown'} · capability digest ${diff.capability_digest_changed ? 'changed' : 'unchanged'}`
          : 'Baseline unknown: the installed version’s access could not be verified — failing closed to full review'}
      </p>
      {diff.widens.length > 0 && <ChangeList items={diff.widens} tone="danger" />}
      {diff.code_changes.length > 0 && (
        <ul className="mt-1 space-y-0.5 font-mono text-xs text-warning">
          {diff.code_changes.map((change) => <li key={`${change.kind}:${change.path}`}>~ {change.kind} {change.change}: {change.path}</li>)}
        </ul>
      )}
      {diff.narrows && diff.narrows.length > 0 && <ChangeList items={diff.narrows} tone="accent" />}
      {(diff.changed_requirements || []).length > 0 && (
        <p className="mt-1 text-xs text-text-secondary" data-testid="update-requirements">
          Consent required again for: {diff.changed_requirements.join(', ')}
        </p>
      )}
      {files && (files.unknown ? (
        <p className="mt-2 text-xs text-text-tertiary" data-testid="update-files">
          Files changed: unknown (no verified baseline to compare against)
        </p>
      ) : (
        <div className="mt-2" data-testid="update-files">
          <p className="text-xs text-text-tertiary">
            Files: {files.added.length} added, {files.changed.length} changed, {files.removed.length} removed
          </p>
          <ul className="mt-1 max-h-40 overflow-auto font-mono text-2xs">
            {files.added.map((path) => <li key={`+${path}`} className="break-all text-accent">+ {path}</li>)}
            {files.changed.map((path) => <li key={`~${path}`} className="break-all text-warning">~ {path}</li>)}
            {files.removed.map((path) => <li key={`-${path}`} className="break-all text-danger">- {path}</li>)}
          </ul>
        </div>
      ))}
      <div className="mt-2" data-testid="update-deltas">
        <p className="text-xs font-semibold text-text-primary">Scanner, hidden-character and provenance changes</p>
        <ul className="mt-1 max-h-40 overflow-auto rounded-lg bg-bg-elevated p-2 font-mono text-2xs text-text-secondary">
          {deltaLeaves.map(([path, value]) => (
            <li key={path} data-leaf={path} className="break-all">
              {path} = {value}
            </li>
          ))}
        </ul>
      </div>
      {diff.text_diffs?.entries?.filter((entry) => entry.binary).map((entry) => (
        <p key={`bin:${entry.path}`} className="mt-1 font-mono text-2xs text-text-tertiary">
          {entry.path}: binary file {entry.change}
          {(entry.old_sha256 || entry.new_sha256) && ` (${entry.old_sha256 || 'none'} → ${entry.new_sha256 || 'none'})`}
        </p>
      ))}
      {diff.text_diffs?.entries?.filter((entry) => entry.diff).map((entry) => (
        <pre key={entry.path} className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-bg-elevated p-2 text-2xs text-text-secondary">{entry.diff}</pre>
      ))}
      {diff.text_diffs?.unavailable && <p className="mt-1 text-xs text-warning">Text diff unavailable.</p>}
      {diff.text_diffs?.truncated && <p className="mt-1 text-xs text-warning">Diff truncated.</p>}
    </section>
  );
}
