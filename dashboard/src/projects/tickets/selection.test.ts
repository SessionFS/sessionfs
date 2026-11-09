import { describe, expect, it } from 'vitest';
import { selectRange, toggleId, visibleSelection } from './selection';

const ORDER = ['a', 'b', 'c', 'd', 'e'];

describe('row selection', () => {
  it('toggles one id without touching the input set', () => {
    const start = new Set(['a']);
    expect([...toggleId(start, 'b')]).toEqual(['a', 'b']);
    expect([...toggleId(start, 'a')]).toEqual([]);
    expect([...start]).toEqual(['a']);
  });

  it('selects a range in either direction and keeps what was selected', () => {
    expect([...selectRange(ORDER, 'b', 'd', new Set())].sort()).toEqual(['b', 'c', 'd']);
    expect([...selectRange(ORDER, 'd', 'b', new Set(['e']))].sort()).toEqual(['b', 'c', 'd', 'e']);
  });

  it('selects only the target without an anchor on screen, and nothing for an unknown target', () => {
    expect([...selectRange(ORDER, null, 'c', new Set())]).toEqual(['c']);
    expect([...selectRange(ORDER, 'gone', 'c', new Set())]).toEqual(['c']);
    expect([...selectRange(ORDER, 'a', 'gone', new Set(['b']))]).toEqual(['b']);
  });

  it('keeps only selected rows that are on screen, in screen order', () => {
    expect(visibleSelection(new Set(['e', 'hidden', 'a']), ORDER)).toEqual(['a', 'e']);
    expect(visibleSelection(new Set(), ORDER)).toEqual([]);
  });
});
