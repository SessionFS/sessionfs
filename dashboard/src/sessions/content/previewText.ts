import { describeToolUse } from './toolSummary';
import { looksLikeError } from './text';

type Block = Record<string, unknown>;

function firstLine(text: string): string {
  return text.split('\n').map((l) => l.trim()).find(Boolean) ?? '';
}

function resultText(block: Block): string {
  const c = block.content ?? block.output;
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) {
    return c
      .map((p) => (typeof p === 'object' && p !== null && typeof (p as Block).text === 'string' ? String((p as Block).text) : ''))
      .join('\n');
  }
  return '';
}

/**
 * One line that says what a message holds, for the session header preview.
 * Text wins; a message made only of tool blocks reads as the tool call
 * ("Read src/x.ts", "Bash npm test") or its result ("Result: …", "Error: …"),
 * using only what the message already carries.
 */
export function previewText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  const blocks = content.filter((b): b is Block => typeof b === 'object' && b !== null);

  const text = blocks.find((b) => b.type === 'text' && String(b.text ?? '').trim());
  if (text) return String(text.text);

  const use = blocks.find((b) => b.type === 'tool_use');
  if (use) {
    const name = String(use.name || 'Tool');
    const { summary } = describeToolUse(name, use.input as Record<string, unknown> | undefined);
    return summary && summary !== name ? `${name} ${firstLine(summary)}` : name;
  }

  const result = blocks.find((b) => b.type === 'tool_result');
  if (result) {
    const body = resultText(result);
    const label = result.is_error || looksLikeError(body) ? 'Error' : 'Result';
    const line = firstLine(body);
    return line ? `${label}: ${line}` : label;
  }

  if (blocks.some((b) => b.type === 'thinking')) return 'Thinking';
  return '';
}

/**
 * True when a message is only tool traffic (calls or results, no text). The
 * header preview badges these "Tool": a tool result arrives in a user-role
 * message, but the person did not write it.
 */
export function isToolOnly(content: unknown): boolean {
  if (!Array.isArray(content)) return false;
  const blocks = content.filter((b): b is Block => typeof b === 'object' && b !== null);
  const hasText = blocks.some((b) => b.type === 'text' && String(b.text ?? '').trim());
  return !hasText && blocks.some((b) => b.type === 'tool_use' || b.type === 'tool_result');
}
