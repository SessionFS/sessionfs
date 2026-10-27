import { useState } from 'react';
import type { NativeCatalog, WorksIn, WorksInBadge } from '../api/client';
import { Badge } from '../components/Badge';
import { Button, Dialog, IconButton, Select, TableShell, Tabs, TabPanel } from '../components/ui';
import { useNativeCatalogs, useWorksIn } from '../hooks/useNativeCatalogs';
import { CommandWell } from './CommandWell';
import { isCommand } from './commands';
import { CloseIcon, TerminalIcon } from './icons';
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
  add: 'Add the catalog',
  cli: 'From your shell',
  install: 'Install a package',
  add_hosted: 'Or add from SessionFS',
};

function scopeLabel(catalog: NativeCatalog): string {
  if (catalog.scope === 'public') return 'Public';
  if (catalog.scope === 'user') return 'Personal';
  return `Org ${catalog.scope_id}`;
}

/**
 * Each tool's own add/install commands for a native catalog (marketplace v2
 * §7.6), one tool at a time. Rendered inside the "Install in your tool" dialog.
 */
export function UseInYourTool() {
  const catalogs = useNativeCatalogs();
  const [selected, setSelected] = useState(0);
  const [toolKey, setToolKey] = useState('');
  if (catalogs.isLoading || catalogs.error || !catalogs.data || catalogs.data.items.length === 0) return null;
  const items = catalogs.data.items;
  const catalog = items[Math.min(selected, items.length - 1)];
  const tools = Object.keys(catalog.tools);
  const tool = tools.includes(toolKey) ? toolKey : tools[0];
  const steps = tool ? catalog.tools[tool] : {};
  return (
    <div>
      <p className="text-sm leading-6 text-text-secondary">
        Add the SessionFS catalog to your tool once. Packages then install with the tool&apos;s own plugin commands, and
        only approved versions are listed{catalog.plugin_count != null ? ` (${catalog.plugin_count} right now)` : ''}.
      </p>
      {items.length > 1 && (
        <div className="mt-4 max-w-xs">
          <Select
            title="Catalog"
            value={String(Math.min(selected, items.length - 1))}
            onValueChange={(value) => setSelected(Number(value))}
            options={items.map((item, index) => ({ value: String(index), label: scopeLabel(item) }))}
          />
        </div>
      )}
      {tools.length > 0 && (
        <div className="mt-5">
          <Tabs
            bare
            idPrefix="install-tool"
            aria-label="Tool"
            tabs={tools.map((key) => ({ key, label: TOOL_LABELS[key] ?? key }))}
            activeKey={tool}
            onChange={setToolKey}
          />
          <TabPanel idPrefix="install-tool" tabKey={tool} className="mt-1 rounded-sm pt-3 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]">
            <ol className="space-y-3" data-testid={`use-in-${tool}`}>
              {Object.entries(steps).map(([step, command]) => (
                <li key={step}>
                  <p className="mb-1 text-xs font-medium text-text-tertiary">{STEP_LABELS[step] ?? step}</p>
                  {isCommand(command) ? (
                    <CommandWell command={command} label={`${TOOL_LABELS[tool] ?? tool} ${STEP_LABELS[step] ?? step}`} />
                  ) : (
                    <p className="text-sm leading-6 text-text-secondary" data-testid={`step-prose-${step}`}>{command}</p>
                  )}
                </li>
              ))}
            </ol>
          </TabPanel>
        </div>
      )}
      {catalog.mirror && catalog.mirror.url.startsWith('https://') ? (
        <p className="mt-4 text-xs leading-5 text-text-secondary" data-testid="catalog-mirror">
          No SessionFS key needed: this catalog is mirrored to{' '}
          <a href={catalog.mirror.url} target="_blank" rel="noopener noreferrer" className="font-mono text-brand hover:underline">
            {catalog.mirror.repo}
          </a>{' '}
          on GitHub. Gemini CLI still installs from SessionFS.
        </p>
      ) : null}
      <div className="mt-5 border-t border-border pt-4">
        <p className="text-xs leading-5 text-text-secondary">
          Run this once so git can sign in to this server with your SessionFS key.
        </p>
        <div className="mt-2">
          <CommandWell command={`sfs catalog add --for ${tool ?? '<tool>'}`} label="Sign-in command" />
        </div>
        <p className="mt-3 truncate font-mono text-2xs text-text-tertiary" data-testid="catalog-clone-url" title={catalog.clone_url}>
          {catalog.marketplace} · {catalog.clone_url}
        </p>
      </div>
    </div>
  );
}

/** Header button that opens the per-tool install commands in a dialog.
 *  Renders nothing when no native catalog is available. */
export function InstallInYourTool() {
  const catalogs = useNativeCatalogs();
  const [open, setOpen] = useState(false);
  if (catalogs.isLoading || catalogs.error || !catalogs.data || catalogs.data.items.length === 0) return null;
  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>
        <TerminalIcon size={15} />
        Install in your tool
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} titleId="install-in-tool-title" className="!max-w-xl max-h-[90vh] overflow-y-auto">
        <div className="mb-3 flex items-start justify-between gap-3">
          <h2 id="install-in-tool-title" className="text-lg font-semibold text-text-primary">Install in your tool</h2>
          <IconButton label="Close" size="sm" onClick={() => setOpen(false)}>
            <CloseIcon size={16} />
          </IconButton>
        </div>
        <UseInYourTool />
      </Dialog>
    </>
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
  if (worksIn.isLoading) return <p className="mt-6 text-sm text-text-tertiary first:mt-0">Checking which tools this works in…</p>;
  if (worksIn.error) {
    return (
      <p role="status" className="mt-6 text-sm text-text-tertiary first:mt-0">
        {isTierGate(worksIn.error)
          ? 'Works-in verification of private packages is available on Team and above.'
          : `Works-in evidence unavailable: ${errorMessage(worksIn.error)}`}
      </p>
    );
  }
  if (!worksIn.data) return null;
  const rows = Object.entries(worksIn.data.tools);
  return (
    <section className="mt-6 first:mt-0" aria-labelledby="works-in-heading">
      <h3 id="works-in-heading" className="text-sm font-semibold text-text-primary">Works in</h3>
      <p className="mt-1 text-xs text-text-tertiary">
        Verified means the tool&apos;s own installer accepted it. Format valid means the catalog entry matches the tool&apos;s
        documented format (checked {worksIn.data.vendor_docs_checked}).
      </p>
      <TableShell minWidth={560} bordered wrapperClassName="mt-2" label="Works in table">
          <thead className="bg-bg-sunken text-xs text-text-tertiary">
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
