import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import HelpPage from './HelpPage';
import mcpToolData from './mcpTools.json';
import { axe } from '../test/axe';

const { openHelperMock, helperState } = vi.hoisted(() => ({
  openHelperMock: vi.fn(),
  helperState: { available: true },
}));
vi.mock('../helper/HelperProvider', () => ({
  useHelper: () => ({ openHelper: helperState.available ? openHelperMock : undefined }),
}));

const TOOL_COUNT = mcpToolData.groups.reduce((n, g) => n + g.tools.length, 0);

describe('HelpPage', () => {
  beforeEach(() => {
    openHelperMock.mockReset();
    helperState.available = true;
    // localStorage and data-theme are reset by the shared setup.ts.
    // jsdom doesn't ship navigator.clipboard — stub it here per-test so each
    // test can override it (e.g. to test the unavailable-API path).
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
    });
  });

  function renderPage() {
    return render(
      <MemoryRouter>
        <HelpPage />
      </MemoryRouter>,
    );
  }

  it('renders the hero and MCP-first headline', () => {
    renderPage();
    expect(screen.getByText(/you don't need to memorize commands/i)).toBeInTheDocument();
    expect(
      screen.getByText(/install the mcp server for your ai tool/i),
    ).toBeInTheDocument();
  });

  it('defaults to Claude Code with its install command', () => {
    renderPage();
    const claudePill = screen.getByRole('tab', { name: 'Claude Code' });
    expect(claudePill).toHaveAttribute('aria-selected', 'true');
    // Terminal shows the selected tool's command — CLI uses `--for <tool>` flag
    expect(screen.getByText(/sfs mcp install --for claude-code/)).toBeInTheDocument();
  });

  it('switches the terminal command when a different tool is selected', () => {
    renderPage();
    // Verify Codex is not initially selected
    expect(screen.queryByText(/sfs mcp install --for codex/)).not.toBeInTheDocument();
    // Click Codex pill
    fireEvent.click(screen.getByRole('tab', { name: 'Codex' }));
    // Terminal should now show the Codex command and output
    expect(screen.getByText(/sfs mcp install --for codex/)).toBeInTheDocument();
    expect(
      screen.getByText(new RegExp(`Codex will now have access to ${TOOL_COUNT} tools`)),
    ).toBeInTheDocument();
    // Codex pill should be marked selected, Claude should not
    expect(screen.getByRole('tab', { name: 'Codex' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('tab', { name: 'Claude Code' })).toHaveAttribute(
      'aria-selected',
      'false',
    );
  });

  it('exposes all 10 supported AI tool pills', () => {
    renderPage();
    const expected = [
      'Claude Code',
      'Codex',
      'Gemini CLI',
      'Cursor',
      'Copilot CLI',
      'Amp',
      'Cline',
      'Roo Code',
      'Kilo Code',
      'Antigravity',
    ];
    for (const label of expected) {
      expect(screen.getByRole('tab', { name: label })).toBeInTheDocument();
    }
  });

  it('copies the selected install command to clipboard', async () => {
    renderPage();
    fireEvent.click(screen.getByRole('tab', { name: 'Gemini CLI' }));
    fireEvent.click(screen.getByRole('button', { name: /copy command/i }));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        'sfs mcp install --for gemini',
      );
    });
    // Button flips to "Copied" on success
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /copy command/i })).toHaveTextContent(
        /copied/i,
      );
    });
  });

  it('shows "Copy failed" when the clipboard API is unavailable', async () => {
    // Simulate secure-context-less environment: no clipboard API at all
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    });
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: /copy command/i }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /copy command/i })).toHaveTextContent(
        /copy failed/i,
      );
    });
  });

  it('shows "Copy failed" when clipboard.writeText rejects', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: vi.fn().mockRejectedValue(new Error('Permission denied')),
      },
    });
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: /copy command/i }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /copy command/i })).toHaveTextContent(
        /copy failed/i,
      );
    });
  });

  it('renders the "what you can tell your agent" use-case cards', () => {
    renderPage();
    for (const title of [
      'Find & Resume',
      'Team Handoff',
      'Audit & Verify',
      'Project Knowledge',
      'Tickets & Agents',
      'Sessions & Checkpoints',
      'Project Rules',
      'Skills Marketplace',
    ]) {
      expect(screen.getByRole('heading', { name: title })).toBeInTheDocument();
    }
  });

  it('renders the curated CLI quick-reference rows', () => {
    renderPage();
    // `sfs list` and `sfs push <id>` appear in both the getting-started terminal
    // and the CLI table, so allow duplicates via getAllByText.
    expect(screen.getAllByText('sfs list').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('sfs push <id>').length).toBeGreaterThanOrEqual(1);
    // These only appear in the CLI table, so single-match is safe
    expect(screen.getByText('sfs handoff <id> --to <email>')).toBeInTheDocument();
    expect(screen.getByText('sfs dlp scan <id>')).toBeInTheDocument();
    expect(screen.getByText('sfs audit <id>')).toBeInTheDocument();
    expect(screen.getByText('sfs project edit')).toBeInTheDocument();
  });

  it('shows the MCP tool count from the data file', () => {
    renderPage();
    expect(TOOL_COUNT).toBeGreaterThan(80);
    expect(
      screen.getByRole('heading', { level: 2, name: `MCP Tools (${TOOL_COUNT})` }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(`${TOOL_COUNT} tools in ${mcpToolData.groups.length} groups`),
    ).toBeInTheDocument();
  });

  it('lists every MCP tool once all groups are expanded', () => {
    renderPage();
    // Collapsed by default: tool names are not rendered yet.
    expect(screen.queryByText('watch_ticket')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    // One DOM pass instead of 85 getByText scans keeps this fast under load.
    const rendered = new Set(
      Array.from(document.querySelectorAll('li > code')).map((el) => el.textContent),
    );
    const expected = mcpToolData.groups.flatMap((g) => g.tools.map((t) => t.name));
    expect(expected.filter((name) => !rendered.has(name))).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }));
    expect(screen.queryByText('watch_ticket')).not.toBeInTheDocument();
  });

  it('expands and collapses a single tool group', () => {
    renderPage();
    const toggle = screen.getByRole('button', { name: /^Handoffs/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('list', { name: 'Handoffs tools' })).toBeInTheDocument();
    expect(screen.getByText('claim_handoff')).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('claim_handoff')).not.toBeInTheDocument();
  });

  it('filters the tool list and opens the matching groups', () => {
    renderPage();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter MCP tools' }), {
      target: { value: 'handoff' },
    });
    expect(screen.getByText('claim_handoff')).toBeInTheDocument();
    expect(screen.queryByText('watch_ticket')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Work queues/ })).not.toBeInTheDocument();
    expect(screen.getByText(/of \d+ tools match/)).toBeInTheDocument();

    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter MCP tools' }), {
      target: { value: 'zzz-no-such-tool' },
    });
    expect(screen.getByText(/No tools match/)).toBeInTheDocument();
  });

  it('renders the session helper section with a docs link and the settings route', () => {
    renderPage();
    expect(screen.getByRole('heading', { level: 2, name: 'Session helper' })).toBeInTheDocument();
    for (const title of [
      'Clickable references',
      'Actions you confirm',
      'Answers from the docs',
      'Trash and history',
    ]) {
      expect(screen.getByRole('heading', { name: title })).toBeInTheDocument();
    }
    expect(screen.getByRole('link', { name: /session helper docs/i })).toHaveAttribute(
      'href',
      expect.stringContaining('https://sessionfs.dev/session-helper/'),
    );
    expect(screen.getByRole('link', { name: 'Settings → Assistant' })).toHaveAttribute(
      'href',
      '/settings/assistant',
    );
  });

  it('opens the helper drawer from the "Ask the helper" button', () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Ask the helper' }));
    expect(openHelperMock).toHaveBeenCalledTimes(1);
    expect(openHelperMock).toHaveBeenCalledWith();
  });

  it('hides the "Ask the helper" button when the drawer is unavailable', () => {
    helperState.available = false;
    renderPage();
    expect(screen.queryByRole('button', { name: 'Ask the helper' })).not.toBeInTheDocument();
  });

  it('has no axe violations with the tool groups expanded', async () => {
    const { container } = render(
      <MemoryRouter>
        <main>
          <HelpPage />
        </main>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    expect(await axe(container)).toHaveNoViolations();
  }, 20000);

  it('passes the theme query parameter on sessionfs.dev resource links', () => {
    // Prime localStorage so the initial theme read is deterministic
    window.localStorage.setItem('sfs-theme', 'dark');
    renderPage();
    const docsLink = screen.getByRole('link', { name: /full documentation/i });
    const href = docsLink.getAttribute('href') ?? '';
    expect(href).toContain('https://sessionfs.dev/quickstart/');
    expect(href).toContain('theme=dark');
  });

  it('updates resource link theme parameter when data-theme attribute changes', async () => {
    window.localStorage.setItem('sfs-theme', 'dark');
    document.documentElement.setAttribute('data-theme', 'dark');
    renderPage();

    // Initial href uses dark
    expect(
      screen.getByRole('link', { name: /full documentation/i }).getAttribute('href'),
    ).toContain('theme=dark');

    // Simulate the user toggling to light — ThemeToggle writes data-theme in an effect
    document.documentElement.setAttribute('data-theme', 'light');

    await waitFor(() => {
      expect(
        screen.getByRole('link', { name: /full documentation/i }).getAttribute('href'),
      ).toContain('theme=light');
    });
  });

  it('has external resource links configured with rel=noopener', () => {
    renderPage();
    const githubLink = screen.getByRole('link', { name: /report an issue/i });
    expect(githubLink).toHaveAttribute(
      'href',
      'https://github.com/SessionFS/sessionfs/issues',
    );
    expect(githubLink).toHaveAttribute('target', '_blank');
    expect(githubLink).toHaveAttribute('rel', expect.stringContaining('noopener'));
  });

  it('does not contain forbidden brand references', () => {
    const { container } = renderPage();
    const text = container.textContent ?? '';
    expect(text.toLowerCase()).not.toContain('dropbox');
    expect(text.toLowerCase()).not.toContain('alwaysnix');
  });
});
