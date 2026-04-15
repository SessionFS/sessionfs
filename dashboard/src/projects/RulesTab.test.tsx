import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import RulesTab from './RulesTab';

/**
 * UI coverage for the v0.9.9 Rules tab. Hook internals are mocked one
 * layer up (at `../hooks/useRules` and `../hooks/useToast`) so the tests
 * focus on the UI, not react-query wiring.
 */

const { hooks, mockAddToast } = vi.hoisted(() => ({
  hooks: {
    useProjectRules: vi.fn(),
    useUpdateProjectRules: vi.fn(),
    useCompileRules: vi.fn(),
    useRulesVersions: vi.fn(),
    useRulesVersion: vi.fn(),
    isStaleEtagError: vi.fn(),
  },
  mockAddToast: vi.fn(),
}));

vi.mock('../hooks/useRules', () => hooks);

vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));

function makeMutation(extra: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
    ...extra,
  };
}

function defaultRules() {
  return {
    static_rules: '# Project preferences\n\nPrefer TypeScript.',
    include_knowledge: true,
    knowledge_types: ['decision', 'convention'],
    knowledge_max_tokens: 4000,
    include_context: true,
    context_sections: ['overview', 'architecture'],
    context_max_tokens: 2000,
    tool_overrides: {},
    enabled_tools: ['claude-code', 'codex'],
    version: 3,
    updated_at: '2026-04-10T12:00:00Z',
  };
}

function defaultVersionDetail() {
  return {
    version: 3,
    compiled_at: '2026-04-10T12:00:00Z',
    content_hash: 'abcdef0123456789',
    compiled_by: 'user@example.com',
    static_rules: '# Project preferences\n\nPrefer TypeScript.',
    compiled_outputs: {
      'claude-code': {
        filename: 'CLAUDE.md',
        content: '# SessionFS-managed\n\nCompiled content for Claude Code.',
        token_count: 512,
      },
      codex: {
        filename: 'codex.md',
        content: '# SessionFS-managed\n\nCompiled content for Codex.',
        token_count: 480,
      },
    },
  };
}

beforeEach(() => {
  for (const h of Object.values(hooks)) h.mockReset();
  mockAddToast.mockReset();

  hooks.useProjectRules.mockReturnValue({
    data: { data: defaultRules(), etag: 'W/"v3"' },
    isLoading: false,
    error: null,
  });
  hooks.useUpdateProjectRules.mockReturnValue(makeMutation());
  hooks.useCompileRules.mockReturnValue(makeMutation());
  hooks.useRulesVersions.mockReturnValue({
    data: {
      versions: [
        {
          version: 3,
          compiled_at: '2026-04-10T12:00:00Z',
          content_hash: 'abcdef0123456789',
          compiled_by: 'user@example.com',
        },
        {
          version: 2,
          compiled_at: '2026-04-01T12:00:00Z',
          content_hash: '1234567890abcdef',
          compiled_by: 'user@example.com',
        },
      ],
    },
    isLoading: false,
  });
  hooks.useRulesVersion.mockReturnValue({ data: defaultVersionDetail(), isLoading: false });
  hooks.isStaleEtagError.mockImplementation(
    (err: unknown) => err instanceof ApiError && err.status === 409,
  );
});

describe('RulesTab', () => {
  it('shows a loading state while rules fetch', () => {
    hooks.useProjectRules.mockReturnValue({ data: undefined, isLoading: true, error: null });
    render(<RulesTab projectId="sessionfs/sessionfs" />);
    expect(screen.getByText(/loading rules/i)).toBeInTheDocument();
  });

  it('shows an error state when rules fail to load', () => {
    hooks.useProjectRules.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('boom'),
    });
    render(<RulesTab projectId="sessionfs/sessionfs" />);
    expect(screen.getByText(/failed to load rules/i)).toBeInTheDocument();
  });

  it('renders all sections when data loads', () => {
    render(<RulesTab projectId="sessionfs/sessionfs" />);

    // Current version badge appears (v3 is shown in header + history list,
    // so just assert at least one is present)
    expect(screen.getAllByText('v3').length).toBeGreaterThan(0);
    // Section headings
    expect(screen.getByRole('heading', { name: /project rules/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /static preferences/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /enabled tools/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /knowledge injection/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /context injection/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /compiled outputs/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /version history/i })).toBeInTheDocument();

    // Static preferences rendered read-only
    expect(screen.getByText(/prefer typescript/i)).toBeInTheDocument();
    // Tool checkboxes
    expect(screen.getByRole('checkbox', { name: /enable claude code/i })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /enable cursor/i })).not.toBeChecked();

    // Compiled output cards (per enabled tool)
    expect(screen.getByText('CLAUDE.md')).toBeInTheDocument();
    expect(screen.getByText('codex.md')).toBeInTheDocument();
  });

  it('clicking Compile calls the compile mutation', async () => {
    const compile = makeMutation();
    hooks.useCompileRules.mockReturnValue(compile);

    render(<RulesTab projectId="sessionfs/sessionfs" />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^compile$/i }));
    expect(compile.mutate).toHaveBeenCalledWith(undefined, expect.any(Object));
  });

  it('clicking a version row opens the version modal with compiled outputs', async () => {
    render(<RulesTab projectId="sessionfs/sessionfs" />);
    const user = userEvent.setup();

    // Click the v2 row in the version list.
    const versionButtons = screen.getAllByRole('button', { name: /v[23]/ });
    // There are two version buttons in the history list; grab the second (v2).
    const v2Button = versionButtons.find((b) => within(b).queryByText('v2'));
    expect(v2Button).toBeDefined();
    await user.click(v2Button!);

    // Modal appears with role=dialog and compiled content
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByText(/compiled content for claude code/i)).toBeInTheDocument();
  });

  it('stale ETag (409) triggers a refresh toast on save', async () => {
    const update = makeMutation();
    update.mutate = vi.fn((_vars, opts: { onError?: (e: unknown) => void }) => {
      opts.onError?.(new ApiError(409, 'stale'));
    });
    hooks.useUpdateProjectRules.mockReturnValue(update);

    render(<RulesTab projectId="sessionfs/sessionfs" />);
    const user = userEvent.setup();

    // Enter edit mode, change text, save.
    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    const textarea = await screen.findByRole('textbox');
    await user.clear(textarea);
    await user.type(textarea, 'new content');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(mockAddToast).toHaveBeenCalledWith(
        'error',
        expect.stringMatching(/refresh and try again/i),
      );
    });
  });

  it('toggling a tool checkbox persists via the update mutation', async () => {
    const update = makeMutation();
    hooks.useUpdateProjectRules.mockReturnValue(update);

    render(<RulesTab projectId="sessionfs/sessionfs" />);
    const user = userEvent.setup();

    // Enable Cursor (currently off)
    await user.click(screen.getByRole('checkbox', { name: /enable cursor/i }));

    expect(update.mutate).toHaveBeenCalled();
    const [args] = update.mutate.mock.calls[0];
    expect(args.etag).toBe('W/"v3"');
    expect(args.rules.enabled_tools).toEqual(
      expect.arrayContaining(['claude-code', 'codex', 'cursor']),
    );
  });

  it('toggling a knowledge type checkbox persists via the update mutation', async () => {
    const update = makeMutation();
    hooks.useUpdateProjectRules.mockReturnValue(update);

    render(<RulesTab projectId="sessionfs/sessionfs" />);
    const user = userEvent.setup();

    // "Pattern" is not in defaultRules().knowledge_types
    await user.click(screen.getByRole('checkbox', { name: /knowledge type pattern/i }));

    expect(update.mutate).toHaveBeenCalled();
    const [args] = update.mutate.mock.calls[0];
    expect(args.rules.knowledge_types).toEqual(
      expect.arrayContaining(['decision', 'convention', 'pattern']),
    );
  });

  it('clicking View on a compiled-output card opens the single-tool modal with a Copy button', async () => {
    render(<RulesTab projectId="sessionfs/sessionfs" />);
    const user = userEvent.setup();

    // The Compiled outputs section has two cards (claude-code + codex);
    // both have a "View" button. Click the first one.
    const viewButtons = screen.getAllByRole('button', { name: /^view$/i });
    await user.click(viewButtons[0]);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('CLAUDE.md')).toBeInTheDocument();
    // Copy button is present in the modal
    expect(within(dialog).getByRole('button', { name: /copy/i })).toBeInTheDocument();
    // Content is rendered
    expect(within(dialog).getByText(/compiled content for claude code/i)).toBeInTheDocument();
  });

  it('shows empty-state when no versions exist', () => {
    hooks.useRulesVersions.mockReturnValue({ data: { versions: [] }, isLoading: false });
    // No version to fetch either
    hooks.useRulesVersion.mockReturnValue({ data: undefined, isLoading: false });

    render(<RulesTab projectId="sessionfs/sessionfs" />);
    expect(screen.getByText(/no versions compiled yet/i)).toBeInTheDocument();
    expect(screen.getByText(/^no versions yet\.?$/i)).toBeInTheDocument();
  });

  it('shows successful compile toast with new version', async () => {
    const compile = makeMutation();
    compile.mutate = vi.fn(
      (_vars, opts: { onSuccess?: (r: unknown) => void }) => {
        opts.onSuccess?.({ version: 4, created_new_version: true, aggregate_hash: 'xyz', outputs: [] });
      },
    );
    hooks.useCompileRules.mockReturnValue(compile);

    render(<RulesTab projectId="sessionfs/sessionfs" />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /^compile$/i }));

    await waitFor(() => {
      expect(mockAddToast).toHaveBeenCalledWith(
        'success',
        expect.stringMatching(/compiled v4/i),
      );
    });
  });

  it('shows "no changes" info toast when compile reports changed=false', async () => {
    const compile = makeMutation();
    compile.mutate = vi.fn(
      (_vars, opts: { onSuccess?: (r: unknown) => void }) => {
        opts.onSuccess?.({ version: 3, created_new_version: false, aggregate_hash: 'abc', outputs: [] });
      },
    );
    hooks.useCompileRules.mockReturnValue(compile);

    render(<RulesTab projectId="sessionfs/sessionfs" />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /^compile$/i }));

    await waitFor(() => {
      expect(mockAddToast).toHaveBeenCalledWith(
        'info',
        expect.stringMatching(/no changes/i),
      );
    });
  });

  it('renders tool_overrides as read-only JSON when present', () => {
    hooks.useProjectRules.mockReturnValue({
      data: {
        data: { ...defaultRules(), tool_overrides: { 'claude-code': { max_tokens: 2000 } } },
        etag: 'W/"v3"',
      },
      isLoading: false,
      error: null,
    });
    render(<RulesTab projectId="sessionfs/sessionfs" />);
    expect(screen.getByRole('heading', { name: /tool overrides/i })).toBeInTheDocument();
    expect(screen.getByText(/max_tokens/)).toBeInTheDocument();
    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
  });
});
