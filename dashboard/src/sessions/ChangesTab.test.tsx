import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ChangesTab from './ChangesTab';
import { axe } from '../test/axe';

const mockAuth = vi.fn();
const mockGetChanges = vi.fn();

vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));

function renderTab() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ChangesTab sessionId="ses_123" />
    </QueryClientProvider>,
  );
}

const EMPTY = { files: [], git_ref: null, truncated: false };

const TWO_FILES = {
  git_ref: 'main@abc123',
  truncated: false,
  files: [
    {
      path: 'src/app.py',
      operation: 'edited',
      diff: '--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-return 1\n+return 2',
      added_lines: 1,
      removed_lines: 1,
      message_indexes: [1],
      truncated: false,
    },
    {
      path: 'src/new.py',
      operation: 'created',
      diff: '--- /dev/null\n+++ b/src/new.py\n+def hi():\n+    pass',
      added_lines: 2,
      removed_lines: 0,
      message_indexes: [2],
      truncated: false,
    },
  ],
};

describe('ChangesTab', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    mockGetChanges.mockReset();
    mockAuth.mockReturnValue({ auth: { client: { getChanges: mockGetChanges } } });
  });

  it('shows empty state when the tool records no file changes', async () => {
    mockGetChanges.mockResolvedValue(EMPTY);
    renderTab();
    expect(await screen.findByText("This tool doesn't record file changes.")).toBeInTheDocument();
  });

  it('renders file list with operation badges and +/- counts', async () => {
    mockGetChanges.mockResolvedValue(TWO_FILES);
    const { container } = renderTab();

    expect(await screen.findByText('src/app.py')).toBeInTheDocument();
    expect(screen.getByText('edited')).toBeInTheDocument();
    expect(screen.getByText('created')).toBeInTheDocument();
    expect(screen.getByText('main@abc123')).toBeInTheDocument();
    expect(screen.getByText('+1')).toBeInTheDocument();
    expect(screen.getByText('-1')).toBeInTheDocument();
    expect(screen.getByText('+2')).toBeInTheDocument();

    expect(await axe(container)).toHaveNoViolations();
  });

  it('expands a file to reveal its diff with added/removed lines', async () => {
    mockGetChanges.mockResolvedValue(TWO_FILES);
    renderTab();

    const summary = await screen.findByText('src/app.py');
    await userEvent.click(summary);

    const diff = screen.getByText('-return 1').closest('pre');
    expect(diff).toBeInTheDocument();
    expect(within(diff as HTMLElement).getByText('+return 2')).toBeInTheDocument();
  });

  it('renders created-file content as +-prefixed diff lines', async () => {
    mockGetChanges.mockResolvedValue(TWO_FILES);
    renderTab();

    const summary = await screen.findByText('src/new.py');
    await userEvent.click(summary);

    // Created files render as a proper unified diff, so every content line
    // carries a leading `+` and is styled as an addition.
    const added = screen.getByText('+def hi():');
    expect(added).toBeInTheDocument();
    expect(added.closest('pre')).toBeInTheDocument();
  });

  it('marks a file as truncated', async () => {
    mockGetChanges.mockResolvedValue({
      git_ref: null,
      truncated: false,
      files: [
        {
          path: 'big.txt',
          operation: 'created',
          diff: 'a\nb',
          added_lines: 2,
          removed_lines: 0,
          message_indexes: [0],
          truncated: true,
        },
      ],
    });
    renderTab();
    expect(await screen.findByText('truncated')).toBeInTheDocument();
  });
});
