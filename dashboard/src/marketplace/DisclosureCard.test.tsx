import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { MarketplaceDisclosure } from '../api/client';
import { DisclosureCard, UpdateDiff, cardLeaves } from './DisclosureCard';

const tool = (root: string) => ({
  delivery: 'sessionfs-emission',
  scope: 'project' as const,
  summary: `Writes 2 files, adds 1 script, adds 1 hook (PreToolUse) under ${root}.`,
  writes: [`${root}/kit/SKILL.md`, `${root}/kit/scripts/run.sh`],
  writes_total: 2,
  runs: { scripts: 1, hooks: [{ skill: 'kit', event: 'PreToolUse', matcher: 'Bash' }], mcp_local: 1 },
  connects_to: ['api.example.com'],
  auto_allowed: ['Bash(git:*)'],
  omitted: [{ component: 'portable-mcp', reason: 'MCP registration requires the separate enable-mcp consent flow' }],
  consent_required: ['skill-scripts', 'skill-hooks'],
});

const disclosure: MarketplaceDisclosure = {
  version_id: 'skv_1',
  content_hash: 'sha256:abc',
  capability_digest: 'sha256:def',
  capabilities: ['instructions', 'scripts', 'hooks', 'mcp', 'network'],
  provenance: { publisher_verified: false, attested: false },
  scan: { ruleset: '2026.10.1', max_severity: 'high', finding_count: 2 },
  unicode: {
    flagged_files: 1,
    findings: [{ path: 'skills/kit/SKILL.md', line: 4, column: 7, rule_id: 'unicode.bidi_control', class: 'bidi_control', severity: 'critical', code_point: 'U+202E', visible: '⟦U+202E⟧' }],
  },
  shell_commands: [{ path: 'skills/kit/SKILL.md', line: 9, command: 'npm', commands: ['npm'], text: 'npm install -g kit' }],
  network_endpoints: ['api.example.com'],
  env_names: ['DB_URL'],
  mcp_servers: { local: [{ name: 'db', command: 'npx', env_names: ['DB_URL'] }], remote: [{ name: 'search', origin: 'https://mcp.example.com' }], declared: true },
  allowed_tools: ['allowed-tools:Bash(git:*)'],
  persistence_targets: [],
  executable_extensions: [],
  tools: { codex: tool('.codex/skills'), 'claude-code': tool('.claude/skills') },
  consent_required: ['instructions', 'skill-scripts', 'skill-hooks', 'hidden-unicode', 'scan-findings'],
};

describe('DisclosureCard', () => {
  it('renders every field the approval digest covers', () => {
    const full = {
      ...disclosure,
      scope: 'user' as const,
      card_version: 1,
      provenance: { publisher_verified: true, attested: false },
      scan: { ruleset: '2026.10.1', max_severity: 'high', finding_count: 2, counts: { high: 1, medium: 1 }, truncated: true },
      unicode: { ...disclosure.unicode, counts: { critical: 3 }, truncated: true },
      persistence_targets: ['persistence.shell_rc'],
      executable_extensions: [{ path: 'io.sessionfs.portability/vendor/claude-plugin/hooks/hooks.json', kind: 'hooks' }],
      network_endpoints_total: 5,
      incomplete: ['scan_truncated'],
      tools: { codex: { ...tool('.codex/skills'), writes_total: 4 }, 'claude-code': tool('.claude/skills') },
    };
    render(<DisclosureCard disclosure={full} />);
    expect(screen.getByTestId('disclosure-target')).toHaveTextContent('For codex, claude-code · user scope · card v1');
    expect(screen.getByTestId('disclosure-provenance')).toHaveTextContent('publisher verified · release not attested');
    expect(screen.getByTestId('disclosure-scan')).toHaveTextContent('ruleset 2026.10.1 · 2 findings · worst high · 1 high · 1 medium · truncated');
    expect(screen.getByTestId('disclosure-unicode')).toHaveTextContent('3 critical');
    expect(screen.getByTestId('disclosure-unicode')).toHaveTextContent('scan truncated');
    expect(screen.getByTestId('disclosure-unicode')).toHaveTextContent('+2 more not listed');
    expect(screen.getByTestId('disclosure-incomplete')).toHaveTextContent('scan_truncated');
    expect(screen.getByTestId('disclosure-persistence')).toHaveTextContent('persistence.shell_rc');
    expect(screen.getByTestId('disclosure-executables')).toHaveTextContent('vendor/claude-plugin/hooks/hooks.json (hooks)');
    expect(screen.getByTestId('disclosure-network')).toHaveTextContent('api.example.com (+4 more not listed)');
    expect(screen.getByTestId('disclosure-env')).toHaveTextContent('DB_URL');
    expect(screen.getByTestId('disclosure-hooks')).toHaveTextContent('kit: PreToolUse (Bash)');
    expect(screen.getByTestId('disclosure-allowed')).toHaveTextContent('Bash(git:*)');
    expect(screen.getByTestId('disclosure-mcp-local')).toHaveTextContent('db: npx');
    expect(screen.getByTestId('disclosure-mcp-remote')).toHaveTextContent('search: https://mcp.example.com');
    expect(screen.getByTestId('disclosure-shell')).toHaveTextContent('npm install -g kit');
    // Every tool's writes are on screen (no tabs hiding them), with omitted counts.
    expect(screen.getByTestId('disclosure-tool-codex')).toHaveTextContent('writes .codex/skills/kit/SKILL.md');
    expect(screen.getByTestId('disclosure-tool-codex')).toHaveTextContent('+2 more files not listed');
    expect(screen.getByTestId('disclosure-tool-claude-code')).toHaveTextContent('writes .claude/skills/kit/SKILL.md');
    expect(screen.getAllByText(/Not installed: portable-mcp/).length).toBe(2);
    expect(screen.getByText(/Consent required: scripts, hooks \/ pre-approved tools, hidden characters, scanner findings/)).toBeInTheDocument();
  });

  it('never interprets package text as markup', () => {
    const hostile = { ...disclosure, tools: { codex: { ...tool('.codex/skills'), summary: '<img src=x onerror=alert(1)>' } } };
    const { container } = render(<DisclosureCard disclosure={hostile} />);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
  });
});

describe('UpdateDiff', () => {
  const baseDiff = {
    update_class: 'widening' as const,
    baseline_known: true,
    baseline_digest: 'sha256:cap1',
    widens: [{ kind: 'new_hook', item: 'kit: PostToolUse (*)' }],
    narrows: [{ kind: 'removed_endpoint', item: 'old.example.com' }],
    code_changes: [{ kind: 'script', change: 'changed', path: 'skills/kit/scripts/run.sh' }],
    changed_requirements: ['skill-hooks'],
    capability_digest_changed: true,
    files: { added: ['skills/kit/assets/new.txt'], removed: ['skills/kit/old.md'], changed: ['skills/kit/SKILL.md'] },
    scan_delta: {
      old_max_severity: 'medium', new_max_severity: 'high',
      old_finding_count: 1, new_finding_count: 2,
      old_ruleset: '2026.9.1', new_ruleset: '2026.10.1',
    },
    unicode_delta: {
      new_findings: [{ path: 'skills/kit/SKILL.md', line: 4, column: 7, rule_id: 'unicode.bidi_control', class: 'bidi_control', severity: 'critical', code_point: 'U+202E', visible: '⟦U+202E⟧' }],
    },
    provenance_delta: {
      old: { publisher_verified: false, attested: false },
      new: { publisher_verified: true, attested: false },
    },
    consent: { personal: 'explicit_reconsent', org_required: 'never_auto_apply', description: 'The update asks for more access: it always stops and requires explicit re-consent.' },
  };

  it('labels the update class and lists widened access', () => {
    render(
      <UpdateDiff
        diff={{
          ...baseDiff,
          files: { added: [], removed: [], changed: ['skills/kit/SKILL.md'] },
          unicode_delta: { new_findings: [] },
          text_diffs: { entries: [{ path: 'skills/kit/SKILL.md', change: 'changed', binary: false, diff: '-old\n+new' }], truncated: true },
        }}
      />,
    );
    expect(screen.getByText('asks for more access')).toBeInTheDocument();
    expect(screen.getByText('+ new_hook: kit: PostToolUse (*)')).toBeInTheDocument();
    expect(screen.getByText('~ script changed: skills/kit/scripts/run.sh')).toBeInTheDocument();
    expect(screen.getByText(/1 changed/)).toBeInTheDocument();
    expect(screen.getByText('Diff truncated.')).toBeInTheDocument();
  });

  it('lists every changed file path, not just the counts', () => {
    render(<UpdateDiff diff={baseDiff} />);
    expect(screen.getByText(/1 added, 1 changed, 1 removed/)).toBeInTheDocument();
    expect(screen.getByText('+ skills/kit/assets/new.txt')).toBeInTheDocument();
    expect(screen.getByText('~ skills/kit/SKILL.md')).toBeInTheDocument();
    expect(screen.getByText('- skills/kit/old.md')).toBeInTheDocument();
    expect(screen.getByText('Baseline digest sha256:cap1 · capability digest changed')).toBeInTheDocument();
    expect(screen.getByText(/Consent required again for: skill-hooks/)).toBeInTheDocument();
  });

  it('renders every scan, unicode and provenance delta leaf', () => {
    const { container } = render(<UpdateDiff diff={baseDiff} />);
    const deltas = screen.getByTestId('update-deltas');
    const expected: [string, string][] = [
      ['scan_delta.new_finding_count', '2'],
      ['scan_delta.new_max_severity', 'high'],
      ['scan_delta.new_ruleset', '2026.10.1'],
      ['scan_delta.old_finding_count', '1'],
      ['scan_delta.old_max_severity', 'medium'],
      ['scan_delta.old_ruleset', '2026.9.1'],
      ['unicode_delta.new_findings[0].code_point', 'U+202E'],
      ['unicode_delta.new_findings[0].path', 'skills/kit/SKILL.md'],
      ['unicode_delta.new_findings[0].rule_id', 'unicode.bidi_control'],
      ['provenance_delta.new.publisher_verified', 'true'],
      ['provenance_delta.old.attested', 'false'],
    ];
    for (const [path, value] of expected) {
      const item = container.querySelector(`[data-leaf="${CSS.escape(path)}"]`);
      expect(item, path).not.toBeNull();
      expect(item!.textContent).toBe(`${path} = ${value}`);
      expect(deltas.contains(item)).toBe(true);
    }
  });

  it('says the baseline is unknown instead of fabricating one', () => {
    render(
      <UpdateDiff
        diff={{
          ...baseDiff,
          baseline_known: false,
          baseline_digest: undefined,
          widens: [{ kind: 'unknown_baseline', item: "the installed version's access is unknown" }],
          files: { added: [], removed: [], changed: [], unknown: true },
        }}
      />,
    );
    expect(screen.getByText(/Baseline unknown/)).toBeInTheDocument();
    expect(screen.getByText(/Files changed: unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/Baseline digest/)).not.toBeInTheDocument();
  });
});

describe('DisclosureCard exhaustive fields', () => {
  it('renders every leaf of the card JSON, path and value', () => {
    const full = { ...disclosure, card_version: 1, scope: 'project' as const, disclosure_digest: 'sha256:card' };
    const walked: [string, string][] = [];
    const walk = (value: unknown, path: string) => {
      if (Array.isArray(value)) {
        if (value.length === 0) walked.push([path, '[]']);
        value.forEach((item, index) => walk(item, `${path}[${index}]`));
      } else if (value !== null && typeof value === 'object') {
        const keys = Object.keys(value as object);
        if (keys.length === 0) walked.push([path, '{}']);
        keys.forEach((key) => walk((value as Record<string, unknown>)[key], path ? `${path}.${key}` : key));
      } else walked.push([path, typeof value === 'string' ? value : JSON.stringify(value)]);
    };
    walk(full, '');
    expect(cardLeaves(full).length).toBe(walked.length);
    const { container } = render(<DisclosureCard disclosure={full} />);
    const fields = screen.getByTestId('disclosure-fields');
    for (const [path, value] of walked) {
      const item = container.querySelector(`[data-leaf="${CSS.escape(path)}"]`);
      expect(item, path).not.toBeNull();
      expect(item!.textContent).toBe(`${path} = ${value}`);
      expect(fields.contains(item)).toBe(true);
    }
  });
});
