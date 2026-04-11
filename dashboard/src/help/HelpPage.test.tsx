import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import HelpPage from './HelpPage';

describe('HelpPage', () => {
  beforeEach(() => {
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
    expect(screen.getByText(/Codex will now have access to 12 tools/)).toBeInTheDocument();
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

  it('exposes all 8 supported AI tool pills', () => {
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

  it('renders all 5 "what you can tell your agent" use-case cards', () => {
    renderPage();
    for (const title of [
      'Find & Resume',
      'Team Handoff',
      'Audit & Verify',
      'Project Knowledge',
      'Sync & Manage',
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
    expect(screen.getByText('sfs handoff <id> <email>')).toBeInTheDocument();
    expect(screen.getByText('sfs dlp scan <id>')).toBeInTheDocument();
    expect(screen.getByText('sfs audit <id>')).toBeInTheDocument();
    expect(screen.getByText('sfs project edit')).toBeInTheDocument();
  });

  it('lists all 12 MCP tools', () => {
    renderPage();
    const expected = [
      'search_sessions',
      'get_session_context',
      'list_recent_sessions',
      'find_related_sessions',
      'get_session_summary',
      'get_audit_report',
      'get_project_context',
      'search_project_knowledge',
      'ask_project',
      'add_knowledge',
      'update_wiki_page',
      'list_wiki_pages',
    ];
    for (const name of expected) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
  });

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
