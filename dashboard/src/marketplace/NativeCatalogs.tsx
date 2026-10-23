import { useState } from 'react';
import type { NativeCatalog, WorksIn, WorksInBadge } from '../api/client';
import { Badge } from '../components/Badge';
import CopyButton from '../components/CopyButton';
import { Card, TableShell } from '../components/ui';
import { useNativeCatalogs, useWorksIn } from '../hooks/useNativeCatalogs';
import { isTierGate } from './tierGate';
import { errorMessage } from '../utils/errorMessage';

const TOOL_LABELS: Record<string, string> = {
  'claude-code': 'Claude Code',
  copilot: 'GitHub Copilot',
  codex: 'Codex',
  cursor: 'Cursor',
  gemini: 'Gemini CLI',
  antigravity: 'Antigravity',
};

const STEP_LABELS: Record<string, string> = {
  add: 'Add',
  cli: 'Shell',
  install: 'Install',
  add_hosted: 'Or add from SessionFS',
};

function scopeLabel(catalog: NativeCatalog): string {
  if (catalog.scope === 'public') return 'Public';
  if (catalog.scope === 'user') return 'Personal';
  return `Org ${catalog.scope_id}`;
}

/** "Use in your tool": each tool's own add/install command (marketplace v2 §7.6). */
export function UseInYourTool() {
  const catalogs = useNativeCatalogs();
  const [selected, setSelected] = useState(0);
  if (catalogs.isLoading || catalogs.error || !catalogs.data || catalogs.data.items.length === 0) return null;
  const items = catalogs.data.items;
  const catalog = items[Math.min(selected, items.length - 1)];
  return (
    <Card className="mt-6 p-5" aria-labelledby="use-in-tool-heading">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="use-in-tool-heading" className="text-lg font-semibold text-text-primary">Use in your tool</h2>
        <p className="text-xs text-text-tertiary">
          Native catalogs hold only approved versions · {catalog.plugin_count ?? '–'} plugins
        </p>
      </div>
      <p className="mt-1 text-sm text-text-secondary">
        Add a SessionFS catalog with your tool&apos;s own plugin command. Run{' '}
        <code className="font-mono text-xs">sfs catalog add --for &lt;tool&gt;</code> once so git authenticates to
        this host through <code className="font-mono text-xs">{catalogs.data.credential_helper}</code>.
      </p>
      {items.length > 1 && (
        <div role="tablist" aria-label="Catalog scope" className="mt-4 flex flex-wrap gap-2">
          {items.map((item, index) => (
            <button
              key={`${item.scope}:${item.scope_id ?? ''}`}
              type="button"
              role="tab"
              aria-selected={index === selected}
              onClick={() => setSelected(index)}
              className={`rounded-lg border px-3 py-1.5 text-xs ${index === selected ? 'border-brand text-text-primary' : 'border-border text-text-secondary hover:border-border-strong'}`}
            >
              {scopeLabel(item)}
            </button>
          ))}
        </div>
      )}
      <p className="mt-4 break-all font-mono text-xs text-text-tertiary" data-testid="catalog-clone-url">
        {catalog.marketplace} · {catalog.clone_url}
      </p>
      {catalog.mirror && catalog.mirror.url.startsWith('https://') ? (
        <p className="mt-1 text-xs text-text-secondary" data-testid="catalog-mirror">
          No SessionFS key needed: this catalog is mirrored to{' '}
          <a
            href={catalog.mirror.url}
            target="_blank"
            rel="noopener noreferrer"
            className="font-mono text-brand hover:underline"
          >
            {catalog.mirror.repo}
          </a>{' '}
          on GitHub. Gemini CLI still installs from SessionFS.
        </p>
      ) : null}
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        {Object.entries(catalog.tools).map(([tool, steps]) => (
          <div key={tool} className="rounded-lg border border-border bg-bg-sunken p-3" data-testid={`use-in-${tool}`}>
            <p className="text-sm font-semibold text-text-primary">{TOOL_LABELS[tool] ?? tool}</p>
            <ul className="mt-2 space-y-2">
              {Object.entries(steps).map(([step, command]) => (
                <li key={step} className="flex items-start justify-between gap-2">
                  <span className="min-w-0">
                    <span className="text-2xs uppercase tracking-wider text-text-tertiary">{STEP_LABELS[step] ?? step}</span>
                    <span className="block break-all font-mono text-xs text-text-secondary">{command}</span>
                  </span>
                  {command.startsWith('/') || /^[a-z-]+ /.test(command) ? <CopyButton text={command} /> : null}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Card>
  );
}

const BADGE_COPY: Record<WorksInBadge | 'expected', { label: string; variant: 'success' | 'info' | 'danger' | 'warning' | 'default' }> = {
  verified: { label: 'Verified', variant: 'success' },
  'format-valid': { label: 'Format valid', variant: 'info' },
  regressed: { label: 'Regressed', variant: 'danger' },
  failed: { label: 'Failed', variant: 'danger' },
  'format-invalid': { label: 'Format invalid', variant: 'warning' },
  'not-applicable': { label: 'Public mirror only', variant: 'default' },
  unverified: { label: 'Not yet verified', variant: 'default' },
  expected: { label: 'Expected', variant: 'default' },
};

function bestLevel(entry: WorksIn['tools'][string]) {
  for (const level of ['L2', 'L1', 'L0'] as const) {
    const value = entry.levels[level];
    if (value) return { level, ...value };
  }
  return null;
}

/** Per-tool works-in matrix for one version. */
export function WorksInMatrix({ versionId, enabled }: { versionId: string; enabled: boolean }) {
  const worksIn = useWorksIn(versionId, enabled);
  if (!enabled) return null;
  if (worksIn.isLoading) return <p className="mt-4 text-sm text-text-tertiary">Checking which tools this works in…</p>;
  if (worksIn.error) {
    return (
      <p role="status" className="mt-4 text-sm text-text-tertiary">
        {isTierGate(worksIn.error)
          ? 'Works-in verification of private packages is available on Team and above.'
          : `Works-in evidence unavailable: ${errorMessage(worksIn.error)}`}
      </p>
    );
  }
  if (!worksIn.data) return null;
  const rows = Object.entries(worksIn.data.tools);
  return (
    <section className="mt-6" aria-labelledby="works-in-heading">
      <h3 id="works-in-heading" className="text-sm font-semibold text-text-primary">Works in</h3>
      <p className="mt-1 text-xs text-text-tertiary">
        Verified = the tool&apos;s own installer accepted it. Format valid = the generated catalog entry matches the tool&apos;s
        documented format (checked {worksIn.data.vendor_docs_checked}).
      </p>
      <TableShell minWidth={560} bordered wrapperClassName="mt-2">
          <thead className="bg-bg-sunken text-xs uppercase tracking-wider text-text-tertiary">
            <tr><th className="px-3 py-2">Tool</th><th className="px-3 py-2">Status</th><th className="px-3 py-2">Evidence</th></tr>
          </thead>
          <tbody>
            {rows.map(([tool, entry]) => {
              const best = bestLevel(entry);
              const copy = BADGE_COPY[entry.badge] ?? BADGE_COPY.unverified;
              return (
                <tr key={tool} className="border-t border-border" data-testid={`works-in-${tool}`}>
                  <td className="px-3 py-2 text-text-primary">{TOOL_LABELS[tool] ?? tool}</td>
                  <td className="px-3 py-2"><Badge label={copy.label} variant={copy.variant} /></td>
                  <td className="px-3 py-2 text-xs text-text-secondary">
                    {best ? `${best.level} · ${best.tool_version}${best.run_at ? ` · ${best.run_at.slice(0, 10)}` : ''}${best.status !== 'pass' && best.reason ? ` · ${best.reason}` : ''}` : '—'}
                  </td>
                </tr>
              );
            })}
            {Object.entries(worksIn.data.expected_only).map(([tool, entry]) => (
              <tr key={tool} className="border-t border-border" data-testid={`works-in-${tool}`}>
                <td className="px-3 py-2 text-text-primary">{TOOL_LABELS[tool] ?? tool}</td>
                <td className="px-3 py-2"><Badge label={BADGE_COPY.expected.label} variant="default" /></td>
                <td className="px-3 py-2 text-xs text-text-secondary">
                  Via sfs skill · omitted: {entry.omitted_components.join(', ') || 'none'}
                </td>
              </tr>
            ))}
          </tbody>
      </TableShell>
    </section>
  );
}
