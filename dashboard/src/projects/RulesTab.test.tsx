import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
    mutateAsync: vi.fn().mockResolvedValue(undefined),
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

    expect(update.mutateAsync).toHaveBeenCalled();
    const [args] = update.mutateAsync.mock.calls[0];
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

    expect(update.mutateAsync).toHaveBeenCalled();
    const [args] = update.mutateAsync.mock.calls[0];
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

  describe('Max tokens input debounce', () => {
    // Regression for v0.9.9.10 post-release report: typing in the
    // Max tokens fields fired one PUT /rules per keystroke. The
    // first succeeded, subsequent keystrokes 409'd on stale etag,
    // and the dashboard "froze" through a refetch + toast storm.
    // DebouncedTokenInput coalesces typing into a single mutation
    // 600ms after the user stops.
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: false });
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it('coalesces rapid edits into one mutation per pause', async () => {
      const update = makeMutation();
      hooks.useUpdateProjectRules.mockReturnValue(update);

      render(<RulesTab projectId="sessionfs/sessionfs" />);

      const input = screen.getByRole('spinbutton', {
        name: /knowledge injection max tokens/i,
      }) as HTMLInputElement;
      expect(input.value).toBe('4000');

      // Simulate typing 8000 across four keystrokes — fireEvent.change
      // re-runs onChange the same way React would for sequential
      // input events.
      fireEvent.change(input, { target: { value: '8' } });
      fireEvent.change(input, { target: { value: '80' } });
      fireEvent.change(input, { target: { value: '800' } });
      fireEvent.change(input, { target: { value: '8000' } });

      // Before the debounce fires: zero mutations.
      expect(update.mutateAsync).not.toHaveBeenCalled();

      act(() => {
        vi.advanceTimersByTime(600);
      });

      // After the debounce: exactly one mutation with the final value.
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
      expect(update.mutateAsync).toHaveBeenCalledWith({
        rules: { knowledge_max_tokens: 8000 },
        etag: 'W/"v3"',
      });
    });

    it('debounces the context injection input independently', () => {
      const update = makeMutation();
      hooks.useUpdateProjectRules.mockReturnValue(update);

      render(<RulesTab projectId="sessionfs/sessionfs" />);

      const input = screen.getByRole('spinbutton', {
        name: /context injection max tokens/i,
      }) as HTMLInputElement;

      fireEvent.change(input, { target: { value: '5' } });
      fireEvent.change(input, { target: { value: '50' } });
      fireEvent.change(input, { target: { value: '500' } });

      expect(update.mutateAsync).not.toHaveBeenCalled();
      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
      expect(update.mutateAsync).toHaveBeenCalledWith({
        rules: { context_max_tokens: 500 },
        etag: 'W/"v3"',
      });
    });

    it('flushes pending change on blur instead of waiting for the timer', () => {
      const update = makeMutation();
      hooks.useUpdateProjectRules.mockReturnValue(update);

      render(<RulesTab projectId="sessionfs/sessionfs" />);

      const input = screen.getByRole('spinbutton', {
        name: /knowledge injection max tokens/i,
      }) as HTMLInputElement;

      fireEvent.change(input, { target: { value: '7500' } });
      fireEvent.blur(input);

      // Blur fires the commit synchronously — no timer advance needed.
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
      expect(update.mutateAsync).toHaveBeenCalledWith({
        rules: { knowledge_max_tokens: 7500 },
        etag: 'W/"v3"',
      });

      // Subsequent timer tick must NOT fire a second mutation.
      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
    });

    it('awaits the patch mutation before launching Compile', async () => {
      // Round-2 reviewer (KB entry 216, MEDIUM) flagged that without
      // awaiting the patch, flush()+mutate fire then compile fires
      // immediately and can race on HTTP/2. Verify compile is held
      // until the patch promise resolves.
      const update = makeMutation();
      const compile = makeMutation();

      // Deferred promise — we control when patchRules resolves.
      let resolveUpdate: () => void = () => {};
      const updatePromise = new Promise<void>((r) => {
        resolveUpdate = r;
      });
      update.mutateAsync = vi.fn().mockReturnValue(updatePromise);

      hooks.useUpdateProjectRules.mockReturnValue(update);
      hooks.useCompileRules.mockReturnValue(compile);

      render(<RulesTab projectId="sessionfs/sessionfs" />);

      const input = screen.getByRole('spinbutton', {
        name: /knowledge injection max tokens/i,
      }) as HTMLInputElement;

      // User types and immediately clicks Compile inside the debounce window.
      fireEvent.change(input, { target: { value: '9000' } });
      expect(update.mutateAsync).not.toHaveBeenCalled();

      const compileBtn = screen.getByRole('button', { name: /^compile$/i });
      await act(async () => {
        fireEvent.click(compileBtn);
        // Let the synchronous part of handleCompile run (calls flush()).
        await Promise.resolve();
      });

      // Patch fired with the final typed value.
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
      expect(update.mutateAsync).toHaveBeenCalledWith({
        rules: { knowledge_max_tokens: 9000 },
        etag: 'W/"v3"',
      });
      // Compile MUST NOT have fired yet — the await is still pending.
      expect(compile.mutate).not.toHaveBeenCalled();

      // Resolve the patch promise; flush microtasks so the await unwraps.
      await act(async () => {
        resolveUpdate();
        await Promise.resolve();
        await Promise.resolve();
      });

      // Now compile fires.
      expect(compile.mutate).toHaveBeenCalledTimes(1);

      // The pending timer was cancelled by flush — no extra mutation
      // after the timer would otherwise have fired.
      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
    });

    it('skips Compile when the flushed patch fails (409 stale-etag)', async () => {
      // Round-3 reviewer (KB entry 218, MEDIUM) flagged that even
      // with the await chain, a failed patch resolved flush() as
      // success and Compile ran against the stale server snapshot.
      // Now flush() returns false on patch failure and handleCompile
      // short-circuits with a "Compile skipped" toast.
      const update = makeMutation();
      const compile = makeMutation();
      update.mutateAsync = vi.fn().mockRejectedValue(new ApiError(409, 'stale'));
      hooks.useUpdateProjectRules.mockReturnValue(update);
      hooks.useCompileRules.mockReturnValue(compile);

      render(<RulesTab projectId="sessionfs/sessionfs" />);

      const input = screen.getByRole('spinbutton', {
        name: /knowledge injection max tokens/i,
      }) as HTMLInputElement;

      fireEvent.change(input, { target: { value: '9999' } });
      const compileBtn = screen.getByRole('button', { name: /^compile$/i });

      await act(async () => {
        fireEvent.click(compileBtn);
        // Let the synchronous flush() fire mutateAsync, then drain
        // the .then/.catch + Promise.all + handleCompile continuation.
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      // Patch was attempted...
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
      // ...the 409 surfaced the standard stale-etag toast...
      expect(mockAddToast).toHaveBeenCalledWith(
        'error',
        expect.stringMatching(/refresh and try again/i),
      );
      // ...AND compile was short-circuited with its own toast.
      expect(mockAddToast).toHaveBeenCalledWith(
        'error',
        expect.stringMatching(/compile skipped/i),
      );
      // Compile must NOT have fired against the stale server state.
      expect(compile.mutate).not.toHaveBeenCalled();
    });

    it('skips Compile when the flushed patch fails (network/500)', async () => {
      // Mirror of the 409 test but with a non-stale-etag failure to
      // confirm the gate is universal, not specific to 409.
      const update = makeMutation();
      const compile = makeMutation();
      update.mutateAsync = vi.fn().mockRejectedValue(new Error('network down'));
      hooks.useUpdateProjectRules.mockReturnValue(update);
      hooks.useCompileRules.mockReturnValue(compile);

      render(<RulesTab projectId="sessionfs/sessionfs" />);

      const input = screen.getByRole('spinbutton', {
        name: /context injection max tokens/i,
      }) as HTMLInputElement;

      fireEvent.change(input, { target: { value: '3333' } });
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /^compile$/i }));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
      expect(mockAddToast).toHaveBeenCalledWith(
        'error',
        expect.stringMatching(/update failed/i),
      );
      expect(mockAddToast).toHaveBeenCalledWith(
        'error',
        expect.stringMatching(/compile skipped/i),
      );
      expect(compile.mutate).not.toHaveBeenCalled();
    });

    it('disables Compile while an update mutation is in-flight', () => {
      hooks.useUpdateProjectRules.mockReturnValue(makeMutation({ isPending: true }));
      render(<RulesTab projectId="sessionfs/sessionfs" />);
      expect(screen.getByRole('button', { name: /^compile$/i })).toBeDisabled();
    });

    it('flushes pending change on unmount so navigation does not lose typing', () => {
      const update = makeMutation();
      hooks.useUpdateProjectRules.mockReturnValue(update);

      const { unmount } = render(<RulesTab projectId="sessionfs/sessionfs" />);

      const input = screen.getByRole('spinbutton', {
        name: /knowledge injection max tokens/i,
      }) as HTMLInputElement;

      fireEvent.change(input, { target: { value: '6000' } });
      expect(update.mutateAsync).not.toHaveBeenCalled();

      // User switches tab / navigates away within the 600ms window.
      unmount();

      // Unmount-only useEffect cleanup commits the pending value.
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
      expect(update.mutateAsync).toHaveBeenCalledWith({
        rules: { knowledge_max_tokens: 6000 },
        etag: 'W/"v3"',
      });
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
