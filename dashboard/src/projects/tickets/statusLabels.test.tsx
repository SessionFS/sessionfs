import { describe, expect, it } from 'vitest';
import { STATUS_FILTERS, formatStatusLabel, joinMeta, plural } from './constants';

describe('status labels', () => {
  it('uses one sentence-case set everywhere', () => {
    expect(formatStatusLabel('in_progress')).toBe('In progress');
    expect(formatStatusLabel('review')).toBe('In review');
    expect(formatStatusLabel('suggested')).toBe('Suggested');
    expect(formatStatusLabel('some_new_state')).toBe('Some new state');
    expect(STATUS_FILTERS.map((s) => s.label)).toEqual([
      'All statuses', 'Suggested', 'Open', 'In progress', 'Blocked', 'In review', 'Done', 'Closed', 'Cancelled',
    ]);
  });

  it('joins only the parts that are present', () => {
    expect(joinMeta([false, '1 dependency', null, undefined, ''])).toBe('1 dependency');
    expect(joinMeta(['a', 'b'])).toBe('a · b');
    expect(joinMeta([])).toBe('');
    expect(plural(1, 'dep', 'deps')).toBe('1 dep');
    expect(plural(2, 'dep', 'deps')).toBe('2 deps');
  });
});
