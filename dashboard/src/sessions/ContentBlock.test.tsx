import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import ContentBlock from './ContentBlock';

// ── Helpers ────────────────────────────────────────────────────────

function toolUseBlock(name: string, input: Record<string, unknown> = {}) {
  return { type: 'tool_use', name, input, id: 't1' };
}

function toolResultBlock(content: string, opts: { isError?: boolean } = {}) {
  return { type: 'tool_result', content, is_error: opts.isError ?? false };
}

// ── Tool-Use: Bash ─────────────────────────────────────────────────

describe('ContentBlock — tool_use (Bash)', () => {
  it('shows the command in the collapsed header, not JSON', () => {
    const block = toolUseBlock('Bash', { command: 'npm run build' });
    render(<ContentBlock block={block} />);

    // Should show the command text, not raw JSON
    expect(screen.getByText('npm run build')).toBeInTheDocument();
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.queryByText(/"command"/)).not.toBeInTheDocument();
    expect(screen.queryByText(/"npm run build"/)).not.toBeInTheDocument();
  });

  it('truncates long commands in the header', () => {
    const longCmd = 'a'.repeat(200);
    const block = toolUseBlock('Bash', { command: longCmd });
    render(<ContentBlock block={block} />);

    const summary = screen.getByText(/a{97}…/); // 100 chars truncated
    expect(summary).toBeInTheDocument();
  });

  it('expands to show terminal well with $ prefix', async () => {
    const block = toolUseBlock('Bash', { command: 'ls -la' });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Bash: ls -la/ });
    await userEvent.click(toggle);

    // Terminal well visible with $ prefix and label
    expect(screen.getByText('$')).toBeInTheDocument();
    expect(screen.getByText('terminal')).toBeInTheDocument();
    // Command appears in both header and terminal — confirm at least 2 occurrences
    const occurrences = screen.getAllByText('ls -la');
    expect(occurrences.length).toBeGreaterThanOrEqual(2);
  });

  it('shows working directory when present', async () => {
    const block = toolUseBlock('Bash', { command: 'make', workdir: '/app/src' });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Bash: make/ });
    await userEvent.click(toggle);

    expect(screen.getByText(/cd \/app\/src/)).toBeInTheDocument();
  });

  it('has a copy button for the command', async () => {
    const block = toolUseBlock('Bash', { command: 'npm test' });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Bash: npm test/ });
    await userEvent.click(toggle);

    // Copy button appears both in header (group-hover) and terminal well
    const copyButtons = screen.getAllByLabelText('Copy command');
    expect(copyButtons.length).toBeGreaterThanOrEqual(1);
  });

  it('is keyboard accessible with Enter/Space and aria-expanded', async () => {
    const block = toolUseBlock('Bash', { command: 'echo hi' });
    render(<ContentBlock block={block} />);

    const btn = screen.getByRole('button', { name: /Bash: echo hi/ });
    expect(btn).toHaveAttribute('aria-expanded', 'false');

    fireEvent.keyDown(btn, { key: 'Enter' });
    expect(btn).toHaveAttribute('aria-expanded', 'true');

    fireEvent.keyDown(btn, { key: ' ' });
    expect(btn).toHaveAttribute('aria-expanded', 'false');
  });
});

// ── Tool-Use: Edit (diff) ──────────────────────────────────────────

describe('ContentBlock — tool_use (Edit)', () => {
  it('shows file path and "edit" in header, not raw JSON', () => {
    const block = toolUseBlock('Edit', {
      file_path: 'src/app.ts',
      old_string: 'const x = 1;',
      new_string: 'const x = 2;',
    });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Edit')).toBeInTheDocument();
    expect(screen.getByText(/src\/app\.ts — edit/)).toBeInTheDocument();
    expect(screen.queryByText(/"old_string"/)).not.toBeInTheDocument();
  });

  it('expands to show a diff with removed and added lines', async () => {
    const block = toolUseBlock('Edit', {
      file_path: 'src/foo.ts',
      old_string: 'old line\nkept line',
      new_string: 'new line\nkept line',
    });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Edit: src\/foo\.ts — edit/ });
    await userEvent.click(toggle);

    // Diff view: removed line has - prefix + danger styling
    expect(screen.getByText('old line')).toBeInTheDocument();
    // Added line has + prefix
    const prefixEls = screen.getAllByText('-');
    expect(prefixEls.length).toBeGreaterThan(0);
    const addEls = screen.getAllByText('+');
    expect(addEls.length).toBeGreaterThan(0);

    // File path shown as mono chip
    expect(screen.getByText('src/foo.ts')).toBeInTheDocument();
  });

  it('renders removed lines with danger tint and added lines with accent tint', async () => {
    const block = toolUseBlock('Edit', {
      old_string: 'remove me',
      new_string: 'add me',
    });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Edit/ });
    await userEvent.click(toggle);

    // Find the diff line divs
    const removedLine = screen.getByText('remove me').closest('div');
    expect(removedLine?.className).toMatch(/danger/);

    const addedLine = screen.getByText('add me').closest('div');
    expect(addedLine?.className).toMatch(/accent/);
  });

  it('renders NOT raw JSON in expanded detail', async () => {
    const block = toolUseBlock('Edit', {
      file_path: 'src/bar.ts',
      old_string: 'before',
      new_string: 'after',
    });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Edit/ });
    await userEvent.click(toggle);

    // Should NOT show JSON-style quoting
    expect(screen.queryByText(/"old_string"/)).not.toBeInTheDocument();
    expect(screen.queryByText(/"new_string"/)).not.toBeInTheDocument();
  });
});

// ── Tool-Use: Read / Write ─────────────────────────────────────────

describe('ContentBlock — tool_use (Read/Write)', () => {
  it('shows file path as summary', () => {
    const block = toolUseBlock('Read', { file_path: 'src/index.ts' });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Read')).toBeInTheDocument();
    expect(screen.getByText('src/index.ts')).toBeInTheDocument();
  });

  it('shows offset/limit in expanded detail', async () => {
    const block = toolUseBlock('Read', { file_path: 'src/app.ts', offset: 10, limit: 20 });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Read: src\/app\.ts/ });
    await userEvent.click(toggle);

    expect(screen.getByText(/Offset: 10/)).toBeInTheDocument();
    expect(screen.getByText(/Limit: 20/)).toBeInTheDocument();
  });
});

// ── Tool-Use: Grep / Glob ──────────────────────────────────────────

describe('ContentBlock — tool_use (Grep/Glob)', () => {
  it('shows pattern and path in header', () => {
    const block = toolUseBlock('Grep', { pattern: 'TODO', path: 'src/' });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Grep')).toBeInTheDocument();
    expect(screen.getByText(/TODO in src\//)).toBeInTheDocument();
  });

  it('shows pattern only when no path', () => {
    const block = toolUseBlock('Glob', { pattern: '*.ts' });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Glob')).toBeInTheDocument();
    expect(screen.getByText('*.ts')).toBeInTheDocument();
  });
});

// ── Tool-Use: WebFetch / WebSearch ─────────────────────────────────

describe('ContentBlock — tool_use (WebFetch/WebSearch)', () => {
  it('shows URL for WebFetch', () => {
    const block = toolUseBlock('WebFetch', { url: 'https://example.com/api' });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('WebFetch')).toBeInTheDocument();
    expect(screen.getByText('https://example.com/api')).toBeInTheDocument();
  });

  it('shows query for WebSearch', () => {
    const block = toolUseBlock('WebSearch', { query: 'TypeScript generics' });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('WebSearch')).toBeInTheDocument();
    expect(screen.getByText('TypeScript generics')).toBeInTheDocument();
  });
});

// ── Tool-Use: TodoWrite ────────────────────────────────────────────

describe('ContentBlock — tool_use (TodoWrite)', () => {
  it('shows todo count in header', () => {
    const block = toolUseBlock('TodoWrite', {
      todos: [
        { content: 'Add tests', status: 'pending' },
        { content: 'Write docs', status: 'completed' },
      ],
    });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('2 todos')).toBeInTheDocument();
  });

  it('renders todos as checklist with status markers', async () => {
    const block = toolUseBlock('TodoWrite', {
      todos: [
        { content: 'Task A', status: 'completed' },
        { content: 'Task B', status: 'in_progress' },
        { content: 'Task C', status: 'pending' },
      ],
    });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /TodoWrite: 3 todos/ });
    await userEvent.click(toggle);

    expect(screen.getByText('Task A')).toBeInTheDocument();
    expect(screen.getByText('Task B')).toBeInTheDocument();
    expect(screen.getByText('Task C')).toBeInTheDocument();
    expect(screen.getByText('1/3 completed')).toBeInTheDocument();

    // Completed item has line-through
    expect(screen.getByText('Task A').className).toMatch(/line-through/);
  });
});

// ── Tool-Use: Task / Agent ─────────────────────────────────────────

describe('ContentBlock — tool_use (Task/Agent)', () => {
  it('shows description for Task', () => {
    const block = toolUseBlock('Task', { description: 'Fix auth bug' });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Task')).toBeInTheDocument();
    expect(screen.getByText('Fix auth bug')).toBeInTheDocument();
  });

  it('shows subagent_type in expanded detail', async () => {
    const block = toolUseBlock('Task', {
      description: 'Review code',
      subagent_type: 'code-reviewer',
    });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Task: Review code/ });
    await userEvent.click(toggle);

    expect(screen.getByText('code-reviewer')).toBeInTheDocument();
  });
});

// ── Tool-Use: Unknown Tool ─────────────────────────────────────────

describe('ContentBlock — tool_use (unknown tool)', () => {
  it('shows top-level input keys as summary', () => {
    const block = toolUseBlock('CustomTool', { arg1: 'val1', arg2: 42 });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('CustomTool')).toBeInTheDocument();
    // Keys shown in summary
    expect(screen.getByText(/arg1, arg2/)).toBeInTheDocument();
  });

  it('hides raw JSON behind a toggle', async () => {
    const block = toolUseBlock('CustomTool', { secret: 'xyz' });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /CustomTool: secret/ });
    await userEvent.click(toggle);

    // Raw JSON should NOT be visible by default
    expect(screen.queryByText(/"secret"/)).not.toBeInTheDocument();

    // Click "Show raw"
    const showRawBtn = screen.getByText('Show raw');
    await userEvent.click(showRawBtn);

    // Now raw JSON is visible
    expect(screen.getByText(/"secret"/)).toBeInTheDocument();
    expect(screen.getByText('Hide raw')).toBeInTheDocument();
  });
});

// ── Tool-Use: Keyboard & A11y ──────────────────────────────────────

describe('ContentBlock — tool_use (a11y)', () => {
  it('has aria-expanded that toggles', async () => {
    const block = toolUseBlock('Read', { file_path: 'f.ts' });
    render(<ContentBlock block={block} />);

    const btn = screen.getByRole('button', { name: /Read: f\.ts/ });
    expect(btn).toHaveAttribute('aria-expanded', 'false');

    await userEvent.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
  });

  it('opens/closes with Space key', () => {
    const block = toolUseBlock('Bash', { command: 'ls' });
    render(<ContentBlock block={block} />);

    const btn = screen.getByRole('button', { name: /Bash: ls/ });
    expect(btn).toHaveAttribute('aria-expanded', 'false');

    fireEvent.keyDown(btn, { key: ' ' });
    expect(btn).toHaveAttribute('aria-expanded', 'true');
  });
});

// ── Tool-Result: basic rendering ───────────────────────────────────

describe('ContentBlock — tool_result', () => {
  it('shows line count and collapsed preview line', () => {
    const block = toolResultBlock('line one\nline two\nline three');
    render(<ContentBlock block={block} />);

    expect(screen.getByText(/3 lines/)).toBeInTheDocument();
    expect(screen.getByText('line one')).toBeInTheDocument();
  });

  it('expands to show full content', async () => {
    const block = toolResultBlock('hello world');
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Tool result — 1 line/ });
    await userEvent.click(toggle);

    expect(screen.getByText('hello world')).toBeInTheDocument();
    expect(screen.getByText('Output')).toBeInTheDocument();
  });

  it('line-clamps long output and provides "Show full output"', async () => {
    const manyLines = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join('\n');
    const block = toolResultBlock(manyLines);
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /Tool result — 20 lines/ });
    await userEvent.click(toggle);

    // The pre should have line-clamp applied
    const pre = screen.getByText(/line 1/).closest('pre');
    expect(pre?.className).toMatch(/line-clamp/);

    // "Show full output" button present
    expect(screen.getByText(/Show full output/)).toBeInTheDocument();
  });

  it('toggles between "Show full output" and "Show less"', async () => {
    const manyLines = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join('\n');
    const block = toolResultBlock(manyLines);
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /20 lines/ });
    await userEvent.click(toggle);

    const expandBtn = screen.getByText(/Show full output/);
    await userEvent.click(expandBtn);

    expect(screen.getByText('Show less')).toBeInTheDocument();

    await userEvent.click(screen.getByText('Show less'));
    expect(screen.getByText(/Show full output/)).toBeInTheDocument();
  });

  it('has a copy button on the output', async () => {
    const block = toolResultBlock('some output');
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /1 line/ });
    await userEvent.click(toggle);

    expect(screen.getByLabelText('Copy output')).toBeInTheDocument();
  });
});

// ── Tool-Result: error detection ───────────────────────────────────

describe('ContentBlock — tool_result (error)', () => {
  it('detects is_error=true and shows Error label', () => {
    const block = toolResultBlock('something broke', { isError: true });
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Error')).toBeInTheDocument();
    expect(screen.queryByText('Result')).not.toBeInTheDocument();
  });

  it('detects error from content starting with "Error"', () => {
    const block = toolResultBlock('Error: file not found');
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Error')).toBeInTheDocument();
  });

  it('detects error from content starting with "Traceback"', async () => {
    const block = toolResultBlock('Traceback (most recent call last):\n  File "x.py", line 1\nValueError');
    render(<ContentBlock block={block} />);

    // Collapsed header shows Error label
    expect(screen.getByText('Error')).toBeInTheDocument();

    // Expand to see error output section
    const toggle = screen.getByRole('button', { name: /error/i });
    await userEvent.click(toggle);

    expect(screen.getByText('Error output')).toBeInTheDocument();
  });

  it('does not flag normal content as error', () => {
    const block = toolResultBlock('Build completed successfully');
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Result')).toBeInTheDocument();
    expect(screen.queryByText('Error')).not.toBeInTheDocument();
  });

  it('uses danger tint for error header and content', async () => {
    const block = toolResultBlock('Error: failed', { isError: true });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /error/i });
    await userEvent.click(toggle);

    const errorLabel = screen.getByText('Error output');
    expect(errorLabel.className).toMatch(/danger/);
  });
});

// ── Non-tool blocks remain unchanged ───────────────────────────────

describe('ContentBlock — non-tool blocks', () => {
  it('renders thinking block collapsibly', () => {
    const block = { type: 'thinking', thinking: 'internal reasoning...' };
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Thinking')).toBeInTheDocument();
  });

  it('renders text block with markdown', () => {
    const block = { type: 'text', text: 'Hello **world**' };
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Hello')).toBeInTheDocument();
    expect(screen.getByText('world')).toBeInTheDocument();
  });

  it('renders inline code with accent styling', () => {
    const block = { type: 'text', text: 'Use the `api.call()` method' };
    render(<ContentBlock block={block} />);

    const code = screen.getByText('api.call()');
    expect(code.tagName).toBe('CODE');
    expect(code.className).toMatch(/accent/);
  });

  it('renders fenced code block with language label', () => {
    const block = {
      type: 'text',
      text: '```tsx\nconst x: number = 1;\n```',
    };
    render(<ContentBlock block={block} />);

    expect(screen.getByText('tsx')).toBeInTheDocument();
    expect(screen.getByText(/const x/)).toBeInTheDocument();
  });

  it('renders fenced code block with copy button', () => {
    const block = {
      type: 'text',
      text: '```python\nprint("hello")\n```',
    };
    render(<ContentBlock block={block} />);

    // Language label
    expect(screen.getByText('python')).toBeInTheDocument();
    // Copy button on the code block
    const copyButtons = screen.getAllByLabelText('Copy code');
    expect(copyButtons.length).toBeGreaterThanOrEqual(1);
  });

  it('renders fenced code without language as "code" label', () => {
    const block = {
      type: 'text',
      text: '```\necho "no lang"\n```',
    };
    render(<ContentBlock block={block} />);

    expect(screen.getByText('code')).toBeInTheDocument();
  });

  it('renders markdown headings with design tokens', () => {
    const block = {
      type: 'text',
      text: '## Section Title\n\nParagraph text.',
    };
    render(<ContentBlock block={block} />);

    const heading = screen.getByText('Section Title');
    expect(heading.tagName).toBe('H2');
  });

  it('renders markdown links with brand color', () => {
    const block = {
      type: 'text',
      text: '[Click here](https://example.com)',
    };
    render(<ContentBlock block={block} />);

    const link = screen.getByText('Click here');
    expect(link.tagName).toBe('A');
    expect(link).toHaveAttribute('href', 'https://example.com');
    expect(link.className).toMatch(/brand/);
  });

  it('renders markdown lists', () => {
    const block = {
      type: 'text',
      text: '- Item one\n- Item two\n- Item three',
    };
    render(<ContentBlock block={block} />);

    expect(screen.getByText('Item one')).toBeInTheDocument();
    expect(screen.getByText('Item two')).toBeInTheDocument();
    expect(screen.getByText('Item three')).toBeInTheDocument();
  });

  it('renders blockquote quietly', () => {
    const block = {
      type: 'text',
      text: '> A quoted passage',
    };
    render(<ContentBlock block={block} />);

    const quote = screen.getByText(/A quoted passage/);
    const bq = quote.closest('blockquote');
    expect(bq).toBeInTheDocument();
  });

  it('renders prose without the generic prose-invert class', () => {
    const block = { type: 'text', text: 'Sample text' };
    render(<ContentBlock block={block} />);

    // The outer wrapper should NOT contain prose-invert
    const wrapper = screen.getByText('Sample text').closest('[class*="max-w-"]');
    expect(wrapper?.className).not.toMatch(/prose-invert/);
    expect(wrapper?.className).not.toMatch(/prose-sm/);
  });

  it('renders image block', () => {
    const block = {
      type: 'image',
      source: { type: 'base64', media_type: 'image/png', data: 'abc123' },
    };
    render(<ContentBlock block={block} />);

    const img = screen.getByAltText('Session image');
    expect(img).toBeInTheDocument();
    expect(img.tagName).toBe('IMG');
  });

  it('falls back to JSON for unknown block types', () => {
    const block = { type: 'custom_unknown', foo: 'bar' };
    render(<ContentBlock block={block} />);

    // Shows JSON fallback
    expect(screen.getByText(/"foo"/)).toBeInTheDocument();
    expect(screen.getByText(/"bar"/)).toBeInTheDocument();
  });
});

// ── MultiEdit ──────────────────────────────────────────────────────

describe('ContentBlock — tool_use (MultiEdit)', () => {
  it('shows multiple edit diffs', async () => {
    const block = toolUseBlock('MultiEdit', {
      file_path: 'src/multi.ts',
      edits: [
        { old_string: 'line1-old', new_string: 'line1-new' },
        { old_string: 'line2-old', new_string: 'line2-new' },
      ],
    });
    render(<ContentBlock block={block} />);

    const toggle = screen.getByRole('button', { name: /MultiEdit: src\/multi\.ts — edit/ });
    await userEvent.click(toggle);

    expect(screen.getByText('Edit 1 of 2')).toBeInTheDocument();
    expect(screen.getByText('Edit 2 of 2')).toBeInTheDocument();
  });
});
