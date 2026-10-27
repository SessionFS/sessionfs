import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import type { NativeCatalogList, WorksIn } from '../api/client';
import { InstallInYourTool, UseInYourTool, WorksInMatrix } from './NativeCatalogs';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useNativeCatalogs: vi.fn(),
    useWorksIn: vi.fn(),
    addToast: vi.fn(),
  },
}));

vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));

vi.mock('../hooks/useNativeCatalogs', () => ({
  useNativeCatalogs: () => hooks.useNativeCatalogs(),
  useWorksIn: (versionId?: string, enabled?: boolean) => hooks.useWorksIn(versionId, enabled),
}));

const clone = 'https://api.sessionfs.dev/api/v1/git/public/sessionfs.git';
const catalogs: NativeCatalogList = {
  credential_helper: 'sfs catalog credential',
  vendor_docs_checked: '2026-10-20',
  items: [
    {
      scope: 'public', scope_id: null, marketplace: 'sessionfs', clone_url: clone,
      current_commit: 'a'.repeat(40), generated_at: null, plugin_count: 3, export_remote: null,
      tools: {
        'claude-code': { add: `/plugin marketplace add ${clone}`, install: '/plugin install <plugin>@sessionfs' },
        gemini: { install: 'gemini extensions install https://api.sessionfs.dev/api/v1/git/public/plugins/<plugin>.git' },
      },
    },
    {
      scope: 'org', scope_id: 'org_1', marketplace: 'sessionfs-org-org-1',
      clone_url: 'https://api.sessionfs.dev/api/v1/git/org/org_1/catalog.git',
      current_commit: null, generated_at: null, plugin_count: null, export_remote: null,
      tools: { copilot: { add: 'copilot plugin marketplace add https://api.sessionfs.dev/api/v1/git/org/org_1/catalog.git' } },
    },
  ],
};

const worksIn: WorksIn = {
  version_id: 'skv_1',
  content_hash: 'sha256:abc',
  vendor_docs_checked: '2026-10-20',
  tools: {
    'claude-code': {
      badge: 'verified',
      levels: {
        L0: { status: 'pass', tool_version: 'docs-2026-10-20', run_at: '2026-10-20T00:00:00+00:00', reason: 'ok', projection_hash: null },
        L1: { status: 'pass', tool_version: '2.1.240', run_at: '2026-10-20T01:00:00+00:00', reason: 'ok', projection_hash: null },
      },
    },
    cursor: {
      badge: 'format-invalid',
      levels: { L0: { status: 'fail', tool_version: 'docs-2026-10-20', run_at: null, reason: 'plugin name must be lowercase', projection_hash: null } },
    },
    gemini: { badge: 'not-applicable', levels: {} },
  },
  expected_only: { amp: { badge: 'expected', omitted_components: ['mcp'] } },
};

describe('UseInYourTool', () => {
  beforeEach(() => {
    hooks.useNativeCatalogs.mockReturnValue({ data: catalogs, isLoading: false, error: null });
  });

  it("shows each tool's own add command and switches tool and scope", async () => {
    const user = userEvent.setup();
    render(<UseInYourTool />);
    expect(screen.getByTestId('use-in-claude-code')).toHaveTextContent(`/plugin marketplace add ${clone}`);
    await user.click(screen.getByRole('tab', { name: 'Gemini CLI' }));
    expect(screen.getByTestId('use-in-gemini')).toHaveTextContent('gemini extensions install');
    expect(screen.queryByTestId('use-in-claude-code')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Catalog/ }));
    await user.click(screen.getByRole('option', { name: 'Org org_1' }));
    expect(screen.getByTestId('catalog-clone-url')).toHaveTextContent('sessionfs-org-org-1');
    expect(screen.queryByRole('tab', { name: 'Gemini CLI' })).not.toBeInTheDocument();
    expect(screen.getByTestId('use-in-copilot')).toHaveTextContent('copilot plugin marketplace add');
  });

  it('keeps long commands on one line in a scrolling well with a Copy button that toasts', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<UseInYourTool />);
    // The command is one untouched preformatted text the keyboard can reach
    // (so a long one scrolls inside its well instead of being broken up).
    const well = screen.getByRole('group', { name: 'Claude Code Add the catalog' });
    expect(well.tagName).toBe('PRE');
    expect(well).toHaveAttribute('tabindex', '0');
    expect(well.textContent).toBe(`/plugin marketplace add ${clone}`);
    expect(well.childNodes).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Copy: Claude Code Add the catalog' }));
    expect(writeText).toHaveBeenCalledWith(`/plugin marketplace add ${clone}`);
    expect(hooks.addToast).toHaveBeenCalledWith('success', 'Command copied');
    // The sign-in command follows the selected tool.
    expect(screen.getByRole('group', { name: 'Sign-in command' })).toHaveTextContent('sfs catalog add --for claude-code');
  });

  it('renders instruction sentences as prose without a Copy button', async () => {
    const user = userEvent.setup();
    hooks.useNativeCatalogs.mockReturnValue({
      data: {
        ...catalogs,
        items: [{
          ...catalogs.items[0],
          tools: {
            codex: { install: 'Open Codex, run /plugins and choose the sessionfs marketplace.' },
            cursor: { add: 'In Cursor Settings → Plugins, add the marketplace URL above.' },
            antigravity: { add: 'Add the clone URL in Antigravity → Extensions → Sources.' },
          },
        }],
      },
      isLoading: false,
      error: null,
    });
    render(<UseInYourTool />);
    expect(screen.getByTestId('step-prose-install')).toHaveTextContent('Open Codex, run /plugins');
    expect(screen.queryByRole('button', { name: /^Copy: Codex/ })).toBeNull();
    await user.click(screen.getByRole('tab', { name: 'Cursor' }));
    expect(screen.getByTestId('step-prose-add')).toHaveTextContent('In Cursor Settings');
    expect(screen.queryByRole('button', { name: /^Copy: Cursor/ })).toBeNull();
    await user.click(screen.getByRole('tab', { name: 'Antigravity' }));
    expect(screen.getByTestId('step-prose-add').tagName).toBe('P');
    expect(screen.queryByRole('button', { name: /^Copy: Antigravity/ })).toBeNull();
    // The sign-in command is still a real command.
    expect(screen.getByRole('button', { name: 'Copy: Sign-in command' })).toBeInTheDocument();
  });

  it('opens from a header button into a dialog', async () => {
    const user = userEvent.setup();
    render(<InstallInYourTool />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Install in your tool' }));
    const dialog = screen.getByRole('dialog', { name: 'Install in your tool' });
    expect(dialog).toContainElement(screen.getByTestId('use-in-claude-code'));
    await user.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows no install button when no catalog is available', () => {
    hooks.useNativeCatalogs.mockReturnValue({ data: { ...catalogs, items: [] }, isLoading: false, error: null });
    const { container } = render(<InstallInYourTool />);
    expect(container).toBeEmptyDOMElement();
  });

  it('leads with the keyless GitHub mirror and keeps the hosted command', () => {
    const mirrored: NativeCatalogList = {
      ...catalogs,
      items: [
        {
          ...catalogs.items[0],
          mirror: { repo: 'SessionFS/marketplace', url: 'https://github.com/SessionFS/marketplace', branch: 'main' },
          tools: {
            'claude-code': {
              add: '/plugin marketplace add SessionFS/marketplace',
              install: '/plugin install <plugin>@sessionfs',
              add_hosted: `/plugin marketplace add ${clone}`,
            },
          },
        },
      ],
    };
    hooks.useNativeCatalogs.mockReturnValue({ data: mirrored, isLoading: false, error: null });
    render(<UseInYourTool />);
    const link = screen.getByRole('link', { name: 'SessionFS/marketplace' });
    expect(link).toHaveAttribute('href', 'https://github.com/SessionFS/marketplace');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    const card = screen.getByTestId('use-in-claude-code');
    expect(card).toHaveTextContent('/plugin marketplace add SessionFS/marketplace');
    expect(card).toHaveTextContent('Or add from SessionFS');
    expect(card).toHaveTextContent(`/plugin marketplace add ${clone}`);
  });

  it('shows no mirror line without one', () => {
    render(<UseInYourTool />);
    expect(screen.queryByTestId('catalog-mirror')).not.toBeInTheDocument();
  });

  it('renders nothing while unavailable', () => {
    hooks.useNativeCatalogs.mockReturnValue({ data: undefined, isLoading: false, error: new Error('x') });
    const { container } = render(<UseInYourTool />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('WorksInMatrix', () => {
  it('separates harness verification from format checks and expectations', () => {
    hooks.useWorksIn.mockReturnValue({ data: worksIn, isLoading: false, error: null });
    render(<WorksInMatrix versionId="skv_1" enabled />);
    expect(screen.getByTestId('works-in-claude-code')).toHaveTextContent('Verified');
    expect(screen.getByTestId('works-in-claude-code')).toHaveTextContent('L1 · 2.1.240 · 2026-10-20');
    expect(screen.getByTestId('works-in-cursor')).toHaveTextContent('Format invalid');
    expect(screen.getByTestId('works-in-cursor')).toHaveTextContent('plugin name must be lowercase');
    expect(screen.getByTestId('works-in-gemini')).toHaveTextContent('Public mirror only');
    expect(screen.getByTestId('works-in-amp')).toHaveTextContent('Expected');
  });

  it('explains the Team gate for private packages', () => {
    hooks.useWorksIn.mockReturnValue({
      data: undefined, isLoading: false,
      error: new ApiError(403, JSON.stringify({ error: { code: 'upgrade_required', message: 'Team' } })),
    });
    render(<WorksInMatrix versionId="skv_1" enabled />);
    expect(screen.getByRole('status')).toHaveTextContent('available on Team and above');
  });

  it('does nothing for a non-deliverable version', () => {
    hooks.useWorksIn.mockReturnValue({ data: worksIn, isLoading: false, error: null });
    const { container } = render(<WorksInMatrix versionId="skv_1" enabled={false} />);
    expect(container).toBeEmptyDOMElement();
  });
});
