import { describe, expect, it } from 'vitest';
import { isToolOnly, previewText } from './previewText';

describe('previewText', () => {
  it('returns plain string content as is', () => {
    expect(previewText('hello')).toBe('hello');
  });

  it('prefers a text block over tool blocks', () => {
    expect(previewText([
      { type: 'tool_use', name: 'Read', input: { file_path: 'a.ts' } },
      { type: 'text', text: 'I read it' },
    ])).toBe('I read it');
  });

  it('names a tool call with its target', () => {
    expect(previewText([{ type: 'tool_use', name: 'Read', input: { file_path: 'src/x.ts' } }])).toBe('Read src/x.ts');
    expect(previewText([{ type: 'tool_use', name: 'Bash', input: { command: 'npm test\nnpm run lint' } }])).toBe('Bash npm test');
  });

  it('falls back to the tool name when the call has no target', () => {
    expect(previewText([{ type: 'tool_use', name: 'Bash', input: {} }])).toBe('Bash');
  });

  it('labels a tool result with its first line', () => {
    expect(previewText([{ type: 'tool_result', content: '\nThe file was updated\nmore' }])).toBe('Result: The file was updated');
    expect(previewText([{ type: 'tool_result', content: [{ type: 'text', text: 'ok' }] }])).toBe('Result: ok');
    expect(previewText([{ type: 'tool_result', content: '' }])).toBe('Result');
  });

  it('labels a failed tool result as an error', () => {
    expect(previewText([{ type: 'tool_result', is_error: true, content: 'boom' }])).toBe('Error: boom');
  });

  it('labels a message that only thinks', () => {
    expect(previewText([{ type: 'thinking', thinking: '…' }])).toBe('Thinking');
  });

  it('returns an empty string when there is nothing to show', () => {
    expect(previewText([])).toBe('');
    expect(previewText(undefined)).toBe('');
  });
});

describe('isToolOnly', () => {
  it('is true for a message made only of tool calls or results', () => {
    expect(isToolOnly([{ type: 'tool_use', name: 'Bash', input: {} }])).toBe(true);
    expect(isToolOnly([{ type: 'tool_result', content: 'ok' }])).toBe(true);
    expect(isToolOnly([{ type: 'tool_result', content: 'ok' }, { type: 'text', text: '  ' }])).toBe(true);
  });

  it('is false when the message carries text, or no tool blocks', () => {
    expect(isToolOnly([{ type: 'text', text: 'hi' }, { type: 'tool_use', name: 'Read', input: {} }])).toBe(false);
    expect(isToolOnly('plain text')).toBe(false);
    expect(isToolOnly([{ type: 'thinking', thinking: '…' }])).toBe(false);
    expect(isToolOnly(undefined)).toBe(false);
  });
});
