import { describe, it, expect } from 'vitest';
import { ellipsis, computeLineDiff, looksLikeError } from './text';

describe('ellipsis', () => {
  it('leaves short text alone and truncates long text', () => {
    expect(ellipsis('abc', 5)).toBe('abc');
    expect(ellipsis('abcdef', 3)).toBe('abc…');
  });
});

describe('computeLineDiff', () => {
  it('marks removed, added and unchanged lines', () => {
    const out = computeLineDiff('a\nb', 'a\nc');
    expect(out[0]).toEqual({ type: 'same', text: 'a' });
    expect(out.filter((l) => l.type === 'added').map((l) => l.text)).toEqual(['c']);
    expect(out.filter((l) => l.type === 'removed').map((l) => l.text)).toEqual(['b']);
  });

  it('falls back to remove-then-add for very large inputs', () => {
    const big = Array.from({ length: 600 }, (_, i) => `l${i}`).join('\n');
    const out = computeLineDiff(big, big + '\nx');
    expect(out.every((l) => l.type !== 'same')).toBe(true);
  });
});

describe('looksLikeError', () => {
  it('detects errors and tracebacks', () => {
    expect(looksLikeError('Error: boom')).toBe(true);
    expect(looksLikeError('x\nTraceback (most recent call last):')).toBe(true);
    expect(looksLikeError('all good')).toBe(false);
  });
});
